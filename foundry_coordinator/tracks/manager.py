"""Track manager (Team Lead piece 2): parallel sprints on git worktrees.

A track is a row of the `tracks` table and a git worktree of the project's
repository at `<workspace>/.tracks/<name>` on branch `track/<name>`, cut from
the profile's branch (master). Its sprints are queued "on the track"
(`queue.track`), so the engine runs them with the track's workspace, locks and
scope: `project_for()` hands the engine a Project whose profile is the main one
with the worktree as workspace, the track branch, no Studio/Rojo names in its
exclusive resources (a worktree never drives Studio), the capacity resources
its needs map to, publish off and the fast lane off (the Studio gate runs at
merge time, in the main checkout: merge.py). The main track is the main
checkout, unchanged.

`max_agents` caps the sprints running at once across the main checkout and
every track: a live `[automation]` setting (the GUI / config store, or the
chat's `set_max_agents`), with the old `--max-writers` flag kept as an
override. The main sprint always counts first: a track sprint may take writer
slots 1..max_agents-1 only, slot 0 is the main checkout's.

Track states: planned -> running -> merging -> merged | stopped | failed, each
change recorded as a `track_*` event.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Callable, Optional

from .. import states as S
from ..adapters import Project, load_adapter
from ..adapters.git import GitError, Workspace
from . import analysis as AN
from .resources import OBSERVE_EVERY_S, ResourceManager, needs_locks

STATES = ("planned", "running", "merging", "merged", "stopped", "failed")
LIVE_STATES = ("planned", "running", "merging")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
TRACKS_DIR = ".tracks"
MAX_AGENTS_LIMIT = 16
PLAN_TTL_S = 24 * 3600


class TrackError(ValueError):
    pass


def _git(cwd: str, *args, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    from ..adapters.git import git_env
    # track worktrees sit one level under the workspace: core.longpaths (see git_env)
    env = git_env(None, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise TrackError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


class TrackManager:
    def __init__(self, c, clock: Callable[[], float] = time.time):
        self._fetched: dict = {}                           # (project, lane) -> last fetch
        self.c = c
        self.store = c.store
        self.clock = clock
        self.resources = ResourceManager(c, clock)
        from .merge import MergeManager
        self.merger = MergeManager(self)
        self._projects: dict = {}            # (pid, name, worktree) -> Project
        self._observed: dict = {}            # session id -> last transcript read
        # E (2026-10-28): each project's max_agents as its profile file says it (None =
        # not set there), read before any chat/CLI value is applied over it
        self._profile_max: dict = {P.id: self._raw_max_agents(P) for P in c.projects.values()}
        self._restore_max_agents()

    # ------------------------------------------------------------------ settings

    def setting(self, P, key: str):
        from ..automation import get
        return get(P, key)

    @staticmethod
    def _raw_max_agents(P):
        try:
            v = ((getattr(P.profile, "raw", None) or {}).get("automation") or {}).get(
                "max_agents")
        except AttributeError:
            return None
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def _restore_max_agents(self) -> None:
        """A chat-set max_agents survives a restart (store meta) until the router
        deploys a revision with another value, or the profile file's own
        `[automation] max_agents` changed since it was set (E, 2026-10-28: the
        profile setting then wins; before, an old chat value hid it for good). A
        value stored before 0.155 has no profile stamp: it is kept and stamped."""
        from ..automation import set_live
        for P in self.c.projects.values():
            v = self.store.meta(f"max_agents:{P.id}")
            if not (v and v.isdigit()):
                continue
            prof = self._profile_max.get(P.id)
            stamp = self.store.meta(f"max_agents_profile:{P.id}")
            if stamp and stamp != str(prof):
                self.store.set_meta(f"max_agents:{P.id}", "")
                self.store.set_meta(f"max_agents_profile:{P.id}", "")
                self.store.event(P.id, "", "max_agents_changed", detail={
                    "to": prof if prof is not None else 1, "from_override": int(v),
                    "actor": "profile file", "override": False})
                continue
            if not stamp:
                self.store.set_meta(f"max_agents_profile:{P.id}", str(prof))
            try:
                set_live(P, "max_agents", max(1, min(MAX_AGENTS_LIMIT, int(v))))
            except Exception:                                     # noqa: BLE001
                pass

    @staticmethod
    def _doc_max_agents(doc):
        try:
            return ((doc or {}).get("settings") or {}).get("automation", {}).get("max_agents")
        except AttributeError:
            return None

    def after_config_apply(self, P, prev_doc, new_doc) -> None:
        """A router revision just replaced P's profile tables (config_pull), which
        drops a chat/CLI-set max_agents. It comes back unless this revision itself
        changed max_agents: then the router wins and the stored value is dropped.
        Before 0.144.3 every re-apply of the same revision reset the cap to 1, and a
        track sprint waited for a writer slot forever (2026-10-27, GF-NAME1)."""
        try:
            self.refresh_track_profiles(P)
        except Exception:                                         # noqa: BLE001
            pass
        key = f"max_agents:{P.id}"
        new_v = self._doc_max_agents(new_doc)
        if new_v is not None and new_v != self._doc_max_agents(prev_doc):
            if self.store.meta(key):
                self.store.set_meta(key, "")
                self.store.event(P.id, "", "max_agents_changed", detail={
                    "to": new_v, "actor": "router revision", "override": False})
            return
        v = self.store.meta(key)
        if v and v.isdigit():
            from ..automation import set_live
            try:
                set_live(P, "max_agents", max(1, min(MAX_AGENTS_LIMIT, int(v))))
            except Exception:                                     # noqa: BLE001
                pass

    def max_agents(self) -> int:
        """The live cap: the --max-writers override when given, else the largest
        [automation] max_agents across the projects (1 = today's behaviour)."""
        ov = getattr(self.c, "max_writers_override", None)
        if ov:
            return max(1, int(ov))
        vals = []
        from ..budget_mode import max_agents as mode_cap       # budget mode "save all"
        for P in self.c.projects.values():
            # 2026-11-09: a cap set from another process (the CLI's `tracks set-max-agents`)
            # is in the store, not in this process's profile; the stored value wins, so the
            # running coordinator follows it at once. (It is cleared when a router revision
            # or the profile file changes max_agents, see after_config_apply / restore.)
            stored = self.store.meta(f"max_agents:{P.id}")
            if stored and str(stored).isdigit():
                vals.append(mode_cap(P, int(stored)))
                continue
            try:
                vals.append(mode_cap(P, int(self.setting(P, "max_agents") or 1)))
            except (TypeError, ValueError):
                pass
        return max(1, min(MAX_AGENTS_LIMIT, max(vals or [1])))

    def set_max_agents(self, P, n: int, actor: str) -> int:
        from ..automation import set_live
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise TrackError("max_agents must be a whole number") from None
        if not 1 <= n <= MAX_AGENTS_LIMIT:
            raise TrackError(f"max_agents must be between 1 and {MAX_AGENTS_LIMIT}")
        before = self.max_agents()
        set_live(P, "max_agents", n)
        self.store.set_meta(f"max_agents:{P.id}", str(n))
        self.store.set_meta(f"max_agents_profile:{P.id}", str(self._profile_max.get(P.id)))
        self.store.event(P.id, "", "max_agents_changed",
                         detail={"from": before, "to": n, "actor": actor,
                                 "override": bool(getattr(self.c, "max_writers_override", None))})
        return self.max_agents()

    def writer_slots(self, P, t: Optional[dict] = None) -> range:
        """Writer slots this task may take: the main checkout any of 0..n-1, a
        track 1..n-1 (the main sprint is counted first)."""
        n = self.max_agents()
        return range(1, n) if self.is_track(P) else range(0, n)

    def agents_running(self) -> int:
        return len([l for l in self.store.locks() if l["resource"].startswith("writer:")])

    # ------------------------------------------------------------------ track projects

    # check kinds that need Studio: a track sprint has none, the merge gate runs them (0.145.3)
    STUDIO_CHECK_KINDS = ("suite", "boot", "capture", "gate", "regression", "plan", "call",
                          "runtime")

    @classmethod
    def merge_gate_owns(cls, kind, ref) -> bool:
        """Evidence a track sprint can't produce: Studio checks and the gate log.
        The merge gate in the main checkout runs them on the merge result."""
        ref = str(ref or "")
        if kind == "check":
            return ref.partition(":")[0] in cls.STUDIO_CHECK_KINDS
        return "_EVIDENCE_RAW.log" in ref.replace("\\", "/")

    @classmethod
    def merge_gate_owns_item(cls, e: dict) -> bool:
        """An evidence item the merge gate owns: by its file, or by its check name
        (2026-10-29 MV2-5: `gate:MV2-5` / `suite:MV2_5` items have no path; the reviewer
        saw them MISSING and blocked the track sprint)."""
        e = e or {}
        if cls.merge_gate_owns("file", e.get("path")):
            return True
        for k in ("check_id", "check", "title"):
            v = str(e.get(k) or "").strip()
            if v and cls.merge_gate_owns("check", v.split()[0].strip("()")):
                return True
        return False

    @staticmethod
    def is_track(P) -> bool:
        return bool(getattr(P.profile, "__dict__", {}).get("track"))

    def studio_less(self, P) -> bool:
        """A track WITHOUT Studio: unbound to a Studio slot (2026-10-31). Its Studio rows,
        visual approval and gate belong to the merge gate; a slot-bound track runs them.
        The slot is read from the store (2026-11-02: `set-slot none` from the CLI runs in
        another process, so the live engine's cached track profile still names the slot)."""
        d = getattr(P.profile, "__dict__", {})
        if not d.get("track"):
            return False
        try:
            row = self.store.track(P.id, d["track"])
            if row is not None:
                return not ((row.get("data") or {}).get("slot"))
        except Exception:                                         # noqa: BLE001
            pass
        return not d.get("slot")

    @staticmethod
    def track_name(P) -> str:
        return str(getattr(P.profile, "__dict__", {}).get("track") or "")

    def root(self, P) -> str:
        return os.path.join(os.path.realpath(P.profile.workspace), TRACKS_DIR)

    def track_project(self, P, row: dict) -> Project:
        key = (P.id, row["name"], row["worktree"])
        hit = self._projects.get(key)
        if hit is not None:
            return hit
        prof = P.profile
        tp = dataclasses.replace(prof, **self._track_fields(prof, row))
        tp.__dict__.update(self._track_extras(prof, row))
        tp.__dict__["_track_row"] = dict(row)
        try:
            adapter = load_adapter(tp, self.c.state_dir)
        except Exception as e:                                    # noqa: BLE001
            raise TrackError(f"track {row['name']}: adapter for the worktree: {e}") from None
        TP = Project(profile=tp, adapter=adapter, client=P.client, foundry=P.foundry,
                     ws=self._workspace(P, row))
        self._projects[key] = TP
        return TP

    def _workspace(self, P, row: dict):
        """The track's workspace: its worktree here, or (Distributed Foundry) on the slot its
        row names as `host`, reached through that slot's lane agent. A remote lane's row
        worktree is its read mirror here (phase c); the slot's path is data.remote_worktree."""
        data = row.get("data") or {}
        host = str(data.get("host") or "")
        if not host:
            return Workspace(row["worktree"])
        from .. import lane_tunnel
        from ..adapters.remote_git import LaneAgent, RemoteWorkspace
        agent = LaneAgent(lambda: lane_tunnel.MANAGER.endpoint(host, "agent"), name=host)
        remote = str(data.get("remote_worktree") or row["worktree"])
        mirror = row["worktree"] if data.get("remote_worktree") else ""

        def fetched(_sha, P=P, name=row["name"]):
            cur = self.store.track(P.id, name)
            if cur:
                self.lane_fetch(P, cur, force=True)
        return RemoteWorkspace(agent, P.id, row["name"], remote, mirror=mirror,
                               on_commit=fetched)

    @staticmethod
    def _track_fields(prof, row: dict) -> dict:
        res = needs_locks(row.get("needs") or [])
        if "studio" in (row.get("needs") or []) and                 not str((row.get("data") or {}).get("slot") or ""):
            # v0.181.74 (Victor 2026-11-06 "vr-c waits for Studio"): a track with no Studio
            # slot reaches the WORKSTATION Studio through its Studio MCP servers. Its sessions
            # take the main Studio's exclusive rows (studio:<host>, rojo:<port>) like a main
            # sprint, so they never start or stop a play session under the main sprint
            # (BAL-1's bot matches were cut off ~2 min in by vr-c's ASSET_CHECK runs).
            res += [r for r in (prof.resources or [])
                    if r.split(":")[0] in ("studio", "rojo") and r not in res]
        return {"workspace": row["worktree"], "branch": row["branch"],
                "resources": res,
                "publish": {"mode": "none"},
                "fast_lane": {**(prof.fast_lane or {}), "enabled": False}}

    @staticmethod
    def _track_extras(prof, row: dict) -> dict:
        # 2026-10-31: a track bound to a Studio slot has Studio (in the slot)
        return {"track": row["name"], "main_workspace": prof.workspace,
                "slot": str((row.get("data") or {}).get("slot") or ""),
                # Distributed Foundry phase b: the slot holding the worktree ('' = here)
                "lane_host": str((row.get("data") or {}).get("host") or ""),
                # phase c: the worktree's path ON the slot (the row's own is the read mirror)
                "remote_workspace": str((row.get("data") or {}).get("remote_worktree") or "")}

    def refresh_track_profiles(self, P) -> int:
        """A router revision updated P's profile in place (config_pull); each cached
        track Project holds a copy taken when the track started, so it gets the main
        profile's new values again (its own workspace, branch, locks and slot kept).
        2026-11-03: revision 20 raised context_ceiling_pct to 85 on main, while HK-TEST2 on
        track test2 kept handing off at the old 63% (144.5k)."""
        n = 0
        for key, TP in list(self._projects.items()):
            if key[0] != P.id:
                continue
            tp = TP.profile
            row = tp.__dict__.get("_track_row") or {}
            if not row:
                continue
            fresh = dataclasses.replace(P.profile, **self._track_fields(P.profile, row))
            fresh.__dict__.update(self._track_extras(P.profile, row))
            fresh.__dict__["_track_row"] = row
            slot = tp.__dict__.get("slot")             # a later set-slot keeps its value
            tp.__dict__.update(fresh.__dict__)
            if slot is not None:
                tp.__dict__["slot"] = slot
            n += 1
        return n

    def forget_project(self, P, name: str) -> None:
        for k in [k for k in self._projects if k[0] == P.id and k[1] == name]:
            self._projects.pop(k, None)

    def project_for(self, P, t: dict) -> Project:
        """The Project the engine runs this task with: the track's worktree when
        the task is queued on a live track, else the main checkout."""
        if self.is_track(P):
            return P
        name = self.store.task_track(P.id, t["task_id"])
        if not name:
            return P
        row = self.store.track(P.id, name)
        if row is None or row["state"] not in LIVE_STATES:
            return P
        return self.track_project(P, row)

    def main_of(self, P) -> Project:
        """The main-checkout Project of a track project (or P itself)."""
        if not self.is_track(P):
            return P
        return self.c.projects[P.id]

    def effective_base(self, P, base: str, head: str) -> str:
        """The base a track sprint's own changes are measured from. A track that
        merged the main branch in (to pick up the main sprint's work) would
        otherwise be blamed for every file the main branch changed (GF-NAME1,
        2026-10-27: 30 files "outside scope"). When `base` is an ancestor of the
        newest main-branch commit the track contains, that commit is the base."""
        if not self.is_track(P) or not base or not head:
            return base
        branch = str(getattr(self.main_of(P).profile, "branch", "") or "main")
        try:
            mb = P.ws.run("merge-base", head, f"refs/heads/{branch}", check=False).strip()
        except Exception:                                         # noqa: BLE001
            return base
        if mb and mb != base and P.ws.is_ancestor(base, mb):
            return mb
        return base

    # ------------------------------------------------------------------ create / queue

    def _check_name(self, name: str) -> str:
        name = str(name or "").strip().lower()
        if not NAME_RE.match(name) or name in ("main", "master"):
            raise TrackError("a track name is 1-40 characters of a-z, 0-9, '.', '_' or '-'")
        return name

    def _check_sprints(self, P, sprints: list[str]) -> list[str]:
        out = []
        for s in sprints or []:
            s = str(s).strip()
            if not s or s in out:
                continue
            try:
                P.adapter.describe(s)
            except Exception as e:                                # noqa: BLE001
                raise TrackError(f"sprint {s}: {e}") from None
            t = self.store.task(P.id, s)
            if t and t["state"] not in S.TERMINAL and not self._movable(P, t)                     and not self._movable_from_track(P, t):
                where = self.store.task_track(P.id, s) or "the main checkout"
                raise TrackError(f"sprint {s} is already {t['state']} on {where}"
                                 + (" (started; only a never-started sprint moves)"
                                    if not self.store.task_track(P.id, s) else ""))
            if t and t["state"] == S.COMPLETE:
                raise TrackError(f"sprint {s} is already COMPLETE")
            out.append(s)
        if not out:
            raise TrackError("a track needs at least one sprint")
        return out

    def base_sha(self, P) -> str:
        r = _git(P.profile.workspace, "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{P.profile.branch}", check=False)
        sha = r.stdout.strip()
        return sha or P.ws.head()

    def _exclude_tracks_dir(self, P) -> None:
        """`.tracks/` goes into the repository's info/exclude (shared by every
        worktree), so the main checkout's status never lists the worktrees."""
        try:
            common = _git(P.profile.workspace, "rev-parse", "--git-common-dir").stdout.strip()
            if not os.path.isabs(common):
                common = os.path.join(P.profile.workspace, common)
            path = os.path.join(common, "info", "exclude")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cur = ""
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    cur = f.read()
            if f"/{TRACKS_DIR}/" not in cur.splitlines():
                with open(path, "a", encoding="utf-8") as f:
                    f.write(("" if cur.endswith("\n") or not cur else "\n")
                            + f"/{TRACKS_DIR}/\n")
        except (TrackError, OSError):
            pass

    def _kilo_config(self, P, row: dict) -> str:
        """The track's kilo.jsonc: `services.track_kilo_config(track)` (tl-services
        writes the MCP allow-list from the track's needs) when that module provides
        it; else a copy of the main checkout's file. Returns the path or ''."""
        try:
            from .. import services                               # provided by tl-services
            fn = getattr(services, "track_kilo_config", None)
        except Exception:                                         # noqa: BLE001
            fn = None
        if callable(fn):
            try:
                out = fn(row, profile=P.profile)       # services.track_kilo_config(track, *, profile)
                if out:
                    return str(out)
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, "", "track_kilo_config_failed",
                                 detail={"track": row["name"], "error": str(e)[:300]})
        from ..kilo_config import kilo_path
        from foundry_automation.agent_backends import is_opencode_config
        src = kilo_path(P.profile)
        dst = os.path.join(row["worktree"], os.path.basename(src) if is_opencode_config(src)
                           else "kilo.jsonc")
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
                return dst
            except OSError as e:
                self.store.event(P.id, "", "track_kilo_config_failed",
                                 detail={"track": row["name"], "error": str(e)[:300]})
        return dst if os.path.isfile(dst) else ""

    def create(self, P, name: str, sprints: list[str], needs: Optional[list[str]] = None,
               actor: str = "operator", kind: str = "parallel", slot: str = "") -> dict:
        """Create the worktree + branch and register the track (state planned). `slot`: a
        [slots.<id>] Studio slot the track's Studio gate and Studio MCP run in (2026-10-31)."""
        P = self.main_of(P)
        name = self._check_name(name)
        if slot:
            from .. import slots as SL
            if slot not in SL.configured(P.profile):
                raise TrackError(f"no enabled Studio slot {slot!r} in [slots] "
                                 f"({', '.join(SL.configured(P.profile)) or 'none configured'})")
        if self.store.track(P.id, name):
            raise TrackError(f"track {name} already exists ({self.store.track(P.id, name)['state']})")
        sprints = self._check_sprints(P, sprints)
        if slot:
            self._refuse_workstation_only(P, sprints, slot)
        remote = self._remote_lane_slot(P, slot)
        if needs is None:
            plans = [AN.plan_from_spec(P.adapter.describe(s)) for s in sprints]
            try:
                plans = [p for p in AN.collect_plans(P) if p.task_id in sprints] or plans
            except Exception:                                     # noqa: BLE001
                pass
            needs = AN._normalize(n for p in plans for n in p.needs)
        if remote:
            return self._create_remote(P, name, kind, sprints, needs, actor, slot, remote)
        if slot and "studio" not in (needs or []):
            # 2026-11-01: a slot track's kilo.jsonc enables (and points at the slot) only the
            # Studio MCP servers its needs turn on. Blender plans read as `blender` alone, so
            # AS-F04 / AS-J06 sessions had no Studio tools on a track bound to slot-1.
            needs = AN._normalize(list(needs or []) + ["studio"])
        base = self.base_sha(P)
        path = os.path.join(self.root(P), name)
        branch = f"track/{name}"
        self._exclude_tracks_dir(P)
        os.makedirs(self.root(P), exist_ok=True)
        _git(P.profile.workspace, "worktree", "prune", check=False)
        tip = _git(P.profile.workspace, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}", check=False).stdout.strip()
        if os.path.exists(path):
            if tip and os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(
                    os.path.join(path, ".git")):
                pass                                               # a crashed create: adopt
            else:
                raise TrackError(f"{path} exists and is not a track worktree")
        else:
            if tip:
                _git(P.profile.workspace, "worktree", "add", path, branch)
            else:
                _git(P.profile.workspace, "worktree", "add", "-b", branch, path, base)
        data = {"created_ts": self.clock()}
        if slot:
            data["slot"] = slot
        row = self.store.add_track(P.id, name, kind, os.path.realpath(path), branch, base,
                                   list(needs or []), sprints, actor, data=data)
        kc = self._kilo_config(P, row)
        if slot and kc:
            # the track's sessions reach the slot's Studio, never the workstation's
            from .. import slots as SL
            try:
                changed = SL.patch_kilo_config(kc, {**SL.configured(P.profile)[slot], "id": slot})
                self.store.event(P.id, "", "track_slot_mcp", detail={
                    "track": name, "slot": slot, "servers": changed})
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, "", "track_kilo_config_failed",
                                 detail={"track": name, "error": f"slot mcp: {e}"[:300]})
        row = self.store.update_track(P.id, name, data={**row["data"], "kilo_config": kc})
        self.store.event(P.id, "", "track_created", detail={
            "track": name, "branch": branch, "worktree": row["worktree"], "base": base[:12],
            "sprints": sprints, "needs": list(needs or []), "kilo_config": kc, "actor": actor})
        return row

    # ------------------------------------------------------------------ lanes on slots
    #
    # Distributed Foundry phase b (docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md B6): a track on a
    # slot with `lanes` on, in a project with [automation] remote_lanes on, keeps its worktree,
    # sessions and checks ON the slot (lane_fetch.py); its commits are fetched back here.

    REMOTE_FETCH_EVERY_S = 60.0

    def _remote_lane_slot(self, P, slot: str) -> dict:
        """The slot's settings (+ id) when this track goes to the slot as a remote lane."""
        if not slot:
            return {}
        from .. import slots as SL
        s = SL.configured(P.profile).get(slot) or {}
        if s.get("lanes") is not True:
            return {}
        try:
            from ..automation import get as auto_get
            on = auto_get(P, "remote_lanes") is True
        except Exception:                                         # noqa: BLE001
            on = False
        return {**s, "id": slot} if on else {}

    def _lane_agent(self, sid: str):
        from .. import lane_tunnel
        from ..adapters.remote_git import LaneAgent
        return LaneAgent(lambda: lane_tunnel.MANAGER.endpoint(sid, "agent"), name=sid)

    def _create_remote(self, P, name: str, kind: str, sprints: list, needs, actor: str,
                       slot: str, s: dict) -> dict:
        from .. import lane_fetch as LF
        if needs is None:
            plans = [AN.plan_from_spec(P.adapter.describe(x)) for x in sprints]
            needs = AN._normalize(n for pl in plans for n in pl.needs)
        # phase c: Studio (the slot's own, its gate and MCP) and Lune run on the slot. Not yet:
        # Blender on a lane (its pool runs from this machine's worktree), and Studio on a
        # Windows slot (its native-Studio gate is not built)
        bad = sorted({n for n in (needs or []) if n == "blender"
                      or (n == "studio" and s.get("os") == "windows")})
        if bad:
            raise TrackError(f"a lane on slot {slot} has no {' or '.join(bad)} yet: start the "
                             f"track without lanes, or turn [automation] remote_lanes off")
        base = self.base_sha(P)
        branch = f"track/{name}"
        res = LF.create(P.profile.workspace, s, self._lane_agent(slot), P.id, name, base, branch)
        if not res["ok"]:
            raise TrackError(f"lane {name} on {slot}: {res['detail']}")
        # phase c: the read mirror here, on the lane's branch at its base
        mir = LF.mirror_create(P.profile.workspace, slot, name, branch, base, sprints)
        if not mir["ok"]:
            raise TrackError(f"lane {name}: the read mirror here: {mir['detail']} (the worktree "
                             f"on {slot} stays; stop it with tracks stop)")
        data = {"created_ts": self.clock(), "slot": slot, "host": slot,
                "remote_worktree": res["worktree"]}
        row = self.store.add_track(P.id, name, kind, os.path.realpath(mir["path"]), branch, base,
                                   list(needs or []), sprints, actor, data=data)
        kc = self._remote_kilo_config(P, row)
        row = self.store.update_track(P.id, name, data={**row["data"], "kilo_config": kc})
        self.store.event(P.id, "", "track_created", detail={
            "track": name, "branch": branch, "worktree": row["worktree"], "base": base[:12],
            "sprints": sprints, "needs": list(needs or []), "kilo_config": kc, "actor": actor,
            "host": slot, "remote": res["detail"], "remote_worktree": res["worktree"],
            "mirror": mir["detail"]})
        return row

    def _remote_kilo_config(self, P, row: dict) -> str:
        """The lane's kilo.jsonc / opencode.jsonc, made here as for a local track and written
        into the worktree on the slot. Local MCP servers are off on the slot (their commands
        are this machine's); remote (url) servers stay as they are."""
        import tempfile
        from .. import kilo_config as KC
        from foundry_automation import jsonc
        from foundry_automation.agent_backends import is_opencode_config
        src = KC.kilo_path(P.profile)
        name = os.path.basename(src) if is_opencode_config(src) else "kilo.jsonc"
        with tempfile.TemporaryDirectory(prefix="lane-cfg-") as tmp:
            fake = {**row, "worktree": tmp, "data": {**(row.get("data") or {}), "slot": ""}}
            slot_wt = str((row.get("data") or {}).get("remote_worktree") or row["worktree"])
            try:
                path = KC.track_kilo_config(fake, profile=P.profile, workspace=slot_wt)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, "", "track_kilo_config_failed",
                                 detail={"track": row["name"], "error": str(e)[:300]})
                return ""
        from .. import slots as SL
        slot = SL.configured(P.profile).get(str((row.get("data") or {}).get("host") or "")) or {}
        try:
            data = jsonc.parse(text)
            for mname, block in ((data.get("mcp") or {}) if isinstance(data, dict) else {}).items():
                if not (isinstance(block, dict) and block.get("type", "local") == "local"
                        and block.get("enabled") is not False):
                    continue
                if mname in SL.STUDIO_MCP and slot.get(SL.STUDIO_MCP[mname]):
                    # phase c: the slot's own Studio MCP server, a local command on the slot
                    text = jsonc.replace_value(text, ("mcp", mname), {
                        **block, "enabled": True,
                        "command": [str(slot[SL.STUDIO_MCP[mname]])]})
                    continue
                text = jsonc.replace_value(text, ("mcp", mname), {**block, "enabled": False})
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, "", "track_kilo_config_failed",
                             detail={"track": row["name"], "error": f"mcp: {e}"[:300]})
            return ""
        try:
            self._workspace(P, row).write(name, text.encode("utf-8"))
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, "", "track_kilo_config_failed",
                             detail={"track": row["name"], "error": f"write on the slot: {e}"[:300]})
            return ""
        return slot_wt.rstrip("/") + "/" + name

    def lane_fetch(self, P, row: dict, force: bool = False) -> dict:
        """Fetch a remote lane's commits here (fast-forward only); throttled to once a minute
        unless `force`. {} for a local track."""
        host = str((row.get("data") or {}).get("host") or "")
        if not host:
            return {}
        P = self.main_of(P)
        last = self._fetched.get((P.id, row["name"]), -1e18)
        if not force and self.clock() - last < self.REMOTE_FETCH_EVERY_S:
            return {}
        self._fetched[(P.id, row["name"])] = self.clock()
        from .. import lane_fetch as LF
        from .. import slots as SL
        s = SL.configured(P.profile).get(host)
        if not s:
            return {"ok": False, "detail": f"slot {host} is not configured any more"}
        mirror = row["worktree"] if (row.get("data") or {}).get("remote_worktree") else ""
        out = LF.fetch(P.profile.workspace, s, host, P.id, row["name"], row.get("branch") or "",
                       mirror=mirror)
        if out.get("moved") or not out.get("ok"):
            kind = ("track_lane_fetched" if out.get("ok") else
                    "track_lane_diverged" if out.get("diverged") else "track_lane_fetch_failed")
            self.store.event(P.id, "", kind, detail={"track": row["name"], "host": host,
                                                     "detail": out.get("detail", "")[:300],
                                                     "tip": (out.get("tip") or "")[:12]})
        self._slot_away(P, row, host, out)
        if out.get("diverged") and not (row.get("data") or {}).get("diverged_notice"):
            self.store.merge_track_data(P.id, row["name"], diverged_notice=self.clock())
            self.store.event(P.id, "", "operator_notice", detail={
                "kind": "lane_diverged", "alert_class": "watch", "track": row["name"],
                "title": f"lane {row['name']} on {host}: branches differ",
                "text": f"{out.get('detail')}. Nothing was overwritten; decide which tip to keep."})
        return out

    def _slot_away(self, P, row: dict, host: str, out: dict) -> None:
        """Phase d: a lane whose slot cannot be reached (fetch failing, not diverged) gets one
        notice after [automation] slot_away_minutes; its work is never moved elsewhere by itself
        (the commits since the last fetch exist only on the slot). Back: the mark goes."""
        data = row.get("data") or {}
        if out.get("ok") or out.get("diverged"):
            if data.get("away_since"):
                self.store.merge_track_data(P.id, row["name"], away_since=None, away_notice=None)
                self.store.event(P.id, "", "track_lane_back", detail={"track": row["name"],
                                                                      "host": host})
            return
        now = self.clock()
        since = float(data.get("away_since") or 0)
        if not since:
            self.store.merge_track_data(P.id, row["name"], away_since=now)
            return
        try:
            from ..automation import get as auto_get
            limit = float(auto_get(P, "slot_away_minutes")) * 60
        except Exception:                                         # noqa: BLE001
            limit = 1800.0
        if limit and now - since >= limit and not data.get("away_notice"):
            self.store.merge_track_data(P.id, row["name"], away_notice=now)
            tip = ""
            try:
                tip = _git(P.profile.workspace, "rev-parse", "--verify", "-q",
                           f"refs/lanes/{host}/{row['name']}", check=False).stdout.strip()[:12]
            except Exception:                                     # noqa: BLE001
                pass
            self.store.event(P.id, "", "operator_notice", detail={
                "kind": "slot_away", "alert_class": "watch", "track": row["name"], "slot": host,
                "title": f"{host} away: lane {row['name']} waits",
                "text": (f"{host} has not answered for {int((now - since) // 60)} min "
                         f"({out.get('detail', '')[:200]}). Lane {row['name']} waits; its last "
                         f"commit here is {tip or 'its base'}. Nothing is moved by itself. When "
                         f"{host} is back it carries on; to go on here from that commit: "
                         f"tracks bring-home {P.id} {row['name']} --force-local (anything "
                         f"newer stays on {host}).")})

    def _movable(self, P, t: dict) -> bool:
        """A main-queue sprint that never started (QUEUED / READY, attempt 0, no session):
        `tracks start` moves it onto the track as it is. 2026-11-02 vfx-2: moving AS-V08..V12
        meant cancelling them first; AS-D18 and S53 saw a CANCELLED dependency in that window,
        stopped BLOCKED and opened two Lead cases."""
        if t.get("state") not in (S.QUEUED, S.READY) or self.store.task_track(P.id, t["task_id"]):
            return False
        if int(t.get("attempt") or 0) == 0 and int(t.get("sessions") or 0) == 0 \
                and not t.get("session_id"):
            return True
        # 2026-11-20 FIN-4a (Victor: "Only move FIN-4a"): a main-line sprint that already
        # ran sessions waited READY behind FIN-2c for the one main checkout while two slots
        # sat idle. With the operator's explicit --move-started it moves too, but only when
        # it is READY (between sessions) and its last session has ended: its commits are on
        # master already, so the track branch (cut from master) holds all of its work.
        if t["task_id"] not in self.move_started or t.get("state") != S.READY:
            return False
        sid = t.get("session_id")
        row = self.store.session(sid) if sid else None
        return not sid or not row or bool(row.get("ended_ts"))

    move_started: frozenset = frozenset()

    def _movable_from_track(self, P, t: dict) -> str:
        """The running/planned track a never-started sprint waits on ('' = none). 2026-11-03:
        HK-COPY1 / HK-DATA1 waited hours behind HK-ASSET1 on one local track and AS-F26 behind
        AS-E11b, while agents and the Blender pool sat idle; no command could split them."""
        old = self.store.task_track(P.id, t["task_id"])
        if not old or t.get("state") not in (S.QUEUED, S.READY):
            return ""
        if int(t.get("attempt") or 0) or int(t.get("sessions") or 0) or t.get("session_id"):
            return ""
        row = self.store.track(P.id, old)
        if not row or row["state"] not in ("planned", "running"):
            return ""
        if row.get("current_sprint") == t["task_id"] and row["state"] == "running":
            return ""
        return old

    def _detach(self, P, old: str, sid: str, actor: str) -> list:
        """Take a never-started sprint off its track: the track's next sprint now follows the
        one before it. Returns the chain deps the sprint had from that track."""
        row = self.store.track(P.id, old)
        seq = list(row["sprints"])
        i = seq.index(sid)
        before = seq[i - 1] if i > 0 else ""
        after = seq[i + 1] if i + 1 < len(seq) else ""
        if after:
            deps = [d for d in ((self.store.queue_entry(P.id, after) or {}).get("deps") or [])
                    if d != sid]
            if before and before not in deps:
                deps.append(before)
            self.c.set_deps(P.id, after, sorted(deps), actor)
        seq.pop(i)
        self.store.update_track(P.id, old, sprints=seq)
        self.store.event(P.id, sid, "moved_off_track", detail={
            "track": old, "relinked": after, "to_follow": before, "actor": actor})
        return [before] if before else []

    def start(self, P, name: str, actor: str = "operator") -> dict:
        """Queue the track's sprints on it (a chain: each depends on the one
        before) and mark it running."""
        P = self.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        if row["state"] != "planned":
            raise TrackError(f"track {name} is {row['state']}, not planned")
        prev = ""
        for s in row["sprints"]:
            t = self.store.task(P.id, s)
            if not t:
                self.c.approve(P.id, s, [prev] if prev else [], actor,
                               note=f"on track {name}", track=name)
            elif self._movable(P, t) or self._movable_from_track(P, t):
                # its own deps stay; the track chain adds the sprint before it (a sprint
                # moved from another track drops that track's chain link)
                src = self._movable_from_track(P, t)
                chain = self._detach(P, src, s, actor) if src else []
                own = set(getattr(P.adapter.describe(s), "deps", None) or []) if src else set()
                old = [d for d in ((self.store.queue_entry(P.id, s) or {}).get("deps") or [])
                       if d not in chain or d in own]
                deps = sorted(set(old) | ({prev} if prev else set()))
                with self.store.tx():
                    if t["state"] == S.READY:
                        self.store.release(P.id, s)
                        self.store.transition(P.id, s, S.QUEUED,
                                              {"moved_to_track": name}, actor=actor)
                    self.store.approve(P.id, s, deps, actor, note=f"moved onto track {name}",
                                       track=name)
                self.store.event(P.id, s, "moved_to_track", detail={
                    "track": name, "deps": deps, "actor": actor})
            elif t["state"] == S.CANCELLED:
                # 2026-10-29 HK-STORE1: a sprint cancelled off the main queue to move it
                # onto a track was accepted here but stayed CANCELLED; the track failed 2 s
                # later. It is reopened onto the track (fresh attempts).
                self.c.reopen(P.id, s, [prev] if prev else [], actor,
                              reason=f"on track {name}", track=name)
            prev = s
        row = self.store.update_track(P.id, name, state="running",
                                      current_sprint=row["sprints"][0])
        self.store.event(P.id, "", "track_started", detail={"track": name, "actor": actor,
                                                           "sprints": row["sprints"]})
        return row

    def set_needs(self, P, name: str, needs: list[str], actor: str = "operator") -> dict:
        """Change a track's needs (studio, blender, audio, images, gpu, backend) and rewrite
        its kilo.jsonc, so its MCP servers follow (2026-10-28: the assets-local track was
        started with `blender` only, so Media_Gen stayed off for its asset sprints)."""
        from foundry_automation import service_map as SM
        P = self.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        new = SM.normalize_needs(needs)
        if not new:
            raise TrackError(f"no valid needs in {needs!r} (one of {', '.join(SM.NEEDS)})")
        row = self.store.update_track(P.id, name, needs=new)
        kc = self._kilo_config(P, row)
        slot = str((row.get("data") or {}).get("slot") or "")
        if slot and kc:
            from .. import slots as SL
            cfg = SL.configured(P.profile).get(slot)
            if cfg:
                SL.patch_kilo_config(kc, {**cfg, "id": slot})
        # Kilo caches each folder's config: the engine reloads this worktree's instance
        # before the track's next session (2026-11-01: set-needs only took effect after a
        # Kilo restart, which cut 4 running turns)
        row = self.store.update_track(P.id, name, data={**(row.get("data") or {}),
                                                        "kilo_config": kc, "kilo_reload": True})
        self.store.event(P.id, "", "track_needs_changed", detail={
            "track": name, "needs": new, "kilo_config": kc, "actor": actor})
        return row

    def _refuse_workstation_only(self, P, sprints: list, slot: str) -> None:
        """[automation] workstation_only_sprints: a sprint whose Studio checks measure frames
        or performance never goes on a slot-bound track (2026-11-02 AS-V08: the slots render
        in software at ~36 fps and its one-frame timing check failed there)."""
        import fnmatch
        try:
            pats = [str(p) for p in (self.setting(P, "workstation_only_sprints") or [])]
        except (KeyError, TypeError):
            pats = []
        hits = [s for s in sprints if any(fnmatch.fnmatchcase(s, p) for p in pats)]
        if hits:
            raise TrackError(f"{', '.join(hits)} run(s) Studio work on the workstation "
                             f"([automation] workstation_only_sprints): not on slot {slot}; "
                             f"start the track without --slot (its Studio checks then run at "
                             f"the merge on the workstation)")

    def _unbind_slot(self, P, row: dict, old: str, actor: str) -> dict:
        """`set-slot <track> none`: the track has no Studio of its own. Its kilo.jsonc is
        rewritten without the Studio MCP servers (`studio` leaves its needs, so its sessions
        never reach the workstation's Studio), and its sprints' Studio checks run on the
        workstation in the merge's Studio batch."""
        name = row["name"]
        data = {k: v for k, v in (row.get("data") or {}).items() if k != "slot"}
        needs = [n for n in (row.get("needs") or []) if n != "studio"]
        row = self.store.update_track(P.id, name, needs=needs, data=data)
        kc = self._kilo_config(P, row)
        row = self.store.update_track(P.id, name, data={**(row.get("data") or {}),
                                                        "kilo_config": kc, "kilo_reload": True})
        self.forget_project(P, name)
        self.store.event(P.id, "", "track_slot_changed", detail={
            "track": name, "from": old, "to": "", "servers": [], "actor": actor,
            "unbound": True})
        return row

    def set_slot(self, P, name: str, slot: str, actor: str = "operator") -> dict:
        """Move a running track to another Studio slot (2026-11-01, Victor: "split them": all 8
        Blender tracks were bound to slot-1 while slot-2 sat idle). Refused while one of its
        sprints holds a slot lock or has a scripted Studio gate running or pending. The track
        needs `studio`; its kilo.jsonc is rewritten with the Studio MCP pointed at the new
        slot, and the engine reloads the worktree's Kilo instance before its next session."""
        from .. import slots as SL
        P = self.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        unbind = str(slot or "").strip().lower() in ("", "none", "-")
        if not unbind and slot not in SL.configured(P.profile):
            raise TrackError(f"no enabled Studio slot {slot!r} in [slots] "
                             f"({', '.join(SL.configured(P.profile)) or 'none configured'})")
        old = str((row.get("data") or {}).get("slot") or "")
        if (unbind and not old) or old == slot:
            return row
        sprints = list(row.get("sprints") or [])
        if not unbind:
            live = [s for s in sprints
                    if (self.store.task(P.id, s) or {}).get("state") not in S.TERMINAL]
            self._refuse_workstation_only(P, live, slot)
        for lk in self.store.locks():
            if lk["project_id"] == P.id and lk["task_id"] in sprints \
                    and str(lk["resource"]).startswith(SL.LOCK_PREFIX):
                raise TrackError(f"{lk['task_id']} holds {lk['resource']}: move the track once "
                                 f"its Studio work is done")
        calls = getattr(self.c, "studio_calls", None)
        for tid in sprints:
            try:
                busy = bool(calls is not None and calls.active(P, tid))
            except Exception:                                     # noqa: BLE001
                busy = False
            if busy:
                raise TrackError(f"{tid} has a scripted Studio gate running or pending")
        if unbind:
            return self._unbind_slot(P, row, old, actor)
        needs = list(row.get("needs") or [])
        if "studio" not in needs:
            needs = AN._normalize(needs + ["studio"])
            row = self.store.update_track(P.id, name, needs=needs)
        row = self.store.update_track(P.id, name, data={**(row.get("data") or {}), "slot": slot})
        self.forget_project(P, name)          # the cached track profile carries the old slot
        kc = self._kilo_config(P, row)
        changed = []
        if kc:
            changed = SL.patch_kilo_config(kc, {**SL.configured(P.profile)[slot], "id": slot})
        row = self.store.update_track(P.id, name, data={**(row.get("data") or {}),
                                                        "kilo_config": kc, "kilo_reload": True})
        self.store.event(P.id, "", "track_slot_changed", detail={
            "track": name, "from": old, "to": slot, "servers": changed, "actor": actor})
        return row

    def stop(self, P, name: str, actor: str = "operator", reason: str = "") -> dict:
        """Cancel the track's unfinished sprints (their sessions aborted), release
        its resources; the worktree and branch are kept for inspection."""
        P = self.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        if row["state"] in ("merged", "stopped"):
            raise TrackError(f"track {name} is already {row['state']}")
        cancelled = []
        for s in row["sprints"]:
            t = self.store.task(P.id, s)
            if t and t["state"] not in S.TERMINAL:
                try:
                    self.c.cancel(P.id, s, f"track {name} stopped: {reason or actor}"[:300],
                                  actor=actor)
                    cancelled.append(s)
                except Exception as e:                            # noqa: BLE001
                    self.store.event(P.id, s, "warning", detail={
                        "track_stop": f"{type(e).__name__}: {e}"[:300]})
        self.merger.abort(P, row, f"stopped by {actor}")
        if (row.get("data") or {}).get("host"):
            self._stop_remote(P, row)
        self.resources.release_studio(P, name, "track stopped")
        self.resources.release_for_track(P, name)
        self.store.merge_track_data(P.id, name, stopped={"by": actor, "reason": reason,
                                                         "ts": self.clock()})
        row = self.store.update_track(P.id, name, state="stopped")
        self.store.event(P.id, "", "track_stopped", detail={"track": name, "actor": actor,
                                                           "reason": reason,
                                                           "cancelled": cancelled})
        return row

    def bring_home(self, P, name: str, actor: str = "coordinator", why: str = "",
                   force_local: bool = False) -> dict:
        """Phase d: a remote lane becomes an ordinary local track. Its last commits are fetched
        here, its uncommitted files on the slot are saved here, and its read mirror is made a full
        checkout and becomes the track's worktree. From then on its sessions, checks, merge, Lead
        repair and merge gate run as for any local track. The slot's worktree is kept until the
        track is merged, then removed (only at the fetched tip, with a clean tree).
        {"ok", "detail"}; nothing changes when the fetch fails (a slot that is away)."""
        from .. import lane_fetch as LF
        P = self.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        data = dict(row.get("data") or {})
        host = str(data.get("host") or "")
        if not host:
            return {"ok": True, "detail": f"track {name} is already here"}
        live = [t["task_id"] for t in (self.store.task(P.id, s) for s in row["sprints"])
                if t and t["state"] in S.ACTIVE and t["state"] not in (S.QUEUED, S.READY)]
        if live and why != "merge":
            return {"ok": False, "detail": f"lane {name} has a sprint in progress "
                                           f"({', '.join(live)}): bring it home when it is idle"}
        got = self.lane_fetch(P, row, force=True)
        dest = os.path.join(self.c.state_dir, "lane-saves", name,
                            time.strftime("%Y%m%d-%H%M%S") + "-home")
        if got.get("ok"):
            try:
                saved = LF.save_dirty(self._workspace(P, row), dest)
            except Exception as e:                                # noqa: BLE001
                saved = {"ok": False, "detail": str(e)[:300], "files": 0}
            if not saved.get("ok"):
                return {"ok": False, "detail": f"lane {name}: its uncommitted files on {host} "
                                               f"could not be read ({saved.get('detail')}); "
                                               f"nothing changed"}
        elif force_local and not got.get("diverged"):
            # the owner's choice for a slot that is away: go on here from the last commit
            # fetched. Nothing is discarded: the slot's worktree stays, and it is removed only
            # at a tip this machine holds (worktree-remove checks), which it will not be
            saved = {"ok": True, "detail": f"{host} away: from the last fetched commit; anything "
                                           f"newer stays on {host}", "dir": ""}
        else:
            return {"ok": False, "detail": f"lane {name}: its last commits are not here yet "
                                           f"({got.get('detail', '')}); nothing changed"}
        full = LF.mirror_full(row["worktree"])
        if not full["ok"]:
            return {"ok": False, "detail": f"lane {name}: the mirror's full checkout: {full['detail']}"}
        data.pop("host", None)
        data.update(was_host=host, home_ts=self.clock(), home_saves=saved.get("dir") or "",
                    home_forced=bool(force_local and not got.get("ok")))
        row = self.store.update_track(P.id, name, data=data)
        self.forget_project(P, name)          # its cached Project still holds the slot workspace
        self.store.event(P.id, "", "track_lane_home", detail={
            "track": name, "host": host, "actor": actor, "why": why, "fetch": got.get("detail"),
            "saved": saved.get("detail"), "saves": saved.get("dir") or "",
            "worktree": row["worktree"]})
        return {"ok": True, "detail": f"lane {name} is home ({got.get('detail')}; "
                                      f"{saved.get('detail')})"}

    def _remove_slot_worktree(self, P, row: dict) -> None:
        """After a brought-home lane merged: its worktree on the slot goes, only at the tip this
        machine holds and with a clean tree (the agent checks both). Best effort; an event says."""
        data = row.get("data") or {}
        host = str(data.get("was_host") or "")
        if not host or data.get("slot_worktree_removed"):
            return
        from .. import lane_fetch as LF
        tip = ""
        try:
            r = _git(P.profile.workspace, "rev-parse", "--verify", "-q",
                     f"refs/lanes/{host}/{row['name']}", check=False)
            tip = r.stdout.strip()
        except Exception:                                         # noqa: BLE001
            pass
        # the files bring-home copied here (its _list.txt): only those may go with the worktree
        saved = []
        listing = os.path.join(str(data.get("home_saves") or ""), "_list.txt")
        if data.get("home_saves") and os.path.isfile(listing):
            with open(listing, encoding="utf-8") as f:
                saved = [ln[3:].strip() for ln in f if len(ln) > 3]
        out = LF.wait_job(self._lane_agent(host), P.id, row["name"], "worktree-remove",
                          {"tip": tip, "saved": saved}, wait_s=600)
        self.store.merge_track_data(P.id, row["name"], slot_worktree_removed=bool(out.get("ok")))
        self.store.event(P.id, "", "track_lane_slot_removed" if out.get("ok") else
                         "track_lane_slot_remove_failed",
                         detail={"track": row["name"], "host": host,
                                 "detail": (out.get("output") or "")[-300:]})

    def _stop_remote(self, P, row: dict) -> None:
        """A remote lane stops: its commits are fetched here first and its uncommitted files
        copied to <state>/lane-saves/<track>/<time>/. The worktree stays on the slot (as a
        local track's stays here), so nothing is discarded."""
        got = self.lane_fetch(P, row, force=True)
        dest = os.path.join(self.c.state_dir, "lane-saves", row["name"],
                            time.strftime("%Y%m%d-%H%M%S"))
        from .. import lane_fetch as LF
        try:
            saved = LF.save_dirty(self._workspace(P, row), dest)
        except Exception as e:                                    # noqa: BLE001
            saved = {"ok": False, "detail": str(e)[:300]}
        self.store.event(P.id, "", "track_lane_stopped", detail={
            "track": row["name"], "host": (row.get("data") or {}).get("host"),
            "fetch": (got or {}).get("detail", ""), "saved": saved.get("detail", ""),
            "saves": saved.get("dir", "")})

    def remove_worktree(self, P, row: dict, why: str = "") -> bool:
        """`git worktree remove --force` (the branch is kept). False when it fails."""
        P = self.main_of(P)
        path = row.get("worktree") or ""
        ok = True
        if path and os.path.isdir(path):
            r = _git(P.profile.workspace, "worktree", "remove", "--force", path, check=False)
            ok = r.returncode == 0
            if not ok:
                self.store.event(P.id, "", "track_worktree_remove_failed", detail={
                    "track": row["name"], "error": (r.stderr or r.stdout)[:300]})
        _git(P.profile.workspace, "worktree", "prune", check=False)
        self.forget_project(P, row["name"])
        self.store.event(P.id, "", "track_worktree_removed", detail={"track": row["name"],
                                                                     "why": why, "ok": ok})
        if why == "merged" and (row.get("data") or {}).get("was_host"):
            try:
                self._remove_slot_worktree(P, row)
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, "", "track_lane_slot_remove_failed",
                                 detail={"track": row["name"], "detail": str(e)[:300]})
        return ok

    def fail(self, P, row: dict, why: str) -> dict:
        self.resources.release_studio(P, row["name"], why)
        self.resources.release_for_track(P, row["name"])
        self.store.merge_track_data(P.id, row["name"], failed={"why": why[:500],
                                                               "ts": self.clock()})
        row = self.store.update_track(P.id, row["name"], state="failed")
        self.store.event(P.id, "", "track_failed", detail={"track": row["name"], "why": why[:500]})
        return row

    # ------------------------------------------------------------------ the tick

    def tick(self, P) -> None:
        if self.is_track(P):
            return
        rows = [r for r in self.store.tracks(P.id) if r["state"] in LIVE_STATES]
        if not rows:
            return
        self.resources.tick(P)
        self.merger.sweep_decisions(P)
        for row in rows:
            try:
                self._tick_track(P, row)
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, "", "track_error", detail={
                    "track": row["name"], "error": f"{type(e).__name__}: {e}"[:400]})
                self.c.log(f"track {row['name']}: {type(e).__name__}: {e}")

    def _tick_track(self, P, row: dict) -> None:
        row = self.store.track(P.id, row["name"]) or row      # a decision sweep may have moved it
        if row["state"] not in LIVE_STATES:
            return
        if (row.get("data") or {}).get("host"):
            try:
                self.lane_fetch(P, row)                    # once a minute, fast-forward only
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, "", "track_lane_fetch_failed",
                                 detail={"track": row["name"], "detail": str(e)[:300]})
        if row["state"] == "running":
            # 2026-10-29: a sprint cancelled before it ever started (MV2-1, queued by mistake)
            # leaves the track; it never fails it (the failed track moved MV2-0 to the main
            # checkout mid-sprint, away from its staged work)
            dropped = [s for s in row["sprints"]
                       if (self.store.task(P.id, s) or {}).get("state") == S.CANCELLED
                       and not (self.store.task(P.id, s) or {}).get("started_ts")
                       and not int((self.store.task(P.id, s) or {}).get("attempt") or 0)]
            if dropped and len(dropped) < len(row["sprints"]):
                keep = [s for s in row["sprints"] if s not in dropped]
                row = self.store.update_track(P.id, row["name"], sprints=keep)
                self.store.event(P.id, "", "track_sprints_dropped", detail={
                    "track": row["name"], "sprints": dropped,
                    "why": "cancelled before it started"})
            states = {s: (self.store.task(P.id, s) or {}).get("state") for s in row["sprints"]}
            cur = next((s for s, st in states.items() if st != S.COMPLETE), "")
            if cur != row.get("current_sprint"):
                self.store.update_track(P.id, row["name"], current_sprint=cur)
            bad = [f"{s} {st}" for s, st in states.items() if st in (S.FAILED, S.CANCELLED)]
            if bad:
                self.fail(P, row, "sprint " + ", ".join(bad))
                return
            if all(st == S.COMPLETE for st in states.values()):
                self.resources.release_for_track(P, row["name"])
                self.store.merge_track_data(P.id, row["name"], merge_stage="wait")
                row = self.store.update_track(P.id, row["name"], state="merging",
                                              current_sprint="")
                self.store.event(P.id, "", "track_ready_to_merge", detail={"track": row["name"]})
        if row["state"] == "merging":
            self.merger.step(P, self.store.track(P.id, row["name"]))

    # ------------------------------------------------------------------ main-sprint facts

    def main_tasks(self, P) -> list[dict]:
        P = self.main_of(P)
        return [t for t in self.store.tasks(P.id) if not self.store.task_track(P.id, t["task_id"])]

    QUIET_BLOCKERS = (S.IMPLEMENTING, S.VERIFYING, S.REVIEWING, S.APPROVED, S.CLOSING)

    def main_quiescent(self, P) -> tuple[bool, str]:
        """No live main session and no main task between a bound head and its
        close: the only time a merge may move master."""
        busy = [f"{t['task_id']} {t['state']}" for t in self.main_tasks(P)
                if t["state"] in self.QUIET_BLOCKERS]
        if busy:
            return False, "main sprint busy: " + ", ".join(busy[:4])
        return True, ""

    def main_running_sprint(self, P) -> str:
        for t in self.main_tasks(P):
            if t["state"] in S.ACTIVE and t["state"] != S.QUEUED:
                return t["task_id"]
        return ""

    def observe_session(self, P, t: dict, sid: str) -> None:
        """Read a main session's transcript (rate limited) for Studio tool calls,
        only while a track could want Studio (planned/running/merging)."""
        if self.is_track(P) or not sid:
            return
        if not any(r["state"] in LIVE_STATES for r in self.store.tracks(P.id)):
            return
        now = self.clock()
        if now - self._observed.get(sid, -1e18) < OBSERVE_EVERY_S:
            return
        self._observed[sid] = now
        try:
            msgs = P.client.transcript(sid, directory=P.profile.workspace) or []
        except Exception:                                         # noqa: BLE001
            return
        self.resources.observe_transcript(P, msgs, sid)

    def verify_track(self, P, spec, head: str, base: str, phase: str = "verify") -> list:
        """A track sprint's project checks without Studio (verify and closeout):
        the adapter's `verify_track` when it has one, else its plain verify."""
        fn = getattr(P.adapter, "verify_track", None)
        if callable(fn) and self.studio_less(P):
            return fn(spec, phase, head, base)
        # a slot-bound track has Studio: the full checks, gate binding included
        return P.adapter.verify(spec, phase, head, base)

    # ------------------------------------------------------------------ analysis / proposal

    def analyze(self, P) -> dict:
        P = self.main_of(P)
        # 2026-10-30: a CANCELLED sprint (S51B: finished as S51B-R; the superseded UI-02..06)
        # was proposed and listed on the main chain again; only an explicit start reopens one
        done = [t["task_id"] for t in self.store.tasks(P.id)
                if t["state"] in (S.COMPLETE, S.CANCELLED)]
        return AN.analyze_project(P, done=done, append_safe=self.setting(P, "track_append_safe"),
                                  main_sprint=self.main_running_sprint(P))

    def propose(self, P, max_agents: Optional[int] = None, actor: str = "") -> dict:
        P = self.main_of(P)
        n = int(max_agents) if max_agents else self.max_agents()
        n = max(1, min(MAX_AGENTS_LIMIT, n))
        an = self.analyze(P)
        running = [t["task_id"] for t in self.store.tasks(P.id) if t["state"] not in S.TERMINAL]
        existing = {s for r in self.store.tracks(P.id) if r["state"] in LIVE_STATES
                    for s in r["sprints"]}
        from ..slot_blender import blender_capacity
        prop = AN.propose(an, n, running=list(set(running) | existing),
                          capacity={"blender": blender_capacity(P.profile)})
        plan_id = "trp-" + uuid.uuid4().hex[:12]
        plan = {"plan_id": plan_id, "project": P.id, "ts": self.clock(), "actor": actor,
                "max_agents": n, "current_max_agents": self.max_agents(),
                "agents": prop["agents"], "main": prop["main"], "tracks": prop["tracks"],
                "notes": prop["notes"], "serial": an["serial"][:40], "warnings": an["warnings"]}
        import json
        self.store.set_meta(f"track_plan:{plan_id}", json.dumps(plan, default=str))
        return plan

    def plan(self, plan_id: str) -> Optional[dict]:
        import json
        v = self.store.meta(f"track_plan:{plan_id}") if plan_id else None
        if not v:
            return None
        try:
            return json.loads(v)
        except ValueError:
            return None

    def start_plan(self, P, plan_id: str, actor: str, names: Optional[list[str]] = None) -> list[dict]:
        """Create and start every track of a proposal (or the named ones)."""
        P = self.main_of(P)
        plan = self.plan(plan_id)
        if not plan or plan.get("project") != P.id:
            raise TrackError(f"no proposal {plan_id} for {P.id} (propose again)")
        if self.clock() - float(plan.get("ts") or 0) > PLAN_TTL_S:
            raise TrackError(f"proposal {plan_id} is older than a day; propose again")
        if plan.get("started"):
            raise TrackError(f"proposal {plan_id} already started")
        out = []
        for t in plan.get("tracks") or []:
            if names and t["name"] not in names:
                continue
            row = self.create(P, t["name"], t["sprints"], t.get("needs"), actor)
            out.append(self.start(P, row["name"], actor))
        import json
        plan["started"] = {"ts": self.clock(), "by": actor, "tracks": [r["name"] for r in out]}
        self.store.set_meta(f"track_plan:{plan_id}", json.dumps(plan, default=str))
        return out

    # ------------------------------------------------------------------ status

    def status(self, P) -> dict:
        """What the heartbeat, the chat and the Tracks tab show."""
        P = self.main_of(P)
        rows = []
        for r in self.store.tracks(P.id):
            sprints = []
            for s in r["sprints"]:
                t = self.store.task(P.id, s) or {}
                sprints.append({"task_id": s, "state": t.get("state") or "NOT_QUEUED",
                                "next": str(t.get("next_action") or "")[:160],
                                "blocker": str(t.get("blocker") or "")[:160]})
            d = r["data"] or {}
            rows.append({"name": r["name"], "kind": r["kind"], "state": r["state"],
                         "worktree": r["worktree"], "branch": r["branch"],
                         "base": (r["base_sha"] or "")[:12], "needs": r["needs"],
                         "current_sprint": r["current_sprint"], "sprints": sprints,
                         "created_by": r["created_by"], "created_ts": r["created_ts"],
                         "updated_ts": r["updated_ts"], "merged_sha": (r["merged_sha"] or "")[:12],
                         "merge": {k: d.get(k) for k in ("merge_stage", "merge_wait", "conflict",
                                                         "decision", "gate", "merged_files",
                                                         "merge_head", "merge_base", "failed",
                                                         "stopped", "merge_requested_by")
                                   if d.get(k) is not None},
                         "kilo_config": d.get("kilo_config") or "",
                         # Distributed Foundry phase b: where the worktree is ('' = here)
                         "host": d.get("host") or "",
                         "lease": (self.resources.lease(P) or {}).get("track") == r["name"]})
        quiet, why = self.main_quiescent(P)
        try:
            from .. import lane_tunnel
            tunnels = lane_tunnel.MANAGER.view()
        except Exception:                                         # noqa: BLE001
            tunnels = {}
        return {"max_agents": self.max_agents(),
                "max_agents_override": getattr(self.c, "max_writers_override", None),
                "agents_running": self.agents_running(),
                "main": {"sprint": self.main_running_sprint(P), "quiescent": quiet, "why": why},
                "tracks": rows, "studio": self.resources.studio_view(P),
                "lane_tunnels": tunnels}

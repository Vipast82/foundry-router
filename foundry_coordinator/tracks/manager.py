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
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise TrackError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:400]}")
    return r


class TrackManager:
    def __init__(self, c, clock: Callable[[], float] = time.time):
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
        for P in self.c.projects.values():
            try:
                vals.append(int(self.setting(P, "max_agents") or 1))
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
        tp = dataclasses.replace(
            prof, workspace=row["worktree"], branch=row["branch"],
            resources=needs_locks(row.get("needs") or []),
            publish={"mode": "none"},
            fast_lane={**(prof.fast_lane or {}), "enabled": False})
        tp.__dict__["track"] = row["name"]
        tp.__dict__["main_workspace"] = prof.workspace
        try:
            adapter = load_adapter(tp, self.c.state_dir)
        except Exception as e:                                    # noqa: BLE001
            raise TrackError(f"track {row['name']}: adapter for the worktree: {e}") from None
        TP = Project(profile=tp, adapter=adapter, client=P.client, foundry=P.foundry,
                     ws=Workspace(row["worktree"]))
        self._projects[key] = TP
        return TP

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
            if t and t["state"] not in S.TERMINAL:
                where = self.store.task_track(P.id, s) or "the main checkout"
                raise TrackError(f"sprint {s} is already {t['state']} on {where}")
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
        src = kilo_path(P.profile)
        dst = os.path.join(row["worktree"], "kilo.jsonc")
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
                return dst
            except OSError as e:
                self.store.event(P.id, "", "track_kilo_config_failed",
                                 detail={"track": row["name"], "error": str(e)[:300]})
        return dst if os.path.isfile(dst) else ""

    def create(self, P, name: str, sprints: list[str], needs: Optional[list[str]] = None,
               actor: str = "operator", kind: str = "parallel") -> dict:
        """Create the worktree + branch and register the track (state planned)."""
        P = self.main_of(P)
        name = self._check_name(name)
        if self.store.track(P.id, name):
            raise TrackError(f"track {name} already exists ({self.store.track(P.id, name)['state']})")
        sprints = self._check_sprints(P, sprints)
        if needs is None:
            plans = [AN.plan_from_spec(P.adapter.describe(s)) for s in sprints]
            try:
                plans = [p for p in AN.collect_plans(P) if p.task_id in sprints] or plans
            except Exception:                                     # noqa: BLE001
                pass
            needs = AN._normalize(n for p in plans for n in p.needs)
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
        row = self.store.add_track(P.id, name, kind, os.path.realpath(path), branch, base,
                                   list(needs or []), sprints, actor,
                                   data={"created_ts": self.clock()})
        kc = self._kilo_config(P, row)
        row = self.store.update_track(P.id, name, data={**row["data"], "kilo_config": kc})
        self.store.event(P.id, "", "track_created", detail={
            "track": name, "branch": branch, "worktree": row["worktree"], "base": base[:12],
            "sprints": sprints, "needs": list(needs or []), "kilo_config": kc, "actor": actor})
        return row

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
        row = self.store.update_track(P.id, name, data={**(row.get("data") or {}),
                                                        "kilo_config": kc})
        self.store.event(P.id, "", "track_needs_changed", detail={
            "track": name, "needs": new, "kilo_config": kc, "actor": actor})
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
        self.resources.release_studio(P, name, "track stopped")
        self.resources.release_for_track(P, name)
        self.store.merge_track_data(P.id, name, stopped={"by": actor, "reason": reason,
                                                         "ts": self.clock()})
        row = self.store.update_track(P.id, name, state="stopped")
        self.store.event(P.id, "", "track_stopped", detail={"track": name, "actor": actor,
                                                           "reason": reason,
                                                           "cancelled": cancelled})
        return row

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
        if callable(fn):
            return fn(spec, phase, head, base)
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
        prop = AN.propose(an, n, running=list(set(running) | existing))
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
                         "lease": (self.resources.lease(P) or {}).get("track") == r["name"]})
        quiet, why = self.main_quiescent(P)
        return {"max_agents": self.max_agents(),
                "max_agents_override": getattr(self.c, "max_writers_override", None),
                "agents_running": self.agents_running(),
                "main": {"sprint": self.main_running_sprint(P), "quiescent": quiet, "why": why},
                "tracks": rows, "studio": self.resources.studio_view(P)}

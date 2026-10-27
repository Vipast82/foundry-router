"""Pull the router's deployed automation settings and apply them at a safe point.

The router (C1 config store) holds the settings the operator edits in the GUI
as versioned revisions. Each run-loop tick (at most every
[config_pull] interval_seconds) this asks the router for the deployed revision
of each project (GET /v1/foundry/control/config, orchestration key):

  * a new revision is overlaid onto the LOCAL project.toml (secrets, URLs,
    env-var names and the workspace path always stay local) THREE-WAY against
    the revision applied before (W2.4): a key the router did not change keeps
    its local value, so a hand edit of project.toml survives; a key both
    sides changed goes to the router and is logged (`config_local_drift`
    event, `conflicts`); then validated with the same profile loader as a
    TOML file, and applied in place (the live Profile object is updated, so
    every module sees the new values on its next read) -- but only when no
    session of that project is live; until then it is reported `pending`;
  * restart-scoped keys (schema `apply: restart`: client.url, supervisor,
    repair_root ...) are NOT applied in place: the live value stays, the
    revision is cached, a `restart_coordinator` client request is written
    (client_requests.py; the Foundry Client restarts the coordinator at its
    safe point) and the report carries `restart_needed`; the restarted
    coordinator applies the cached revision whole (`restart_pending`);
  * a change to role_rules regenerates the kilo.jsonc agent blocks
    (kilo_config.regenerate, backed up and verified) and writes a
    `reload_kilo` client request, since Kilo reads its config at start;
  * an invalid one is reported `rejected` and the last applied revision stays;
  * the applied revision is cached at <state_dir>/config/<project>.json, so a
    restart, or a router that can't be reached, keeps the last applied
    settings (no cache: the TOML alone, as before).

Every outcome is reported back (POST /v1/foundry/control/config/applied) and
recorded as a coordinator event (config_applied / config_rejected).
Turn it off per project with `[config_pull] enabled = false` in project.toml.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import time
import tomllib
from typing import Callable, Optional

from foundry_automation import document as D
from . import client_requests
from .adapters.foundry import FoundryError
from .profile import ProfileError, from_raw

LIVE_SESSION_MAX_AGE_S = 24 * 3600


def restart_paths(prev: Optional[dict], doc: dict) -> list[str]:
    """Changed settings whose field is `apply: restart`."""
    return [p for p in D.restart_needed(prev, doc) if not p.startswith("role_rules.")]


def hold_restart_keys(merged: dict, live_raw: dict, paths: list[str]) -> dict:
    """`merged` with every restart-scoped path reset to the value the running
    process loaded (`live_raw`), so nothing restart-scoped changes in place."""
    out = copy.deepcopy(merged)
    for p in paths:
        cur = D._get_path(live_raw, p, D._MISSING)
        if cur is D._MISSING:
            D._pop_path(out, p)
        else:
            D._set_path(out, p, copy.deepcopy(cur))
    return out


def role_rules_changed(prev: Optional[dict], doc: dict) -> bool:
    return any(c["path"].startswith("role_rules.") for c in D.diff(prev, doc))


def startup_overlay(state_dir: str, raw: dict) -> tuple[dict, Optional[dict]]:
    """(profile raw with the cached revision applied whole, the cache record)
    for a coordinator that is STARTING: the hook for the profile loader, so
    restart-scoped keys of the last applied revision are in force before any
    object is built from the profile (not wired by this module; see
    audit2/W2_notes.md). Without a cache: (raw, None)."""
    pid = str(raw.get("id") or "")
    path = os.path.join(state_dir, "config", f"{pid}.json")
    try:
        with open(path, encoding="utf-8") as f:
            body = json.load(f)
    except (OSError, ValueError):
        return raw, None
    if not (isinstance(body, dict) and isinstance(body.get("doc"), dict)):
        return raw, None
    if not D.validate(body["doc"], pid)["ok"]:
        return raw, None
    local_base = body.get("local") if isinstance(body.get("local"), dict) else None
    merged, _ = D.overlay3(raw, body["doc"], body["doc"], local_base)
    return merged, body


class ConfigPuller:
    def __init__(self, coordinator, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time, source_id: str = ""):
        self.c = coordinator
        self.store = coordinator.store
        self.log = log
        self.clock = clock
        self.source_id = source_id or f"coordinator:{socket.gethostname()}"
        self.dir = os.path.join(coordinator.state_dir, "config")
        self._last_poll: dict[str, float] = {}
        self._applied: dict[str, dict] = {}      # pid -> {rev, hash, doc} applied in this process
        self._pending: dict[str, dict] = {}      # pid -> body waiting for a safe point
        self._reported: dict[str, tuple] = {}    # pid -> (rev, status) last reported
        self._fresh: set = set()                 # pids whose cached revision applies whole
        self._unreachable: dict[str, bool] = {}  # pid -> last poll failed (log once per change)
        for pid in coordinator.projects:
            cached = self.cached(pid)
            if cached and self.enabled(pid):
                self._pending[pid] = cached      # applied at the first safe point
                self._fresh.add(pid)             # a fresh process: restart keys apply too

    # ------------------------------------------------------------------ settings

    def _cp(self, pid: str) -> dict:
        P = self.c.projects[pid]
        return dict((getattr(P.profile, "raw", None) or {}).get("config_pull") or {})

    def enabled(self, pid: str) -> bool:
        P = self.c.projects.get(pid)
        return bool(P is not None and P.foundry is not None and
                    self._cp(pid).get("enabled", True))

    def interval(self, pid: str) -> float:
        try:
            return max(10.0, float(self._cp(pid).get("interval_seconds", 60)))
        except (TypeError, ValueError):
            return 60.0

    def kilo_regen(self, pid: str) -> bool:
        """[config_pull] kilo_config_apply (default true): rewrite kilo.jsonc
        agent blocks when deployed role rules change."""
        return bool(self._cp(pid).get("kilo_config_apply", True))

    # ------------------------------------------------------------------ cache

    def _cache_path(self, pid: str) -> str:
        return os.path.join(self.dir, f"{pid}.json")

    def cached(self, pid: str) -> Optional[dict]:
        try:
            with open(self._cache_path(pid), encoding="utf-8") as f:
                body = json.load(f)
            return body if isinstance(body, dict) and isinstance(body.get("doc"), dict) else None
        except (OSError, ValueError):
            return None

    def _write_cache(self, pid: str, body: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self._cache_path(pid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**body, "applied_ts": self.clock()}, f, indent=1)
        os.replace(tmp, self._cache_path(pid))

    # ------------------------------------------------------------------ safe point

    def busy(self, pid: str) -> str:
        """Why now is not a safe point for this project ("" = safe)."""
        q = self.store.q
        since = self.clock() - LIVE_SESSION_MAX_AGE_S
        n = q("SELECT COUNT(*) AS n FROM sessions WHERE project_id=? AND ended_ts IS NULL "
              "AND prompt_ts IS NOT NULL AND created_ts >= ?", (pid, since))[0]["n"]
        if n:
            return f"{n} live session(s)"
        try:
            h = q("SELECT COUNT(*) AS n FROM helper_units WHERE project_id=? AND "
                  "status='running'", (pid,))[0]["n"]
        except Exception:                                        # noqa: BLE001
            h = 0
        return f"{h} helper(s) running" if h else ""

    # ------------------------------------------------------------------ loop

    def poll(self) -> None:
        for pid in sorted(self.c.projects):
            if not self.enabled(pid):
                continue
            try:
                self.sync(pid)
            except Exception as e:                               # noqa: BLE001
                self.log(f"config: {pid}: {type(e).__name__}: {e}")

    def sync(self, pid: str, force: bool = False) -> str:
        """One pull/apply step; returns what happened (for logs and tests)."""
        P = self.c.projects[pid]
        now = self.clock()
        if force or now - self._last_poll.get(pid, 0) >= self.interval(pid):
            self._last_poll[pid] = now
            have = self._pending.get(pid) or self._applied.get(pid) or {}
            try:
                status, body = P.foundry.get_config(pid, etag=str(have.get("hash") or ""))
                if self._unreachable.get(pid):
                    self.log(f"config: {pid}: router reachable again")
                self._unreachable[pid] = False
            except FoundryError as e:
                if not self._unreachable.get(pid):               # once per state change
                    self.log(f"config: {pid}: router unreachable, keeping revision "
                             f"{(self._applied.get(pid) or {}).get('rev', 'local')}: {e}")
                self._unreachable[pid] = True
                status, body = "unreachable", None
            if status == "ok" and body and body.get("hash") != \
                    (self._applied.get(pid) or {}).get("hash"):
                self._pending[pid] = body
                self._fresh.discard(pid)
        body = self._pending.get(pid)
        if not body:
            return "current"
        why = self.busy(pid)
        if why:
            self._report(pid, body, "pending", f"waiting for a safe point: {why}")
            return "pending"
        return self.apply(pid, body)

    def apply(self, pid: str, body: dict) -> str:
        P = self.c.projects[pid]
        self._pending.pop(pid, None)
        fresh = pid in self._fresh
        self._fresh.discard(pid)
        prof = P.profile
        try:
            with open(prof.path, "rb") as f:
                local = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            self._reject(pid, body, f"cannot read the local profile {prof.path}: {e}")
            return "rejected"
        problems = D.validate(body["doc"], pid)
        if not problems["ok"]:
            self._reject(pid, body, "; ".join(f"{e['path']}: {e['message']}"
                                              for e in problems["errors"][:8]))
            return "rejected"
        prev_rec = self._applied.get(pid) or self.cached(pid) or {}
        prev = prev_rec.get("doc") if isinstance(prev_rec.get("doc"), dict) else None
        local_base = prev_rec.get("local") if isinstance(prev_rec.get("local"), dict) else None
        merged, report = D.overlay3(local, body["doc"], prev, local_base)
        # restart-scoped keys: hold the live values, ask the client for a restart
        restart = [] if fresh else restart_paths(prev if prev is not None else
                                                 D.import_profile(prof.raw, pid)[0], body["doc"])
        if restart:
            merged = hold_restart_keys(merged, prof.raw, restart)
        try:
            new = from_raw(merged, prof.path)
        except ProfileError as e:
            self._reject(pid, body, str(e))
            return "rejected"
        if new.id != prof.id:
            self._reject(pid, body, f"the merged profile is for {new.id!r}")
            return "rejected"
        rules_changed = role_rules_changed(prev, body["doc"]) or (prev is None)
        prof.__dict__.update(new.__dict__)          # in place: every holder sees it
        tracks = getattr(self.c, "tracks", None)
        if tracks is not None and hasattr(tracks, "after_config_apply"):
            try:
                tracks.after_config_apply(P, prev, body["doc"])
            except Exception as e:                                # noqa: BLE001
                self.log(f"config: {pid}: max_agents restore failed: {e}")
        self.store.register_project(prof.id, prof.name, prof.path, prof.hash, prof.version,
                                    prof.workspace)
        if report["kept_local"] or report["conflicts"]:
            self.store.event(pid, "", "config_local_drift",
                             detail={"rev": body.get("rev"),
                                     "kept_local": report["kept_local"][:40],
                                     "conflicts": report["conflicts"][:40]})
            if report["conflicts"]:
                self.log(f"config: {pid}: local edits overridden by revision "
                         f"{body.get('rev')}: {', '.join(report['conflicts'][:10])}")
            if report["kept_local"]:
                self.log(f"config: {pid}: local edits kept (router unchanged): "
                         f"{', '.join(report['kept_local'][:10])}")
        self.store.event(pid, "", "config_applied",
                         detail={"rev": body.get("rev"), "hash": body.get("hash"),
                                 "restart_needed": restart, "fresh": fresh,
                                 "kept_local": report["kept_local"][:40],
                                 "conflicts": report["conflicts"][:40]})
        rec = {k: body.get(k) for k in ("project", "rev", "hash", "ts", "actor", "doc")}
        rec["restart_pending"] = restart
        rec["local"] = D.local_view(local)         # the TOML as it was at this apply
        self._applied[pid] = rec
        try:
            self._write_cache(pid, rec)
        except OSError as e:
            self.log(f"config: {pid}: cache not written: {e}")
        if restart:
            client_requests.request(
                self.c.state_dir, "restart_coordinator", pid,
                f"router revision {body.get('rev')} changed restart-scoped settings: "
                + ", ".join(restart[:8]), detail={"rev": body.get("rev"), "paths": restart},
                store=self.store, clock=self.clock)
        if rules_changed:
            self._regenerate_kilo(pid, body)
        self.log(f"config: {pid}: applied router revision {body.get('rev')}"
                 + (f" (restart needed for {', '.join(restart)})" if restart else ""))
        self._report(pid, body, "applied", "", restart)
        return "applied"

    def _regenerate_kilo(self, pid: str, body: dict) -> Optional[dict]:
        """W2.4 (C1 action): rewrite the kilo.jsonc agent blocks from the
        deployed role rules and ask the client to reload Kilo. Never rejects
        the revision: a failure is an event and a log line."""
        if not self.kilo_regen(pid):
            return None
        P = self.c.projects[pid]
        from .kilo_config import KiloConfigError, regenerate
        try:
            out = regenerate(P.profile, body["doc"].get("role_rules"), apply=True,
                             store=self.store, clock=self.clock)
        except KiloConfigError as e:
            self.log(f"config: {pid}: kilo.jsonc not regenerated: {e}")
            self.store.event(pid, "", "kilo_config_skipped",
                             detail={"rev": body.get("rev"), "why": str(e)[:500]})
            return None
        if out.get("missing"):
            self.log(f"config: {pid}: kilo.jsonc has no agent block for "
                     f"{', '.join(out['missing'])} (never created)")
        if out.get("applied"):
            client_requests.request(
                self.c.state_dir, "reload_kilo", pid,
                f"role rules revision {body.get('rev')} rewrote kilo.jsonc agent blocks "
                f"{', '.join(out['changed'])}", detail={"rev": body.get("rev"),
                                                        "changed": out["changed"],
                                                        "backup": out.get("backup")},
                store=self.store, clock=self.clock)
        return out

    def applied(self, pid: str) -> Optional[dict]:
        return self._applied.get(pid)

    # ------------------------------------------------------------------ reports

    def _reject(self, pid: str, body: dict, why: str) -> None:
        self.log(f"config: {pid}: router revision {body.get('rev')} rejected: {why[:300]}")
        self.store.event(pid, "", "config_rejected",
                         detail={"rev": body.get("rev"), "hash": body.get("hash"),
                                 "why": why[:1000]})
        self._report(pid, body, "rejected", why)

    def _report(self, pid: str, body: dict, status: str, detail: str,
                restart: Optional[list] = None) -> None:
        key = (body.get("rev"), body.get("hash"), status)
        if self._reported.get(pid) == key:
            return
        P = self.c.projects[pid]
        try:
            P.foundry.config_applied({"project": pid, "rev": body.get("rev"),
                                      "hash": body.get("hash"), "status": status,
                                      "detail": detail[:3000], "restart_needed": restart or [],
                                      "source_id": self.source_id})
            self._reported[pid] = key
        except FoundryError as e:
            self.log(f"config: {pid}: could not report {status}: {e}")

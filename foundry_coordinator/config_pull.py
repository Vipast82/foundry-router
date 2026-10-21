"""Pull the router's deployed automation settings and apply them at a safe point.

The router (C1 config store) holds the settings the operator edits in the GUI
as versioned revisions. Each run-loop tick (at most every
[config_pull] interval_seconds) this asks the router for the deployed revision
of each project (GET /v1/foundry/control/config, orchestration key):

  * a new revision is overlaid onto the LOCAL project.toml (secrets, URLs,
    env-var names and the workspace path always stay local), validated with
    the same profile loader as a TOML file, and applied in place (the live
    Profile object is updated, so every module sees the new values on its next
    read) -- but only when no session of that project is live; until then it
    is reported `pending`;
  * an invalid one is reported `rejected` and the last applied revision stays;
  * the applied revision is cached at <state_dir>/config/<project>.json, so a
    restart, or a router that can't be reached, keeps the last applied
    settings (no cache: the TOML alone, as before).

Every outcome is reported back (POST /v1/foundry/control/config/applied) and
recorded as a coordinator event (config_applied / config_rejected).
Turn it off per project with `[config_pull] enabled = false` in project.toml.
"""

from __future__ import annotations

import json
import os
import socket
import time
import tomllib
from typing import Callable, Optional

from foundry_automation import document as D

from .adapters.foundry import FoundryError
from .profile import ProfileError, from_raw

LIVE_SESSION_MAX_AGE_S = 24 * 3600


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
        for pid in coordinator.projects:
            cached = self.cached(pid)
            if cached and self.enabled(pid):
                self._pending[pid] = cached      # applied at the first safe point

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
            except FoundryError as e:
                self.log(f"config: {pid}: router unreachable, keeping revision "
                         f"{(self._applied.get(pid) or {}).get('rev', 'local')}: {e}")
                status, body = "unreachable", None
            if status == "ok" and body and body.get("hash") != \
                    (self._applied.get(pid) or {}).get("hash"):
                self._pending[pid] = body
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
        merged = D.overlay(local, body["doc"])
        try:
            new = from_raw(merged, prof.path)
        except ProfileError as e:
            self._reject(pid, body, str(e))
            return "rejected"
        if new.id != prof.id:
            self._reject(pid, body, f"the merged profile is for {new.id!r}")
            return "rejected"
        prev = (self._applied.get(pid) or {}).get("doc") or D.import_profile(prof.raw, pid)[0]
        restart = D.restart_needed(prev, body["doc"])
        prof.__dict__.update(new.__dict__)          # in place: every holder sees it
        self.store.register_project(prof.id, prof.name, prof.path, prof.hash, prof.version,
                                    prof.workspace)
        self.store.event(pid, "", "config_applied",
                         detail={"rev": body.get("rev"), "hash": body.get("hash"),
                                 "restart_needed": restart})
        rec = {k: body.get(k) for k in ("project", "rev", "hash", "ts", "actor", "doc")}
        self._applied[pid] = rec
        try:
            self._write_cache(pid, rec)
        except OSError as e:
            self.log(f"config: {pid}: cache not written: {e}")
        self.log(f"config: {pid}: applied router revision {body.get('rev')}"
                 + (f" (restart needed for {', '.join(restart)})" if restart else ""))
        self._report(pid, body, "applied", "", restart)
        return "applied"

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

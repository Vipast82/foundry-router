"""File-based requests for the client, from other programs on this workstation (the
coordinator, the permission escalation flow). Drop a JSON file in
`<state_dir>/client-requests/`:

    {"action": "reload_kilo", "id": "abc", "force": false, "reason": "..."}

actions: reload_kilo (safe-point restart of the Kilo server), restart_rojo,
restart_coordinator (safe point), apply_kilo_config (optional "project", "dry_run"),
start_service ("component" or detail.component: start it if stopped, restart it if unhealthy).

The client moves the file to `processing/`, answers in `results/<same file name>`
({"status": "pending" | "done" | "failed", "message", "ts", ...}; a safe-point restart
shows "pending" with the reason until it happens), and archives the request in `done/`.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

from .config import expand

ALIASES = {
    "reload_kilo": "reload_kilo", "kilo_reload": "reload_kilo", "reload_kilo_config": "reload_kilo",
    "restart_kilo": "reload_kilo", "stack_reload_kilo": "reload_kilo",
    "restart_rojo": "restart_rojo", "stack_restart_rojo": "restart_rojo",
    "restart_coordinator": "restart_coordinator", "stack_restart_coordinator": "restart_coordinator",
    "apply_kilo_config": "apply_kilo_config", "kilo_config_apply": "apply_kilo_config",
    "start_service": "start_service", "service_start": "start_service",
}
TIMEOUT_S = 2 * 3600          # a request still pending after this is reported failed


def action_of(req: dict) -> str:
    raw = next((str(req[k]) for k in ("action", "kind", "type", "request") if req.get(k)), "")
    return ALIASES.get(re.sub(r"[\s\-]+", "_", raw.strip().lower()), "")


class RequestQueue:
    def __init__(self, mgr):
        self.mgr = mgr
        self.inflight: dict[str, dict] = {}
        self.lock = threading.Lock()

    # -- paths
    def root(self) -> Path:
        return Path(expand(self.mgr.cfg["client"]["state_dir"])) / "client-requests"

    def _write(self, name: str, body: dict) -> None:
        d = self.root() / "results"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps({**body, "ts": self.mgr.clock()}, indent=2), encoding="utf-8")
        os.replace(tmp, d / name)

    def _archive(self, name: str) -> None:
        src = self.root() / "processing" / name
        if src.is_file():
            dst = self.root() / "done"
            dst.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst / name)

    # -- the loop
    def poll(self) -> int:
        """Pick up new requests and settle the ones waiting for a safe point."""
        root = self.root()
        n = 0
        if root.is_dir():
            for d in (root, root / "processing"):
                if not d.is_dir():
                    continue
                for f in sorted(d.glob("*.json")):
                    if f.name in self.inflight:
                        continue
                    n += self._take(f)
        for name in list(self.inflight):
            self._settle(name)
        return n

    def _take(self, f: Path) -> int:
        name = f.name
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            return 0
        try:
            req = json.loads(text)
            if not isinstance(req, dict):
                req = {"action": str(req)}
        except ValueError:
            req = {"action": text.strip()}
        if f.parent == self.root():
            (self.root() / "processing").mkdir(parents=True, exist_ok=True)
            try:
                os.replace(f, self.root() / "processing" / name)
            except OSError:
                return 0                           # the writer still has it open: next poll
        act = action_of(req)
        rec = {"req": req, "action": act, "since": self.mgr.clock()}
        if not act:
            return self._finish(name, rec, "failed", "unknown action "
                                f"{(req.get('action') or req.get('kind') or '')!r}")
        try:
            return self._start(name, rec)
        except Exception as e:                                     # noqa: BLE001
            return self._finish(name, rec, "failed", f"{type(e).__name__}: {e}")

    def _start(self, name: str, rec: dict) -> int:
        m, act, req = self.mgr, rec["action"], rec["req"]
        if act == "apply_kilo_config":
            self.inflight[name] = rec
            self._write(name, {"status": "pending", "message": "applying kilo.jsonc"})
            threading.Thread(target=self._apply_worker, args=(name, rec), daemon=True,
                             name="foundry-client-kilo-config").start()
            return 1
        if act == "start_service":
            return self._start_service(name, rec)
        comp = {"reload_kilo": "kilo", "restart_rojo": "rojo",
                "restart_coordinator": "coordinator"}[act]
        if comp not in m.procs or not m.cfg["components"][comp]["enabled"]:
            return self._finish(name, rec, "failed", f"{comp} is not managed by this client "
                                                     f"(it is disabled in the settings)")
        rec["component"] = comp
        rec["restarts0"] = m.procs[comp].restarts
        st = m.restart(comp, force=bool(req.get("force")))
        self.inflight[name] = rec
        if st["result"]:
            self._write(name, {"status": "pending", "message": st["result"], "component": comp})
        self._settle(name)
        return 1

    def _start_service(self, name: str, rec: dict) -> int:
        """A sprint needs a service: start the managed component when it is stopped, restart it
        when it is unhealthy, leave it alone when it runs. Never enables a disabled component
        (that is the operator's switch)."""
        m, req = self.mgr, rec["req"]
        comp = str(req.get("component") or (req.get("detail") or {}).get("component") or "")
        if comp not in m.procs:
            return self._finish(name, rec, "failed", f"{comp or 'that service'} is not a component "
                                                     f"of this client (it is external)")
        if not m.cfg["components"][comp]["enabled"]:
            return self._finish(name, rec, "failed", f"{comp} is switched off in the settings; "
                                                     f"turn it on in the Services tab")
        pr = m.procs[comp]
        if pr.state == "running":
            return self._finish(name, rec, "done", f"{pr.spec.label} is already running",
                                component=comp, state=pr.state)
        if pr.state == "unhealthy":
            st = m.restart(comp, force=bool(req.get("force")))
        else:
            st = m.start(comp)
        return self._finish(name, rec, "done", f"{pr.spec.label}: {st.get('state')}"
                            + (f" ({st['detail']})" if st.get("detail") else ""),
                            component=comp, state=st.get("state"))

    def _apply_worker(self, name: str, rec: dict) -> None:
        try:
            res = self.mgr.apply_kilo_config(str(rec["req"].get("project") or ""),
                                             bool(rec["req"].get("dry_run")),
                                             reload=bool(rec["req"].get("reload", True)))
        except Exception as e:                                     # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        rec["done"] = res

    def _settle(self, name: str) -> None:
        rec = self.inflight.get(name)
        if rec is None:
            return
        m = self.mgr
        if rec["action"] == "apply_kilo_config":
            res = rec.get("done")
            if res is not None:
                self._finish(name, rec, "done" if res.get("ok") else "failed",
                             res.get("error") or ("kilo.jsonc updated" if res.get("applied")
                                                  else "kilo.jsonc already up to date"
                                                  if not res.get("changed") else "dry run"),
                             **{k: v for k, v in res.items() if k != "error"})
            return
        pr = m.procs[rec["component"]]
        if pr.pending_restart:
            if m.clock() - rec["since"] > TIMEOUT_S:
                m.cancel_restart(rec["component"])
                self._finish(name, rec, "failed", f"gave up waiting for a safe point "
                                                  f"({pr.pending_reason})")
            else:
                msg = f"waiting for a safe point: {pr.pending_reason}"
                if rec.get("last") != msg:
                    rec["last"] = msg
                    self._write(name, {"status": "pending", "message": msg,
                                       "component": rec["component"]})
            return
        if pr.restart_result == "restarted" or pr.restarts > rec["restarts0"]:
            extra = " Reconnect the Rojo plugin in Studio." if rec["component"] == "rojo" else ""
            self._finish(name, rec, "done", f"{pr.spec.label} restarted ({pr.state}).{extra}",
                         component=rec["component"], state=pr.state)
        else:
            self._finish(name, rec, "failed", pr.restart_result or "restart did not happen",
                         component=rec["component"], state=pr.state)

    def _finish(self, name: str, rec: dict, status: str, message: str, **extra) -> int:
        self._write(name, {"status": status, "message": message, "action": rec["action"],
                           "id": rec["req"].get("id"), **extra})
        self._archive(name)
        self.inflight.pop(name, None)
        self.mgr._event("client", f"request {name}: {status}: {message}")
        return 1

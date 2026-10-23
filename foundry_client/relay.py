"""Router relay (W3, 2026-10-22): the Foundry Client reports the workstation
stack to the router and carries out the stack actions the operator chat (or a
control-center button) files there.

  heartbeat   POST /v1/foundry/telemetry/heartbeat, source kind `client`,
              feature `stack_control`, each component's state (manager.status())
  claim       POST /v1/foundry/control/claim {source_kind: "client"} -> the
              pending client_* actions; applied through the local Manager
              (never through the client's own HTTP API) and reported with
              POST /v1/foundry/control/actions/<id>/status

Credential: a router client key with the `operator` scope (or the coordinator's
`orchestration` key), read from the OS keyring under the name in
`client.router_key` (default FOUNDRY_CLIENT_KEY; docs/CLIENT_MANAGER.md). The
router never reaches into the workstation: the client polls. Nothing here
changes settings or secrets; the only verbs are restart and reload-config.
"""
from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from . import __version__, config, secrets
from .config import TOGGLE_COMPONENTS

CONTRACT = "foundry.cc.v1"            # control_center.CONTRACT (the client never imports the router)
FEATURES = ["stack_control"]
KINDS = ("client_restart", "client_reload_kilo_config", "client_service_enable")
HEARTBEAT_S = 15.0
CLAIM_S = 3.0
BACKOFF = (5, 15, 30, 60, 120)


class RelayError(RuntimeError):
    pass


class Relay:
    def __init__(self, mgr, cfg: dict, key: str, source_id: str = "",
                 post: Optional[Callable[[str, dict], dict]] = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 kilo_dispose: Optional[Callable[[], tuple[bool, str]]] = None,
                 save: Optional[Callable[[dict], object]] = None):
        self.mgr = mgr
        self.cfg = cfg
        self.url = str(cfg["client"]["backend_url"]).rstrip("/")
        self.key = key
        self.source_id = source_id or f"client:{socket.gethostname()}"
        self._post = post or self._http_post
        self.clock = clock
        self.log = log
        self._dispose = kilo_dispose or self._kilo_dispose
        self._save = save or config.save
        self.last_beat = 0.0
        self.last_ok = 0.0
        self.last_error = ""
        self.fails = 0
        self.compat: dict = {}
        self.handled: list[dict] = []
        self.thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- transport

    def _http_post(self, path: str, body: dict, timeout: float = 10) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                b = json.loads(raw)
                err = b.get("error")
                msg = err.get("message") if isinstance(err, dict) else (err or b.get("detail"))
            except ValueError:
                msg = raw[:200].decode(errors="replace")
            raise RelayError(f"{path}: HTTP {e.code} {msg}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise RelayError(f"{path}: {e}") from None

    # ---------------------------------------------------------------- heartbeat

    def beat_payload(self) -> dict:
        st = self.mgr.status()
        try:
            sv = self.mgr.mcp_status()
        except Exception:                                        # noqa: BLE001
            sv = {}
        return {"mcp": [{k: r.get(k) for k in ("name", "type", "enabled", "available", "detail",
                                                "target")} for r in sv.get("mcp") or []],
                "services": sv.get("services") or [],"source": {"id": self.source_id, "kind": "client", "host": socket.gethostname(),
                           "version": __version__, "capabilities": {"actions": list(KINDS)}},
                "contract": CONTRACT, "features": list(FEATURES), "client_version": __version__,
                "overall": st.get("overall"),
                "components": [{k: c.get(k) for k in ("name", "label", "state", "detail", "enabled",
                                                      "want", "pid", "uptime_s", "restarts",
                                                      "pending_restart")} for c in
                               st.get("components") or []],
                "config_problems": list(st.get("config_problems") or []),
                "events": list(st.get("events") or [])[-10:],
                "sent_ts": self.clock()}

    def heartbeat(self) -> dict:
        out = self._post("/v1/foundry/telemetry/heartbeat", self.beat_payload())
        self.last_beat = self.clock()
        self.compat = {"state": out.get("compat"), "reason": out.get("compat_reason"),
                       "server_version": out.get("server_version")}
        if out.get("compat") in ("warn", "refuse") and out.get("compat_reason"):
            self.log(f"relay: router says {out['compat']}: {out['compat_reason']}")
        return out

    # ---------------------------------------------------------------- actions

    def claim(self) -> list[dict]:
        out = self._post("/v1/foundry/control/claim",
                         {"source_id": self.source_id, "source_kind": "client", "limit": 10})
        return [a for a in (out.get("actions") or []) if isinstance(a, dict)]

    def report(self, action_id: str, status: str, detail: str, result: Optional[dict] = None) -> None:
        self._post(f"/v1/foundry/control/actions/{action_id}/status",
                   {"source_id": self.source_id, "status": status, "detail": detail[:900],
                    "result": result or {}})

    def handle(self, a: dict) -> None:
        aid = str(a.get("action_id") or "")
        kind = a.get("kind")
        payload = a.get("payload") or {}
        try:
            if kind == "client_restart":
                comp = str(payload.get("component") or a.get("component") or "")
                if comp not in self.mgr.procs:
                    self.report(aid, "rejected", f"no component {comp!r} on this workstation")
                    return
                force = bool(payload.get("force"))
                st = self.mgr.restart(comp, force=force)
                detail = (f"{st.get('label') or comp}: {st.get('state')}"
                          + (f" ({st.get('detail')})" if st.get("detail") else ""))
                if st.get("pending_restart") == "safe":
                    detail = f"{st.get('label') or comp}: restart waits for a safe point" + (
                        f" ({st.get('detail')})" if st.get("detail") else "")
                self.report(aid, "applied", detail, {"component": comp, "state": st.get("state"),
                                                     "detail": st.get("detail"),
                                                     "pending_restart": st.get("pending_restart"),
                                                     "force": force})
            elif kind == "client_service_enable":
                comp = str(payload.get("component") or a.get("component") or "")
                on = payload.get("enabled")
                if comp not in TOGGLE_COMPONENTS or comp not in self.mgr.procs:
                    self.report(aid, "rejected", f"{comp!r} cannot be switched on or off here")
                    return
                if not isinstance(on, bool):
                    self.report(aid, "rejected", "enabled must be true or false")
                    return
                ok, msg = self._set_enabled(comp, on)
                self.report(aid, "applied" if ok else "failed", msg,
                            {"component": comp, "enabled": on})
            elif kind == "client_reload_kilo_config":
                ok, how = self._dispose()
                if ok:
                    self.report(aid, "applied", f"Kilo re-read its config: {how}",
                                {"method": "dispose", "detail": how})
                else:
                    st = self.mgr.restart("kilo", force=False)
                    self.report(aid, "applied", f"Kilo instance dispose not available ({how}); "
                                f"restart scheduled at a safe point: {st.get('detail') or st.get('state')}",
                                {"method": "restart", "detail": st.get("detail"),
                                 "state": st.get("state")})
            else:
                self.report(aid, "rejected", f"this client does not handle {kind!r}")
        except RelayError:
            raise
        except Exception as e:                                   # noqa: BLE001
            self.report(aid, "failed", f"client error: {type(e).__name__}: {e}")
        self.handled.append({"action_id": aid, "kind": kind, "ts": self.clock()})
        del self.handled[:-50]

    def _set_enabled(self, comp: str, on: bool) -> tuple[bool, str]:
        """Switch a service component on or off: the setting is saved (client.toml), the
        component starts or stops. Refuses an invalid result (enabling without a program)."""
        import copy
        new = copy.deepcopy(self.mgr.cfg)
        new["components"][comp]["enabled"] = on
        errs = config.validate(new)
        if errs and on:
            return False, "; ".join(errs)[:300]
        try:
            self._save(new)
        except Exception as e:                                   # noqa: BLE001
            return False, f"settings not saved: {type(e).__name__}: {e}"[:300]
        self.mgr.reload(new)
        st = self.mgr.start(comp) if on else self.mgr.stop(comp)
        return True, (f"{st.get('label') or comp}: {'enabled' if on else 'disabled'} "
                      f"({st.get('state')}" + (f", {st['detail']}" if st.get("detail") else "")
                      + ")")

    def _kilo_dispose(self) -> tuple[bool, str]:
        """Ask the Kilo server to drop its instance so the next session re-reads
        kilo.jsonc (opencode-derived POST /instance/dispose). (False, why) when
        the server has no such endpoint or refuses; the caller restarts instead."""
        k = self.cfg["components"].get("kilo") or {}
        port = int(k.get("port") or 4096)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/instance/dispose", data=b"{}",
                                     method="POST", headers={"content-type": "application/json"})
        pw = secrets.get("KILO_SERVER_PASSWORD")
        if pw:
            tok = base64.b64encode(f"kilo:{pw}".encode()).decode()
            req.add_header("authorization", f"Basic {tok}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return True, f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            return False, str(e)[:120]

    # ---------------------------------------------------------------- loop

    def tick(self) -> None:
        now = self.clock()
        if now - self.last_beat >= HEARTBEAT_S:
            self.heartbeat()
        for a in self.claim():
            self.handle(a)
        self.last_ok = now
        self.fails = 0
        self.last_error = ""

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
                wait = CLAIM_S
            except RelayError as e:
                self.fails += 1
                self.last_error = str(e)[:300]
                wait = BACKOFF[min(self.fails - 1, len(BACKOFF) - 1)]
                if self.fails in (1, 3) or self.fails % 10 == 0:
                    self.log(f"relay: {e} (retry in {wait} s)")
            except Exception as e:                               # noqa: BLE001
                self.fails += 1
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                wait = BACKOFF[-1]
                self.log(f"relay: unexpected {self.last_error}")
            stop.wait(wait)

    def start_thread(self, stop: threading.Event) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.run_forever, args=(stop,),
                                           name="foundry-client-relay", daemon=True)
            self.thread.start()

    def status(self) -> dict:
        return {"source_id": self.source_id, "router": self.url, "last_beat": self.last_beat,
                "last_ok": self.last_ok, "last_error": self.last_error, "compat": self.compat,
                "handled": self.handled[-10:]}


def router_key(cfg: dict) -> str:
    """The relay's key: client.router_key names the keyring entry (default
    FOUNDRY_CLIENT_KEY, operator scope); the coordinator's orchestration key is
    the fallback, so an existing workstation needs no new secret."""
    name = str((cfg.get("client") or {}).get("router_key") or "FOUNDRY_CLIENT_KEY")
    return secrets.get(name) or secrets.get("FOUNDRY_COORDINATOR_KEY") or ""


def start_if_enabled(mgr, cfg: dict, stop: threading.Event, log: Callable[[str], None] = print
                     ) -> Optional[Relay]:
    """Called by `foundry-client run`: a relay thread when client.relay_enabled
    (default true) and a key is set; None (with a log line) otherwise."""
    if cfg["client"].get("relay_enabled", True) is False:
        return None
    key = router_key(cfg)
    if not key:
        log("relay: no router key (set FOUNDRY_CLIENT_KEY in Secrets); the router cannot see "
            "or restart this stack")
        return None
    relay = Relay(mgr, cfg, key, log=log)
    relay.start_thread(stop)
    return relay

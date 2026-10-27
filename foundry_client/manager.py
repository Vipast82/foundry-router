"""Runs and watches the components: start in dependency order, health-check, restart
with backoff when one dies or hangs, safe-point restarts for the Kilo server and
coordinator, rotating logs. One thread; the API and tray only call its methods."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import __version__, secrets
from . import components as _components
from .components import (Spec, build, kilo_config_path, project_ids, safe_point, self_argv)
from .config import COMPONENTS, expand, validate
from .link import RouterLink
from .probe import pid_alive
from .update import UpdateCheck

BACKOFF = (2, 5, 15, 30, 60, 120)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0      # CREATE_NO_WINDOW
START_GRACE = 90          # seconds a new process may take to become healthy
STOP_WAIT = 10            # seconds to wait for a killed process to go away


class Proc:
    def __init__(self, spec: Spec):
        self.spec = spec
        self.p: Optional[subprocess.Popen] = None
        self.state = "stopped"        # stopped | starting | running | unhealthy | waiting | error
        self.detail = ""
        self.started_ts = 0.0
        self.restarts = 0
        self.fails = 0
        self.next_try = 0.0
        self.want = False             # should be running
        self.pending_restart = ""     # "" | "safe" | "now"
        self.pending_since = 0.0
        self.pending_reason = ""      # why a safe-point restart is still waiting
        self.restart_result = ""      # outcome of the last restart: restarted | failed: ... | cancelled
        self.adopted = False          # running, but started outside the client
        self.unhealthy_since = 0.0
        self.last_exit = ""           # how the last process we launched ended
        self.info: dict = {}          # what the health check learned (Rojo: sessionId ...)
        self.session_id = ""          # Rojo: last seen session id (change = plugin must reconnect)


def _norm(res) -> tuple[bool, str, dict]:
    ok, why, *rest = res
    return bool(ok), str(why), (rest[0] if rest else {}) or {}


class Manager:
    def __init__(self, cfg: dict, log_dir: Optional[str] = None,
                 popen: Callable = subprocess.Popen, clock: Callable[[], float] = time.time,
                 run: Callable = subprocess.run):
        self.cfg = cfg
        self.popen = popen
        self.run = run                 # one-shot commands (kilo.jsonc generator)
        self.clock = clock
        self.lock = threading.RLock()
        self.log_dir = Path(log_dir or (Path(expand(cfg["client"]["state_dir"])) / "client-logs"))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, Proc] = {n: Proc(s) for n, s in build(cfg).items()}
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.net_thread: Optional[threading.Thread] = None
        self.events: list[dict] = []
        self.alerts: dict[str, dict] = {}       # things the operator must act on or know
        self.link = RouterLink(lambda: self.cfg["client"]["backend_url"], clock)
        self.update = UpdateCheck(lambda: self.cfg["client"].get("update_repo") or
                                  "Vipast82/foundry-router", clock)
        from .client_requests import RequestQueue
        self.requests = RequestQueue(self)
        from .studio import StudioReopen
        self.studio = StudioReopen(lambda: self.cfg.get("studio") or {},
                                   cfg["client"]["state_dir"],
                                   alert=lambda key, text: self._alert(key, text))

    # ---------------------------------------------------------------- lifecycle

    def reload(self, cfg: dict) -> None:
        """New settings: rebuild specs; running components keep running until restarted."""
        with self.lock:
            self.cfg = cfg
            for n, s in build(cfg).items():
                if n in self.procs:
                    self.procs[n].spec = s
                else:
                    self.procs[n] = Proc(s)

    def start_all(self) -> None:
        with self.lock:
            for n in self.order():
                if self.cfg["components"][n]["enabled"]:
                    self.procs[n].want = True

    def order(self) -> list[str]:
        done, out = set(), []

        def visit(n, seen=()):
            if n in done or n in seen:
                return
            for d in self.procs[n].spec.after:
                if d in self.procs:
                    visit(d, (*seen, n))
            done.add(n)
            out.append(n)
        for n in COMPONENTS:
            if n in self.procs:
                visit(n)
        return out

    def run_forever(self, interval: float = 2.0) -> None:
        while not self.stop_evt.is_set():
            try:
                self.step()
                self.requests.poll()
            except Exception as e:                                # noqa: BLE001
                self._event("client", f"internal error: {type(e).__name__}: {e}")
            self.stop_evt.wait(interval)

    def net_loop(self) -> None:
        """Router link every 30 s; GitHub release check at start and every 6 h. Own thread:
        both can block for several seconds."""
        next_update = self.clock() + 20
        while not self.stop_evt.is_set():
            try:
                self.link.poll()
                if self.cfg["client"].get("update_check", True) and self.clock() >= next_update:
                    self.update.poll()
                    next_update = self.clock() + 6 * 3600
            except Exception as e:                                # noqa: BLE001
                self._event("client", f"background check failed: {type(e).__name__}: {e}")
            self.stop_evt.wait(30)

    def start_thread(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.run_forever, name="foundry-client-manager",
                                           daemon=True)
            self.thread.start()
            self.net_thread = threading.Thread(target=self.net_loop, name="foundry-client-net",
                                               daemon=True)
            self.net_thread.start()

    def shutdown(self, stop_components: bool = True) -> None:
        self.stop_evt.set()
        if stop_components:
            for n in reversed(self.order()):
                self._kill(self.procs[n], "client exiting", adopted=False)

    # ---------------------------------------------------------------- control

    def start(self, name: str) -> dict:
        with self.lock:
            pr = self.procs[name]
            pr.want, pr.fails, pr.next_try = True, 0, 0.0
        self.step()
        return self.status(name)

    def stop(self, name: str) -> dict:
        with self.lock:
            pr = self.procs[name]
            pr.want = False
            pr.pending_restart = ""
            self._clear_alert(f"restart_wait:{name}")
            if not self._kill(pr, "stopped by the operator", adopted=True):
                pr.state, pr.detail = "error", pr.restart_result or "could not stop it"
            pr.fails, pr.next_try = 0, 0.0
        return self.status(name)

    def restart(self, name: str, force: bool = False) -> dict:
        """Ask for a restart; the safe-point kind waits (see `result` in the answer)."""
        with self.lock:
            pr = self.procs[name]
            pr.want = True
            pr.restart_result = ""
            pr.pending_restart = "now" if force or not pr.spec.safe_restart else "safe"
            pr.pending_since, pr.pending_reason = self.clock(), ""
        self.step()
        return self.with_result(name)

    def cancel_restart(self, name: str) -> dict:
        with self.lock:
            pr = self.procs[name]
            if pr.pending_restart:
                pr.pending_restart, pr.pending_reason = "", ""
                pr.restart_result = "cancelled"
                self._clear_alert(f"restart_wait:{name}")
                self._event(name, "pending restart cancelled")
        return self.with_result(name)

    def with_result(self, name: str) -> dict:
        st = self.status(name)
        pr = self.procs[name]
        st["result"] = (f"waiting for a safe point: {pr.pending_reason}" if pr.pending_restart
                        else pr.restart_result)
        return st

    # ---------------------------------------------------------------- the loop

    def _gather(self) -> tuple[dict, Optional[tuple]]:
        """Health probes and the safe-point read, taken WITHOUT the lock: they do network and
        SQLite I/O that must not stall the status API and the tray."""
        probes: dict = {}
        for n, pr in list(self.procs.items()):
            if (pr.want or pr.pending_restart) and pr.spec.health:
                probes[n] = self._probe(pr)
        safe = None
        if any(pr.pending_restart == "safe" for pr in self.procs.values()):
            safe = safe_point(self.cfg["client"]["state_dir"], (self.cfg.get("workspace") or {}).get("game") or "")
        return probes, safe

    @staticmethod
    def _probe(pr: Proc) -> tuple[bool, str, dict]:
        try:
            return _norm(pr.spec.health())
        except Exception as e:                                    # noqa: BLE001
            return False, f"health check failed: {type(e).__name__}: {e}", {}

    def step(self) -> None:
        probes, safe = self._gather()
        with self.lock:
            now = self.clock()
            for n in self.order():
                self._step_one(n, self.procs[n], now, probes, safe)

    def _step_one(self, n: str, pr: Proc, now: float, probes: dict, safe) -> None:
        cl = self.cfg["client"]
        if pr.p is not None and pr.p.poll() is not None:        # a process we launched ended
            code = pr.p.poll()
            pr.p = None
            pr.last_exit = f"exited with code {code}"
            if pr.want and not pr.pending_restart:
                pr.fails += 1
                pr.next_try = now + BACKOFF[min(pr.fails - 1, len(BACKOFF) - 1)]
                self._event(n, f"exited with code {code}; retry in {int(pr.next_try - now)} s")
                pr.state, pr.detail = "error", pr.last_exit
        running = pr.p is not None or pr.adopted

        if pr.pending_restart:
            blocked = False
            if pr.pending_restart == "safe" and running:
                if safe is None:                         # the request arrived after the gather
                    safe = safe_point(cl["state_dir"], (self.cfg.get("workspace") or {}).get("game") or "")
                ok, why = safe
                if not ok:
                    blocked, pr.pending_reason = True, why
                    limit = int(cl.get("safe_restart_max_wait_minutes") or 0) * 60
                    key = f"restart_wait:{n}"
                    if limit and now - pr.pending_since > limit and key not in self.alerts:
                        self._alert(key, f"The {pr.spec.label} restart has waited "
                                         f"{int((now - pr.pending_since) // 60)} min for a safe "
                                         f"point ({why}). Restart now, or cancel it.")
                        self._event(n, f"restart still waiting for a safe point: {why}")
            if not blocked:
                if self._kill(pr, "restarting", adopted=True):
                    pr.restarts += 1
                    pr.restart_result = "restarted"
                else:
                    pr.restart_result = "failed: " + (pr.restart_result or "could not stop it")
                    pr.state, pr.detail = "error", pr.restart_result
                pr.pending_restart, pr.pending_reason = "", ""
                self._clear_alert(f"restart_wait:{n}")
                pr.next_try, pr.unhealthy_since = 0.0, 0.0
                running = pr.p is not None or pr.adopted
                if pr.restart_result.startswith("failed"):
                    return

        if not pr.want:
            if not running:
                pr.state = "stopped"
                pr.detail = "disabled" if not self.cfg["components"][n]["enabled"] else "stopped"
            return

        if not running:
            if now < pr.next_try:
                return
            if pr.spec.health:                       # a fresh read: the old probe predates any kill
                ok, why, info = self._probe(pr)
                if ok:                               # started outside the client: adopt, don't duplicate
                    pr.adopted, pr.info = True, info
                    pr.state, pr.detail = "running", f"{why} (started outside the client)"
                    self._seen_info(n, pr, info)
                    return
                if pr.spec.port and _components.port_open(pr.spec.port):
                    # something answers on the port but is not healthy (a wrong password, a
                    # wedged server, another program): a second copy could not bind it
                    pr.state, pr.detail = "error", f"{why}; the port is held by another process"
                    pr.next_try = now + 30
                    return
            if any(self.procs[d].want and self.procs[d].state not in ("running",)
                   for d in pr.spec.after if d in self.procs):
                pr.state, pr.detail = "waiting", "waiting for " + ", ".join(pr.spec.after)
                return
            self._launch(pr)
            return

        if pr.spec.health:
            ok, why, info = probes.get(n) or self._probe(pr)
            if ok:
                pr.state = "running"
                pr.detail = why + (" (started outside the client)" if pr.adopted else "")
                pr.unhealthy_since, pr.info = 0.0, info
                self._seen_info(n, pr, info)
                if pr.p is not None and now - pr.started_ts > 120:
                    pr.fails = 0
                return
            if pr.adopted and pr.spec.owner_pid and pr.spec.owner_pid() is None:
                pr.adopted = False                   # it went away on its own: normal start path
                pr.state, pr.detail = "stopped", "the external process ended"
                return
            grace = pr.p is not None and now - pr.started_ts < START_GRACE
            pr.state, pr.detail = ("starting" if grace else "unhealthy"), why
            if grace:
                return
            if not pr.unhealthy_since:
                pr.unhealthy_since = now
            limit = int(cl.get("unhealthy_restart_seconds") or 0)
            if limit and now - pr.unhealthy_since > limit and not pr.pending_restart:
                pr.pending_restart = "safe" if pr.spec.safe_restart else "now"
                pr.pending_since, pr.pending_reason = now, ""
                self._event(n, f"unhealthy for {int(now - pr.unhealthy_since)} s ({why}); "
                               f"restarting ({pr.pending_restart})")
        elif pr.p is not None:
            pr.state, pr.detail = "running", f"pid {pr.p.pid}"

    def _seen_info(self, n: str, pr: Proc, info: dict) -> None:
        """Rojo gets a new session id every time it starts; Studio's plugin does not follow."""
        sid = str((info or {}).get("sessionId") or "")
        if not sid:
            return
        if pr.session_id and sid != pr.session_id:
            self._alert("rojo_reconnect", "Rojo restarted (new session). Reconnect the Rojo "
                                          "plugin in Studio: open the Rojo panel and click "
                                          "Connect.")
            self._event(n, "new Rojo session: the Studio plugin must reconnect")
        pr.session_id = sid

    def _launch(self, pr: Proc) -> None:
        s = pr.spec
        if s.problem:
            pr.state, pr.detail = "error", s.problem
            pr.next_try = self.clock() + 30
            return
        missing = [k for k in s.secrets if not secrets.get(k)]
        if missing:
            pr.state, pr.detail = "error", "missing secret(s): " + ", ".join(missing) + \
                " (set them in Settings)"
            pr.next_try = self.clock() + 30
            return
        env = {**os.environ, **s.env, **secrets.env_for(s.secrets)}
        logf = open(self.log_dir / f"{s.name}.log", "ab", buffering=0)
        logf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start: {self._show(s.argv)}\n"
                   .encode())
        try:
            pr.p = self.popen(s.argv, cwd=s.cwd or None, env=env, stdout=logf,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              creationflags=NO_WINDOW)
        except OSError as e:
            pr.state, pr.detail = "error", f"cannot start: {e}"
            pr.fails += 1
            pr.next_try = self.clock() + BACKOFF[min(pr.fails - 1, len(BACKOFF) - 1)]
            return
        finally:
            logf.close()
        pr.adopted = False
        pr.started_ts = self.clock()
        pr.state, pr.detail = "starting", f"pid {pr.p.pid}"
        self._event(s.name, "started")

    def _kill(self, pr: Proc, why: str, adopted: bool = True) -> bool:
        """Stop the component's process: the one we launched, or (adopted=True) one that was
        running before the client started, found through its port or heartbeat. True when
        nothing of it is left. On failure `restart_result` says why."""
        p = pr.p
        if p is not None and p.poll() is None:
            self._kill_pid(p.pid, p, pr.spec.name != "coordinator")
            pr.p = None
        elif pr.adopted and adopted:
            pr.p = None
            pid = pr.spec.owner_pid() if pr.spec.owner_pid else None
            if pid is not None:
                self._kill_pid(pid, None, pr.spec.name != "coordinator")
                deadline = time.monotonic() + STOP_WAIT
                while pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.25)
                if pid_alive(pid):
                    pr.restart_result = (f"could not stop the process that was started outside "
                                         f"the client (pid {pid})")
                    self._event(pr.spec.name, pr.restart_result)
                    return False
            elif pr.spec.owner_pid is None:
                pr.restart_result = ("it was started outside the client and the client cannot "
                                     "find its process; stop it by hand once")
                return False
        elif pr.adopted:
            return True                      # the client is exiting: leave external processes alone
        else:
            pr.p = None                      # nothing is running: only make the state truthful
            pr.state, pr.detail = "stopped", why
            return True
        pr.adopted = False
        pr.state, pr.detail = "stopped", why
        self._event(pr.spec.name, why)
        return True

    @staticmethod
    def _kill_pid(pid: int, p: Optional[subprocess.Popen], tree: bool = True) -> None:
        try:
            if sys.platform == "win32":
                # W1.9: the coordinator's process tree holds the DETACHED studio_gate
                # runner (and its MCP children) while a gate runs; `/T` on the coordinator
                # killed it mid-playtest. The coordinator is stopped alone; every other
                # component still goes with its tree.
                subprocess.run(["taskkill", "/PID", str(pid), *(["/T"] if tree else []), "/F"],
                               capture_output=True,
                               creationflags=NO_WINDOW, timeout=30)
            elif p is not None:
                p.terminate()
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
                for _ in range(60):
                    if not pid_alive(pid):
                        return
                    time.sleep(0.25)
                os.kill(pid, signal.SIGKILL)
        except Exception:                                        # noqa: BLE001
            pass

    @staticmethod
    def _show(argv: list) -> str:
        return " ".join(str(a) for a in argv)          # argv never holds secrets

    def _event(self, name: str, text: str) -> None:
        self.events.append({"ts": self.clock(), "component": name, "text": text})
        del self.events[:-200]

    def _alert(self, key: str, text: str, level: str = "warn") -> None:
        self.alerts[key] = {"key": key, "text": text, "level": level, "ts": self.clock()}

    def _clear_alert(self, key: str) -> None:
        self.alerts.pop(key, None)

    def dismiss_alert(self, key: str) -> bool:
        with self.lock:
            return self.alerts.pop(key, None) is not None

    # ---------------------------------------------------------------- kilo.jsonc

    def apply_kilo_config(self, project: str = "", dry_run: bool = False,
                          reload: bool = True) -> dict:
        """Regenerate the role-rule permission blocks of kilo.jsonc with the coordinator's
        generator (it backs the file up, writes, reads it back and restores on any mismatch),
        then reload Kilo at the next safe point. Never raises: {ok, error?, ...}."""
        from foundry_automation import jsonc
        cfg = self.cfg
        path = kilo_config_path(cfg)
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": f"kilo.jsonc not found ({path or 'no game folder set'}); "
                                          f"set workspace.kilo_config"}

        def valid() -> str:
            try:
                with open(path, encoding="utf-8") as f:
                    jsonc.parse(f.read())
                return ""
            except (OSError, ValueError) as e:
                return str(e)
        bad = valid()
        if bad:
            return {"ok": False, "error": f"kilo.jsonc is not valid JSONC, nothing changed: {bad}"}
        ids = project_ids(cfg["workspace"]["profiles"])
        project = project or cfg["workspace"].get("project") or (ids[0] if len(ids) == 1 else "")
        if not project:
            return {"ok": False, "error": "which project? " + (
                "set workspace.project (found: " + ", ".join(ids) + ")" if ids else
                "no project.toml under the profiles folder")}
        state = expand(cfg["client"]["state_dir"])
        argv = [*self_argv(), "coordinator", "--state", state, "--profiles",
                expand(cfg["workspace"]["profiles"]), "kilo-config", project, "--path", path]
        if not dry_run:
            argv.append("--apply")
        env = {**os.environ, **secrets.env_for(cfg["components"]["coordinator"].get("secrets") or [])}
        try:
            r = self.run(argv, capture_output=True, text=True, timeout=120, env=env,
                         cwd=expand(cfg["workspace"]["deploy"]) or None, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            return {"ok": False, "error": f"could not run the generator: {e}"}
        out = (r.stdout or "").strip()
        try:
            res = json.loads(out[out.index("{"):]) if "{" in out else {}
        except ValueError:
            res = {}
        if r.returncode != 0 or not isinstance(res, dict) or "changed" not in res:
            msg = ((r.stderr or "").strip() or out)[-500:] or f"exit code {r.returncode}"
            return {"ok": False, "error": msg}
        result = {"ok": True, "project": project, "path": path, "dry_run": dry_run,
                  "changed": res.get("changed", []), "unchanged": res.get("unchanged", []),
                  "missing": res.get("missing", []), "applied": bool(res.get("applied")),
                  "backup": res.get("backup", "")}
        if result["applied"]:
            bad = valid()
            if bad:                                  # the generator verifies too; belt and braces
                if result["backup"] and os.path.isfile(result["backup"]):
                    import shutil
                    shutil.copy2(result["backup"], path)
                return {**result, "ok": False, "applied": False,
                        "error": f"kilo.jsonc no longer parses ({bad}); the backup was restored"}
            self._event("kilo", f"kilo.jsonc updated for {', '.join(result['changed'])}")
            if reload and self.cfg["components"]["kilo"]["enabled"]:
                result["reload"] = self.restart("kilo")["result"] or "restarted"
        return result

    # ---------------------------------------------------------------- views

    def status(self, name: Optional[str] = None) -> dict:
        with self.lock:
            def one(n):
                pr = self.procs[n]
                owned = pr.p is not None and pr.p.poll() is None
                return {"name": n, "label": pr.spec.label, "enabled":
                        self.cfg["components"][n]["enabled"], "want": pr.want,
                        "state": pr.state, "detail": pr.detail,
                        "pid": pr.p.pid if owned else None, "adopted": pr.adopted,
                        "uptime_s": round(self.clock() - pr.started_ts) if owned else 0,
                        "restarts": pr.restarts,
                        "problem": pr.spec.problem, "pending_restart": pr.pending_restart,
                        "pending_reason": pr.pending_reason,
                        "pending_for_s": round(self.clock() - pr.pending_since)
                        if pr.pending_restart else 0,
                        "last_exit": pr.last_exit if pr.want else "",
                        "restart_result": pr.restart_result, "info": pr.info}
            if name:
                return one(name)
            comps = [one(n) for n in self.order()]
            bad = [c for c in comps if c["want"] and c["state"] in ("error", "unhealthy")]
            busy = [c for c in comps if c["want"] and (c["state"] in ("starting", "waiting")
                                                       or c["pending_restart"])]
            idle = not any(c["want"] for c in comps)
            return {"overall": "error" if bad else "busy" if busy else "idle" if idle else "ok",
                    "components": comps, "config_problems": validate(self.cfg),
                    "events": self.events[-30:], "alerts": list(self.alerts.values()),
                    "router": self.link.snapshot(), "update": self.update.snapshot()}

    def mcp_status(self, max_age: float = 30.0, probe: Optional[Callable] = None) -> dict:
        """{"mcp": [server probes], "services": [registry rows], "ts"}: the MCP servers Kilo can
        see and the service registry as this client sees it. Cached `max_age` seconds (the probes
        open sockets); never raises. `probe` replaces the real MCP probe in tests."""
        lock = self.__dict__.setdefault("_mcp_lock", threading.Lock())
        with lock:
            cur = self.__dict__.get("_mcp_cache")
            now = self.clock()
            if cur and now - cur["ts"] < max_age:
                return cur
            try:
                rows = (probe or _components.mcp_report)(self.cfg)
            except Exception as e:                                # noqa: BLE001
                rows = []
                self._event("client", f"MCP probe failed: {type(e).__name__}: {e}")
            comp = {c["name"]: {"state": c["state"], "enabled": c["enabled"]}
                    for c in self.status()["components"]}
            try:
                reg = _components.services_report(self.cfg, rows, comp)
            except Exception as e:                                # noqa: BLE001
                reg = []
                self._event("client", f"service report failed: {type(e).__name__}: {e}")
            cur = {"mcp": rows, "services": reg, "ts": now}
            self._mcp_cache = cur
            return cur

    def observation(self) -> dict:
        """What the relay sends the router: the status without anything local-only."""
        st = self.status()
        return {"client_version": __version__, "overall": st["overall"],
                "components": [{k: c[k] for k in ("name", "label", "state", "detail", "enabled",
                                                  "restarts", "pending_restart", "pending_reason",
                                                  "adopted")} for c in st["components"]],
                "alerts": st["alerts"], "router": st["router"], "update": st["update"],
                "config_problems": st["config_problems"], "events": st["events"][-10:]}

    def tail(self, name: str, lines: int = 200) -> str:
        p = self.log_dir / f"{name}.log"
        if not p.is_file():
            return ""
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64000))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])

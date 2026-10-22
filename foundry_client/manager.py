"""Runs and watches the components: start in dependency order, health-check, restart
with backoff when one dies, safe-point restarts for the Kilo server and coordinator,
rotating logs. One thread; the API and tray only call its methods."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import secrets
from .components import Spec, build, safe_point
from .config import COMPONENTS, expand, validate

BACKOFF = (2, 5, 15, 30, 60, 120)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0      # CREATE_NO_WINDOW


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


class Manager:
    def __init__(self, cfg: dict, log_dir: Optional[str] = None,
                 popen: Callable = subprocess.Popen, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.popen = popen
        self.clock = clock
        self.lock = threading.RLock()
        self.log_dir = Path(log_dir or (Path(expand(cfg["client"]["state_dir"])) / "client-logs"))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, Proc] = {n: Proc(s) for n, s in build(cfg).items()}
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.events: list[dict] = []

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
            self.step()
            self.stop_evt.wait(interval)

    def start_thread(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self.run_forever, name="foundry-client-manager",
                                           daemon=True)
            self.thread.start()

    def shutdown(self, stop_components: bool = True) -> None:
        self.stop_evt.set()
        if stop_components:
            for n in reversed(self.order()):
                self._kill(self.procs[n], "client exiting")

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
            self._kill(pr, "stopped by the operator")
        return self.status(name)

    def restart(self, name: str, force: bool = False) -> dict:
        with self.lock:
            pr = self.procs[name]
            pr.want = True
            pr.pending_restart = "now" if force or not pr.spec.safe_restart else "safe"
        self.step()
        return self.status(name)

    # ---------------------------------------------------------------- the loop

    def step(self) -> None:
        with self.lock:
            now = self.clock()
            for n in self.order():
                pr = self.procs[n]
                alive = pr.p is not None and pr.p.poll() is None
                if pr.pending_restart:
                    if pr.pending_restart == "safe" and alive:
                        ok, why = safe_point(self.cfg["client"]["state_dir"])
                        if not ok:
                            pr.state, pr.detail = "waiting", f"restart waits for a safe point: {why}"
                            continue
                    self._kill(pr, "restarting")
                    pr.pending_restart = ""
                    pr.restarts += 1
                    alive = False
                    pr.next_try = 0.0
                if not pr.want:
                    if not alive and pr.state not in ("stopped",):
                        pr.state = "stopped"
                    continue
                if not alive:
                    if pr.p is not None:
                        code = pr.p.poll()
                        pr.p = None
                        pr.fails += 1
                        pr.next_try = now + BACKOFF[min(pr.fails - 1, len(BACKOFF) - 1)]
                        self._event(n, f"exited with code {code}; retry in "
                                       f"{int(pr.next_try - now)} s")
                        pr.state, pr.detail = "error", f"exited with code {code}"
                    if now < pr.next_try:
                        continue
                    if pr.spec.health and pr.p is None:
                        ok, why = pr.spec.health()
                        if ok:            # started outside the client: adopt, don't duplicate
                            pr.state, pr.detail = "running", f"{why} (started outside the client)"
                            continue
                    if any(self.procs[d].want and self.procs[d].state not in ("running",)
                           for d in pr.spec.after if d in self.procs):
                        pr.state, pr.detail = "waiting", "waiting for " + ", ".join(pr.spec.after)
                        continue
                    self._launch(pr)
                    continue
                if pr.spec.health:
                    ok, why = pr.spec.health()
                    if ok:
                        pr.state, pr.detail = "running", why
                        if now - pr.started_ts > 120:
                            pr.fails = 0
                    else:
                        grace = now - pr.started_ts < 90
                        pr.state, pr.detail = ("starting" if grace else "unhealthy"), why
                else:
                    pr.state, pr.detail = "running", f"pid {pr.p.pid}"

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
        pr.started_ts = self.clock()
        pr.state, pr.detail = "starting", f"pid {pr.p.pid}"
        self._event(s.name, "started")

    def _kill(self, pr: Proc, why: str) -> None:
        p = pr.p
        if p is None or p.poll() is not None:
            pr.p = None
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                               creationflags=NO_WINDOW, timeout=30)
            else:
                p.terminate()
                try:
                    p.wait(15)
                except subprocess.TimeoutExpired:
                    p.kill()
        except Exception:                                        # noqa: BLE001
            pass
        pr.p = None
        pr.state, pr.detail = "stopped", why
        self._event(pr.spec.name, why)

    @staticmethod
    def _show(argv: list) -> str:
        return " ".join(str(a) for a in argv)          # argv never holds secrets

    def _event(self, name: str, text: str) -> None:
        self.events.append({"ts": self.clock(), "component": name, "text": text})
        del self.events[:-200]

    # ---------------------------------------------------------------- views

    def status(self, name: Optional[str] = None) -> dict:
        with self.lock:
            def one(n):
                pr = self.procs[n]
                return {"name": n, "label": pr.spec.label, "enabled":
                        self.cfg["components"][n]["enabled"], "want": pr.want,
                        "state": pr.state, "detail": pr.detail,
                        "pid": pr.p.pid if pr.p and pr.p.poll() is None else None,
                        "uptime_s": round(self.clock() - pr.started_ts) if pr.p and
                        pr.p.poll() is None else 0, "restarts": pr.restarts,
                        "problem": pr.spec.problem, "pending_restart": pr.pending_restart}
            if name:
                return one(name)
            comps = [one(n) for n in self.order()]
            bad = [c for c in comps if c["want"] and c["state"] in ("error", "unhealthy")]
            busy = [c for c in comps if c["want"] and c["state"] in ("starting", "waiting")]
            idle = not any(c["want"] for c in comps)
            return {"overall": "error" if bad else "busy" if busy else "idle" if idle else "ok",
                    "components": comps, "config_problems": validate(self.cfg),
                    "events": self.events[-30:]}

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

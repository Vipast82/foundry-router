"""Resource watch (2026-10-29, Victor): a stuck automatic training run held 32 GB for 30 hours
and nobody noticed until the machine ran out of RAM.

Every `interval_seconds` the client looks at memory and at the development / testing jobs the
stack starts: training runs, scripted Studio checks, gate.ps1, Lune, headless Blender builds,
`rojo build`, pytest and the game repo's tools/ scripts. Such a job that grows past its memory
limit (on two checks in a row) or runs past its time limit is stopped with its whole process
tree, when `auto_stop` is on; the work it was doing stays queued, and every stop is an event and
an alert. Anything else (Studio, Blender's window, the components, your apps) is never touched:
when free RAM is low the alert names the biggest users, that is all."""

from __future__ import annotations

import re
import time
from typing import Callable, Optional

DEFAULTS = {
    "enabled": True,
    "interval_seconds": 60,
    "low_ram_gb": 2,              # alert when free RAM is below this (0 = never)
    "auto_stop": True,            # stop a runaway development / testing job
    "limits": {                   # per kind: memory (GB, the job's whole process tree) + minutes
        "training": {"gb": 6, "minutes": 120},
        "studio_check": {"gb": 3, "minutes": 240},
        "gate_ps1": {"gb": 2, "minutes": 90},
        "lune": {"gb": 2, "minutes": 60},
        "blender_build": {"gb": 8, "minutes": 120},
        "rojo_build": {"gb": 2, "minutes": 20},
        "pytest": {"gb": 4, "minutes": 60},
        "repo_script": {"gb": 2, "minutes": 120},
    },
}

KIND_LABEL = {
    "training": "training-data run", "studio_check": "scripted Studio check",
    "gate_ps1": "gate.ps1 run", "lune": "Lune test run", "blender_build": "headless Blender build",
    "rojo_build": "rojo build", "pytest": "pytest run", "repo_script": "game repo tools/ script",
}

GB = 1024 ** 3


def _cmd(p) -> tuple[str, str]:
    try:
        name = (p.name() or "").lower()
    except Exception:                                             # noqa: BLE001
        name = ""
    try:
        cmd = " ".join(p.cmdline() or []).replace("\\", "/").lower()
    except Exception:                                             # noqa: BLE001
        cmd = ""
    return name, cmd


def classify(name: str, cmd: str, game: str = "") -> Optional[str]:
    """The development / testing job kind of a process, or None (never touched)."""
    game = (game or "").replace("\\", "/").lower().rstrip("/")
    if "-m foundry_training" in cmd:
        return "training"
    if "foundry_coordinator.studio_calls" in cmd or "animaluprising.studio_gate" in cmd:
        return "studio_check"
    if re.search(r"tools/gate\.ps1\b", cmd):
        return "gate_ps1"
    if name.startswith("lune") or re.search(r"(^|[ /])lune(\.exe)? run\b", cmd):
        return "lune"
    if name.startswith("blender") and re.search(r"(^| )(-b|--background)( |$)", cmd):
        return "blender_build"
    if name.startswith("rojo") and re.search(r" build( |$)", cmd):
        return "rojo_build"
    if re.search(r"-m pytest\b|[ /]pytest(\.exe)?( |$)", cmd) or name.startswith("pytest"):
        return "pytest"
    if name.startswith("python") and re.search(r"(^| )\"?tools/[\w./-]+\.py\b", cmd):
        return "repo_script"
    if game and name.startswith("python") and f"{game}/tools/" in cmd:
        return "repo_script"
    return None


def _private(p) -> int:
    try:
        mi = p.memory_info()
    except Exception:                                             # noqa: BLE001
        return 0
    return int(getattr(mi, "private", 0) or getattr(mi, "rss", 0) or 0)


class ResourceWatch:
    def __init__(self, settings: Callable[[], dict], alert: Callable, clear_alert: Callable,
                 event: Callable, game: Callable[[], str] = lambda: "",
                 protected: Callable[[], set] = lambda: set(),
                 clock: Callable[[], float] = time.time, ps=None):
        self.settings, self.alert, self.clear_alert, self.event = settings, alert, clear_alert, event
        self.game, self.protected, self.clock = game, protected, clock
        if ps is None:
            try:
                import psutil as ps                              # noqa: PLW0127
            except ImportError:                                   # no psutil: the watch is off
                ps = None
        self.ps = ps
        self.next_at = 0.0
        self.over: dict[int, int] = {}        # pid -> consecutive checks over its memory limit
        self.stops: list[dict] = []
        self.last: dict = {}

    def cfg(self) -> dict:
        s = {**DEFAULTS, **(self.settings() or {})}
        s["limits"] = {k: {**DEFAULTS["limits"].get(k, {}), **(v or {})}
                       for k, v in {**DEFAULTS["limits"], **(s.get("limits") or {})}.items()}
        return s

    def maybe_tick(self) -> None:
        s = self.cfg()
        if not s.get("enabled") or self.ps is None:
            return
        now = self.clock()
        if now < self.next_at:
            return
        self.next_at = now + max(15, int(s.get("interval_seconds") or 60))
        self.tick(s)

    # ------------------------------------------------------------------ one check
    def jobs(self) -> list[dict]:
        """Top-level development / testing jobs: a matching process whose parent is not a
        job of the same kind (a venv launcher and its python count once), with the memory of
        its whole process tree."""
        ps, game, keep = self.ps, self.game(), set(self.protected() or ())
        kinds: dict[int, tuple] = {}
        for p in ps.process_iter():
            try:
                name, cmd = _cmd(p)
                k = classify(name, cmd, game)
                if k and p.pid not in keep:
                    kinds[p.pid] = (k, p)
            except Exception:                                     # noqa: BLE001
                continue
        out = []
        for pid, (k, p) in kinds.items():
            try:
                par = p.ppid()
            except Exception:                                     # noqa: BLE001
                par = 0
            if par in kinds and kinds[par][0] == k:
                continue
            try:
                tree = [p] + p.children(recursive=True)
                mem = sum(_private(x) for x in tree)
                started = float(p.create_time())
            except Exception:                                     # noqa: BLE001
                continue
            out.append({"pid": pid, "kind": k, "gb": round(mem / GB, 2),
                        "minutes": round((self.clock() - started) / 60, 1),
                        "cmd": _cmd(p)[1][:160]})
        return sorted(out, key=lambda j: -j["gb"])

    def top(self, n: int = 5) -> list[dict]:
        rows = []
        for p in self.ps.process_iter():
            try:
                rows.append({"pid": p.pid, "name": p.name(), "gb": round(_private(p) / GB, 2)})
            except Exception:                                     # noqa: BLE001
                continue
        return sorted(rows, key=lambda r: -r["gb"])[:n]

    def tick(self, s: Optional[dict] = None) -> dict:
        s = s or self.cfg()
        vm = self.ps.virtual_memory()
        free, total = vm.available / GB, vm.total / GB
        jobs = self.jobs()
        live = {j["pid"] for j in jobs}
        self.over = {pid: n for pid, n in self.over.items() if pid in live}
        for j in jobs:
            lim = s["limits"].get(j["kind"]) or {}
            j["limit_gb"], j["limit_minutes"] = lim.get("gb") or 0, lim.get("minutes") or 0
            why = ""
            if j["limit_gb"] and j["gb"] > j["limit_gb"]:
                self.over[j["pid"]] = self.over.get(j["pid"], 0) + 1
                if self.over[j["pid"]] >= 2:
                    why = f"{j['gb']:.1f} GB, over its {j['limit_gb']} GB limit"
            else:
                self.over.pop(j["pid"], None)
            if not why and j["limit_minutes"] and j["minutes"] > j["limit_minutes"]:
                why = f"running {j['minutes']:.0f} min, over its {j['limit_minutes']} min limit"
            if why:
                self._runaway(j, why, bool(s.get("auto_stop")))
        low = float(s.get("low_ram_gb") or 0)
        top = self.top() if low else []
        if low and free < low:
            names = ", ".join(f"{t['name']} {t['gb']:.1f} GB" for t in top)
            self.alert("resources:low_ram", f"RAM low: {free:.1f} GB free of {total:.0f} GB. "
                                            f"Biggest users: {names}.")
        elif low and free >= low + 0.5:
            self.clear_alert("resources:low_ram")
        self.last = {"ts": self.clock(), "free_gb": round(free, 1), "total_gb": round(total, 1),
                     "jobs": jobs, "top": top, "stops": self.stops[-10:]}
        return self.last

    def _runaway(self, j: dict, why: str, stop: bool) -> None:
        label = KIND_LABEL.get(j["kind"], j["kind"])
        key = f"resources:runaway:{j['pid']}"
        if not stop:
            self.alert(key, f"Runaway {label} (pid {j['pid']}): {why}. Auto-stop is off.")
            return
        ok = self._kill_tree(j["pid"])
        rec = {"ts": self.clock(), "pid": j["pid"], "kind": j["kind"], "why": why, "stopped": ok}
        self.stops.append(rec)
        del self.stops[:-50]
        self.over.pop(j["pid"], None)
        self.event("resources", f"stopped runaway {label} pid {j['pid']}: {why}" if ok else
                   f"could not stop runaway {label} pid {j['pid']}: {why}")
        self.alert(key, (f"Stopped a runaway {label} (pid {j['pid']}): {why}. Its work stays "
                         f"queued and the next run resumes it.") if ok else
                   f"Could not stop a runaway {label} (pid {j['pid']}): {why}.",
                   "info" if ok else "warn")

    def _kill_tree(self, pid: int) -> bool:
        try:
            p = self.ps.Process(pid)
            tree = p.children(recursive=True) + [p]
        except Exception:                                         # noqa: BLE001
            return True                                           # already gone
        for x in tree:
            try:
                x.terminate()
            except Exception:                                     # noqa: BLE001
                pass
        try:
            _, alive = self.ps.wait_procs(tree, timeout=5)
        except Exception:                                         # noqa: BLE001
            alive = tree
        for x in alive:
            try:
                x.kill()
            except Exception:                                     # noqa: BLE001
                pass
        try:
            return not self.ps.pid_exists(pid)
        except Exception:                                         # noqa: BLE001
            return True


def validate(s: dict) -> list[str]:
    errs = []
    for k in ("interval_seconds", "low_ram_gb"):
        v = s.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            errs.append(f"resources.{k} must be a number, 0 or more")
    for k in ("enabled", "auto_stop"):
        if not isinstance(s.get(k), bool):
            errs.append(f"resources.{k} must be true or false")
    for kind, lim in (s.get("limits") or {}).items():
        for f in ("gb", "minutes"):
            v = (lim or {}).get(f)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
                errs.append(f"resources.limits.{kind}.{f} must be a number, 0 or more")
    return errs

"""Live test counts for the control center (2026-10-27).

Victor: "When it does the sessions for Lua and gate running for the testing, is there a
way to have a status bar, with 8 out of 30 tests completed, pass / fail values, live
tracker?"

The counts come from what the runs already write, line by line:
  - `PASS: ...` / `FAIL: ...`    one test (the Lune runner, the gate scripts, Studio checks);
                                 a ` (cached)` suffix = reused from AU's per-spec cache
  - `LUNE n/m PASS|FAIL`         the Lune suite finished: n of m passed
  - `NEGATIVE CONTROL PASS|FAIL` the negative control finished
  - `-> N CALLs` / `CALL n `     a Studio gate plan and the CALL being run
  - `GATE <id> PASS|FAIL`        a gate finished
  - `EXIT=<code>`                a command run through a runner (AU's tools/guard/run.py) ended

Sources: files a project adapter names (`live_test_sources`), read incrementally (only the
bytes added since the last read), plus the coordinator's own background checks
(check_progress). Display only: nothing decides on these numbers.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import time
from typing import Optional

_PASS = re.compile(r"^\s*PASS:")
_FAIL = re.compile(r"^\s*FAIL:")
_LUNE = re.compile(r"^LUNE (\d+)/(\d+) (PASS|FAIL)")
_NEG = re.compile(r"^NEGATIVE CONTROL (PASS|FAIL)")
_PLAN = re.compile(r"->\s*(\d{1,5}) CALLs\b")
_CALL = re.compile(r"^CALL (\d{1,5})\b")
_CACHED = re.compile(r"\(cached\)\s*$")
_GATE = re.compile(r"^GATE \S+ (PASS|FAIL)\b")
_EXIT = re.compile(r"^EXIT=(-?\d+)")
_CMD = re.compile(r"^CMD: (.*)")

ACTIVE_S = 180          # a file written this recently is a run in progress
RECENT_S = 900          # a finished run stays on the page this long
STUDIO_ACTIVE_S = 1200  # Studio CALLs are slow: a lines file stays "running" this long
MAX_FILES = 4           # runs shown per task
MAX_READ = 8 << 20      # never read more than this from one file in one pass


class Counts:
    """Running totals over a stream of lines."""

    __slots__ = ("passed", "failed", "call", "calls", "total", "result", "exit", "cmd",
                 "expected", "cached", "_seg", "_tail")

    def __init__(self):
        self.passed = self.failed = self.call = self.expected = self.cached = 0
        self._seg = [0, 0]                        # PASS/FAIL lines since the last summary line
        self.calls = self.total = None
        self.result = self.exit = self.cmd = None
        self._tail = ""

    def feed(self, text: str) -> None:
        text = self._tail + text
        lines = text.split("\n")
        self._tail = lines.pop()                  # a partial last line waits for its end
        for ln in lines:
            self.line(ln.rstrip("\r"))

    def line(self, ln: str) -> None:
        if _CACHED.search(ln) and (_PASS.match(ln) or _FAIL.match(ln)):
            self.cached += 1                     # AU's per-spec cache: reused, not run
        if _PASS.match(ln):
            self.passed += 1
            self._seg[0] += 1
        elif _FAIL.match(ln):
            self.failed += 1
            self._seg[1] += 1
        elif (m := _CALL.match(ln)):
            self.call = max(self.call, int(m.group(1)))
        elif (m := _LUNE.match(ln)):
            self.total, self.result = int(m.group(2)), m.group(3)
            self._seg = [0, 0]
        elif (m := _NEG.match(ln)):
            # a negative control's FAIL lines are the expected outcome: when it
            # passed they are not failures (a gate log runs it after the suite)
            if m.group(1) == "PASS":
                self.failed -= self._seg[1]
                self.expected += self._seg[1]
            self.result = m.group(1)
            self._seg = [0, 0]
        elif (m := _GATE.match(ln)):
            self.result = m.group(1)
        elif (m := _EXIT.match(ln)):
            self.exit = int(m.group(1))
        elif self.cmd is None and (m := _CMD.match(ln)):
            self.cmd = m.group(1)[:300]
        if self.calls is None and (m := _PLAN.search(ln)):
            self.calls = int(m.group(1))

    @property
    def finished(self) -> bool:
        return self.exit is not None or (self.result is not None and self.calls is None)

    def row(self) -> dict:
        """{done, total, pass, fail, result}: done/total are CALLs for a Studio plan,
        tests otherwise (total only when the run reported it)."""
        if self.calls:
            done, total = self.call, self.calls
        else:
            done, total = self.passed + self.failed + self.expected, self.total
        if total is not None and done > total:
            total = None                           # a different count: don't draw a bar
        res = self.result or (("PASS" if self.exit == 0 else "FAIL") if self.exit is not None
                              else None)
        out = {"done": done, "total": total, "pass": self.passed, "fail": self.failed,
               "result": res}
        if self.cached:
            out["cached"] = self.cached          # reused from AU's per-spec cache
        return out


_lock = threading.Lock()
_files: dict[str, dict] = {}                     # path -> {"size", "mtime", "counts"}


def read(path: str) -> Optional[Counts]:
    """The counts of one file, reading only what was added since the last call."""
    try:
        st = os.stat(path)
    except OSError:
        with _lock:
            _files.pop(path, None)
        return None
    with _lock:
        ent = _files.get(path)
        if ent is None or st.st_size < ent["size"]:
            ent = {"size": 0, "mtime": 0.0, "counts": Counts()}  # new or rewritten
            _files[path] = ent
        if st.st_size == ent["size"]:
            ent["mtime"] = st.st_mtime
            return ent["counts"]
        try:
            with open(path, "rb") as f:
                f.seek(ent["size"])
                data = f.read(min(st.st_size - ent["size"], MAX_READ))
        except OSError:
            return ent["counts"]
        ent["counts"].feed(data.decode("utf-8", errors="replace"))
        ent["size"] += len(data)
        ent["mtime"] = st.st_mtime
        return ent["counts"]


def _label(path: str, c: Counts, kind: str) -> str:
    if kind == "studio":
        return "Studio gate CALLs"
    cmd = (c.cmd or "").lower()
    name = os.path.basename(path)
    m = re.match(r"^\d{8}-\d{6}_(.+)\.log$", name)
    nm = m.group(1) if m else os.path.splitext(name)[0]
    if not cmd and "/lune/" in path.replace("\\", "/"):
        what = "Lune negative control" if "negative" in name else "Lune suite"
    elif "lune run" in cmd:
        what = "Lune negative control" if "--negative" in cmd else "Lune suite"
    elif "gate.ps1" in cmd:
        what = "gate (host steps)"
    else:
        what = "run"
    return f"{what} · {nm}"[:80]


def _known_total(state_dir: str, key: str) -> Optional[int]:
    try:
        with open(os.path.join(state_dir, "live_test_totals.json"), encoding="utf-8") as f:
            v = json.load(f).get(key)
        return int(v) if v else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def remember_total(state_dir: str, key: str, total: int) -> None:
    """The last full count of a suite, so the next run's bar has a total while it runs."""
    p = os.path.join(state_dir, "live_test_totals.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    if d.get(key) == total:
        return
    d[key] = total
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, p)
    except OSError:
        pass


def suite_key(c: Counts) -> str:
    cmd = (c.cmd or "").lower()
    if "lune run" in cmd and not re.search(r"run\.luau\s+\S", cmd.split("--negative")[0]):
        return "lune:negative" if "--negative" in cmd else "lune:full"
    return ""


def collect(workspace: str, task_id: str, patterns: list, state_dir: str = "",
            now: Optional[float] = None) -> list[dict]:
    """Rows for the runs in progress (and those finished in the last RECENT_S) among
    the files matching `patterns` (globs relative to the workspace; `{task}` is the
    task id; a pattern containing `studio_lines` is a Studio CALL file)."""
    now = time.time() if now is None else now
    found = []
    for pat in patterns or []:
        kind = "studio" if "studio_lines" in pat else "log"
        g = os.path.join(workspace, pat.replace("{task}", glob.escape(task_id)))
        try:
            paths = glob.glob(g)
        except (OSError, re.error):
            continue
        for p in paths:
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if now - mt <= max(RECENT_S, STUDIO_ACTIVE_S if kind == "studio" else 0):
                found.append((mt, p, kind))
    found.sort(reverse=True)
    rows = []
    for mt, p, kind in found[:MAX_FILES * 2]:
        c = read(p)
        if c is None:
            continue
        r = c.row()
        if kind == "studio" and not c.calls and not c.call:
            continue                               # not a CALL file (yet)
        if kind == "log" and not (r["done"] or c.calls):
            if c.finished or now - mt > ACTIVE_S:
                continue                           # a run without test lines: not a test run
        active = now - mt <= (STUDIO_ACTIVE_S if kind == "studio" else ACTIVE_S)
        done = c.finished if kind == "log" else (c.calls is not None and c.call >= c.calls
                                                 and not active)
        if not active and not done and kind == "log":
            done = True                            # went quiet without an EXIT line
        key = suite_key(c) if kind == "log" else ""
        if r["total"] is None and key and state_dir:
            if c.result is None:
                r["total"] = _known_total(state_dir, key)
                r["estimated"] = r["total"] is not None
        elif key and state_dir and c.result and r["total"]:
            remember_total(state_dir, key, r["total"])
        if r["total"] is not None and r["done"] > r["total"]:
            r["total"] = None
        rows.append({"name": _label(p, c, kind), "state": "running" if active and not done
                     else (r["result"] or "done"), "source": "session",
                     "file": os.path.relpath(p, workspace).replace("\\", "/")[:200],
                     "updated_ts": mt, **r})
        if len(rows) >= MAX_FILES:
            break
    return rows


def forget(paths_under: str) -> None:
    """Drop cached counts of files under a directory (a removed worktree)."""
    with _lock:
        for p in [p for p in _files if p.startswith(paths_under)]:
            _files.pop(p, None)

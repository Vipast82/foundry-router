"""The crash-loop guard (COORDINATOR_AUTOMATION_NEEDS item 4).

On 2026-11-16 and 11-18 the coordinator restarted every ~6.5 minutes: one step of a tick
ran past the heartbeat limit (an evidence rewrite; one git call per commit over 10,573
commits), the Foundry Client restarted the "unhealthy" coordinator, and the new process
walked into the same step. Nothing counted the restarts or remembered what was running:
the first heartbeat of the new process overwrote the old one, and the client's own note
of it lives in memory.

  what is running   the engine names each step it enters (`step(...)`): a task's step with
                    its project, task and state, or one of its own passes (tracks, the
                    self-repair sweep, the prep queue, an after-tick hook). The workspace's
                    git runner names the command it is in.
  the mark          the process's keep-alive thread (every 15 s) writes
                    <state dir>/current-step.json once a step has run SLOW_S: the step, how
                    long, the keep-alive label covering it (or none), how long it has gone
                    uncovered, and the last command. The file is removed when the step ends.
  at the next start a leftover mark is the step the last process died in. It counts only
                    when it had gone UNCOVERED_S without keep-alive cover: that is a hang
                    the client restarts, not a covered long verify cut short by a deploy.
                    The same step twice in a row (LIMIT) raises one alert naming the step,
                    its label and its last command; a task's step is also skipped: the task
                    is held (owner crash_guard:<release>), so the loop ends. The hold is
                    released when another release runs, or by hand (resume). A start with
                    no leftover mark ends the streak.

A pass of the engine's own cannot be skipped: for those the alert is the whole answer.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from typing import Iterator, Optional

STEP_FILE = "current-step.json"
META = "crash_guard"
OWNER_KIND = "crash_guard"
SLOW_S = 30.0                 # a step this old gets a mark
UNCOVERED_S = 90.0            # the client's heartbeat limit: uncovered this long is a hang
LIMIT = 2                     # restarts in a row inside the same step

_lock = threading.Lock()
_cur: Optional[dict] = None
_cmd: dict = {}
_written = {"path": ""}


@contextlib.contextmanager
def step(kind: str, project: str = "", task: str = "", state: str = "") -> Iterator[None]:
    """The engine is inside this step until the block ends."""
    global _cur
    with _lock:
        prev, _cur = _cur, {"kind": str(kind), "project": str(project), "task": str(task),
                            "state": str(state), "since": time.time(), "uncovered_since": None}
    try:
        yield
    finally:
        with _lock:
            _cur = prev
        _clear()


def note_command(argv) -> None:
    """The external command a step is in (the workspace's git runner calls this)."""
    try:
        text = " ".join(str(x) for x in argv)[:300]
    except Exception:                                            # noqa: BLE001
        return
    with _lock:
        _cmd.update(text=text, ts=time.time())


def current() -> Optional[dict]:
    with _lock:
        return dict(_cur) if _cur else None


def key_of(s: dict) -> str:
    return "|".join(str(s.get(k) or "") for k in ("kind", "project", "task", "state"))


def _clear() -> None:
    path = _written.get("path") or ""
    if path:
        _written["path"] = ""
        try:
            os.remove(path)
        except OSError:
            pass


def write(state_dir: str, cover: Optional[dict] = None, now: Optional[float] = None) -> bool:
    """One pass of the keep-alive thread: write the mark for a step that has run SLOW_S
    (`cover` = keepalive.current()). True when a mark was written. Never raises."""
    now = time.time() if now is None else now
    with _lock:
        s = _cur
        if s is None:
            snap = None
        else:
            if cover is None:
                if s.get("uncovered_since") is None:
                    s["uncovered_since"] = max(float(s["since"]), now - 15.0)
            else:
                s["uncovered_since"] = None
            snap = dict(s)
            cmd = dict(_cmd)
    if snap is None or now - float(snap["since"]) < SLOW_S:
        return False
    unc = snap.get("uncovered_since")
    doc = {"pid": os.getpid(), "ts": now, "key": key_of(snap), **{
        k: snap[k] for k in ("kind", "project", "task", "state", "since")},
        "seconds": int(now - float(snap["since"])),
        "label": (cover or {}).get("label") or "",
        "uncovered_s": int(now - float(unc)) if unc is not None else 0,
        "command": cmd.get("text") if cmd.get("ts", 0) >= float(snap["since"]) else ""}
    path = os.path.join(state_dir, STEP_FILE)
    try:
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        _written["path"] = path
        return True
    except OSError:
        return False


def _version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:                                            # noqa: BLE001
        return "0"


def owner_alive(c, ref: str):
    """A crash_guard hold lasts while the release that hung is the one running."""
    return str(ref) == _version()


def register() -> None:
    from . import selfheal
    selfheal.register_hold_owner(OWNER_KIND, owner_alive)


def on_start(c, state_dir: str) -> Optional[dict]:
    """Read the mark the last process left (see the module text). Returns what was found
    ({key, n, counted, held, ...}) or None. Never raises."""
    path = os.path.join(state_dir, STEP_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            left = json.load(f)
    except (OSError, ValueError):
        left = None
    try:
        os.remove(path)
    except OSError:
        pass
    try:
        return _judge(c, left)
    except Exception as e:                                       # noqa: BLE001
        try:
            c.log(f"crash guard: {type(e).__name__}: {e}")
        except Exception:                                        # noqa: BLE001
            pass
        return None


def _judge(c, left) -> Optional[dict]:
    try:
        streak = json.loads(c.store.meta(META) or "{}")
    except ValueError:
        streak = {}
    if not isinstance(left, dict) or not left.get("key") or \
            float(left.get("uncovered_s") or 0) < UNCOVERED_S:
        if streak:
            c.store.set_meta(META, "{}")          # a start that follows no hang: over
        return None
    key = str(left["key"])
    n = int(streak.get("n") or 0) + 1 if streak.get("key") == key else 1
    rec = {"key": key, "n": n, "ts": time.time(), "label": left.get("label") or "",
           "command": left.get("command") or "", "seconds": left.get("seconds"),
           "uncovered_s": left.get("uncovered_s"), "version": _version()}
    c.store.set_meta(META, json.dumps(rec))
    pid, tid = str(left.get("project") or ""), str(left.get("task") or "")
    what = (f"{tid}'s {left.get('state') or 'step'}" if tid
            else f"the coordinator's own pass '{left.get('kind')}'")
    out = {**rec, "project": pid, "task": tid, "counted": True, "held": False}
    home = pid or (sorted(c.projects)[0] if c.projects else "")
    c.store.event(home, tid, "crash_guard_seen", detail={**rec, "what": what, "digest": True})
    if n < LIMIT:
        return out
    held = ""
    if tid and pid in c.projects:
        try:
            t = c.store.task(pid, tid)
            from . import states as S
            if t and t["state"] not in S.TERMINAL and t["state"] != S.PAUSED:
                c.hold(pid, tid, f"{OWNER_KIND}:{_version()}",
                       f"the coordinator restarted {n} times in a row inside this sprint's "
                       f"{left.get('state') or 'step'}; held until another release runs or "
                       f"it is resumed by hand", actor="crash_guard")
                held = tid
        except Exception as e:                                   # noqa: BLE001
            held = ""
            c.log(f"crash guard: could not hold {pid}/{tid}: {e}")
    out["held"] = bool(held)
    text = (f"The coordinator restarted {n} times in a row inside the same step: {what}, "
            f"running {left.get('seconds')} s"
            + (f" under '{left['label']}'" if left.get("label") else " with no keep-alive cover")
            + (f"; last command: {left['command']}" if left.get("command") else "")
            + ". " + (f"{held} is held so the loop ends: it goes on when another release runs, "
                      f"or resume it by hand." if held else
                      "That step is the coordinator's own and cannot be skipped: it needs a "
                      "fix or it will restart again."))
    c.store.event(home, tid, "operator_notice",
                  detail={"text": text[:900], "kind": "crash_loop", "alert_class": "watch",
                          "title": f"the coordinator restarted {n} times inside the same step",
                          "task_id": tid, "key": key, "held": bool(held)})
    return out

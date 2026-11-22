"""The watch log: dated lines of what the coordinator did by itself and why, and of the
orders it was given (COORDINATOR_AUTOMATION_NEEDS item 27).

The operator session kept this by hand in C:/Roblox/_scratch/WATCH_RULES.md: one dated
line per thing it noticed or changed, with the owner's orders and their scope ("keep POL-7
going until complete"). The coordinator now writes its own, from what it already records:

  events        self-repair and auto-heal actions, outages waited out, budget renewals and
                time extensions, standing orders (given, each action, their end), holds,
                Studio reservations, project pauses, settings applied, Lead cases opened,
                sprints parked, decisions answered by their timeout, operator notices,
                coordinator errors
  transitions   every state change NOT made by the engine itself: an operator's unblock
                with its resolution, a self-repair or order putting a sprint back, a
                reviewer model sending one back

One line each, local time, newest last, in <state dir>/watch-log/<YYYY-MM>.md (plain
text; nothing is committed to the game repository). `tick` appends what is new at most
once a minute and never raises; `tail` is what the CLI (`log`), the heartbeat and the
Automation page's Orders tab show. The first run starts a day back.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

EVERY_S = 60.0
BATCH = 500
MAX_LINE = 420
EV_CURSOR, TR_CURSOR = "watch_log_event_id", "watch_log_transition_id"
ENGINE_ACTORS = ("engine", "coordinator", "")


def _s(v, n: int = 240) -> str:
    return " ".join(str(v if v is not None else "").split())[:n]


def _notice(d: dict) -> str:
    return _s(d.get("text"), 380)


def _self_heal(d: dict) -> str:
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    why = ev.get("why") or ev.get("reason") or ev.get("blocker") or ""
    return f"self-repair ({d.get('rule')}): {_s(d.get('action'))}" + (f" - {_s(why, 200)}" if why else "")


def _auto_heal(d: dict) -> str:
    if d.get("waiting"):
        return f"waiting for an outage ({d.get('signature')}); was: {_s(d.get('reason'), 200)}"
    return (f"auto-heal {d.get('n')}/{d.get('of')} ({d.get('signature')}): {d.get('action')} in "
            f"{d.get('delay_s')} s; was: {_s(d.get('reason'), 200)}")


FORMAT = {
    "self_heal": _self_heal,
    "auto_heal": _auto_heal,
    "auto_heal_exhausted": lambda d: f"auto-heal used up ({_s(d.get('key'), 80)}): on to the Lead",
    "infra_waiting": lambda d: f"waits for {d.get('family')} to come back ({_s(d.get('why'), 200)})",
    "infra_back": lambda d: (f"{d.get('signature')}: back after "
                             f"{int(float(d.get('waited_s') or 0) // 60)} min; retrying"),
    "infra_session_refund": lambda d: (f"session {_s(d.get('session_id'), 40)} ended on "
                                       f"infrastructure ({d.get('signature')}): not counted"),
    "stale_playtest_stopped": lambda d: (f"the leftover playtest is stopped "
                                         f"({_s(d.get('output'), 160) or 'Studio is in Edit'})"),
    "operator_notice": _notice,
    "order_added": lambda d: (f"ORDER from {_s(d.get('by'), 60)}: {_s(d.get('order'))}"
                              + (f" - {_s(d.get('note'), 200)}" if d.get("note") else "")),
    "order_action": lambda d: (f"order '{_s(d.get('order'), 120)}': {_s(d.get('action'))}"
                               + (f" - was: {_s(d.get('why'), 160)}" if d.get("why") else "")),
    "order_done": lambda d: (f"ORDER {d.get('status')}: {_s(d.get('order'))}. "
                             f"{_s(d.get('result'), 200)}"),
    "hold_set": lambda d: f"held ({_s(d.get('owner'), 60)}): {_s(d.get('reason'), 200)}",
    "hold_released": lambda d: f"hold released: {_s(d.get('why') or d.get('reason'), 200)}",
    "studio_reserved": lambda d: (f"Studio reserved for the owner by {_s(d.get('by'), 40)}: "
                                  f"{_s(d.get('reason'), 160)}"),
    "studio_released": lambda d: f"Studio reservation released by {_s(d.get('by'), 40)}",
    "project_paused": lambda d: f"project paused: {_s(d.get('note'), 240)}",
    "project_resumed": lambda d: f"project resumed: {_s(d.get('note'), 240)}",
    "config_applied": lambda d: (f"settings applied (revision {d.get('rev') or d.get('revision') or '?'})"
                                 + (f": {_s(d.get('changed') or d.get('keys'), 200)}"
                                    if d.get("changed") or d.get("keys") else "")),
    "lead_case": lambda d: (f"Lead case {_s(d.get('case_id'), 30)} opened "
                            f"({d.get('class') or d.get('trigger')}): {_s(d.get('reason'), 200)}"),
    "sprint_parked": lambda d: f"parked: {_s(d.get('why') or d.get('reason'), 260)}",
    "decision_timeout_applied": lambda d: (f"decision {_s(d.get('decision_id'), 30)} answered by "
                                           f"its timeout: {_s(d.get('answer') or d.get('option'), 160)}"),
    "claude_agent_cap": lambda d: (f"Claude usage {d.get('usage_pct')}%: Claude agent cap is "
                                   f"{d.get('cap')}"),
    "budget_renewal_skipped": lambda d: f"budget not renewed by itself ({d.get('counter')}): {_s(d.get('why'))}",
    "time_extension_skipped": lambda d: f"time not extended by itself: {_s(d.get('why'))}",
    "error": lambda d: f"coordinator error: {_s(d.get('error'), 300)}",
    "router_back": lambda d: f"the router answers again after {d.get('down_s')} s ({_s(d.get('why'), 160)})",
    "crash_guard_seen": lambda d: (f"the last coordinator process died inside {_s(d.get('what'), 120)} "
                                   f"({d.get('seconds')} s, restart {d.get('n')} in a row"
                                   + (f"; last command: {_s(d.get('command'), 160)}" if d.get("command")
                                      else "") + ")"),
}


def _dir(c) -> str:
    return os.path.join(str(getattr(c, "state_dir", "") or "."), "watch-log")


def _file(c, ts: float) -> str:
    return os.path.join(_dir(c), time.strftime("%Y-%m", time.localtime(ts)) + ".md")


def _line(ts: float, pid: str, tid: str, text: str) -> str:
    who = tid or pid or "-"
    return (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}  {who}  {text}")[:MAX_LINE]


def _transition_text(r: dict) -> Optional[str]:
    actor = str(r.get("actor") or "")
    if actor in ENGINE_ACTORS:
        return None
    try:
        ev = json.loads(r.get("evidence") or "{}")
    except ValueError:
        ev = {}
    ev = ev if isinstance(ev, dict) else {}
    why = (ev.get("resolution") or ev.get("reason") or ev.get("resumed") or ev.get("blocker")
           or ev.get("note") or "")
    if not why and ev.get("findings"):
        why = f"{len(ev['findings'])} finding(s): {_s(ev['findings'][0], 160)}"
    extra = " (budget reset)" if ev.get("reset_attempts") else ""
    return (f"{r.get('from_state') or 'new'} -> {r.get('to_state')} by {_s(actor, 60)}{extra}"
            + (f": {_s(why, 260)}" if why else ""))


def tick(c, force: bool = False) -> int:
    """Append what is new (at most once a minute unless `force`). Returns the lines written;
    never raises."""
    try:
        now = time.time()
        if not force and now - float(c.__dict__.get("_watch_log_at") or 0) < EVERY_S:
            return 0
        c.__dict__["_watch_log_at"] = now
        return _write(c, now)
    except Exception as e:                                       # noqa: BLE001
        try:
            c.log(f"watch log: {type(e).__name__}: {e}")
        except Exception:                                        # noqa: BLE001
            pass
        return 0


def _cursor(c, key: str, table: str, now: float) -> int:
    v = c.store.meta(key)
    if v is not None:
        try:
            return int(v)
        except ValueError:
            pass
    r = c.store.q(f"SELECT COALESCE(MIN(id), 0) AS i, COUNT(*) AS n FROM {table} WHERE ts>=?",
                  (now - 86400,))
    if r and r[0]["n"]:
        return int(r[0]["i"]) - 1
    r = c.store.q(f"SELECT COALESCE(MAX(id), 0) AS i FROM {table}")
    return int(r[0]["i"]) if r else 0


def _write(c, now: float) -> int:
    ev_at = _cursor(c, EV_CURSOR, "events", now)
    tr_at = _cursor(c, TR_CURSOR, "transitions", now)
    kinds = tuple(FORMAT)
    q = ",".join("?" * len(kinds))
    evs = c.store.q(f"SELECT id, ts, project_id, task_id, kind, detail FROM events WHERE id>? "
                    f"AND kind IN ({q}) ORDER BY id LIMIT ?", (ev_at, *kinds, BATCH))
    trs = c.store.q("SELECT id, ts, project_id, task_id, from_state, to_state, evidence, actor "
                    "FROM transitions WHERE id>? ORDER BY id LIMIT ?", (tr_at, BATCH))
    rows = []
    for r in evs:
        try:
            d = json.loads(r["detail"] or "{}")
            text = FORMAT[r["kind"]](d if isinstance(d, dict) else {})
        except Exception:                                        # noqa: BLE001
            text = r["kind"]
        if text:
            rows.append((float(r["ts"] or now), r["id"], _line(float(r["ts"] or now),
                                                               r["project_id"], r["task_id"], text)))
    for r in trs:
        text = _transition_text(r)
        if text:
            rows.append((float(r["ts"] or now), r["id"], _line(float(r["ts"] or now),
                                                               r["project_id"], r["task_id"], text)))
    rows.sort(key=lambda x: (x[0], x[1]))
    if rows:
        os.makedirs(_dir(c), exist_ok=True)
        by_file: dict = {}
        for ts, _, line in rows:
            by_file.setdefault(_file(c, ts), []).append(line)
        for path, lines in by_file.items():
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
    if evs:
        c.store.set_meta(EV_CURSOR, str(evs[-1]["id"]))
    elif c.store.meta(EV_CURSOR) is None:
        c.store.set_meta(EV_CURSOR, str(ev_at))
    if trs:
        c.store.set_meta(TR_CURSOR, str(trs[-1]["id"]))
    elif c.store.meta(TR_CURSOR) is None:
        c.store.set_meta(TR_CURSOR, str(tr_at))
    return len(rows)


def tail(c, n: int = 60) -> list:
    """The last `n` lines, oldest first."""
    d = _dir(c)
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    except OSError:
        return []
    out: list = []
    for name in reversed(files):
        try:
            with open(os.path.join(d, name), encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        except OSError:
            continue
        out = lines[-(n - len(out)):] + out if n > len(out) else out
        if len(out) >= n:
            break
    return out[-n:]

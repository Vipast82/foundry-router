"""Deterministic auto-heal before a lead case (revamp B9, B12).

Most stops that reached the lead were tooling or infrastructure faults that no
diagnosis fixes: Kilo not admitting a prompt, a 5xx, Rojo or Studio down, the
studio_gate runner erroring instead of returning a verdict. Before the engine
opens a paid lead case for such a stop, it re-runs the step itself:

  verify   the gate / required checks could not run -> BLOCKED, wait, VERIFYING
           again at the same head (no session, no attempt)
  restart  the session died on infrastructure -> BLOCKED, wait, READY with a free
           retry (no attempt is charged)

Each (head, stop class, signature) is retried at most [automation]
auto_heal_retries times with a doubling backoff ([automation]
infra_backoff_seconds, capped at 15 min); after that the stop goes to the lead
(or the operator) as before. Every retry is journaled as an event and in the
task's data. No model call is made here.

W1.8 (2026-10-22): a retry is preceded by a REPAIR where one is deterministic:
  rojo / rojo_stale   restart Rojo through the Foundry Client's local API when
                      the client is reachable (Rojo re-serves the tree; the Studio
                      plugin reconnects and re-syncs the sources); stale = Studio's
                      loaded sources differ from HEAD (host.live_hash)
  gate_tooling /      clear the studio_gate runner lock when its pid is dead, and
  studio              stop a playtest a dead runner left running
The dead-runner case itself (a runner pid gone without a verdict) is detected
by fast_lane._poll_run each tick (no 45-minute stall) and lands here as
fast_lane_gate_error. What was repaired is journaled in the auto_heal event.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Optional

from . import states as S
from .automation import get as auto_get

# a number followed by a unit is a quantity, not a status code
_NOT_A_QUANTITY = (r"(?!\s*(?:ms|s|secs?|seconds?|milliseconds?|m|mins?|minutes?|h|hours?|"
                   r"tokens?|bytes?|kb|mb|gb|lines?|files?|%)\b)")
# known infrastructure signatures (01_issue_audit.md F10) -> name
INFRA_SIGNATURES = (
    ("kilo_not_admitted", re.compile(r"did not admit the prompt|prompt_not_admitted", re.I)),
    ("kilo_aborted", re.compile(r"MessageAbortedError|aborted every turn", re.I)),
    # Kilo sat in "retry" (router or model outage) until the coordinator stopped it
    ("kilo_retry", re.compile(r"kept retrying the model call", re.I)),
    # 2026-11-07 05:45: the router's own database filled ("database or disk is full",
    # code backend_error) and every session of every sprint ended at its first call; nine
    # sprints spent their attempts in eight minutes. The router is down for everyone.
    ("router_storage", re.compile(r"database or disk is full|No space left on device|"
                                  r"database is locked|disk I/O error", re.I)),
    # a status code, never a quantity ("500 ms", "took 503 seconds", "504 tokens")
    ("backend_5xx", re.compile(r"\b(?:HTTP\s*)?5(?:00|02|03|04)\b" + _NOT_A_QUANTITY +
                               r"|Bad Gateway|Service Unavailable|Gateway Time-?out", re.I)),
    ("rate_limit", re.compile(r"\b429\b" + _NOT_A_QUANTITY + r"|rate[ -]limit(?:ed| exceeded| reached)|"
                              r"rate_limit(?:_error|_exceeded)|RateLimitError|too many "
                              r"requests|quota (?:exceeded|exhausted)|exceeded (?:your )?"
                              r"(?:current )?quota|insufficient_quota", re.I)),
    # 2026-10-29 AS-D11: the local server serves one stream; a second local session is
    # refused ("serves the main stream only and 1 session(s) are active") - wait, no attempt
    ("local_busy", re.compile(r"serves the main stream only|session\(s\) are active \(", re.I)),
    ("connection", re.compile(r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|"
                              r"socket hang up|fetch failed|connection (?:refused|reset|aborted)|"
                              r"timed out connecting|could not connect", re.I)),
    # the Studio plugin is not connected to a running Rojo (a person would click Connect)
    ("rojo_connect", re.compile(r"Rojo\s*(?:→|->|>|-)\s*Connect|rojo[^.\n]{0,40}plugin[^.\n]{0,40}"
                                r"(?:not connected|disconnected|reconnect)|owner click|"
                                r"click[^.\n]{0,30}\bConnect\b", re.I)),
    ("rojo_stale", re.compile(r"host\.live_hash|rojo[^.\n]{0,40}stale|loaded source[^.\n]{0,40}"
                              r"differ|studio[^.\n]{0,40}(?:out of date|behind HEAD)", re.I)),
    ("rojo", re.compile(r"\brojo\b[^.\n]{0,80}(?:exited|down|not running|not syncing|"
                        r"websocket|refused|unreachable|code -1)", re.I)),
    # 2026-10-30 MV2-11: the session found the Studio session itself degraded (stale
    # errors, missing profile, timeouts) and asked for a fresh Studio / place reload
    # 2026-10-30 18:19 GF-RT1 / MV2-11: Studio was running with no place open ("Place is not
    # open", "need exactly one Studio instance, found 0", "place needs reopening")
    ("studio_reload", re.compile(r"(?:fresh|restart(?:ed)?|reload(?:ed)?|reopen(?:ed)?)\s+"
                                 r"(?:the\s+)?(?:roblox\s+)?(?:studio|place)\b|"
                                 r"(?:studio|place)\s+(?:reload|reopen|restart)|"
                                 r"place is not open|no place (?:is )?open|"
                                 r"studio instances?,? found 0|connected: none|"
                                 r"lists no (?:studio )?instances|"
                                 # 2026-10-31 MV2-11: the official MCP's wording when Studio
                                 # is up on its start page (the place closed mid-gate)
                                 r"no (?:roblox )?studio instances? (?:are |is )?connected|"
                                 # 2026-10-31 AS-J05: OfficialStudio.resolve in the container
                                 # slot ("need exactly one Studio, found 0 (open: none)")
                                 r"exactly one studio[^,\n]{0,40},? found 0|\(open: none\)|"
                                 r"place needs (?:reopening|to be reopened)|"
                                 # 2026-11-04 POL-2: Roblox HTTP errors closed the TEST place
                                 # mid-gate; the MCP said "The requested `studio_id` is not
                                 # connected - that Roblox Studio instance may have been closed
                                 # or its place unloaded" (`studio_id` defeats \bstudio\b)
                                 r"studio_id`?\s+is not connected|"
                                 r"studio instance may have been closed|"
                                 r"its place unloaded", re.I)),
    ("studio", re.compile(r"\bstudio\b[^.\n]{0,80}(?:not connected|disconnected|not "
                          r"running|plugin[^.\n]{0,30}(?:connect|timeout)|mcp[^.\n]{0,40}"
                          r"(?:unavailable|not connected|timed out))", re.I)),
    ("python_path", re.compile(r"No module named '?foundry_coordinator|foundry_coordinator"
                               r"[^.\n]{0,30}not importable", re.I)),
    ("context_overflow", re.compile(r"ContextOverflowError|Compaction exhausted|context "
                                    r"(?:window )?(?:exceeded|overflow)|exceeds the available "
                                    r"context size", re.I)),
    ("kilo_auth", re.compile(r"Kilo[^.\n]{0,40}\b401\b", re.I)),
    # 2026-11-20 18:25 FIN-4a: the first push of a lane to slot-3's 36 GB copy ran into the
    # push timeout; the wrapper died with the traceback ("subprocess.TimeoutExpired: Command
    # '['git', '-C', ..., 'push', ..., '<head>:refs/heads/slot/FIN-4a']' timed out after 300
    # seconds"), no signature matched, and the sprint sat in a Lead case for four hours. The
    # wrapper prints "SLOT FAIL: ..." lines now (slots.run); the traceback form stays
    # recognised for a run an older release left behind.
    ("slot_push", re.compile(r"SLOT FAIL: could not push the sources|"
                             r"SLOT push \([^)\n]*\): [^\n]{0,60}(?:timed out|push: )|"
                             r"TimeoutExpired[^\n]{0,600}'push'[^\n]{0,600}refs/heads/slot/",
                             re.I)),
    ("slot_checkout", re.compile(r"SLOT FAIL: slot-checkout", re.I)),
    # 2026-11-16 08:40 LOB-1b: the PC rebooted under a running gate ("the pipeline (pid
    # 38784) is gone without a result"); the stop went to a Lead case and the operator
    # re-ran it. A process that vanished says nothing about the code. Last, so a log tail
    # that names a real cause above wins.
    ("gate_runner_gone", re.compile(r"the pipeline \(pid \d+\) is gone without a result", re.I)),
)
# stop classes whose blocker is the gate / check tooling, not the code
VERIFY_CLASSES = ("fast_lane_gate_error", "checks_unknown")
# stop classes of a session that may have died on infrastructure
SESSION_CLASSES = ("session_blocked", "session_needs_human", "session_error")
# a scripted run that never reached the slot's Studio (the slot's git copy or checkout), or
# whose process vanished: nothing about the sprint, and nothing to reopen
SLOT_SIGS = ("slot_push", "slot_checkout", "gate_runner_gone")
MAX_BACKOFF = 900


def infra_signature(text: str) -> str:
    for name, rx in INFRA_SIGNATURES:
        if rx.search(text or ""):
            return name
    return ""


def plan(cls: str, reason: str) -> Optional[tuple[str, str]]:
    """(signature, action) for a stop the coordinator can retry itself, else None."""
    cls = str(cls or "")
    if cls in VERIFY_CLASSES:
        return (infra_signature(reason) or "gate_tooling"), "verify"
    if cls in SESSION_CLASSES:
        sig = infra_signature(reason)
        if sig:
            return sig, "restart"
    if cls == "studio_calls_error":
        # 2026-10-30 AS-P7/AS-J08: the scripted gate's PRE-PLAY verify found the mv2-2
        # merge's new check files missing in Studio (live=missing): the Rojo restart and
        # reopen (v0.157.74) never ran for this class; both sprints went to a lead case.
        sig = infra_signature(reason)
        if sig in STUDIO_SYNC_SIGS or sig in SLOT_SIGS:
            return sig, "restart"
    return None


# ---- an outage the coordinator can see is waited out (v0.181.229) --------------------------
# 2026-11-17 21:45: a Studio update closed Studio. ORI-2, FIN-5a1, POL-7 and LOB-1b each
# asked for their gate, got "need exactly one Studio matching '[TEST]', found 0", were
# restarted "free" after 60 s into the same missing Studio, and again, five times in an
# hour: every restart was a session (POL-7 hit "44 sessions used"), every session counted
# as "no progress", and three sprints were parked "self-repair exhausted". The head moved
# with each run, so the per-head retry count never filled and no bound applied.
#
# Before a scheduled retry is released, the fault is looked for where it can be seen: a
# Studio with the TEST place connected (the adapter's studio_ready), the router's /health.
# While it is still there the task waits (checked every WAIT_RECHECK_S, the repair asked
# for again every WAIT_REPAIR_S), nothing is restarted, counted or charged, and after
# [automation] infra_wait_notice_minutes one alert names the outage and everyone waiting on
# it. A fault that cannot be seen (a slot's Studio, a slot push) keeps the bounded retries.
WAIT_STUDIO = ("studio", "studio_reload")
WAIT_ROUTER = ("connection", "backend_5xx", "router_storage", "kilo_retry")
WAIT_WHAT = {"studio": "Studio with the TEST place", "router": "the router"}
PROBE_CACHE_S = 45.0
WAIT_RECHECK_S = 60.0
WAIT_REPAIR_S = 900.0
OUTAGE_FORGET_S = 300.0          # nobody waited on it this long: the next one is a new outage
RECENT_WINDOW_S = 6 * 3600.0
RECENT_PER_RETRY = 3             # x auto_heal_retries: restarts of one class and signature


def wait_family(sig: str) -> str:
    return "studio" if sig in WAIT_STUDIO else "router" if sig in WAIT_ROUTER else ""


def fault_present(c, P, sig: str) -> Optional[bool]:
    """Is the fault a stop signature names still there? True (seen), False (seen gone) or
    None (no way to look: retry as before). One look per project and family per
    PROBE_CACHE_S, however many tasks wait. Never raises."""
    fam = wait_family(sig)
    if not fam:
        return None
    cache = c.__dict__.setdefault("_fault_probe", {})
    now = c.clock()
    try:
        main = c.tracks.main_of(P)
    except Exception:                                            # noqa: BLE001
        main = P
    key = (main.id, fam)
    if fam == "studio":
        try:
            from . import slots
            slot = slots.slot_for(c, P)
        except Exception:                                        # noqa: BLE001
            slot = None
        if slot is not None:
            return None                  # a slot's Studio: nothing here can see it
    hit = cache.get(key)
    if hit and now - hit[0] < PROBE_CACHE_S:
        return hit[1]
    out: Optional[bool] = None
    why = ""
    try:
        if fam == "studio":
            fn = getattr(P.adapter, "studio_ready", None)
            if callable(fn):
                verdict, why = fn()
                out = True if verdict == "no_studio" else False if verdict in ("ok", "play") \
                    else None
        else:
            from . import infra
            specs = infra.router_probe(main)
            if specs:
                why = infra.check_endpoint(infra._endpoint(specs[0])[1])
                out = bool(why)
    except Exception as e:                                       # noqa: BLE001
        out, why = None, f"{type(e).__name__}: {e}"
    cache[key] = (now, out, str(why or "")[:200])
    return out


def _fault_why(c, P, sig: str) -> str:
    try:
        main = c.tracks.main_of(P)
    except Exception:                                            # noqa: BLE001
        main = P
    hit = c.__dict__.get("_fault_probe", {}).get((main.id, wait_family(sig)))
    return str(hit[2]) if hit else ""


def _mark_infra_session(c, P, t: dict, sig: str, key: str) -> None:
    """The session whose turn ended in an infrastructure stop says nothing about the
    sprint: it is left out of the no-progress count (recovery._infra_sessions reads
    `infra_stop`), and its place in the session budget is given back (one
    `infra:sessions:<session>` row; the ledger checks the mark and a daily cap)."""
    sid = str(t.get("session_id") or "")
    if not sid:
        return
    try:
        s = c.store.session(sid)
        if not s or (s.get("data") or {}).get("infra_stop"):
            return
        c.store.update_session(sid, data={**(s.get("data") or {}), "infra_stop": sig,
                                          "infra_stop_key": key})
        from .obligations import INFRA_ACTOR, INFRA_REF
        c.obligations.authorize(P.id, t["task_id"], "sessions", 1,
                                f"session {sid} ended on infrastructure ({sig}): not counted",
                                INFRA_ACTOR, f"{INFRA_REF}sessions:{sid}")
        c.store.event(P.id, t["task_id"], "infra_session_refund", detail={
            "session_id": sid, "signature": sig, "key": key})
    except Exception as e:                                       # noqa: BLE001
        c.store.event(P.id, t["task_id"], "infra_session_refund_refused", detail={
            "session_id": sid, "signature": sig, "why": f"{type(e).__name__}: {e}"[:300]})


def try_heal(c, P, t: dict, to: str, evidence: dict, cls: str, fields: dict) -> Optional[dict]:
    """Called by Coordinator._stop before a lead case is opened. Returns the
    BLOCKED transition of a scheduled retry, or None (the stop proceeds)."""
    if to != S.BLOCKED:
        return None
    reason = str(evidence.get("blocker") or evidence.get("reason") or "")
    got = plan(cls, reason)
    if got is None:
        return None
    sig, action = got
    retries = int(auto_get(P, "auto_heal_retries") or 0)
    if retries <= 0:
        return None
    pid, tid = P.id, t["task_id"]
    cur = c.store.task(pid, tid) or t
    if cur["state"] in S.TERMINAL or cur["state"] == S.BLOCKED:
        return None
    try:
        head = P.ws.head()
    except Exception:                                            # noqa: BLE001
        head = ""
    data = dict(fields.get("data") or cur.get("data") or {})
    counts = dict(data.get("auto_heal_counts") or {})
    key = f"{head[:12]}|{cls}|{sig}"
    n = int(counts.get(key) or 0)
    now = c.clock()
    # the count above is per head, and every failed run moved the head (11-17: "auto-heal
    # 1/2" five times in a row, never 2/2). The same class and signature is also counted
    # over RECENT_WINDOW_S whatever the head: RECENT_PER_RETRY times auto_heal_retries
    rkey = f"{cls}|{sig}"
    recent_all = dict(data.get("auto_heal_recent") or {})
    recent = [float(x) for x in recent_all.get(rkey) or [] if now - float(x) < RECENT_WINDOW_S]
    waiting = False
    if n >= retries or len(recent) >= retries * RECENT_PER_RETRY:
        if fault_present(c, P, sig) is not True:
            c.store.event(pid, tid, "auto_heal_exhausted", detail={
                "key": key, "retries": n, "action": action, "reason": reason[:300],
                "recent": len(recent), "recent_hours": RECENT_WINDOW_S / 3600})
            return None
        # the retries are used up and the outage is still there: it is waited out, not
        # handed to a paid Lead case that can only say "infrastructure"
        waiting = True
    if not waiting:
        recent_all[rkey] = recent + [now]
        data["auto_heal_recent"] = recent_all
    if waiting:
        delay = int(WAIT_RECHECK_S)
        blocker = (f"waiting for {WAIT_WHAT[wait_family(sig)]} ({sig}): checked every "
                   f"{delay} s; no attempt, session or Lead call is used; was: {reason}")[:1000]
    else:
        delay = min(MAX_BACKOFF, int(auto_get(P, "infra_backoff_seconds") or 0) * (2 ** n))
        counts[key] = n + 1
        blocker = (f"auto-heal {n + 1}/{retries} ({sig}): "
                   + ("re-running the gate/checks" if action == "verify"
                      else "restarting the session (no attempt charged)")
                   + f" in {delay} s; was: {reason}")[:1000]
    data.update(auto_heal_counts=counts, auto_heal={
        "key": key, "n": n if waiting else n + 1, "action": action, "signature": sig,
        "class": cls, "retry_at": now + delay, "blocker": blocker, "reason": reason[:500],
        "verification_id": evidence.get("verification_id") or fields.get("verification_id")
        or "", **({"wait_since": now} if waiting else {})})
    ev = {**evidence, "blocker": blocker, "auto_heal": True}
    try:
        S.check(cur["state"], S.BLOCKED, ev)
    except S.TransitionError:
        return None
    extra = {k: fields[k] for k in ("verification_id", "head_sha") if fields.get(k)}
    c.store.release(pid, tid)
    out = c.store.transition(pid, tid, S.BLOCKED, ev, blocker=blocker, data=data,
                             next_action=blocker[:500], **extra)
    if action == "restart":
        _mark_infra_session(c, P, cur, sig, key)
    # the stopped task (its blocker names what Studio is missing), not the pre-stop copy
    repairs = repair(c, P, c.store.task(pid, tid) or t, sig, action)  # W1.8
    job = next((r.get("job") for r in repairs if r.get("job")), "")
    if job:
        # 0.145.0: the retry waits for the client's Studio reopen job (step())
        cur2 = c.store.task(pid, tid) or {}
        d2 = dict(cur2.get("data") or {})
        ah = dict(d2.get("auto_heal") or {})
        ah.update(studio_job=job, studio_job_ts=now, retry_at=max(float(ah.get("retry_at") or 0),
                                                                  now + 30))
        d2["auto_heal"] = ah
        c.store.update_task(pid, tid, data=d2)
    c.store.event(pid, tid, "auto_heal", detail={
        "key": key, "n": n if waiting else n + 1, "of": retries, "action": action,
        "signature": sig, "class": cls, "delay_s": delay, "reason": reason[:300],
        "repairs": repairs, **({"waiting": True} if waiting else {})})
    if n == 0:
        # A1: one promotion-pipeline occurrence per auto-heal key (head/class/
        # signature), counted on its first retry; inert without a playbook
        pb = getattr(c, "playbook", None)
        if pb is not None:
            try:
                pb.promote.observe_failure(P, tid, sig, cls, reason)
            except Exception as e:                               # noqa: BLE001
                c.log(f"{pid}/{tid}: playbook observe_failure failed: {e}")
    return out


# ---- W1.8 repairs ------------------------------------------------------------------------

def pid_alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259                # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def runner_lock_path(workspace: str) -> str:
    return os.path.join(os.path.abspath(workspace), ".plan", "tmp", "gate", "runner.lock")


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_stale_runner_lock(workspace: str) -> dict:
    """Remove `.plan/tmp/gate/runner.lock` when the pid it names is dead (a live pid
    is never touched). {"cleared": bool, "pid": int, "playing": bool, "sprint": str}."""
    p = runner_lock_path(workspace)
    cur = _read_json(p)
    if not isinstance(cur, dict):
        return {"cleared": False, "pid": 0, "playing": False, "sprint": ""}
    pid = int(cur.get("pid") or 0)
    if pid and pid_alive(pid):
        return {"cleared": False, "pid": pid, "playing": False, "sprint": str(cur.get("sprint") or "")}
    sprint = str(cur.get("sprint") or "")
    playing = False
    st = _read_json(os.path.join(os.path.dirname(p), sprint, "status.json")) if sprint else None
    if isinstance(st, dict) and st.get("pid") == pid and st.get("playing"):
        playing = True
    try:
        os.remove(p)
        cleared = True
    except OSError:
        cleared = False
    return {"cleared": cleared, "pid": pid, "playing": playing, "sprint": sprint}


def stop_playtest(P, log: str = "") -> str:
    """`studio_gate stop-playtest` in the background (one community call) so Studio
    is back in Edit mode after a dead runner. '' when started, else why not. `log`: a file
    the command's own line goes to ("STOPPED: ...", "STOP FAILED: ...", "BUSY: ..."), so the
    caller can say what came of it (selfheal.stale_playtests); without it the output is
    dropped as before."""
    try:
        from .fast_lane import ROUTER_ROOT, settings
        gm = str(settings(P)["gate_module"]).strip()
    except Exception as e:                                   # noqa: BLE001
        return f"no gate module: {e}"[:200]
    if gm.endswith(".py"):
        return "custom gate script: no known stop-playtest command"
    out = None
    try:
        if log:
            os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
            out = open(log, "w", encoding="utf-8")
        subprocess.Popen([sys.executable, "-m", gm, "--repo", P.profile.workspace, "stop-playtest"],
                         cwd=ROUTER_ROOT, stdin=subprocess.DEVNULL,
                         stdout=out or subprocess.DEVNULL,
                         stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    except OSError as e:
        return f"stop-playtest: {e}"[:200]
    finally:
        if out is not None:
            out.close()                           # the child keeps its own handle
    return ""


# stops a Studio reopen can fix (the plugin lost Rojo, Studio not connected)
STUDIO_SYNC_SIGS = ("rojo_connect", "rojo", "rojo_stale", "studio", "studio_reload")
REOPEN_JOB_MAX_S = 1500          # a client job older than this counts as failed


def studio_guard(c, P, t: dict) -> str:
    """'' when Studio is free for a reopen on behalf of task `t`, else why not:
    another sprint holds the coordinator's Studio lease, a gate runner is alive, or
    the project says Studio is busy (adapter.studio_busy, e.g. AU's studio.lock)."""
    pid, tid = P.id, t["task_id"]
    try:
        for lk in c.store.locks():
            if str(lk.get("resource", "")).startswith("studio:") and \
                    (lk.get("project_id"), lk.get("task_id")) != (pid, tid):
                return f"Studio is leased to {lk.get('project_id')}/{lk.get('task_id')}"
    except Exception:                                            # noqa: BLE001
        pass
    cur = _read_json(runner_lock_path(P.profile.workspace))
    if isinstance(cur, dict) and cur.get("pid") and pid_alive(cur.get("pid")):
        return f"a gate runner is live (pid {cur.get('pid')}, {cur.get('sprint') or '?'})"
    busy = getattr(P.adapter, "studio_busy", None)
    if callable(busy):
        try:
            why = busy(tid)
        except Exception as e:                                   # noqa: BLE001
            return f"studio_busy check failed: {e}"[:200]
        if why:
            return why
    return ""


def request_studio_reopen(c, P, t: dict) -> tuple[str, str]:
    """(job id, '') when the client started a reopen, else ('', why not)."""
    from . import client_api
    try:
        from . import slots
        if slots.slot_for(c, P) is not None:
            return "", "a slot-bound track's Studio is its slot's, never the workstation's"
    except Exception:                                            # noqa: BLE001
        pass
    st = client_api.studio_settings()
    if not st:
        return "", "no Foundry Client with Studio auto-reopen here"
    if not st.get("auto_reopen"):
        return "", "studio.auto_reopen is off in the Foundry Client"
    why = studio_guard(c, P, t)
    if why:
        return "", why
    res = client_api.studio_reopen(t["task_id"])
    if res.get("error") or not res.get("job"):
        return "", str(res.get("error") or "the client returned no job")[:200]
    if res.get("state") == "failed":
        return "", str(res.get("result") or "the reopen failed at once")[:200]
    return str(res["job"]), ""


def repair(c, P, t: dict, sig: str, action: str) -> list[dict]:
    """The deterministic repairs for a stop signature, each journaled:
    [{repair, ok, detail}]. Never raises; never a model call."""
    out: list[dict] = []
    ws = P.profile.workspace

    def add(name, ok, detail=""):
        out.append({"repair": name, "ok": bool(ok), "detail": str(detail)[:300]})
    try:
        slot = None
        try:
            from . import slots
            slot = slots.slot_for(c, P)
        except Exception:                                        # noqa: BLE001
            slot = None
        if slot is not None:
            # 2026-10-31 AS-J05: a slot-bound track's stale sync reopened the WORKSTATION's TEST
            # Studio twice (11:00, 11:04: the main sprint's Studio closed, the slot unchanged).
            # A slot track's Studio / Rojo repairs run inside its slot, never on the workstation.
            if sig in ("gate_tooling",) + STUDIO_SYNC_SIGS or action == "verify":
                ok, detail = slots.resync(slot, restart_rojo=sig != "studio_reload")
                add("slot_resync", ok, f"slot {slot['id']}: {detail}")
            if out:
                c.store.event(P.id, t["task_id"], "auto_heal_repair", detail={
                    "signature": sig, "action": action, "repairs": out, "slot": slot["id"]})
            return out
        if sig in ("gate_tooling",) + STUDIO_SYNC_SIGS or action == "verify":
            lock = clear_stale_runner_lock(ws)
            if lock["cleared"]:
                add("clear_stale_runner_lock", True, f"dead pid {lock['pid']} ({lock['sprint']})")
                if lock["playing"]:
                    why = stop_playtest(P)
                    add("stop_playtest", not why, why or "started")
        if sig == "studio_reload":
            job, rwhy = request_studio_reopen(c, P, t)
            if job:
                out.append({"repair": "reopen_studio", "ok": True, "job": job,
                            "detail": f"client job {job}: the session asked for a fresh "
                                      f"Studio; reopening the TEST place"})
            else:
                add("reopen_studio", False, f"not reopened: {rwhy}")
            return out
        if sig in STUDIO_SYNC_SIGS:
            from . import client_api
            serving = client_api.component_serving("rojo")
            reason = " ".join(str(x or "") for x in (t.get("blocker"), (t.get("data") or {})
                                                       .get("blocker_detail")))
            if serving and "live=missing" in reason and "UNRESOLVED" in reason:
                # 2026-10-29 22:16: a track merge added check files and their mounts to
                # default.project.json; the running Rojo never re-reads its project file, so
                # Studio had none of them (live=missing) and every Studio run failed its
                # pre-play verify. Restart Rojo, then reopen the place so the plugin reconnects.
                why = client_api.restart_component("rojo")
                add("restart_rojo", not why, why or "restart requested: Studio was missing files "
                    "(live=missing), so Rojo reloads its project file")
                serving = "" if not why else serving
                if not why:
                    job, rwhy = request_studio_reopen(c, P, t)
                    if job:
                        out.append({"repair": "reopen_studio", "ok": True, "job": job,
                                    "detail": f"client job {job}: reopening the TEST place so "
                                              f"the Rojo plugin reconnects to the new session"})
                    else:
                        add("reopen_studio", False, f"not reopened: {rwhy}")
                    serving = None
            if serving:
                # a running Rojo is left alone: a restart opens a new session, and the
                # Studio plugin only reconnects on place open, so it would need a
                # Connect click by hand (2026-10-27, GF-GATE1 05:24)
                add("keep_rojo", True, f"Rojo is serving ({serving}); not restarted, a new "
                                       f"session would need a Connect click in Studio")
                # 0.145.0: reopen the TEST place instead (opt-in on the client), so
                # Rojo's Auto Reconnect connects without a click
                job, why = request_studio_reopen(c, P, t)
                if job:
                    out.append({"repair": "reopen_studio", "ok": True, "job": job,
                                "detail": f"client job {job}: closing and reopening the "
                                          f"TEST place in Studio"})
                else:
                    add("reopen_studio", False, f"not reopened: {why}")
            elif serving is not None and sig != "studio":
                why = client_api.restart_component("rojo")
                add("restart_rojo", not why, why or (
                    "restart requested through the Foundry Client"
                    + ("; the Studio plugin re-syncs the sources on reconnect"
                       if sig == "rojo_stale" else "")))
    except Exception as e:                                       # noqa: BLE001
        add("repair", False, f"{type(e).__name__}: {e}")
    if out:
        c.store.event(P.id, t["task_id"], "auto_heal_repair", detail={
            "signature": sig, "action": action, "repairs": out})
    return out


def step(c, P, t: dict) -> bool:
    """A BLOCKED task with a scheduled auto-heal: wait for its time, then retry.
    True when this handled the tick (the caller does nothing else)."""
    data = dict(t.get("data") or {})
    ah = data.get("auto_heal") or {}
    if not ah:
        return False
    pid, tid = P.id, t["task_id"]
    if t.get("blocker") != ah.get("blocker"):
        # the task was blocked again for another reason (or an operator edited it)
        data.pop("auto_heal", None)
        c.store.update_task(pid, tid, data=data)
        return False
    if c.clock() < float(ah.get("retry_at") or 0):
        return True
    if ah.get("studio_job"):
        if not _studio_job_settled(c, P, t, data, ah):
            return True                  # the Studio reopen is still running
        ah = {k: v for k, v in ah.items() if k not in ("studio_job", "studio_job_ts")}
        data["auto_heal"] = ah           # settled and journaled once (a wait asks again)
    sig = str(ah.get("signature") or "")
    if fault_present(c, P, sig) is True:
        _keep_waiting(c, P, t, data, ah)
        return True                      # still down: nothing is restarted or counted
    if ah.get("wait_since"):
        c.store.event(pid, tid, "infra_back", detail={
            "signature": sig, "waited_s": int(c.clock() - float(ah["wait_since"])),
            "checks": int(ah.get("waits") or 0)})
    held = getattr(c, "_held", None)
    if callable(held) and held(P, t, "auto-heal retry"):
        return True
    data.pop("auto_heal", None)
    res = f"auto-heal retry {ah.get('n')} ({ah.get('signature')}): {ah.get('action')}"
    if ah.get("action") == "verify":
        if c._acquire(P, t, c._workspace_locks(P)):
            return True                  # another writer holds the workspace: next tick
        c.store.transition(pid, tid, S.VERIFYING, {
            "resolution": res, "decision_id": f"auto_heal:{ah.get('key')}"},
            data=data, next_action=res[:500])
    else:
        data["free_retry"] = True
        c.store.transition(pid, tid, S.READY, {"resolution": res, "auto_heal": True},
                           data=data, next_action=res[:500])
    c.store.event(pid, tid, "auto_heal_retry", detail={"key": ah.get("key"),
                                                       "action": ah.get("action")})
    return True


def _keep_waiting(c, P, t: dict, data: dict, ah: dict) -> None:
    """The fault is still there at the retry time: look again in WAIT_RECHECK_S, ask for the
    repair again every WAIT_REPAIR_S (a Studio reopen; the client's own limits apply), say so
    on the task, and after [automation] infra_wait_notice_minutes raise the outage once."""
    pid, tid = P.id, t["task_id"]
    now = c.clock()
    sig = str(ah.get("signature") or "")
    fam = wait_family(sig)
    ah = dict(ah)
    first = not ah.get("wait_since")
    ah["wait_since"] = float(ah.get("wait_since") or now)
    ah["waits"] = int(ah.get("waits") or 0) + 1
    ah["retry_at"] = now + WAIT_RECHECK_S
    waited = now - ah["wait_since"]
    if now - float(ah.get("repair_ts") or ah["wait_since"]) >= WAIT_REPAIR_S:
        ah["repair_ts"] = now
        repairs = repair(c, P, t, sig, str(ah.get("action") or ""))
        job = next((r.get("job") for r in repairs if r.get("job")), "")
        if job:
            ah.update(studio_job=job, studio_job_ts=now)
    data["auto_heal"] = ah
    why = _fault_why(c, P, sig)
    note = (f"waiting for {WAIT_WHAT[fam]}"
            + (f" ({why})" if why else "")
            + f": down for {int(waited // 60)} min, checked every {int(WAIT_RECHECK_S)} s; "
              f"no attempt, session or Lead call is used")
    c.store.update_task(pid, tid, data=data, next_action=note[:500])
    if first:
        c.store.event(pid, tid, "infra_waiting", detail={"signature": sig, "family": fam,
                                                          "why": why, "key": ah.get("key")})
    _outage_notice(c, P, fam, sig, why, ah["wait_since"])


def _outage_notice(c, P, fam: str, sig: str, why: str, since: float) -> None:
    """One alert per outage, not per waiting task: raised when the oldest wait on it passed
    infra_wait_notice_minutes, and forgotten when nothing waits on it any more."""
    try:
        main = c.tracks.main_of(P)
    except Exception:                                            # noqa: BLE001
        main = P
    mins = float(auto_get(P, "infra_wait_notice_minutes") or 0)
    seen = c.__dict__.setdefault("_infra_outage", {})
    st = seen.get((main.id, fam))
    if st is None or c.clock() - float(st.get("last") or 0) > OUTAGE_FORGET_S:
        st = seen[(main.id, fam)] = {"since": since, "noticed": False}
    st["since"] = min(st["since"], since)
    st["last"] = c.clock()
    if mins <= 0 or st["noticed"] or c.clock() - st["since"] < mins * 60:
        return
    st["noticed"] = True
    waiting = sorted(t["task_id"] for t in c.store.tasks(main.id)
                     if t["state"] == S.BLOCKED and wait_family(str(
                         ((t.get("data") or {}).get("auto_heal") or {}).get("signature")
                         or "")) == fam and ((t.get("data") or {}).get("auto_heal")
                                             or {}).get("wait_since"))
    down = int((c.clock() - st["since"]) // 60)
    text = (f"{WAIT_WHAT[fam][0].upper() + WAIT_WHAT[fam][1:]} has not answered for {down} "
            f"min" + (f" ({why})" if why else "") + f". Waiting on it: "
            f"{', '.join(waiting) or 'nothing right now'}. They restart by themselves when it "
            f"is back; no attempt, session or Lead call is used meanwhile."
            + (" A reopen is asked for every 15 min; if Studio is open, check that it shows "
               "the TEST place and that its MCP plugin is connected." if fam == "studio" else ""))
    c.store.event(main.id, waiting[0] if waiting else "", "operator_notice", detail={
        "text": text, "kind": "infra_outage", "alert_class": "watch",
        "title": f"{WAIT_WHAT[fam]} has been down for {down} min", "family": fam,
        "signature": sig, "waiting": waiting})


def _studio_job_settled(c, P, t: dict, data: dict, ah: dict) -> bool:
    """False while the client's Studio reopen job runs; True once it ended (done,
    failed or lost), journaled once. The retry goes ahead either way: a reopen that
    did not help shows up as the same stop again, and the retry count escalates it."""
    from . import client_api
    jid = str(ah.get("studio_job"))
    j = client_api.studio_job(jid)
    age = c.clock() - float(ah.get("studio_job_ts") or 0)
    if j and j.get("state") == "running" and age < REOPEN_JOB_MAX_S:
        return False
    if j is None:
        state, result = "failed", "the client no longer knows the job (restarted?)"
    elif j.get("state") == "running":
        state, result = "failed", f"still running after {int(age)} s"
    else:
        state, result = str(j.get("state")), str(j.get("result") or "")
    c.store.event(P.id, t["task_id"], "studio_reopened" if state == "done"
                  else "studio_reopen_failed",
                  detail={"job": jid, "result": result[:300],
                          "steps": [s.get("text") for s in (j or {}).get("steps") or []][-8:]})
    return True

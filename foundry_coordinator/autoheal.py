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
    ("studio", re.compile(r"\bstudio\b[^.\n]{0,80}(?:not connected|disconnected|not "
                          r"running|plugin[^.\n]{0,30}(?:connect|timeout)|mcp[^.\n]{0,40}"
                          r"(?:unavailable|not connected|timed out))", re.I)),
    ("python_path", re.compile(r"No module named '?foundry_coordinator|foundry_coordinator"
                               r"[^.\n]{0,30}not importable", re.I)),
    ("context_overflow", re.compile(r"ContextOverflowError|Compaction exhausted|context "
                                    r"(?:window )?(?:exceeded|overflow)|exceeds the available "
                                    r"context size", re.I)),
    ("kilo_auth", re.compile(r"Kilo[^.\n]{0,40}\b401\b", re.I)),
)
# stop classes whose blocker is the gate / check tooling, not the code
VERIFY_CLASSES = ("fast_lane_gate_error", "checks_unknown")
# stop classes of a session that may have died on infrastructure
SESSION_CLASSES = ("session_blocked", "session_needs_human", "session_error")
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
    return None


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
    if n >= retries:
        c.store.event(pid, tid, "auto_heal_exhausted", detail={
            "key": key, "retries": n, "action": action, "reason": reason[:300]})
        return None
    delay = min(MAX_BACKOFF, int(auto_get(P, "infra_backoff_seconds") or 0) * (2 ** n))
    now = c.clock()
    counts[key] = n + 1
    blocker = (f"auto-heal {n + 1}/{retries} ({sig}): "
               + ("re-running the gate/checks" if action == "verify"
                  else "restarting the session (no attempt charged)")
               + f" in {delay} s; was: {reason}")[:1000]
    data.update(auto_heal_counts=counts, auto_heal={
        "key": key, "n": n + 1, "action": action, "signature": sig, "class": cls,
        "retry_at": now + delay, "blocker": blocker, "reason": reason[:500],
        "verification_id": evidence.get("verification_id") or fields.get("verification_id")
        or ""})
    ev = {**evidence, "blocker": blocker, "auto_heal": True}
    try:
        S.check(cur["state"], S.BLOCKED, ev)
    except S.TransitionError:
        return None
    extra = {k: fields[k] for k in ("verification_id", "head_sha") if fields.get(k)}
    c.store.release(pid, tid)
    out = c.store.transition(pid, tid, S.BLOCKED, ev, blocker=blocker, data=data,
                             next_action=blocker[:500], **extra)
    repairs = repair(c, P, t, sig, action)        # W1.8: fix what can be fixed, then retry
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
        "key": key, "n": n + 1, "of": retries, "action": action, "signature": sig,
        "class": cls, "delay_s": delay, "reason": reason[:300], "repairs": repairs})
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


def stop_playtest(P) -> str:
    """`studio_gate stop-playtest` in the background (one community call) so Studio
    is back in Edit mode after a dead runner. '' when started, else why not."""
    try:
        from .fast_lane import ROUTER_ROOT, settings
        gm = str(settings(P)["gate_module"]).strip()
    except Exception as e:                                   # noqa: BLE001
        return f"no gate module: {e}"[:200]
    if gm.endswith(".py"):
        return "custom gate script: no known stop-playtest command"
    try:
        subprocess.Popen([sys.executable, "-m", gm, "--repo", P.profile.workspace, "stop-playtest"],
                         cwd=ROUTER_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 if os.name == "nt" else 0)
    except OSError as e:
        return f"stop-playtest: {e}"[:200]
    return ""


# stops a Studio reopen can fix (the plugin lost Rojo, Studio not connected)
STUDIO_SYNC_SIGS = ("rojo_connect", "rojo", "rojo_stale", "studio")
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
        if sig in ("gate_tooling",) + STUDIO_SYNC_SIGS or action == "verify":
            lock = clear_stale_runner_lock(ws)
            if lock["cleared"]:
                add("clear_stale_runner_lock", True, f"dead pid {lock['pid']} ({lock['sprint']})")
                if lock["playing"]:
                    why = stop_playtest(P)
                    add("stop_playtest", not why, why or "started")
        if sig in STUDIO_SYNC_SIGS:
            from . import client_api
            serving = client_api.component_serving("rojo")
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
            elif sig != "studio":
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

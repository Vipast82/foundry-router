"""Infrastructure before and around implementer sessions (revamp B12).

  preflight      before a session starts: Kilo is reachable and has the persona,
                 plus the profile's endpoints ([automation] preflight_endpoints:
                 Rojo, the local model, ...) and the adapter's own live checks
                 (adapter.infra_checks(), e.g. the Studio MCP). A failure starts
                 nothing and costs nothing: the task waits with a doubling
                 backoff (60 s .. 15 min) and retries by itself; after
                 [automation] infra_retry_max checks it tells the operator once
                 (operator_notice) and keeps retrying.
  free retries   a session that died on infrastructure (prompt not admitted, Kilo
                 5xx, connection reset, Rojo / Studio down, context overflow)
                 costs no attempt; the next session waits for the backoff.
                 Bounded: after infra_retry_max infra failures in a row the
                 failure counts as an ordinary attempt again.
  context ceiling a session whose context passed [automation]
                 context_ceiling_tokens and stopped before DONE is not continued:
                 a fresh session takes over from the committed state with a short
                 handoff (free), instead of compacting until Kilo gives up.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from .automation import get as auto_get

MAX_BACKOFF = 900
LOCAL_FALLBACK_S = 3600          # a frontier persona seen serving on the local model
CACHE_SECONDS = 30.0


def _endpoint(spec: str) -> tuple[str, str]:
    name, _, url = str(spec).partition("=")
    return (name.strip(), url.strip()) if url else (spec.strip(), spec.strip())


def check_endpoint(url: str, timeout: float = 3.0) -> str:
    """'' when reachable, else why not. tcp://host:port or http(s)://... (any HTTP
    answer below 500 counts as up)."""
    u = urlparse(url)
    try:
        if u.scheme == "tcp":
            with socket.create_connection((u.hostname or "127.0.0.1", int(u.port or 0)),
                                          timeout=timeout):
                return ""
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"),
                                    timeout=timeout) as r:
            return "" if int(getattr(r, "status", 200)) < 500 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return "" if e.code < 500 else f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return f"{type(e).__name__}: {e}"[:200]


def router_probe(P) -> list[str]:
    """["router=<base>/health"] when the project talks to a Foundry router (the
    profile's [foundry] url, through the project's Foundry client) and
    preflight_endpoints does not name a router already. Every model call of a
    session goes through the router: a session started while it is down only
    dies on its first call."""
    if not str(((getattr(P.profile, "foundry", None) or {}).get("url")) or "").strip():
        return []
    url = str(getattr(getattr(P, "foundry", None), "url", "") or "").strip()
    if not url.startswith(("http://", "https://")):
        return []
    named = {_endpoint(s)[0].lower() for s in auto_get(P, "preflight_endpoints") or []}
    if named & {"router", "foundry"}:
        return []
    base = url.rstrip("/")
    for tail in ("/v1", "/api"):
        if base.endswith(tail):
            base = base[:-len(tail)]
    return [f"router={base}/health"]


def preflight(c, P, persona: str) -> list[dict]:
    """The failing infra checks ([] = ready). Cached for CACHE_SECONDS per project
    so a waiting task doesn't re-probe every tick."""
    if not auto_get(P, "preflight"):
        return []
    cache = c.__dict__.setdefault("_infra_cache", {})
    hit = cache.get((P.id, persona))
    now = c.clock()
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    bad: list[dict] = []
    ws = P.profile.workspace
    pf = getattr(P.client, "preflight", None)
    if callable(pf):
        try:
            for level, name, detail in pf(ws) or []:
                if level == "FAIL":
                    bad.append({"name": f"kilo.{name}", "detail": str(detail)[:300]})
        except Exception as e:                                   # noqa: BLE001
            bad.append({"name": "kilo", "detail": f"{type(e).__name__}: {e}"[:300]})
    mc = getattr(P.client, "model_config_problem", None)
    if callable(mc) and not bad and persona:
        try:
            why = mc(ws, persona)
        except Exception as e:                                   # noqa: BLE001
            why = f"Kilo config unreadable: {e}"
        if why:
            bad.append({"name": "kilo.persona", "detail": str(why)[:300]})
    for spec in [*router_probe(P), *(auto_get(P, "preflight_endpoints") or [])]:
        name, url = _endpoint(spec)
        why = check_endpoint(url)
        if why:
            bad.append({"name": name, "detail": f"{url}: {why}"})
    extra = getattr(P.adapter, "infra_checks", None)
    if callable(extra):
        try:
            for r in extra() or []:
                if r.get("ok") is False:
                    bad.append({"name": str(r.get("name")), "detail": str(r.get("detail"))[:300]})
        except Exception as e:                                   # noqa: BLE001
            bad.append({"name": "adapter.infra_checks", "detail": f"{e}"[:300]})
    cache[(P.id, persona)] = (now, bad)
    return bad


def waiting(c, P, t: dict) -> Optional[str]:
    """The note while a scheduled infra retry is not due yet (else None)."""
    w = ((t.get("data") or {}).get("infra_wait") or {})
    if w and c.clock() < float(w.get("retry_at") or 0):
        return (f"infrastructure not ready ({w.get('why') or 'retrying'}); next check in "
                f"{int(float(w['retry_at']) - c.clock())} s (no attempt is used)")
    return None


def _schedule(c, P, t: dict, why: str, kind: str) -> dict:
    data = dict(c.store.task(P.id, t["task_id"])["data"] or {})
    w = dict(data.get("infra_wait") or {})
    n = int(w.get("n") or 0)
    delay = min(MAX_BACKOFF, max(1, int(auto_get(P, "infra_backoff_seconds") or 0)) * (2 ** n))
    w = {"n": n + 1, "retry_at": c.clock() + delay, "why": why[:300], "kind": kind,
         "since": w.get("since") or c.clock(), "noticed": w.get("noticed")}
    lim = int(auto_get(P, "infra_retry_max") or 0)
    if lim and n + 1 >= lim and not w.get("noticed"):
        w["noticed"] = True
        c.store.event(P.id, t["task_id"], "operator_notice", detail={
            "text": f"{t['task_id']}: infrastructure still not ready after {n + 1} checks "
                    f"({why[:200]}); the coordinator keeps retrying every "
                    f"{MAX_BACKOFF // 60} min at most and uses no attempt", "kind": kind})
    c.store.merge_data(P.id, t["task_id"], infra_wait=w)
    c.store.event(P.id, t["task_id"], "infra_wait", detail={**w, "delay_s": delay})
    return w


def preflight_blocks(c, P, t: dict, persona: str) -> bool:
    """Called before an implementer session starts. True = not ready (a retry is
    scheduled; nothing was started or charged)."""
    bad = preflight(c, P, persona)
    if not bad:
        if (t.get("data") or {}).get("infra_wait", {}).get("kind") == "preflight":
            c.store.merge_data(P.id, t["task_id"], infra_wait=None)
            c.store.event(P.id, t["task_id"], "infra_ready", detail={})
        return False
    why = "; ".join(f"{b['name']}: {b['detail']}" for b in bad)[:500]
    w = _schedule(c, P, t, why, "preflight")
    c._note(P, t, f"preflight failed ({why}); retry in {int(w['retry_at'] - c.clock())} s "
                  f"(no attempt is used)")
    return True


def session_failure(c, P, t: dict, why: str, free: bool) -> bool:
    """A session ended on `why`. Returns the free flag to use: an infra failure is
    free (and the next start waits for the backoff) until infra_retry_max of them
    in a row; anything else keeps `free` as given."""
    from .autoheal import infra_signature
    sig = infra_signature(why) or ("kilo_not_admitted" if "admission window" in why else "")
    if not sig:
        return free
    data = c.store.task(P.id, t["task_id"])["data"] or {}
    n = int(data.get("infra_failures") or 0) + 1
    lim = int(auto_get(P, "infra_retry_max") or 0)
    c.store.merge_data(P.id, t["task_id"], infra_failures=n)
    if sig == "local_busy":
        # 2026-10-30: Claude weekly 100% + ChatGPT at its cap -> GF-LOOP1 and MV2-9 fell to the
        # local model held by AS-D14 and failed a session every few minutes. The persona is
        # serving locally now: queue for `gpu` like a local sprint (engine._session_locks).
        c.store.merge_data(P.id, t["task_id"], local_fallback_until=c.clock() + LOCAL_FALLBACK_S)
        c.store.event(P.id, t["task_id"], "local_fallback_gpu", detail={
            "until": c.clock() + LOCAL_FALLBACK_S, "why": why[:200]})
    _schedule(c, P, t, why, "session")
    if sig and lim and n > lim:
        c.store.event(P.id, t["task_id"], "infra_retry_exhausted", detail={
            "failures": n, "limit": lim, "why": why[:300]})
        return free
    if sig and not free:
        c.store.event(P.id, t["task_id"], "infra_free_retry", detail={
            "signature": sig, "n": n, "why": why[:300]})
    return True


def session_ok(c, P, tid: str) -> None:
    """A session reached an ordinary idle: the infra failure streak is over."""
    t = c.store.task(P.id, tid) or {}
    data = t.get("data") or {}
    if data.get("infra_failures") or (data.get("infra_wait") or {}).get("kind") == "session":
        c.store.merge_data(P.id, tid, infra_failures=None,
                           infra_wait=None if (data.get("infra_wait") or {}).get("kind")
                           == "session" else data.get("infra_wait"))


def over_ceiling(P, s, rep: dict, persona: str = "") -> Optional[str]:
    """The handoff reason when an idle session's context passed the ceiling and
    it stopped before DONE (else None). The ceiling is a share of the session
    model's usable window (context_window.py), else context_ceiling_tokens."""
    from . import context_window as CW
    ceil = CW.ceiling(P, persona)
    ctx = getattr(s, "context", None)
    if not ceil or ctx is None or int(ctx) < ceil:
        return None
    if str((rep or {}).get("status") or "").upper() in ("DONE", "BLOCKED", "NEEDS_HUMAN"):
        return None                  # finished, or a real blocker: a fresh session won't help
    summary = str((rep or {}).get("summary") or (rep or {}).get("next") or "").strip()[:600]
    return (f"context ceiling: the previous session reached {int(ctx)} tokens (ceiling "
            f"{ceil}) and was handed off; continue from the committed state and the plan's "
            f"checklist/NEXT line" + (f". Its last report: {summary}" if summary else ""))


# -- C (2026-10-28): outages are infra waits, not failures --------------------------------

ACTIVE_STATES = ("IMPLEMENTING", "VERIFYING", "REVIEWING", "CLOSING")


def active_seconds(c, t: dict, now: Optional[float] = None) -> float:
    """Seconds the task spent in ACTIVE_STATES since its started_ts, summed from its
    transitions (their timestamps are wall time). Idle, parked, paused, blocked and
    waiting time never counts against max_task_minutes."""
    import time as _time
    started = float(t.get("started_ts") or 0)
    rows = c.store.history(t["project_id"], t["task_id"])
    end_now = float(now if now is not None else _time.time())
    total = 0.0
    for i, r in enumerate(rows):
        if r["to_state"] not in ACTIVE_STATES:
            continue
        a = float(r["ts"])
        b = float(rows[i + 1]["ts"]) if i + 1 < len(rows) else end_now
        a = max(a, started)
        if b > a:
            total += b - a
    return total


def kilo_retry(c, P, t: dict, sid: str, s, phase: str) -> bool:
    """Kilo reports the session in "retry" (its provider call keeps failing: the
    router or the model is down). The start of a continuous retry is recorded per
    session (task data kilo_retry); after [limits] infra_retry_abort_seconds (300;
    0 = never) the session is stopped and the task waits like any infra failure:
    free, with the doubling infra backoff. True = stopped this poll."""
    pid, tid = P.id, t["task_id"]
    rs = (t.get("data") or {}).get("kilo_retry") or {}
    if not getattr(s, "retry", False):
        if rs:
            c.store.merge_data(pid, tid, kilo_retry=None)
        return False
    now = c.clock()
    err = str(getattr(s, "error", "") or "")[:300]
    if rs.get("session_id") != sid:
        c.store.merge_data(pid, tid, kilo_retry={"session_id": sid, "since": now,
                                                 "error": err})
        c.store.event(pid, tid, "kilo_retry", session_id=sid,
                      detail={"phase": phase, "error": err})
        return False
    limit = int(P.profile.limit("infra_retry_abort_seconds", 300) or 0)
    held = now - float(rs.get("since") or now)
    if not limit or held < limit:
        return False
    why = (f"Kilo kept retrying the model call for {int(held)} s (router or model outage)"
           + (f": {err}" if err else ""))
    P.client.abort(sid, directory=P.profile.workspace)
    c._end_session(P, sid, "infra_retry")
    c.store.merge_data(pid, tid, kilo_retry=None)
    c.store.event(pid, tid, "kilo_retry_abort", session_id=sid,
                  detail={"phase": phase, "seconds": int(held), "error": err})
    if phase == "closeout":
        # the CLOSE session restarts after the backoff; no closeout attempt is used
        c._release_session(P, t)
        c.store.merge_data(pid, tid, closeout_session=None, closeout_done=None)
        w = _schedule(c, P, t, why, "closeout")
        c._note(P, t, f"{why}; a fresh closeout session starts in "
                      f"{int(w['retry_at'] - c.clock())} s (no attempt is used)")
        return True
    c._session_failed(P, t, why, phase, free=True)
    return True


def closeout_waiting(c, P, t: dict) -> Optional[str]:
    """The note while a closeout infra wait (kilo_retry) is not due yet, else None
    (a due wait is cleared)."""
    w = ((t.get("data") or {}).get("infra_wait") or {})
    if w.get("kind") != "closeout":
        return None
    note = waiting(c, P, t)
    if note is None:
        c.store.merge_data(P.id, t["task_id"], infra_wait=None)
    return note


# -- push retry: a failed push is an infra wait in CLOSING, never a finding ----------------

PUBLISH_CHECK = "coordinator.published"


def push_failed_only(results: list) -> bool:
    """Every required result passed except the coordinator's own push."""
    bad = [r for r in results if r.required and r.result != "PASS"]
    return bool(bad) and all(r.check == PUBLISH_CHECK for r in bad)


def push_wait(c, P, t: dict, head: str, results: list, why: str) -> dict:
    """Schedule the next push of `head` (doubling infra backoff, capped at 15 min;
    an operator notice after infra_retry_max tries, and it keeps retrying). The
    passing closeout results are kept so the retry needs no checks again."""
    from dataclasses import asdict
    data = c.store.task(P.id, t["task_id"])["data"] or {}
    pw = dict(data.get("push_wait") or {})
    if pw.get("head") != head:
        pw = {"head": head, "n": 0, "since": c.clock()}
    n = int(pw.get("n") or 0)
    delay = min(MAX_BACKOFF, max(1, int(auto_get(P, "infra_backoff_seconds") or 0)) * (2 ** n))
    keep = []
    for r in results:
        if r.check == PUBLISH_CHECK:
            continue
        d = asdict(r)
        d["detail"] = str(d.get("detail") or "")[:500]
        keep.append(d)
    pw.update(n=n + 1, retry_at=c.clock() + delay, why=why[:500], results=keep)
    lim = int(auto_get(P, "infra_retry_max") or 0)
    if lim and n + 1 >= lim and not pw.get("noticed"):
        pw["noticed"] = True
        c.store.event(P.id, t["task_id"], "operator_notice", detail={
            "text": f"{t['task_id']}: the push of {head[:12]} still fails after {n + 1} tries "
                    f"({why[:200]}); the coordinator keeps retrying every "
                    f"{MAX_BACKOFF // 60} min at most. Closeout passed; nothing else waits.",
            "kind": "push"})
    c.store.merge_data(P.id, t["task_id"], push_wait=pw)
    c.store.event(P.id, t["task_id"], "push_wait", detail={
        "head": head, "n": n + 1, "delay_s": delay, "why": why[:300]})
    return pw


def push_results(pw: dict) -> list:
    """The kept closeout results of a push wait, as VerificationResults."""
    from .adapters.base import VerificationResult
    out = []
    for d in pw.get("results") or []:
        try:
            out.append(VerificationResult(**d))
        except TypeError:
            continue
    return out

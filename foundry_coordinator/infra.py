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
    for spec in auto_get(P, "preflight_endpoints") or []:
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


def over_ceiling(P, s, rep: dict) -> Optional[str]:
    """The handoff reason when an idle session's context passed the ceiling and
    it stopped before DONE (else None)."""
    ceil = int(auto_get(P, "context_ceiling_tokens") or 0)
    ctx = getattr(s, "context", None)
    if not ceil or ctx is None or int(ctx) < ceil:
        return None
    if str((rep or {}).get("status") or "").upper() in ("DONE", "BLOCKED", "NEEDS_HUMAN"):
        return None                  # finished, or a real blocker: a fresh session won't help
    summary = str((rep or {}).get("summary") or (rep or {}).get("next") or "").strip()[:600]
    return (f"context ceiling: the previous session reached {int(ctx)} tokens (ceiling "
            f"{ceil}) and was handed off; continue from the committed state and the plan's "
            f"checklist/NEXT line" + (f". Its last report: {summary}" if summary else ""))

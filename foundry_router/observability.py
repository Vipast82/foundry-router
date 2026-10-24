"""Who uses the router and how the brain performs (v0.141.0).

client_label     a keyed caller's client name, or "untracked · <IP>" for a
                 request without a key (one entry per IP)
brain_metrics    brain_calls over a window: calls per hour, latency p50/p95,
                 tokens/s, outcome counts and rates (failed / skipped = the
                 request fell back to the static rule), by purpose, a time
                 series, recent failures
clients_activity per client (tracked or untracked): requests, errors, tokens,
                 personas and models used, IPs and user agents, last seen, a
                 request time series, and what it is running right now
live             in-flight backend calls and brain calls with who asked
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

UNTRACKED = "untracked"


def client_label(client_id: Optional[str], addr: Optional[str] = None) -> str:
    cid = (client_id or "").strip()
    if cid and cid != "anonymous":
        return cid
    return f"{UNTRACKED} · {addr or 'unknown address'}"


def parse_label(label: str) -> tuple[str, Optional[str]]:
    """(client_id, addr or None) behind a label."""
    if label.startswith(f"{UNTRACKED} · "):
        addr = label.split(" · ", 1)[1]
        return "anonymous", (None if addr == "unknown address" else addr)
    return label, None


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _pct(vals: list, q: float) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))], 1)


def _bucket_of(ts: str, start: datetime, step_s: float) -> int:
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return -1
    return int((t - start).total_seconds() // step_s)


def _buckets(hours: float, n: int = 24) -> tuple[datetime, float, list[str]]:
    step = hours * 3600 / n
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    return start, step, [(start + timedelta(seconds=step * i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------------------- brain

def brain_metrics(db, hours: float = 24, buckets: int = 24) -> dict:
    hours = max(0.05, min(24 * 30.0, float(hours)))
    rows = db.query("SELECT ts, purpose, model, latency_ms, prompt_tokens, completion_tokens, "
                    "tps, outcome, reason, request_id, persona, client_id, client_addr "
                    "FROM brain_calls WHERE ts >= ? ORDER BY id", (_since(hours),))
    n = len(rows)
    outcomes: dict = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    called = [r for r in rows if r["outcome"] != "skipped"]
    ok = [r for r in called if r["outcome"] in ("ok", "retried")]
    start, step, labels = _buckets(hours, buckets)
    series = [{"t": t, "calls": 0, "ok": 0, "failed": 0, "skipped": 0, "lat": []}
              for t in labels]
    for r in rows:
        i = _bucket_of(r["ts"], start, step)
        if 0 <= i < len(series):
            s = series[i]
            s["calls"] += 1
            key = "ok" if r["outcome"] in ("ok", "retried") else r["outcome"]
            if key in s:
                s[key] += 1
            if r["outcome"] in ("ok", "retried"):
                s["lat"].append(r["latency_ms"])
    for s in series:
        s["p50_ms"] = _pct(s.pop("lat"), .5)
    purposes: dict = {}
    for r in rows:
        d = purposes.setdefault(r["purpose"] or "?", {"calls": 0, "lat": [], "failed": 0,
                                                      "skipped": 0})
        d["calls"] += 1
        if r["outcome"] in ("ok", "retried"):
            d["lat"].append(r["latency_ms"])
        elif r["outcome"] in ("failed", "skipped"):
            d[r["outcome"]] += 1
    reasons: dict = {}
    for r in rows:
        if r["outcome"] in ("failed", "skipped") and r["reason"]:
            k = r["reason"][:120]
            reasons[k] = reasons.get(k, 0) + 1
    return {
        "hours": hours, "calls": n, "called": len(called),
        "per_hour": round(n / hours, 1),
        "outcomes": outcomes,
        "failure_rate": round(outcomes.get("failed", 0) / len(called), 3) if called else None,
        "fallback_rate": round((outcomes.get("failed", 0) + outcomes.get("skipped", 0)) / n, 3)
        if n else None,
        "latency_ms": {"p50": _pct([r["latency_ms"] for r in ok], .5),
                       "p95": _pct([r["latency_ms"] for r in ok], .95)},
        "tps_p50": _pct([r["tps"] for r in ok], .5),
        "tokens": {"prompt": sum(int(r["prompt_tokens"] or 0) for r in rows),
                   "completion": sum(int(r["completion_tokens"] or 0) for r in rows)},
        "by_purpose": {k: {"calls": v["calls"], "p50_ms": _pct(v["lat"], .5),
                           "p95_ms": _pct(v["lat"], .95), "failed": v["failed"],
                           "skipped": v["skipped"]} for k, v in purposes.items()},
        "models": sorted({r["model"] for r in rows if r["model"]}),
        "series": series,
        "top_reasons": sorted(({"reason": k, "count": v} for k, v in reasons.items()),
                              key=lambda x: -x["count"])[:8],
        "recent_failures": [{"ts": r["ts"], "purpose": r["purpose"], "outcome": r["outcome"],
                             "reason": r["reason"], "request_id": r["request_id"],
                             "client": client_label(r["client_id"], r["client_addr"])}
                            for r in reversed(rows) if r["outcome"] in ("failed",)][:10],
    }


# ---------------------------------------------------------------------------- clients

def live(svc) -> dict:
    """What is running right now: backend calls and brain calls, with who asked."""
    calls = []
    try:
        for c in svc.pool.active_requests():
            calls.append({**c, "client_label": client_label(c.get("client"), c.get("addr"))})
    except Exception:                                            # noqa: BLE001
        pass
    brain = []
    try:
        for b in svc.brain.active():
            brain.append({**b, "client_label": client_label(b.get("client_id"),
                                                            b.get("client_addr"))})
    except Exception:                                            # noqa: BLE001
        pass
    return {"calls": calls, "brain": brain, "ts": time.time()}


def clients_activity(svc, hours: float = 24, buckets: int = 24) -> dict:
    hours = max(0.05, min(24 * 30.0, float(hours)))
    cfg = svc.config_store.config
    rows = svc.db.query(
        "SELECT ts, client_id, client_addr, user_agent, persona, served_model, served_backend, "
        "status, outcome, duration_ms, models_used FROM request_log WHERE ts >= ? ORDER BY id",
        (_since(hours),))
    start, step, labels = _buckets(hours, buckets)
    out: dict = {}

    def entry(label):
        return out.setdefault(label, {
            "client": label, "tracked": not label.startswith(UNTRACKED), "requests": 0,
            "errors": 0, "input_tokens": 0, "output_tokens": 0, "last_seen": None,
            "addrs": {}, "agents": {}, "personas": {}, "models": {}, "durations": [],
            "series": [0] * len(labels), "live": 0})
    for r in rows:
        label = client_label(r["client_id"], r["client_addr"])
        e = entry(label)
        e["requests"] += 1
        e["errors"] += int((r["outcome"] or r["status"] or "ok") in ("error", "timeout"))
        try:
            used = json.loads(r["models_used"] or "[]")
        except ValueError:
            used = []
        e["input_tokens"] += sum(int(u.get("prompt_tokens") or 0) for u in used
                                 if isinstance(u, dict))
        e["output_tokens"] += sum(int(u.get("completion_tokens") or 0) for u in used
                                  if isinstance(u, dict))
        e["last_seen"] = r["ts"]
        for k, v in (("addrs", r["client_addr"]), ("agents", r["user_agent"]),
                     ("personas", r["persona"]), ("models", r["served_model"])):
            if v:
                e[k][v] = e[k].get(v, 0) + 1
        e["durations"].append(r["duration_ms"])
        i = _bucket_of(r["ts"], start, step)
        if 0 <= i < len(labels):
            e["series"][i] += 1
    for c in live(svc)["calls"]:
        entry(c["client_label"])["live"] += 1
    # configured keys that haven't called in the window still show (count 0)
    seen = {}
    try:
        from .client_keys import activity
        seen = activity.snapshot().get("seen") or {}
    except Exception:                                            # noqa: BLE001
        pass
    for c in cfg.clients or []:
        e = entry(c.name)
        e["configured"] = True
        e["enabled"] = c.enabled
        e["scopes"] = list(c.scopes or [])
        s = seen.get(c.name) or {}
        if s.get("addr"):
            e["addrs"].setdefault(s["addr"], 0)
        if s.get("agent"):
            e["agents"].setdefault(s["agent"], 0)
        if s.get("last_ts") and not e["last_seen"]:
            e["last_seen"] = datetime.fromtimestamp(s["last_ts"], timezone.utc).isoformat()
    clients = []
    for e in out.values():
        d = e.pop("durations")
        e["p50_s"] = round((_pct(d, .5) or 0) / 1000, 1)
        for k in ("addrs", "agents", "personas", "models"):
            e[k] = [x for x, _ in sorted(e[k].items(), key=lambda kv: -kv[1])][:8]
        clients.append(e)
    clients.sort(key=lambda e: (-e["live"], -e["requests"], e["client"]))
    return {"hours": hours, "buckets": labels, "clients": clients,
            "untracked": sum(1 for e in clients if not e["tracked"]),
            "total_requests": len(rows)}

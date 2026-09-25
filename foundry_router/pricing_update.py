"""Automatic price updates for the cost calculator.

Hand-maintaining a table of vendor prices goes stale within weeks. This module
refreshes it from OpenRouter's public model list (no key needed — the same feed
the registry already polls), which publishes per-token prompt / completion /
cache-read prices for Anthropic, OpenAI, Google, DeepSeek, xAI, Mistral … at
(essentially) the vendors' own list rates.

The one fuzzy step is matching an operator's service name ("Claude Opus 4.8")
to an OpenRouter id ("anthropic/claude-opus-4.8"):

  1. a remembered match (source_id) from a previous run — no guessing at all;
  2. the routing BRAIN, handed each service plus a short list of plausible
     candidates, answers with a JSON mapping (validated against the catalog —
     it can only pick ids that exist);
  3. when the brain is down / not configured / unsure, a deterministic token
     matcher (every word + version number of the service name must appear in
     the candidate).

Safety rails: locked rows are never touched; prices must be positive and sane
(< $1000 / 1M); a change of more than 10x is reported as suspicious and not
applied unless forced; every applied change is recorded in the Events log and
returned as an old -> new diff for the UI.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .db import Database, utcnow
from . import pricing

log = logging.getLogger("foundry.pricing_update")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
KV_AUTO = "pricing_auto_update"          # "1" / "0"
KV_DAYS = "pricing_update_days"          # interval in days
KV_LAST = "pricing_last_update"          # ISO timestamp of the last run
KV_LAST_REPORT = "pricing_last_report"   # JSON summary of the last run
MAX_PER_1M = 1000.0
SUSPICIOUS_RATIO = 10.0


def _per_1m(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 1e6, 4) if f >= 0 else None


async def fetch_catalog(client) -> list[dict]:
    """OpenRouter models -> [{id, name, input_per_1m, output_per_1m,
    cached_input_per_1m}] (paid text models only)."""
    r = await client.get(OPENROUTER_MODELS_URL, timeout=30)
    r.raise_for_status()
    out = []
    for m in r.json().get("data") or []:
        p = m.get("pricing") or {}
        pin, pout = _per_1m(p.get("prompt")), _per_1m(p.get("completion"))
        if not m.get("id") or not pin or not pout:
            continue                       # free / variable-priced / broken rows
        if str(m["id"]).endswith(":free"):
            continue
        out.append({"id": m["id"], "name": m.get("name") or m["id"],
                    "input_per_1m": pin, "output_per_1m": pout,
                    "cached_input_per_1m": _per_1m(p.get("input_cache_read"))})
    return out


_WORD = re.compile(r"[a-z]+|\d+(?:\.\d+)*")


def _tokens(s: str) -> list[str]:
    # "GPT-5.6 Luna" -> ["gpt", "5.6", "luna"]; "claude-opus-4-8" -> [..., "4", "8"]
    return _WORD.findall((s or "").lower().replace("_", " "))


def _norm_versions(tokens: list[str]) -> set[str]:
    """Versions written 4.8 / 4-8 / 48 should compare equal: keep both the
    dotted form and the digits-only form."""
    out = set()
    for t in tokens:
        out.add(t)
        if "." in t:
            out.add(t.replace(".", ""))
    return out


def candidates_for(service: str, catalog: list[dict], limit: int = 12) -> list[dict]:
    """Catalog entries sharing the most name tokens with the service."""
    want = _norm_versions(_tokens(service))
    scored = []
    for c in catalog:
        toks = _tokens(c["id"]) + _tokens(c["name"])
        # join split version digits: "4", "8" -> "4.8" and "48"
        have = _norm_versions(toks)
        for a, b in zip(toks, toks[1:]):
            if a.isdigit() and b.isdigit():
                have |= {f"{a}.{b}", f"{a}{b}"}
        score = len(want & have)
        if score:
            scored.append((score, -len(c["id"]), c))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [c for _, _, c in scored[:limit]]


def heuristic_match(service: str, catalog: list[dict]) -> Optional[str]:
    """Deterministic fallback: a candidate that contains EVERY word/version of
    the service name, preferring the shortest (least-qualified) id — so
    "Claude Sonnet 5" matches anthropic/claude-sonnet-5, not a -thinking or
    :beta variant. None when nothing contains all the words."""
    want = _tokens(service)
    if not want:
        return None
    best = None
    for c in candidates_for(service, catalog, limit=40):
        toks = _tokens(c["id"]) + _tokens(c["name"])
        have = _norm_versions(toks)
        for a, b in zip(toks, toks[1:]):
            if a.isdigit() and b.isdigit():
                have |= {f"{a}.{b}", f"{a}{b}"}
        if all(w in have or w.replace(".", "") in have for w in want):
            if best is None or len(c["id"]) < len(best["id"]):
                best = c
    return best["id"] if best else None


_MATCH_PROMPT = """You map paid AI API service names to OpenRouter model ids so their prices can be looked up.
For each service below pick the ONE candidate id that is the same model (same vendor, family, version and tier).
Prefer the plain model over ":beta", "-thinking", "-preview" or dated variants unless the service name says so.
If no candidate is clearly the same model, use null. Answer with ONLY a JSON object: {{"<service name>": "<id or null>", ...}}

{items}"""


async def brain_match(brain, services: list[str], catalog: list[dict]) -> dict[str, Optional[str]]:
    """Ask the brain to map service names to catalog ids. Only ids present in
    each service's candidate list are accepted. Raises on brain failure (the
    caller falls back to the heuristic)."""
    blocks, allowed = [], {}
    for name in services:
        cands = candidates_for(name, catalog)
        allowed[name] = {c["id"] for c in cands}
        if cands:
            blocks.append(f'Service "{name}" — candidates: ' +
                          ", ".join(f'{c["id"]} ({c["name"]})' for c in cands))
    if not blocks:
        return {}
    text = await brain.complete(_MATCH_PROMPT.format(items="\n".join(blocks)), aux=True)
    m = re.search(r"\{.*\}", text or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    out: dict[str, Optional[str]] = {}
    for name in services:
        v = data.get(name)
        out[name] = v if isinstance(v, str) and v in allowed.get(name, set()) else None
    return out


async def update_prices(db: Database, client, brain=None, *, force: bool = False,
                        dry_run: bool = False) -> dict:
    """Refresh every unlocked service's rates from OpenRouter. Returns a report:
    {updated:[{name, id, old, new}], unchanged, unmatched, locked,
     suspicious:[…], matcher, error}."""
    pricing.ensure_seed(db)
    report: dict = {"updated": [], "unchanged": [], "unmatched": [], "locked": [],
                    "suspicious": [], "matcher": "", "error": "", "ts": utcnow(),
                    "dry_run": dry_run}
    try:
        catalog = await fetch_catalog(client)
    except Exception as e:
        report["error"] = f"could not fetch OpenRouter prices: {e}"
        db.log_event("warning", "pricing", "automatic price update failed", report["error"])
        return report
    by_id = {c["id"]: c for c in catalog}
    rows = pricing.list_services(db)
    todo = []
    for r in rows:
        if r.get("locked"):
            report["locked"].append(r["name"])
        else:
            todo.append(r)

    # 1) remembered matches; 2) brain for the rest; 3) heuristic fallback.
    match: dict[str, Optional[str]] = {r["name"]: (r.get("source_id")
                                                   if r.get("source_id") in by_id else None)
                                       for r in todo}
    need = [n for n, v in match.items() if not v]
    used = []
    if need and brain is not None:
        try:
            got = await brain_match(brain, need, catalog)
            match.update({k: v for k, v in got.items() if v})
            used.append("brain")
        except Exception as e:
            log.info("brain price matching unavailable (%s) — using heuristic", e)
    still = [n for n, v in match.items() if not v]
    if still:
        for n in still:
            match[n] = heuristic_match(n, catalog)
        used.append("heuristic")
    report["matcher"] = " + ".join(used) or "remembered"

    now = utcnow()
    for r in todo:
        mid = match.get(r["name"])
        if not mid:
            report["unmatched"].append(r["name"])
            if not dry_run:
                db.execute("UPDATE service_pricing SET last_checked=? WHERE id=?", (now, r["id"]))
            continue
        c = by_id[mid]
        old = {"input_per_1m": r.get("input_per_1m"), "output_per_1m": r.get("output_per_1m"),
               "cached_input_per_1m": r.get("cached_input_per_1m")}
        new = {"input_per_1m": c["input_per_1m"], "output_per_1m": c["output_per_1m"],
               "cached_input_per_1m": (c["cached_input_per_1m"]
                                       if c["cached_input_per_1m"] is not None
                                       else r.get("cached_input_per_1m"))}
        entry = {"name": r["name"], "id": mid, "old": old, "new": new}
        if any(v is None or v <= 0 or v > MAX_PER_1M
               for v in (new["input_per_1m"], new["output_per_1m"])):
            report["suspicious"].append({**entry, "why": "price out of range"})
            continue
        jump = max((new[k] / old[k]) if old.get(k) else 1.0 for k in ("input_per_1m", "output_per_1m"))
        drop = max((old[k] / new[k]) if old.get(k) and new[k] else 1.0
                   for k in ("input_per_1m", "output_per_1m"))
        if not force and max(jump, drop) > SUSPICIOUS_RATIO:
            report["suspicious"].append({**entry, "why": f"{max(jump, drop):.0f}x change"})
            continue
        changed = any(abs((old.get(k) or 0) - (new[k] or 0)) > 1e-9 for k in new)
        if not dry_run:
            db.execute(
                "UPDATE service_pricing SET input_per_1m=?, output_per_1m=?, "
                "cached_input_per_1m=?, source='openrouter', source_id=?, last_checked=?"
                + (", updated_at=?" if changed else "") + " WHERE id=?",
                (new["input_per_1m"], new["output_per_1m"], new["cached_input_per_1m"],
                 mid, now, *( (now,) if changed else ()), r["id"]))
        (report["updated"] if changed else report["unchanged"]).append(entry)

    if not dry_run:
        db.kv_set(KV_LAST, now)
        db.kv_set(KV_LAST_REPORT, json.dumps({k: report[k] for k in (
            "ts", "matcher", "error")} | {k: len(report[k]) for k in (
                "updated", "unchanged", "unmatched", "locked", "suspicious")}))
        if report["updated"]:
            db.log_event("info", "pricing",
                         f"prices updated for {len(report['updated'])} service(s) "
                         f"(matcher: {report['matcher']})",
                         "; ".join(f"{u['name']}: in {u['old']['input_per_1m']}→"
                                   f"{u['new']['input_per_1m']}, out "
                                   f"{u['old']['output_per_1m']}→{u['new']['output_per_1m']}"
                                   for u in report["updated"])[:3900])
        if report["suspicious"]:
            db.log_event("warning", "pricing",
                         f"{len(report['suspicious'])} suspicious price change(s) NOT applied",
                         "; ".join(f"{s['name']}: {s['why']}" for s in report["suspicious"]))
    return report


def settings(db: Database) -> dict:
    try:
        last_report = json.loads(db.kv_get(KV_LAST_REPORT) or "null")
    except (TypeError, ValueError):
        last_report = None
    return {"auto": db.kv_get(KV_AUTO) == "1",
            "days": int(db.kv_get(KV_DAYS) or 7),
            "last_update": db.kv_get(KV_LAST) or "",
            "last_report": last_report}


def save_settings(db: Database, auto: Optional[bool] = None, days: Optional[int] = None) -> dict:
    if auto is not None:
        db.kv_set(KV_AUTO, "1" if auto else "0")
    if days is not None:
        db.kv_set(KV_DAYS, str(max(1, min(int(days), 90))))
    return settings(db)


def due(db: Database) -> bool:
    """Is a scheduled automatic update due now?"""
    from datetime import datetime, timedelta, timezone
    s = settings(db)
    if not s["auto"]:
        return False
    if not s["last_update"]:
        return True
    try:
        last = datetime.fromisoformat(s["last_update"])
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(days=s["days"])

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

Following the newest version (v0.181.119): a price table that keeps "Claude Opus
4.8" forever is stale the day Opus 5 ships, however fresh its prices. Each
matched row belongs to a model FAMILY: its OpenRouter id with the version taken
out ("anthropic/claude-opus-4.8" -> "anthropic/claude-opus", "openai/gpt-5.6-sol"
-> "openai/gpt-sol"). When the catalog lists a newer member of the family (by its
release date; batch and dated-snapshot ids are not members), the row moves to it:
new name, new id, new prices. A family that is renamed outright (Gemini 3.1 Pro ->
Gemini 4 Carbon) can't be followed; add that row by hand. Turn it off under
"Automatic price updates", or lock a row to keep it where it is.

New models join the list (v0.181.119): for each vendor already in the table, a
model family that isn't listed yet and had a release in the last 120 days is added
as a new row (Claude Fable 5.2, GPT-6 Astra, Gemini 4 Carbon), at most two per
vendor per run and 24 rows in all. Image, audio, embedding, preview and
experimental models are left out, and so is a "Pro" variant of a family that is
already there. A row you delete is remembered and not added again. Nothing is
ever removed automatically.

The schedule is on unless switched off, daily by default (it was opt-in, weekly).

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
KV_FOLLOW = "pricing_follow_latest"      # "0" = keep each row on the exact model it names
KV_ADD = "pricing_add_new"               # "0" = never add rows for newly released models
KV_DISMISSED = "pricing_dismissed_families"   # JSON list: families whose row was deleted
DEFAULT_DAYS = 1
NEW_WINDOW_DAYS = 120
MAX_NEW_PER_VENDOR = 2
MAX_ROWS = 24
# not chat models, or not a model you would price a coding session against
SKIP_WORDS = frozenset({"image", "audio", "tts", "embed", "embedding", "banana", "preview",
                        "exp", "experimental", "beta", "build", "safeguard", "guard", "oss",
                        "customtools", "agent", "latest", "realtime", "search", "transcribe",
                        "moderation", "vision"})
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
        try:
            created = int(m.get("created") or 0)
        except (TypeError, ValueError):
            created = 0
        out.append({"id": m["id"], "name": m.get("name") or m["id"], "created": created,
                    "input_per_1m": pin, "output_per_1m": pout,
                    "cached_input_per_1m": _per_1m(p.get("input_cache_read"))})
    return out


_VERSION = re.compile(r"^v?\d{1,3}(\.\d+)*$")
_SNAPSHOT = re.compile(r"^\d{4,}$")            # 0731, 1017, 2024, 20250514: a dated snapshot


def family_of(model_id: str) -> Optional[str]:
    """The model's family: its id without the version. None for ids that are
    variants of another id (":batch", ":thinking", dated snapshots).

      anthropic/claude-opus-4.8 -> anthropic/claude-opus
      openai/gpt-5.6-sol        -> openai/gpt-sol
      deepseek/deepseek-v4.1-flash -> deepseek/deepseek-flash
      deepseek/deepseek-v4.1-flash-1017, openai/gpt-6-sol:batch -> None"""
    mid = str(model_id or "")
    if not mid or ":" in mid or "/" not in mid:
        return None
    vendor, _, slug = mid.partition("/")
    words = []
    for t in slug.lower().split("-"):
        if _SNAPSHOT.match(t):
            return None
        if _VERSION.match(t):
            continue
        words.append(t)
    return f"{vendor}/{'-'.join(words)}" if words else None


def newest_in_family(model_id: str, catalog: list[dict]) -> Optional[dict]:
    """The catalog entry that supersedes `model_id`: the most recently released
    member of its family, when that isn't `model_id` itself. None otherwise."""
    fam = family_of(model_id)
    if not fam:
        return None
    members = [c for c in catalog if family_of(c["id"]) == fam and c.get("created")]
    if not members:
        return None
    best = max(members, key=lambda c: c["created"])
    cur = next((c for c in catalog if c["id"] == model_id), None)
    if best["id"] == model_id or (cur and (cur.get("created") or 0) >= best["created"]):
        return None
    return best


def dismissed(db: Database) -> set[str]:
    try:
        return set(json.loads(db.kv_get(KV_DISMISSED) or "[]"))
    except (TypeError, ValueError):
        return set()


def dismiss(db: Database, model_id: str) -> None:
    """Remember that the operator deleted this model's row: its family is not
    added back by a later update."""
    fam = family_of(model_id)
    if fam:
        db.kv_set(KV_DISMISSED, json.dumps(sorted(dismissed(db) | {fam})))


def new_families(listed_ids: list[str], catalog: list[dict], now_ts: float,
                 skip: set[str], room: int) -> list[dict]:
    """Catalog entries to add as new rows: the newest member of each recently
    released family that isn't listed, for the vendors already in the table."""
    have = {family_of(i) for i in listed_ids} - {None}
    vendors = {f.split("/")[0] for f in have}
    fams: dict[str, dict] = {}
    for c in catalog:
        fam = family_of(c["id"])
        if fam and c.get("created") and c["created"] > (fams.get(fam) or {}).get("created", 0):
            fams[fam] = c
    cutoff = now_ts - NEW_WINDOW_DAYS * 86400
    out: list[dict] = []
    for vendor in sorted(vendors):
        picked = 0
        mine = sorted(((f, c) for f, c in fams.items() if f.split("/")[0] == vendor),
                      key=lambda fc: -fc[1]["created"])
        for fam, c in mine:
            if picked >= MAX_NEW_PER_VENDOR or len(out) >= room:
                break
            words = fam.split("/", 1)[1].split("-")
            if fam in have or fam in skip or c["created"] < cutoff:
                continue
            if SKIP_WORDS & set(words):
                continue
            if words[-1] == "pro" and len(words) > 1 and f"{vendor}/{'-'.join(words[:-1])}" in fams:
                continue                       # "GPT-6 Sol Pro" next to "GPT-6 Sol"
            out.append(c)
            picked += 1
    return out


def display_name(entry: dict) -> str:
    """"Anthropic: Claude Opus 5.5" -> "Claude Opus 5.5"."""
    name = str(entry.get("name") or entry.get("id") or "")
    return name.split(": ", 1)[1].strip() if ": " in name else name.strip()


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
                    "suspicious": [], "replaced": [], "added": [], "matcher": "", "error": "",
                    "ts": utcnow(), "dry_run": dry_run}
    cfg = settings(db)
    follow = cfg["follow"]
    final_ids: list[str] = []            # the model each row ends this run on
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
            if r.get("source_id"):
                final_ids.append(r["source_id"])
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
    names = {x["name"] for x in rows}          # service names are unique
    for r in todo:
        mid = match.get(r["name"])
        if not mid:
            report["unmatched"].append(r["name"])
            if not dry_run:
                db.execute("UPDATE service_pricing SET last_checked=? WHERE id=?", (now, r["id"]))
            continue
        # the family's newest version takes the row over (see the module docstring)
        newer = newest_in_family(mid, catalog) if follow else None
        new_name = r["name"]
        if newer is not None:
            cand = display_name(newer)
            if cand and (cand == r["name"] or cand not in names):
                report["replaced"].append({"from": r["name"], "to": cand, "id": newer["id"],
                                           "was_id": mid})
                names.discard(r["name"])
                names.add(cand)
                mid, new_name = newer["id"], cand
            else:
                # the newer model already has its own row (two rows matched to one
                # model, or one added by hand): this row stays on the model it names
                newer = None
        c = by_id[mid]
        final_ids.append(mid)
        old = {"input_per_1m": r.get("input_per_1m"), "output_per_1m": r.get("output_per_1m"),
               "cached_input_per_1m": r.get("cached_input_per_1m")}
        new = {"input_per_1m": c["input_per_1m"], "output_per_1m": c["output_per_1m"],
               "cached_input_per_1m": (c["cached_input_per_1m"]
                                       if c["cached_input_per_1m"] is not None
                                       else r.get("cached_input_per_1m"))}
        entry = {"name": new_name, "id": mid, "old": old, "new": new}
        if newer is not None:
            entry["was"] = r["name"]
        if any(v is None or v <= 0 or v > MAX_PER_1M
               for v in (new["input_per_1m"], new["output_per_1m"])):
            report["suspicious"].append({**entry, "why": "price out of range"})
            continue
        jump = max((new[k] / old[k]) if old.get(k) else 1.0 for k in ("input_per_1m", "output_per_1m"))
        drop = max((old[k] / new[k]) if old.get(k) and new[k] else 1.0
                   for k in ("input_per_1m", "output_per_1m"))
        # a different model may honestly cost 10x less or more: the ratio rule is for
        # the SAME model's price moving
        if not force and newer is None and max(jump, drop) > SUSPICIOUS_RATIO:
            report["suspicious"].append({**entry, "why": f"{max(jump, drop):.0f}x change"})
            continue
        changed = (newer is not None
                   or any(abs((old.get(k) or 0) - (new[k] or 0)) > 1e-9 for k in new))
        if not dry_run:
            db.execute(
                "UPDATE service_pricing SET name=?, input_per_1m=?, output_per_1m=?, "
                "cached_input_per_1m=?, source='openrouter', source_id=?, last_checked=?"
                + (", updated_at=?" if changed else "") + " WHERE id=?",
                (new_name, new["input_per_1m"], new["output_per_1m"],
                 new["cached_input_per_1m"], mid, now, *( (now,) if changed else ()), r["id"]))
        (report["updated"] if changed else report["unchanged"]).append(entry)

    # newly released families join the list (see the module docstring)
    if cfg["add_new"]:
        import time as _time
        for c in new_families(final_ids, catalog, _time.time(), dismissed(db),
                              max(0, MAX_ROWS - len(rows))):
            name = display_name(c)
            if not name or name in names:
                continue
            if any(v is None or v <= 0 or v > MAX_PER_1M
                   for v in (c["input_per_1m"], c["output_per_1m"])):
                continue
            names.add(name)
            new = {"input_per_1m": c["input_per_1m"], "output_per_1m": c["output_per_1m"],
                   "cached_input_per_1m": c["cached_input_per_1m"]}
            report["added"].append({"name": name, "id": c["id"], "old": {}, "new": new})
            if not dry_run:
                db.execute(
                    "INSERT INTO service_pricing (name, input_per_1m, output_per_1m, "
                    "cached_input_per_1m, enabled, notes, updated_at, source, source_id, "
                    "last_checked, locked) VALUES (?,?,?,?,1,?,?,'openrouter',?,?,0)",
                    (name, new["input_per_1m"], new["output_per_1m"],
                     new["cached_input_per_1m"], "added automatically (new release)", now,
                     c["id"], now))

    if not dry_run:
        db.kv_set(KV_LAST, now)
        db.kv_set(KV_LAST_REPORT, json.dumps({k: report[k] for k in (
            "ts", "matcher", "error")} | {k: len(report[k]) for k in (
                "updated", "unchanged", "unmatched", "locked", "suspicious", "replaced",
                "added")}))
        if report["added"]:
            db.log_event("info", "pricing",
                         f"{len(report['added'])} newly released model(s) added to the cost list",
                         "; ".join(f"{x['name']} ({x['id']})" for x in report["added"])[:3900])
        if report["replaced"]:
            db.log_event("info", "pricing",
                         f"{len(report['replaced'])} service(s) moved to a newer version",
                         "; ".join(f"{x['from']} → {x['to']} ({x['id']})"
                                   for x in report["replaced"])[:3900])
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
    # v0.181.119: on unless switched off (it was opt-in), daily by default
    days = int(db.kv_get(KV_DAYS) or DEFAULT_DAYS)
    last = db.kv_get(KV_LAST) or ""
    auto = db.kv_get(KV_AUTO) != "0"
    return {"auto": auto, "days": days,
            "follow": db.kv_get(KV_FOLLOW) != "0",
            "add_new": db.kv_get(KV_ADD) != "0",
            "last_update": last,
            "next_update": _next_update(last, days) if auto else "",
            "last_report": last_report}


def _next_update(last: str, days: int) -> str:
    from datetime import datetime, timedelta, timezone
    try:
        t = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t + timedelta(days=days)).isoformat()


def save_settings(db: Database, auto: Optional[bool] = None, days: Optional[int] = None,
                  follow: Optional[bool] = None, add_new: Optional[bool] = None) -> dict:
    if add_new is not None:
        db.kv_set(KV_ADD, "1" if add_new else "0")
    if auto is not None:
        db.kv_set(KV_AUTO, "1" if auto else "0")
    if days is not None:
        db.kv_set(KV_DAYS, str(max(1, min(int(days), 90))))
    if follow is not None:
        db.kv_set(KV_FOLLOW, "1" if follow else "0")
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

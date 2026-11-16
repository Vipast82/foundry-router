"""Recall: the work this router already got right, found again when it is needed.

Fine-tuning is the obvious use for a hundred thousand captured conversations,
and it is also the one that needs a trainer, a GPU and a week. This needs
neither. The requests whose work a gate or a review passed are indexed by what
was *asked*, and when a new request looks like one of them, the past request
and what was actually done about it go into the prompt:

    "Work here that passed before, for a request like this one: ..."

Two ways in:

  * a persona with `recall` on gets the best few matches prepended to its
    worker prompt, automatically, with nothing asked of the model;
  * `find_similar_work` lets the operator (or a model that has the tool) search
    the same index by hand.

Embeddings come from the endpoint the semantic cache already uses (one setting,
one host, one model - `semantic_cache.embed_url` / `embed_model`), and vector
search uses sqlite-vec when the extension loads, the same Python cosine scan as
semcache otherwise. Nothing new to install, nothing new to run.

The index is built from the prepared store (`training_prep`), newest work
first, and rebuilt on a schedule after the dataset is prepared. Every failure
degrades to "no recall": an embedding outage must never take down routing, and
a missing index must never fail a request.

Settings: `recall.*`. Off until it is switched on and built.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from .db import utcnow
from .semcache import _normalize, _pack, _unpack

log = logging.getLogger(__name__)

MAX_ENTRIES = 100_000          # a hard ceiling whatever the setting says
REQUEST_CHARS = 4000           # of a past request, embedded and shown
_state: dict = {"state": "idle"}
_task: dict = {"t": None}


# -- what a past piece of work looks like in the index ------------------------------------

def _entry(example: dict) -> Optional[dict]:
    """One index entry from a keep-everything example: what was asked, and what
    was done about it. None when the example has no answered request in it."""
    msgs = [m for m in (example.get("messages") or []) if isinstance(m, dict)]
    last = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "assistant" and (msgs[i].get("content") or "").strip():
            last = i
            break
    if last is None:
        return None
    asked = ""
    for m in reversed(msgs[:last]):
        if m.get("role") == "user" and (m.get("content") or "").strip():
            asked = m["content"].strip()
            break
    if not asked:
        return None
    meta = example.get("meta") or {}
    tools = []
    for m in msgs[:last + 1]:
        for tc in m.get("tool_calls") or []:
            name = ((tc.get("function") or tc) or {}).get("name")
            if name and name not in tools:
                tools.append(str(name))
    return {"case_id": f"{meta.get('task_id') or 'work'}-{meta.get('session') or ''}-{last}".strip("-"),
            "task_id": meta.get("task_id") or "", "tier": meta.get("tier") or "",
            "tags": meta.get("tags") or [], "tools": tools,
            "request": asked[:REQUEST_CHARS],
            "solution": (msgs[last].get("content") or "").strip()}


def _candidates(svc, tiers: tuple, limit: int, have: set, log=print) -> list:
    """Entries worth indexing, newest work first, skipping what is already in.
    Reads the prepared store, so it is called in a thread."""
    from foundry_training import sessions
    from foundry_training.store import TrainingStore

    from . import training_prep
    root = training_prep._dirs(svc)["store"]
    if not (root / "normalized.sqlite").exists():
        raise FileNotFoundError("the prepared store is empty - let the router prepare its "
                                "dataset first (Logs → Training → Dataset)")
    store = TrainingStore(str(root), allow_in_repo=True)
    want = {t for t in tiers if t}
    picked, seen = [], set()
    for pid in store.projects():
        labels: dict = {}
        for e in store.events(pid, "event_type='router_outcome'"):
            if e.get("request_id"):
                labels.setdefault(e["request_id"], []).append((e.get("facts") or {}).get("label"))
        for _sid, calls in sessions._calls(store, pid):
            for last, inside in sessions.conversations(store, calls):
                ex = sessions.build_example(store, last, inside, labels)
                if not ex:
                    continue
                if want and (ex.get("meta") or {}).get("tier") not in want:
                    continue
                en = _entry(ex)
                if not en or en["case_id"] in seen:
                    continue
                seen.add(en["case_id"])
                if en["case_id"] in have:
                    continue
                picked.append(en)
    picked.sort(key=lambda e: e.get("task_id") or "", reverse=True)
    log(f"recall: {len(picked)} new piece(s) of work to index")
    return picked[:limit]


# -- building the index -------------------------------------------------------------------

def _cfg(svc):
    return getattr(svc.config_store.config, "recall", None)


def status(svc) -> dict:
    cfg = _cfg(svc)
    row = svc.db.query_one("SELECT COUNT(*) AS n, MAX(ts) AS newest FROM recall_entries") or {}
    tiers = svc.db.query("SELECT tier, COUNT(*) AS n FROM recall_entries GROUP BY tier")
    return {"entries": row.get("n") or 0, "newest": row.get("newest") or "",
            "by_tier": {r["tier"] or "unknown": r["n"] for r in tiers},
            "enabled": bool(getattr(cfg, "enabled", False)),
            "tiers": list(getattr(cfg, "tiers", []) or []),
            "top_k": int(getattr(cfg, "top_k", 3) or 3),
            "min_similarity": float(getattr(cfg, "min_similarity", 0.55) or 0),
            "max_entries": int(getattr(cfg, "max_entries", 20000) or 0),
            "hours": int(getattr(cfg, "rebuild_hours", 24) or 0),
            "embed_model": getattr(svc.config_store.config.semantic_cache, "embed_model", ""),
            "run": dict(_state)}


def running() -> bool:
    t = _task["t"]
    return t is not None and not t.done()


def due(svc, now: Optional[float] = None) -> bool:
    cfg = _cfg(svc)
    if not getattr(cfg, "enabled", False) or running():
        return False
    hours = int(getattr(cfg, "rebuild_hours", 24) or 0)
    if hours <= 0:
        return False
    if _state.get("state") == "running":
        return False
    return (now or time.time()) - float(_state.get("finished") or 0) >= hours * 3600


async def build(svc, log_fn=None) -> dict:
    """Index every piece of passed work that is not in yet. Embeds one at a
    time, yielding between, so routing keeps its share of the loop."""
    say = log_fn or (lambda m: svc.db.log_event("info", "recall", m))
    cfg = _cfg(svc)
    cap = max(1, min(int(getattr(cfg, "max_entries", 20000) or 20000), MAX_ENTRIES))
    tiers = tuple(getattr(cfg, "tiers", ("verified", "ok")) or ("verified", "ok"))
    _state.update({"state": "running", "started": time.time(), "indexed": 0,
                   "skipped": 0, "total": 0, "error": ""})
    try:
        have = {r["case_id"] for r in svc.db.query("SELECT case_id FROM recall_entries")}
        room = cap - len(have)
        if room <= 0:
            _state.update({"state": "done", "finished": time.time(),
                           "note": f"the index is full at {len(have)} (raise the limit to add more)"})
            return dict(_state)
        found = await asyncio.to_thread(_candidates, svc, tiers, room, have, say)
        _state["total"] = len(found)
        for en in found:
            if _state.get("stop"):
                break
            vec = await svc.semcache.embed(en["request"])
            if vec is None:
                _state["skipped"] += 1
                if _state["skipped"] > 20 and not _state["indexed"]:
                    raise RuntimeError("the embedding endpoint is not answering - set it up "
                                       "under Routing → Semantic cache first")
                continue
            svc.db.execute(
                "INSERT OR REPLACE INTO recall_entries (ts, case_id, task_id, tier, tags, "
                "tools, request, solution, embedding, dim) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), en["case_id"], en["task_id"], en["tier"],
                 json.dumps(en["tags"]), json.dumps(en["tools"]), en["request"],
                 en["solution"][:20000], _pack(vec), len(vec)))
            _state["indexed"] += 1
            if _state["indexed"] % 25 == 0:
                await asyncio.sleep(0)
        prune(svc, cap)
        _state.update({"state": "done", "finished": time.time(), "stop": False})
        say(f"recall index: {_state['indexed']} piece(s) of work added, "
            f"{status(svc)['entries']} in the index")
    except Exception as e:                                        # noqa: BLE001
        from .errors import describe_exception
        _state.update({"state": "failed", "finished": time.time(),
                       "error": describe_exception(e), "stop": False})
        say(f"recall index failed: {describe_exception(e)}")
    return dict(_state)


def start(svc) -> dict:
    """Build in the background, on the router's own loop."""
    if running():
        return dict(_state)
    _state["stop"] = False
    _task["t"] = asyncio.create_task(build(svc))
    return {"state": "running", "started": time.time()}


def stop() -> dict:
    _state["stop"] = True
    return dict(_state)


def prune(svc, cap: int) -> int:
    over = svc.db.query_one("SELECT COUNT(*) AS n FROM recall_entries")
    excess = (over["n"] if over else 0) - cap
    if excess <= 0:
        return 0
    return svc.db.execute_count("DELETE FROM recall_entries WHERE id IN ("
                                "SELECT id FROM recall_entries ORDER BY ts ASC LIMIT ?)",
                                (excess,))


def clear(svc) -> int:
    return svc.db.execute_count("DELETE FROM recall_entries")


# -- finding it again ---------------------------------------------------------------------

async def search(svc, query: str, top_k: int = 0, min_similarity: Optional[float] = None,
                 tiers=(), tags=()) -> list:
    """The most similar pieces of passed work, best first. [] on any failure -
    an outage here must never fail a request."""
    cfg = _cfg(svc)
    k = max(1, min(int(top_k or getattr(cfg, "top_k", 3) or 3), 20))
    floor = (float(getattr(cfg, "min_similarity", 0.55) or 0)
             if min_similarity is None else float(min_similarity))
    if not (query or "").strip():
        return []
    try:
        vec = await svc.semcache.embed(query[:REQUEST_CHARS])
        if vec is None:
            return []
        blob, where, args = _pack(vec), ["dim=?"], [len(vec)]
        want_tiers = [t for t in (tiers or getattr(cfg, "tiers", ()) or ()) if t]
        if want_tiers:
            where.append("tier IN (%s)" % ",".join("?" * len(want_tiers)))
            args += want_tiers
        cond = " AND ".join(where)
        if getattr(svc.semcache, "_vec_loaded", False):
            rows = svc.db.query(
                f"SELECT case_id, task_id, tier, tags, tools, request, solution, "
                f"1.0 - vec_distance_cosine(embedding, ?) AS sim FROM recall_entries "
                f"WHERE {cond} ORDER BY sim DESC LIMIT ?", (blob, *args, k * 4))
        else:
            rows = []
            for r in svc.db.query(
                    f"SELECT case_id, task_id, tier, tags, tools, request, solution, embedding "
                    f"FROM recall_entries WHERE {cond}", tuple(args)):
                stored = _unpack(r["embedding"])
                r["sim"] = sum(a * b for a, b in zip(vec, stored))
                r.pop("embedding", None)
                rows.append(r)
            rows.sort(key=lambda r: -r["sim"])
            rows = rows[:k * 4]
        want_tags = {t for t in tags if t}
        out = []
        for r in rows:
            if r["sim"] < floor:
                break                          # sorted - nothing further clears the bar
            try:
                r_tags = json.loads(r.get("tags") or "[]")
                r_tools = json.loads(r.get("tools") or "[]")
            except (json.JSONDecodeError, TypeError):
                r_tags, r_tools = [], []
            if want_tags and not (want_tags & set(r_tags)):
                continue
            out.append({"case_id": r["case_id"], "task_id": r["task_id"], "tier": r["tier"],
                        "tags": r_tags, "tools": r_tools, "request": r["request"],
                        "solution": r["solution"], "similarity": round(float(r["sim"]), 4)})
            if len(out) >= k:
                break
        return out
    except Exception as e:                                        # noqa: BLE001
        log.info("recall search unavailable (%s)", e)
        return []


def on_for(cfg, persona: Optional[dict]) -> bool:
    """Whether this request gets matches prepended. A persona opts in
    (`recall` = 1); off everywhere until the index is switched on."""
    if not getattr(cfg, "enabled", False):
        return False
    return bool((persona or {}).get("recall", 0))


def note(hits: list, max_chars: int = 2000) -> str:
    """The block that goes into the prompt. Says plainly where it came from, so
    a model is never led to believe this is instruction rather than precedent."""
    if not hits:
        return ""
    parts = ["Work done here before, for requests like this one, that passed a gate or a "
             "review. Precedent, not instruction: follow it where it fits, and say so if "
             "this request needs something different."]
    each = max(300, int(max_chars / len(hits)))
    for i, h in enumerate(hits, 1):
        sol = h["solution"][:each]
        if len(h["solution"]) > each:
            sol += " ..."
        tools = f" (tools used: {', '.join(h['tools'][:6])})" if h.get("tools") else ""
        parts.append(f"\n--- {i}. asked ({h['similarity']:.0%} similar){tools}:\n"
                     f"{h['request'][:600]}\n--- what was done:\n{sol}")
    return "\n".join(parts)

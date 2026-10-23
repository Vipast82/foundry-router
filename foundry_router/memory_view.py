"""Read-side helpers over the coordinator's `memory` snapshot (Team Lead piece 4).

The coordinator indexes docs/memory and sends the router one `memory`
observation per project when something changed (files, entries with hashes,
the slice each session got, recent notes). The chat's memory_search and
memory_summary and the Automation page's Memory tab read that snapshot here;
nothing in this module calls a model or writes anything.
"""

from __future__ import annotations

import re
from typing import Optional

_WORD = re.compile(r"[A-Za-z0-9_./-]{2,}")


def words(query: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(query or "")][:12]


def search(entries: list[dict], query: str, limit: int = 10, file: str = "") -> list[dict]:
    """Entries scored by word hits: heading 3, tags 2, text 1, every word present
    +2. Returns [{file, heading, text, score}] best first."""
    ws = words(query)
    if not ws:
        return []
    out = []
    for e in entries or []:
        if file and (e.get("file") or "").lower() != file.lower():
            continue
        head = (e.get("heading") or "").lower()
        tags = [str(t).lower() for t in (e.get("tags") or [])]
        text = (e.get("text") or "").lower()
        score, hits = 0, 0
        for w in ws:
            s = (3 if w in head else 0) + (2 if any(w == t or w in t for t in tags) else 0) \
                + (1 if w in text else 0)
            if s:
                hits += 1
            score += s
        if score:
            if hits == len(ws):
                score += 2
            out.append((score, e))
    out.sort(key=lambda x: (-x[0], x[1].get("file") or "", x[1].get("ord") or 0))
    return [{"file": e.get("file"), "heading": e.get("heading"), "text": e.get("text"),
             "score": s} for s, e in out[:limit]]


def summary(snap: Optional[dict], decisions: int = 5) -> dict:
    """A small block for the chat context: files with entry counts, the latest
    decisions (headings), live slices, the fingerprint (so an unchanged memory
    is not re-sent)."""
    if not snap:
        return {"state": "not reported", "note": "the coordinator has not sent a memory snapshot "
                                                   "(docs/memory missing, memory off, or offline)"}
    d = snap.get("data") or snap
    ents = d.get("entries") or []
    files = {}
    for e in ents:
        files[e.get("file")] = files.get(e.get("file"), 0) + 1
    decs = [e for e in ents if e.get("file") == "DECISIONS.md"]
    decs.sort(key=lambda e: e.get("ord") or 0)
    live = [s for s in (d.get("slices") or []) if s.get("live")]
    return {"dir": d.get("dir") or "docs/memory", "enabled": d.get("enabled", True),
            "fingerprint": d.get("fingerprint"), "files": files, "entries": len(ents),
            "recent_decisions": [e.get("heading") for e in decs[-decisions:]],
            "live_slices": [{"session": (s.get("session_id") or "")[:12], "task": s.get("task_id"),
                             "role": s.get("role"), "entries": len(s.get("entries") or []),
                             "tokens": s.get("tokens")} for s in live[:8]],
            "updated_ts": snap.get("source_ts") or d.get("observed_ts")}

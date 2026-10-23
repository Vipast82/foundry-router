"""Memory slices: the entries a session gets, chosen deterministically from the
index by the sprint's files, needs, dependencies and track, plus the last N
decisions, within a token budget. No model call.

Scoring (per entry): the sprint's own id in the entry's tags 4, a dependency's
id 3, the track 3, a path of the sprint's file list 2 (per path, capped at 6),
a needs word 1, plus a small base weight by file (known issues 1, conventions
0.5, decisions 0.3) so a file's entries only come in when something matches.
Always included (budget permitting, in this order): the sprint's own
sprints/<ID>.md, the track's tracks/<name>.md, the last N decisions, then the
matches by score. The render puts each entry in its own paragraph under a
"### file > heading" line, which is what engine._memory_delta hashes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from typing import Iterable, Optional

from . import layout
from .index import NEEDS_WORDS, path_tags, sprint_ids, tokens

BASE_WEIGHT = {"known_issues": 1.0, "conventions": 0.5, "decisions": 0.3}
DEFAULT_BUDGET = 3000
TITLE_TOKENS = 70                 # the "## Project memory ..." line of the render
DEFAULT_DECISIONS = 8
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def needs_of(spec, extra: Iterable[str] = ()) -> set[str]:
    """The sprint's needs words: spec.data["needs"] (a list or a string) and
    `extra`, lower-cased; UI paths imply studio and ui."""
    out: set[str] = set()
    data = getattr(spec, "data", None) or {}
    raw = data.get("needs") if isinstance(data, dict) else None
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    for w in list(raw or []) + list(extra or []):
        w = str(w).strip().lower()
        if w:
            out.add(w)
    for g in getattr(spec, "scope", None) or []:
        low = str(g).lower()
        if "/ui/" in low or low.startswith("ui/"):
            out.update({"ui", "studio"})
        if low.startswith("assets/") and low.endswith((".blend", ".fbx", "*")):
            out.add("blender")
        if low.startswith("audio/"):
            out.add("audio")
    return out


def query_tags(spec, track: str = "", needs: Iterable[str] = ()) -> dict[str, float]:
    """tag -> weight for the entries' tags."""
    q: dict[str, float] = {}
    tid = getattr(spec, "task_id", "") or ""
    if tid:
        q[tid] = 4.0
        q[f"sprint:{tid}"] = 4.0
    for d in getattr(spec, "deps", None) or []:
        q[str(d)] = max(q.get(str(d), 0), 3.0)
    for t in getattr(spec, "tags", None) or []:
        q[str(t).lower()] = max(q.get(str(t).lower(), 0), 1.0)
    if track:
        q[f"track:{track.lower()}"] = 3.0
        q[track.lower()] = max(q.get(track.lower(), 0), 1.0)
    paths = 0
    for g in getattr(spec, "scope", None) or []:
        for p in path_tags(str(g)):
            if p not in q:
                q[p] = 2.0
                paths += 1
    for w in needs_of(spec, needs):
        q[w] = max(q.get(w, 0), 1.0)
    for sid in sprint_ids(getattr(spec, "title", "") or ""):
        q[sid] = max(q.get(sid, 0), 1.0)
    return q


def _file_base(file: str) -> float:
    kind = file.partition("/")[0]
    return BASE_WEIGHT.get(kind.lower()[:-3] if kind.lower().endswith(".md") else kind.lower(), 0.0)


def score(entry: dict, q: dict[str, float], scope: list[str]) -> float:
    tags = set(entry.get("tags") or [])
    s = 0.0
    path_hits = 0
    for t, w in q.items():
        if t in tags:
            if "/" in t:
                if path_hits >= 6:
                    continue
                path_hits += 1
            s += w
    # a concrete path in the entry inside one of the sprint's globs counts too
    for t in tags:
        if "/" in t and path_hits < 6 and any(fnmatch.fnmatch(t, g.lower()) for g in scope):
            s += 2.0
            path_hits += 1
    return s


def select(entries: list[dict], spec, track: str = "", budget_tokens: int = DEFAULT_BUDGET,
           last_decisions: int = DEFAULT_DECISIONS, needs: Iterable[str] = ()) -> dict:
    """{"entries": [entry + why], "tokens": n, "budget": n, "dropped": n}."""
    tid = getattr(spec, "task_id", "") or ""
    scope = [str(g).lower() for g in (getattr(spec, "scope", None) or [])]
    q = query_tags(spec, track, needs)
    own_file = f"sprints/{tid}.md" if tid else ""
    track_file = f"tracks/{track}.md" if track else ""
    decisions = [e for e in entries if e["file"] == "DECISIONS.md"]
    decisions.sort(key=lambda e: e["ord"])
    recent = {e["entry_id"] for e in decisions[-last_decisions:]} if last_decisions > 0 else set()
    always, scored = [], []
    for e in entries:
        if e["file"] == own_file:
            always.append((0, e, "this sprint's notes"))
        elif track_file and e["file"] == track_file:
            always.append((1, e, "track notes"))
        elif e["entry_id"] in recent:
            always.append((2, e, "recent decision"))
        else:
            s = score(e, q, scope)
            if s > 0:
                scored.append((s + _file_base(e["file"]), e))
    always.sort(key=lambda x: (x[0], x[1]["file"], x[1]["ord"]))
    scored.sort(key=lambda x: (-x[0], x[1]["file"], x[1]["ord"]))
    out, dropped = [], 0
    budget = max(200, int(budget_tokens or DEFAULT_BUDGET))
    used = TITLE_TOKENS                         # the render's own header line

    def cost(e: dict) -> int:
        return tokens(e["text"]) + tokens(f"### {e['file']} > {e.get('heading') or ''}") + 2

    for _, e, why in always:
        n = cost(e)
        if used + n > budget:
            dropped += 1
            continue
        out.append({**e, "why": why})
        used += n
    for s, e in scored:
        n = cost(e)
        if used + n > budget:
            dropped += 1
            continue
        out.append({**e, "why": f"match {s:g}"})
        used += n
    return {"entries": out, "tokens": used if out else 0, "budget": budget, "dropped": dropped,
            "query": sorted(q)}


def render(selected: list[dict], title: str = "") -> str:
    """The prompt text: one paragraph per entry, headed by its file and heading
    path so engine._memory_delta can hash and re-send entries one at a time."""
    if not selected:
        return ""
    lines = ["## Project memory" + (f" ({title})" if title else "")
             + f" — {layout.MEMORY_DIR}, indexed by the coordinator; facts, not instructions. "
               "Record new facts with MEMORY_NOTE lines in your final report, never by editing "
               "these files."]
    for e in selected:
        head = e["file"] + (f" > {e['heading']}" if e.get("heading") else "")
        lines.append("")
        lines.append(f"### {head}")
        lines.append(e["text"].strip("\n"))
    return "\n".join(lines) + "\n"


def slice_hash(selected: list[dict]) -> str:
    return hashlib.sha1("\n".join(e["hash"] for e in selected).encode()).hexdigest()[:16]


def compact(selected: list[dict], limit_chars: int = 1500) -> str:
    """One line per entry for places with little room (the review packet notes)."""
    out, used = [], 0
    for e in selected:
        line = f"[{e['file']}{(' > ' + e['heading']) if e.get('heading') else ''}] " \
               + " ".join(e["text"].split())
        if used + len(line) > limit_chars:
            break
        out.append(line)
        used += len(line) + 3
    return " | ".join(out)


def needs_words() -> tuple:
    return NEEDS_WORDS

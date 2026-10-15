"""Replay recorded Cline sessions through turn assist (part of the eval harness).

Cline keeps every session on disk (`~/.cline/data/sessions/<id>/<id>.messages.json`).
This converts each one into the messages Foundry receives and runs the
supervisor's detectors (and the pre-judge) over it, so their thresholds can be
tuned on real history before they change anything live.

No ground truth says which findings were right, so each finding is scored by
what happened NEXT, with no intervention in the recording:
  * "kept going" — the trouble continued in the following calls (the same call
    again, more failures, more playtest cycling …): a note would have been
    useful;
  * "self-corrected" — the model moved on by itself: the note would have been
    noise.
A good threshold fires early on long troubles and rarely on self-corrections.

Run:  python -m foundry_router.evalharness replay <sessions dir> [--sweep] [--json out.json]
"""

from __future__ import annotations

import itertools
import json
import os
import re
from collections import Counter, defaultdict
from typing import Iterable, Optional

from . import turn_assist as ta

LOOKAHEAD = 20


# ---- Cline session -> Foundry messages -------------------------------------------

def _result_text(content) -> tuple[str, list]:
    """A stored tool_result content -> (wire text, images)."""
    images = []
    if isinstance(content, list):
        if all(isinstance(x, dict) and "query" in x for x in content):
            return json.dumps(content, ensure_ascii=False), images
        texts = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "image":
                images.append(x.get("data") or "")
            elif isinstance(x, dict) and x.get("type") == "text":
                texts.append(x.get("text") or "")
            else:
                texts.append(json.dumps(x, ensure_ascii=False))
        return "\n".join(texts), images
    return ("" if content is None else str(content)), images


def convert(stored: list[dict]) -> list[dict]:
    """Cline's stored messages -> the OpenAI-shaped messages Foundry sees."""
    out: list[dict] = []
    for m in stored:
        parts = m.get("content")
        if not isinstance(parts, list):
            parts = [{"type": "text", "text": str(parts or "")}]
        if m.get("role") == "assistant":
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            calls = [{"id": p.get("id"), "type": "function",
                      "function": {"name": p.get("name"), "arguments": p.get("input") or {}}}
                     for p in parts if p.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
        for p in parts:
            if p.get("type") == "tool_result":
                text, images = _result_text(p.get("content"))
                tm = {"role": "tool", "tool_call_id": p.get("tool_use_id"), "content": text}
                if images:
                    tm["images"] = images
                out.append(tm)
        if any(t.strip() for t in texts):
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def load_sessions(root: str) -> Iterable[tuple[str, dict, list]]:
    """(session id, metadata, converted messages) for each session folder."""
    for sid in sorted(os.listdir(root)):
        d = os.path.join(root, sid)
        try:
            with open(os.path.join(d, sid + ".messages.json"), encoding="utf-8") as f:
                stored = json.load(f).get("messages") or []
            try:
                with open(os.path.join(d, sid + ".json"), encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                meta = {}
        except (OSError, ValueError):
            continue
        yield sid, meta, convert(stored)


# ---- outcome of a finding -------------------------------------------------------------

def _kept_going(kind: str, evs: list, i: int, cfg: dict) -> bool:
    """Did the trouble behind finding at event i continue with no intervention?"""
    e, nxt = evs[i], evs[i + 1:i + 1 + LOOKAHEAD]
    if not nxt:
        return False
    if kind == "loop":
        return any(x.key == e.key for x in nxt)
    if kind == "errors":
        head = nxt[:10]
        return sum(1 for x in head if not x.ok) >= max(1, len(head) // 2)
    if kind == "edit":
        p = e.args.get("path")
        return any(x.name == "editor" and not x.ok and x.args.get("path") == p for x in nxt)
    if kind == "cycling":
        return sum(1 for x in nxt if ta._PLAY_RX.search(x.name)) >= 4
    if kind == "polling":
        return sum(1 for x in nxt[:10]
                   if any(re.search(r"Start-Sleep", c, re.I) for c in ta._commands(x.args)[1])) >= 2
    if kind == "capture":
        return any(x.key == e.key for x in nxt)
    return False


def analyse(messages: list[dict], cfg: dict) -> dict:
    """Findings for one conversation, each scored kept-going / self-corrected,
    plus what the pre-judge would have blocked or fixed."""
    evs = [e for e in ta.events(messages, ta.error_rx(cfg)) if e.result_idx >= 0]
    by_result = {e.result_idx: n for n, e in enumerate(evs)}
    findings = ta._detect(evs, cfg)
    rows = []
    for f in findings:
        n = by_result.get(f.at)
        if n is None:
            continue
        rows.append({"kind": f.kind, "call": n + 1, "kept_going": _kept_going(f.kind, evs, n, cfg),
                     "detail": f.detail})
    pj = Counter()
    for e in evs:
        j = ta.prejudge([{"id": "x", "name": e.name, "arguments": e.args}],
                        {**cfg, "prejudge": "observe"})
        for _, rule, _ in j.blocked:
            pj["block: " + rule] += 1
        for _, _, desc in j.fixes:
            pj["fix: " + desc] += 1
    return {"calls": len(evs), "errors": sum(1 for e in evs if not e.ok),
            "findings": rows, "prejudge": dict(pj)}


# ---- whole-corpus replay + threshold sweep ----------------------------------------------

def replay(root: str, cfg: Optional[dict] = None, sessions: Optional[list] = None) -> dict:
    cfg = {**ta.ASSIST_DEFAULTS, **(cfg or {})}
    data = sessions if sessions is not None else list(load_sessions(root))
    totals = {"sessions": 0, "calls": 0, "errors": 0}
    kinds: dict = defaultdict(lambda: {"fired": 0, "kept_going": 0, "sessions": set(),
                                       "first_calls": []})
    pj = Counter()
    per_session = []
    for sid, meta, msgs in data:
        a = analyse(msgs, cfg)
        totals["sessions"] += 1
        totals["calls"] += a["calls"]
        totals["errors"] += a["errors"]
        pj.update(a["prejudge"])
        seen = set()
        for f in a["findings"]:
            k = kinds[f["kind"]]
            k["fired"] += 1
            k["kept_going"] += int(f["kept_going"])
            k["sessions"].add(sid)
            if f["kind"] not in seen:
                k["first_calls"].append(f["call"])
                seen.add(f["kind"])
        per_session.append({"session": sid, "prompt": (meta.get("prompt") or "")[:90],
                            "status": meta.get("status"), "calls": a["calls"],
                            "errors": a["errors"],
                            "findings": Counter(f["kind"] for f in a["findings"]),
                            "first": {f["kind"]: f["call"] for f in reversed(a["findings"])}})
    per_kind = {}
    for kind, k in kinds.items():
        fc = sorted(k["first_calls"])
        per_kind[kind] = {
            "fired": k["fired"], "sessions": len(k["sessions"]),
            "per_1000_calls": round(1000 * k["fired"] / max(totals["calls"], 1), 2),
            "kept_going_pct": round(100 * k["kept_going"] / max(k["fired"], 1), 1),
            "median_first_call": fc[len(fc) // 2] if fc else None}
    return {"config": {k: cfg[k] for k in cfg if k.startswith("sup_")},
            "totals": totals, "detectors": per_kind, "prejudge": dict(pj.most_common()),
            "sessions": per_session}


SWEEP = {
    "loop": {"sup_loop_repeats": [2, 3, 4]},
    "errors": {"sup_error_streak": [4, 5, 6, 8]},
    "edit": {"sup_edit_fails": [2, 3]},
    "cycling": {"sup_play_calls": [6, 8, 10, 12], "sup_play_window": [40]},
    "polling": {"sup_sleep_run": [3, 4, 6]},
    "capture": {"sup_capture_repeats": [2, 3]},
}


def sweep(root: str, base: Optional[dict] = None) -> dict:
    """Each detector's thresholds varied on their own (others at base)."""
    data = list(load_sessions(root))
    out = {}
    for kind, grid in SWEEP.items():
        rows = []
        keys = list(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            over = dict(zip(keys, combo))
            r = replay(root, {**(base or {}), **over}, data)
            d = r["detectors"].get(kind, {"fired": 0, "per_1000_calls": 0,
                                          "kept_going_pct": None, "sessions": 0,
                                          "median_first_call": None})
            rows.append({**over, **d})
        out[kind] = rows
    return out


def report(r: dict) -> str:
    t = r["totals"]
    lines = [f"{t['sessions']} sessions · {t['calls']:,} tool calls · {t['errors']:,} "
             f"errors ({100 * t['errors'] / max(t['calls'], 1):.1f}/100)",
             "", "detector   fired  /1000 calls  sessions  kept going  median first call"]
    for kind, d in sorted(r["detectors"].items(), key=lambda kv: -kv[1]["fired"]):
        lines.append(f"{kind:10} {d['fired']:5}  {d['per_1000_calls']:11}  {d['sessions']:8}  "
                     f"{d['kept_going_pct']:9}%  {d['median_first_call']}")
    if r.get("prejudge"):
        lines += ["", "pre-judge (observe):"] + [f"  {n:6}  {k}" for k, n in r["prejudge"].items()]
    return "\n".join(lines)


def report_sweep(s: dict) -> str:
    lines = []
    for kind, rows in s.items():
        lines.append(f"\n{kind}:")
        for row in rows:
            knobs = " ".join(f"{k}={v}" for k, v in row.items() if k.startswith("sup_"))
            lines.append(f"  {knobs:40} fired {row['fired']:5}  /1000 {row['per_1000_calls']:6}  "
                         f"kept going {row['kept_going_pct']}%  first@{row['median_first_call']}")
    return "\n".join(lines)

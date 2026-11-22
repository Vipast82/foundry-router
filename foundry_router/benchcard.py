"""What a model profile card says beyond its scores (v0.181.211).

A score says how well. The operator's question is sharper - where does this
model excel, where does it fail, HOW does it fail, and how fast is it at each
kind of work - so a card carries, for every suite:

**A breakdown.** Difficulty tiers where the suite has them (easy, medium, hard,
expert), and the one dimension that suite is about otherwise: context size and
decoys for the needle suite, the tool server for the finder suite, whether the
verified answer was a tool call or words for the baseline. A suite average
hides a model that is perfect on easy cases and lost on expert ones, which is
exactly the shape that says whether a new model is an upgrade.

**How it missed.** Every case that did not get full marks is classified - the
finder never surfaced the tool, or surfaced it and the model used another; the
needle answer named the decoy; the code value was wrong - and counted, and each
one can be opened with what was wanted next to what came back.

**Speed, per kind of work.** Decode, prefill and end-to-end rates from the
performance samples each run recorded under its own label, medians rather than
means: one sample whose prompt cache went unreported turned a 1,400 t/s prefill
into a 16,522 t/s average on the brain lane. A prefill rate is only shown over
prompts long enough to measure one, since a 60-token prompt is all overhead.

**A ceiling flag.** A suite a model aces cannot tell it from a better one; the
card says so instead of letting 100 read as a strength.

And a live view: how far a run is, how fast it is going, and when it will
finish, estimated from this model's own history where there is any.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Optional

TIER_ORDER = ("easy", "medium", "hard", "expert")
# Prefill below this many tokens is fixed overhead, not throughput: the brain
# lane's 63-token code prompts "prefilled" at 58 t/s.
MIN_PREFILL_TOKENS = 512
# A prefill faster than this is a prompt cache the backend did not report, not
# a speed: 31,895 tokens in 104 ms on the brain lane.
MAX_PLAUSIBLE_PREFILL_TPS = 50_000
CEILING = 99.5
# a facet row needs this many cases before it is called a strength or weakness
MIN_FACET_CASES = 3
STRONG, WEAK = 90.0, 60.0

# Plain names for the suites, for a person rather than a file name.
SUITE_NAMES = {
    "baseline": "Real work replay",
    "codereason": "Code reasoning",
    "needle": "Long-context recall",
    "toolflow": "Find and use tools",
    "toolpick": "Pick the right tool",
    "agent": "Agent deliverables",
    "protocol": "Coordinator protocol",
    "grounded": "Grounded answers",
}
SUITE_ASKS = {
    "baseline": "Given a real conversation up to a verified step, does it make the same move?",
    "codereason": "What does this Python do? Every answer was computed by running it.",
    "needle": "Find one unguessable code in a long context, sometimes beside a decoy.",
    "toolflow": "Search the live tool registry, then use the tool the task needs.",
    "toolpick": "From every tool this router has, name the one that fits a need.",
    "agent": "Build a deliverable from this ecosystem's real work. Judged, not scored.",
    "protocol": "End a sprint turn the way the coordinator reads it: a valid report, the "
                "right status, the BLOCKED rules, a scope request.",
    "grounded": "Answer from a stack of work records: the value and the record it came "
                "from, or say the documents do not have it.",
}


def _tags(row) -> list:
    try:
        return [str(t) for t in json.loads(row["tags"] or "[]")]
    except (ValueError, TypeError):
        return []


def _tag(tags: list, key: str) -> Optional[str]:
    return next((t.split(":", 1)[1] for t in tags if t.startswith(key + ":")), None)


def _list(text) -> list:
    try:
        v = json.loads(text or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _detail(row) -> dict:
    """The finder's code_detail ("found=True picked=x searches=2 used=a,b")."""
    out = {}
    for bit in str(row["code_detail"] or "").split():
        if "=" in bit:
            k, v = bit.split("=", 1)
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# How a case missed                                                           #
# --------------------------------------------------------------------------- #

def _gave_avoided(row) -> bool:
    """Whether the answer names one of the codes the case marked as wrong."""
    exp = str(row["expected"] or "")
    ans = str(row["answer"] or "")
    if "decoy " not in exp:
        return False
    tail = exp.split("decoy ", 1)[1]
    import re as _re
    return any(c in ans for c in _re.findall(r"\d{6}", tail))


def miss_kind(suite: str, row) -> Optional[str]:
    """Why a case did not get full marks, in words; None when it did."""
    score = row["score"]
    if score is None:
        return None                          # a deliverable: not scored at all
    if row["error"]:
        return "the call failed"
    if score >= 100:
        return None
    if _detail(row).get("not_sent"):
        # above the two levels of its ladder it missed in a row - v0.181.223
        return "above the level it stopped passing (not sent)"
    if _detail(row).get("timed_out"):
        return "ran out of time before answering"
    if _detail(row).get("out_of_room"):
        # thought through the whole budget (8,192 tokens after one retry) and
        # gave no answer - v0.181.219; or went past it on a backend that does
        # not enforce one - v0.181.223
        return "ran out of tokens before answering"
    tags = _tags(row)
    if suite == "grounded" or "grounded" in tags:
        # v0.181.228: how a grounded answer went wrong, by what the case was for
        said = str(row["answer"] or "").lower() if "answer" in row.keys() else ""
        wanted_none = "not found" in str(row["expected"] or "").lower() \
            if "expected" in row.keys() else False
        if wanted_none:
            return "answered when the documents did not say"
        if "not found" in said[-200:]:
            return "said not found when a document had it"
        if "g-tally" in tags:
            return "wrong count or total"
        if 0 < float(score) < 100:
            return "right value, citation missing or wrong"
        return {"g-counter": "answered from what it believes, not the document",
                "g-scope": "used a document outside the tier it was told to use",
                "g-conflict": "took a superseded document",
                "g-join": "did not put the two records together"}.get(
                    next((t for t in tags if t.startswith("g-")), ""), "wrong value")
    if suite == "protocol" or "protocol" in tags:
        from .protocolbench import CHECK_WORDS
        if _detail(row).get("wrote_call"):
            return CHECK_WORDS["wrote_call"]            # v0.181.220
        failed = _detail(row).get("failed", "")
        first = failed.split(",")[0] if failed else ""
        return CHECK_WORDS.get(first, "report scored low")
    if suite == "toolflow" or "finder" in tags:
        d = _detail(row)
        if d.get("found") != "True":
            return "never found the tool"
        used = [u for u in (d.get("used") or "").split(",") if u and u != "-"]
        if not used and d.get("picked") in (None, "", "-"):
            return "found it, then used no tool"
        if d.get("args") == "False" and d.get("picked") not in (None, "", "-")                 and float(row["score"] or 0) >= 70:
            return "right tool, wrong arguments"          # v0.181.225
        return "found it, used another tool"
    if "decoy" in tags:
        exp = str(row["expected"] or "")
        ans = str(row["answer"] or "")
        if "decoy " in exp:
            fake = exp.split("decoy ", 1)[1].split()[0].strip(",)")
            if fake and fake in ans:
                return "named the decoy"
        return "wrong or missing code"
    if "multi" in tags:
        return "missed some of the three codes"
    if "updated" in tags:
        return "gave the superseded code" if _gave_avoided(row) else "wrong or missing code"
    if "paraphrase" in tags:
        return "picked another note's code" if _gave_avoided(row) else "wrong or missing code"
    if suite == "needle" or "needle" in tags:
        return "wrong or missing code"
    if suite == "codereason" or "codereason" in tags:
        return "wrong exception" if "exception" in tags else "wrong value"
    if suite == "toolpick" or "toolpick" in tags:
        if "none-fits" in tags:
            return "named a tool where none fits"
        if "none" in str(row["answer"] or "").lower().split():
            return "said none where a tool fits"
        return "named its look-alike or another tool" if "lookalike" in tags             else "named another tool"
    want = _list(row["tools_expected"])
    got = _list(row["tools_called"])
    if not want and got:
        return "used a tool where the verified answer was words"
    if want and not got:
        return "answered in words where a tool was needed"
    if want and got and want[:1] != got[:1]:
        # v0.181.218: a look where the session looked scores 0.9 (bench.move_kind)
        if float(score) >= 85:
            return "the same move with another tool"
        return "reached for a different tool first"
    if want and got:
        return "right first tool, different sequence"
    return "answer scored low"


# --------------------------------------------------------------------------- #
# The breakdown                                                               #
# --------------------------------------------------------------------------- #

def _group(rows: list, key_of, order=None, limit: int = 0) -> list:
    buckets: dict = {}
    for r in rows:
        if r["score"] is None:
            continue
        k = key_of(r)
        if k is None:
            continue
        b = buckets.setdefault(k, {"label": k, "n": 0, "passed": 0, "sum": 0.0})
        b["n"] += 1
        b["sum"] += float(r["score"])
        b["passed"] += 1 if float(r["score"]) >= 100 else 0
    out = [{"label": b["label"], "n": b["n"], "passed": b["passed"],
            "score": round(b["sum"] / b["n"], 1)} for b in buckets.values()]
    if order:
        rank = {v: i for i, v in enumerate(order)}
        out.sort(key=lambda x: rank.get(x["label"], 99))
    else:
        out.sort(key=lambda x: (-x["n"], x["label"]))
    return out[:limit] if limit else out


def _kchars(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1000:
        return f"{n} chars"
    # tokens are what a context window is counted in; this filler runs about
    # 3.5 characters a token on both local models (v0.181.216)
    return f"{n // 1000}k chars (~{max(1, round(n / 3500))}k tokens)"


def ladder_reach(rows: list) -> list:
    """One row per generated ladder: the highest level passed, out of the levels
    in the suite (v0.181.223). The share of levels passed is the bar; the label
    says where it got to, which is the number that moves as models improve."""
    by: dict = {}
    for r in rows:
        t = _tags(r)
        lad, lvl = _tag(t, "ladder"), _tag(t, "level")
        if not lad or r["score"] is None:
            continue
        try:
            lvl = int(lvl)
        except (TypeError, ValueError):
            continue
        b = by.setdefault(lad, {"levels": [], "passed": [], "sum": 0.0, "rungs": []})
        b["levels"].append(lvl)
        b["sum"] += float(r["score"])
        if float(r["score"]) >= 100:
            b["passed"].append(lvl)
        # one rung, for the page to draw (v0.181.227): passed, missed, or not
        # sent because the two below it were missed
        d = str(r["code_detail"] or "") if "code_detail" in r.keys() else ""
        b["rungs"].append({"level": lvl, "tier": _tag(t, "tier") or "",
                           "state": "pass" if float(r["score"]) >= 100
                           else "skipped" if "not_sent=1" in d else "miss"})
    out = []
    for lad in sorted(by):
        b = by[lad]
        top = max(b["passed"]) if b["passed"] else 0
        # "trace:grid" -> grid; "ctx:ledger:1" -> ledger (second set)
        bits = lad.split(":")
        name = bits[1] if len(bits) > 1 else bits[0]
        if len(bits) > 2 and bits[2] not in ("", "0"):
            name += " (second set)"
        out.append({"label": f"{name}: level {top} of {max(b['levels'])}" if top
                    else f"{name}: none of {max(b['levels'])}",
                    "n": len(b["levels"]), "passed": len(b["passed"]),
                    "score": round(b["sum"] / len(b["levels"]), 1),
                    "name": name, "top": top,
                    "rungs": sorted(b["rungs"], key=lambda x: x["level"])})
    return out


def facets(suite: str, rows: list) -> list:
    """The breakdowns this suite is read by, most telling first."""
    out = []
    tiered = [r for r in rows if _tag(_tags(r), "tier")]
    if tiered:
        out.append({"name": "Difficulty",
                    "rows": _group(tiered, lambda r: _tag(_tags(r), "tier"),
                                   TIER_ORDER)})
    if suite == "needle" or any("needle" in _tags(r) for r in rows):
        lengths = sorted({int(_tag(_tags(r), "chars") or 0) for r in rows})
        out.append({"name": "Context size",
                    "rows": _group(rows, lambda r: _kchars(_tag(_tags(r), "chars")),
                                   [_kchars(n) for n in lengths])})
        kinds = (("decoy", "beside a decoy"), ("multi", "three to collect"),
                 ("updated", "corrected later"), ("paraphrase", "asked in other words"),
                 # v0.181.224: many notes to put together
                 ("ctx-chain", "follow a chain of notes"), ("ctx-asof", "in force on a date"),
                 ("ctx-top", "compare several totals"),
                 ("ctx-blocked", "the status of many sprints"),
                 ("ctx-ledger", "add up a ledger"))

        def _kind(r):
            t = _tags(r)
            return next((label for k, label in kinds if k in t), "one needle")
        if any(k in _tags(r) for r in rows for k, _l in kinds):
            out.append({"name": "Kind of needle",
                        "rows": _group(rows, _kind,
                                       ["one needle"] + [label for _k, label in kinds])})
    if suite == "toolflow" or any("finder" in _tags(r) for r in rows):
        out.append({"name": "Tool server",
                    "rows": _group(rows, lambda r: _tag(_tags(r), "server") or "?",
                                   limit=10)})
    if suite == "toolpick":
        out.append({"name": "Kind of case",
                    "rows": _group(rows, lambda r: "none of the tools fits"
                                   if "none-fits" in _tags(r) else
                                   ("the tool has a look-alike" if "lookalike" in _tags(r)
                                    else "one tool clearly fits"),
                                   ("one tool clearly fits", "the tool has a look-alike",
                                    "none of the tools fits"))})
        out.append({"name": "Tool server",
                    "rows": _group([r for r in rows if "none-fits" not in _tags(r)],
                                   lambda r: (str(r["task_id"] or "").split("__", 1)[0]
                                              if "__" in str(r["task_id"] or "")
                                              else "router"), limit=10)})
    if suite == "protocol":
        names = {"done": "DONE", "not_done": "NOT DONE", "product": "BLOCKED: product",
                 "tooling": "BLOCKED: tooling", "user_action": "BLOCKED: user_action",
                 "scope": "a SCOPE_REQUEST"}
        out.append({"name": "What the turn had to say",
                    "rows": _group(rows, lambda r: names.get(_tag(_tags(r), "want") or "", None),
                                   list(names.values()))})
        out.append({"name": "Source",
                    "rows": _group(rows, lambda r: "real coordinator turn"
                                   if "real" in _tags(r) else "scenario")})
    if suite == "grounded":
        kinds = {"g-cite": "one record has it", "g-join": "two records put together",
                 "g-conflict": "the latest of several", "g-absent": "no record has it",
                 "g-counter": "the record against what it believes",
                 "g-scope": "only some tiers may be used", "g-tally": "a total over many records"}
        out.append({"name": "Kind of question",
                    "rows": _group(rows, lambda r: next(
                        (kinds[t] for t in _tags(r) if t in kinds), None),
                        list(kinds.values()))})
    if suite == "codereason":
        families = {"stacked": "several rules at once", "lying": "names that lie",
                    "trace": "long traces", "search": "too big to trace"}
        if any(_tag(_tags(r), "family") for r in rows):
            out.append({"name": "Kind of problem",
                        "rows": _group(rows, lambda r: families.get(
                            _tag(_tags(r), "family") or "", "short snippets"),
                            ["short snippets"] + list(families.values()))})
        out.append({"name": "Question",
                    "rows": _group(rows, lambda r: "what it raises"
                                   if "exception" in _tags(r) else "what it returns")})
    reach = ladder_reach(rows)
    if reach:
        # any suite with generated ladders (v0.181.223, every suite v0.181.224)
        out.insert(1 if out and out[0]["name"] == "Difficulty" else 0,
                   {"name": "How far up each ladder", "rows": reach})
    if suite not in ("needle", "toolflow", "toolpick", "codereason", "agent", "protocol",
                     "grounded"):
        out.append({"name": "What the verified step was",
                    "rows": _group(rows, lambda r: "a tool call"
                                   if _list(r["tools_expected"]) else "an answer in words")})
        generic = {"needle", "context", "toolflow", "finder", "tool_use", "toolpick", "split-turn",
                   "tool_choice", "codereason", "python", "code", "exception"}
        work = [r for r in rows if any(":" not in t and t not in generic
                                       for t in _tags(r))]
        if work:
            out.append({"name": "Kind of work",
                        "rows": _group(work, lambda r: next(
                            (t for t in _tags(r) if ":" not in t and t not in generic),
                            None), limit=8)})
    return [f for f in out if f["rows"]]


def misses(suite: str, rows: list) -> list:
    """How the misses happened, most common first."""
    counts: dict = {}
    for r in rows:
        k = miss_kind(suite, r)
        if k:
            counts[k] = counts.get(k, 0) + 1
    return [{"kind": k, "n": n} for k, n in
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def failures(svc, run_ids: list, suite: str, limit: int = 200) -> list:
    """Every case that missed, with what was wanted next to what came back."""
    out = []
    for rid in run_ids:
        for r in svc.db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id",
                              (int(rid),)):
            kind = miss_kind(suite, r)
            if not kind:
                continue
            tags = _tags(r)
            out.append({
                "run_id": int(rid), "case_id": r["case_id"], "task_id": r["task_id"],
                "kind": kind, "score": r["score"], "seconds": r["seconds"],
                "tier": _tag(tags, "tier") or "",
                "where": ", ".join(t for t in tags if t.startswith(("chars:", "depth:", "server:")))
                         + (", decoy" if "decoy" in tags else ""),
                "expected": r["expected"] or _old_expected(r),
                "answer": r["answer"] or _old_answer(r),
                "answer_kept": bool(r["answer"]),
                "tools_expected": _list(r["tools_expected"]),
                "tools_called": _list(r["tools_called"]),
                "detail": r["code_detail"] or "",
                "error": r["error"] or "",
            })
            if len(out) >= limit:
                return out
    return out


def _old_expected(r) -> str:
    """What a run from before v0.181.211 wanted, from what it did keep."""
    tags = _tags(r)
    if "finder" in tags and r["task_id"]:
        return f"use {r['task_id']}"
    want = _list(r["tools_expected"])
    if want:
        return "call " + ", then ".join(want)
    if r["tools_expected"] is not None and not any(
            t in tags for t in ("needle", "codereason", "toolpick")):
        return "answer in words, no tool"
    return ""


def _old_answer(r) -> str:
    """What a run from before v0.181.211 did, from what it did keep - the
    answer text itself was not stored then."""
    d = _detail(r)
    if "finder" in _tags(r) and d:
        picked = d.get("picked") or "-"
        return (f"used {picked}" if picked != "-" else "used no tool") + \
            f" after {d.get('searches', '?')} search(es)" + \
            ("" if d.get("found") == "True" else "; the right tool never came up")
    got = _list(r["tools_called"])
    return ("called " + ", ".join(got)) if got else ""


# --------------------------------------------------------------------------- #
# Speed                                                                       #
# --------------------------------------------------------------------------- #

def _median(xs) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


_SAMPLE_COLS = ("prompt_tokens, prefill_tokens, cached_tokens, completion_tokens, "
                "decode_tps, prefill_tps, eff_tps, prefill_ms, decode_ms, wall_ms, "
                "spec_accept_pct, timing_src, backend")


def samples_for(db, labels: list) -> list:
    if not labels:
        return []
    qs = ",".join("?" for _ in labels)
    return db.query(f"SELECT {_SAMPLE_COLS} FROM perf_samples "
                    f"WHERE run_label IN ({qs})", tuple(labels))


def speed(samples: list) -> dict:
    """Medians over one set of samples, with the unmeasurable ones left out."""
    measured = [s for s in samples if (s["timing_src"] or "") == "server"]
    pre = [s for s in measured
           if (s["prefill_tokens"] or s["prompt_tokens"] or 0) >= MIN_PREFILL_TOKENS
           and s["prefill_tps"] is not None]
    hidden_cache = [s for s in pre if s["prefill_tps"] > MAX_PLAUSIBLE_PREFILL_TPS]
    pre = [s for s in pre if s["prefill_tps"] <= MAX_PLAUSIBLE_PREFILL_TPS]
    # the smaller unreported hits, judged against this set's own median, as
    # perf_history judges a new sample (v0.181.212): samples recorded before
    # that still hold them, e.g. 3,428 tokens in 76 ms on the brain lane
    from .perf_history import UNREPORTED_CACHE_RATIO
    if len(pre) >= 8:
        med = statistics.median(float(s["prefill_tps"]) for s in pre)
        far = [s for s in pre if not (s["cached_tokens"] or 0)
               and s["prefill_tps"] > UNREPORTED_CACHE_RATIO * med]
        hidden_cache += far
        pre = [s for s in pre if s not in far]
    return {
        "samples": len(samples), "measured": len(measured),
        "decode_tps": _median(s["decode_tps"] for s in measured),
        "prefill_tps": _median(s["prefill_tps"] for s in pre),
        "eff_tps": _median(s["eff_tps"] for s in samples),
        "first_token_ms": _median(s["prefill_ms"] for s in measured),
        "wall_ms": _median(s["wall_ms"] for s in samples),
        "prompt_tokens": _median(s["prompt_tokens"] for s in samples),
        "out_tokens": _median(s["completion_tokens"] for s in samples),
        "spec_accept_pct": _median(s["spec_accept_pct"] for s in measured),
        "backend": next((s["backend"] for s in samples if s["backend"]), ""),
        # prompts served from a cache the backend did not report: their
        # "prefill" was a lookup, so they are counted and left out of the rate
        "unreported_cache": len(hidden_cache),
    }


_BUCKETS = ((0, 4000, "under 4k"), (4000, 16000, "4k-16k"),
            (16000, 32000, "16k-32k"), (32000, 10 ** 9, "32k and over"))


def by_context(samples: list) -> list:
    """Decode and prefill by how full the context was: the shape that says
    whether a model slows down at the context sizes this work uses."""
    out = []
    for lo, hi, name in _BUCKETS:
        ss = [s for s in samples if lo <= (s["prompt_tokens"] or 0) < hi]
        if not ss:
            continue
        sp = speed(ss)
        out.append({"context": name, "samples": len(ss),
                    "decode_tps": sp["decode_tps"], "prefill_tps": sp["prefill_tps"],
                    "first_token_ms": sp["first_token_ms"]})
    return out


# --------------------------------------------------------------------------- #
# The card, per suite and overall                                             #
# --------------------------------------------------------------------------- #

MEASURED_PASS = 90.0   # a replay move scored as the recorded one, or one look away


def _frontier(svc, model: str) -> bool:
    """Whether a model is served by a paid backend (a frontier model here)."""
    try:
        from .budget import is_paid
        for st in (getattr(getattr(svc, "pool", None), "backends", None) or {}).values():
            names = list(getattr(st, "models", None) or []) + list(
                getattr(getattr(st, "config", None), "models", None) or [])
            if model in names:
                return bool(is_paid(st.config))
    except Exception:                                             # noqa: BLE001
        pass
    return False


def measured_tiers(svc, suite: str, hashes: list) -> dict:
    """{case_id: tier} from what every model has done on this version of a suite
    (v0.181.225) - for a suite whose cases are cut from real work and carry no
    tier of their own. The same rule as benchhard's: easy = every local model
    passed it, medium = a local model missed and no frontier model did, hard =
    a frontier model missed, expert = two or more frontier models ran it and
    all missed. A case one model alone has run is not tiered, and a call that
    failed is not a miss: ten of Sonnet 5.5's thirty replay results were the
    backend being away."""
    hs = [h for h in (hashes or []) if h]
    if not hs:
        return {}
    marks = ",".join("?" for _ in hs)
    rows = svc.db.query(
        "SELECT r.case_id, r.score, r.error, b.model FROM bench_results r "
        "JOIN bench_runs b ON b.id = r.run_id WHERE b.suite=? AND b.suite_hash IN "
        f"({marks}) AND r.score IS NOT NULL ORDER BY b.id", (suite, *hs))
    latest: dict = {}
    for r in rows:
        if r["error"]:
            continue
        latest[(r["case_id"], r["model"])] = float(r["score"]) >= MEASURED_PASS
    paid: dict = {}
    by: dict = {}
    for (case, model), ok in latest.items():
        if model not in paid:
            paid[model] = _frontier(svc, model)
        by.setdefault(case, {True: [], False: []})[paid[model]].append(ok)
    out = {}
    for case, got in by.items():
        front, local = got[True], got[False]
        if len(front) + len(local) < 2:
            continue
        if front and not any(front):
            out[case] = "expert" if len(front) >= 2 else "hard"
        elif front and not all(front):
            out[case] = "hard"
        elif local and not all(local):
            out[case] = "medium"
        else:
            out[case] = "easy"
    return out


def enrich_part(svc, part: dict, runs: list) -> dict:
    """Add the breakdown, the misses, the speed and the ceiling to one suite."""
    from .bench import run_label
    suite = part["suite"]
    rows = []
    for r in runs:
        rows += svc.db.query("SELECT * FROM bench_results WHERE run_id=?", (r["id"],))
    scored = [r for r in rows if r["score"] is not None]
    part["title"] = SUITE_NAMES.get(suite, suite)
    part["asks"] = SUITE_ASKS.get(suite, "")
    part["passed"] = sum(1 for r in scored if float(r["score"]) >= 100)
    part["total"] = len(scored)
    part["facets"] = facets(suite, rows)
    part["misses"] = misses(suite, rows)
    part["ceiling"] = bool(part.get("score") is not None and part["score"] >= CEILING
                           and part["total"])
    labels = [run_label(r["id"], r["model"], r["label"] or "") for r in runs]
    part["speed"] = speed(samples_for(svc.db, labels))
    part["suite_hash"] = sorted({r["suite_hash"] or "" for r in runs
                                 if "suite_hash" in r.keys()})
    if not any(_tag(_tags(r), "tier") for r in rows):
        # no tiers of its own (the replay suite): tier it from what was measured
        try:
            tiers = measured_tiers(svc, suite, part["suite_hash"])
        except Exception:                                         # noqa: BLE001
            tiers = {}
        if tiers:
            part["facets"].insert(0, {
                "name": "Difficulty, from every model run on these cases",
                "rows": _group([r for r in rows if r["case_id"] in tiers],
                               lambda r: tiers.get(r["case_id"]), TIER_ORDER)})
    return part


def card_speed(svc, runs: list) -> dict:
    from .bench import run_label
    labels = [run_label(r["id"], r["model"], r["label"] or "") for r in runs]
    ss = samples_for(svc.db, labels)
    return {"overall": speed(ss), "by_context": by_context(ss)}


def verdict(card: dict) -> dict:
    """Where it excels and where it fails, read off the breakdowns.

    A suite counts as a strength only when it is not at the ceiling - acing a
    test everything aces says nothing - and a facet row only with enough cases
    to mean something."""
    good, bad, ceil = [], [], []
    for p in card.get("parts") or []:
        title = p.get("title") or p["suite"]
        if p.get("ceiling"):
            ceil.append(title)
        elif p.get("score") is not None and p["score"] >= STRONG:
            good.append({"what": title, "score": p["score"]})
        elif p.get("score") is not None and p["score"] < WEAK:
            bad.append({"what": title, "score": p["score"]})
        for f in p.get("facets") or []:
            if f["name"] == "How far up each ladder":
                continue         # one ladder is not a group: the tier rows say it
            for row in f["rows"]:
                if row["n"] < MIN_FACET_CASES:
                    continue
                if p.get("ceiling") and row["score"] >= STRONG:
                    # acing part of a suite everything aces says nothing
                    # (v0.181.218: "Long-context recall: expert" was listed)
                    continue
                what = f"{title}: {row['label']}"
                if row["score"] < WEAK:
                    bad.append({"what": what, "score": row["score"],
                                "n": row["n"]})
                elif (row["score"] >= STRONG and f["name"] == "Difficulty"
                      and row["label"] in ("hard", "expert")):
                    good.append({"what": what, "score": row["score"],
                                 "n": row["n"]})
    bad.sort(key=lambda x: x["score"])
    good.sort(key=lambda x: -x["score"])
    return {"strengths": good[:6], "weaknesses": bad[:8], "ceiling": ceil}


# --------------------------------------------------------------------------- #
# Live                                                                        #
# --------------------------------------------------------------------------- #

def _per_case_history(db, suite: str, model: str) -> Optional[float]:
    rows = db.query("SELECT seconds FROM bench_runs WHERE suite=? AND model=? "
                    "AND status='done' AND seconds IS NOT NULL ORDER BY id DESC LIMIT 5",
                    (suite, model))
    return _median(r["seconds"] for r in rows)


def _others_per_case(db, suite: str, model: str) -> Optional[float]:
    rows = db.query("SELECT seconds FROM bench_runs WHERE suite=? AND model<>? "
                    "AND status='done' AND seconds IS NOT NULL ORDER BY id DESC LIMIT 10",
                    (suite, model))
    return _median(r["seconds"] for r in rows)


def _suite_cases(svc, suite: str) -> int:
    try:
        from .bench import cases
        return len(cases(svc, suite))
    except Exception:                                             # noqa: BLE001
        return 0


def live(svc, now: Optional[float] = None) -> dict:
    """How far, how fast, how long: the progress strip's data.

    The current suite's remaining time is measured from this run's own pace.
    A suite still to come is estimated from this model's past runs of it, or
    failing that from other models' runs scaled by how this model's pace has
    compared with theirs on the suites already done - and the basis is said,
    so an estimate is never read as a promise."""
    from . import bench, benchprofile
    now = now or time.time()
    b = bench.state()
    p = benchprofile.state()
    out: dict = {"bench": b, "profile": p, "now": now}
    if b.get("state") != "running":
        return out
    done, cases = int(b.get("done") or 0), int(b.get("cases") or 0)
    elapsed = max(0.0, now - float(b.get("started") or now))
    per_case = (elapsed / done) if done else None
    model = b.get("model") or ""
    db = svc.db
    if per_case is None:
        # nothing finished yet: this model's own history of the suite, if any
        per_case = _per_case_history(db, b.get("suite") or "", model)
    run = {"elapsed_s": round(elapsed, 1), "done": done, "cases": cases,
           "per_case_s": round(per_case, 1) if per_case else None,
           "this_case_s": round(now - float(b.get("case_started") or now), 1),
           "eta_s": round((cases - done) * per_case, 0) if per_case else None,
           "pct": round(100.0 * done / cases, 1) if cases else None}
    if b.get("model_seconds") and b.get("out_tokens"):
        run["eff_tps"] = round(b["out_tokens"] / b["model_seconds"], 1)
    try:
        lab = b.get("run_label") or ""
        recent = db.query(f"SELECT {_SAMPLE_COLS} FROM perf_samples "
                          "WHERE run_label=? ORDER BY id DESC LIMIT 8",
                          (lab,)) if lab else []
        sp = speed(recent)
        run["decode_tps"] = sp["decode_tps"]
        run["prefill_tps"] = sp["prefill_tps"]
    except Exception:                                             # noqa: BLE001
        pass
    if done:
        run["passed"] = int(b.get("passed") or 0)
        run["errors"] = int(b.get("errors") or 0)
    out["run"] = run
    if p.get("state") == "running":
        names = list(p.get("suites") or [])
        cur = p.get("suite")
        idx = names.index(cur) if cur in names else int(p.get("done") or 0)
        reps = int(p.get("repeats") or 1)
        rep = int(p.get("rep") or 1)
        remaining = names[idx + 1:] + names * (reps - rep)
        # How this model's pace has compared with others' on what it has done,
        # suite by suite (v0.181.218). One ratio for everything was wrong: the
        # brain lane was 13x slower than flash on the 60k-token replay and 2-4x
        # on the short suites, and one median from the replay put a five-hour
        # profile at fourteen. A suite still to come takes the ratio of the done
        # suite whose per-case time (for the other models) is most like its own.
        import math
        pairs = []                               # (theirs per case, mine / theirs)
        for s in names[:idx]:
            mine = _per_case_history(db, s, model)
            theirs = _others_per_case(db, s, model)
            if mine and theirs:
                pairs.append((theirs, mine / theirs))
        if per_case and cur:
            theirs = _others_per_case(db, cur, model)
            if theirs:
                pairs.append((theirs, per_case / theirs))

        def factor_for(theirs_here):
            if not pairs:
                return None
            return min(pairs, key=lambda p: abs(math.log(max(p[0], 0.1))
                                                - math.log(max(theirs_here, 0.1))))[1]
        total, basis, unknown = run.get("eta_s") or 0.0, set(), []
        for s in remaining:
            n = _suite_cases(svc, s)
            own = _per_case_history(db, s, model)
            if own:
                total += n * own
                basis.add("this model's past runs")
                continue
            theirs = _others_per_case(db, s, model)
            f_here = factor_for(theirs) if theirs else None
            if theirs and f_here:
                total += n * theirs * f_here
                basis.add("other models' runs, scaled by this model's pace on the most "
                          "similar suite done")
                continue
            unknown.append(s)
        out["profile_eta"] = {
            "eta_s": round(total, 0) if not unknown else None,
            "partial_s": round(total, 0),
            "remaining": remaining, "unknown": unknown,
            "basis": "; ".join(sorted(basis)) or "this run's pace",
            "finish_at": (now + total) if not unknown else None,
            "suite_index": idx + 1, "suites": len(names) * reps,
        }
    return out

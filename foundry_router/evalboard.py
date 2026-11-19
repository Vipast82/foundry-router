"""The scoreboard: the same eval set across several personas, ranked into tiers.

One persona's eval run answers "did this change break it". It cannot answer
"which of these is better", which needs the same prompts, repeated, across every
candidate, with the spread reported rather than hidden.

Three things this refuses to do, each because the research on evaluating models
says the opposite is how reports mislead:

**It ranks clusters, not models.** A model starts a new tier only when its whole
confidence interval sits below the leader of the tier above. Two models three
points apart on nine prompts are one tier, and saying so is the honest answer -
an ordered list of nine-prompt scores is a precision that does not exist. With
few prompts almost everything lands in one tier, and `note` says how wide the
interval is, because the fix is more prompts, not a finer ranking.

**It reports repeatability first.** The same prompt asked twice of the same model
here does not reliably give the same answer: the router's own benchmark moves
about five points between identical runs, and per case about nine (see the
`bench-noise-floor` note). A board whose repeat-identity rate is below 1.0 has an
unmeasured noise floor under every number on it, so that rate is computed and
shown before any score.

**It puts score against cost.** The useful question is rarely "which is best" but
"which is best for what it costs", so tokens and latency sit beside the score and
the GUI draws the frontier. Cost is in tokens and seconds, never money, because
this router cannot know a subscription's marginal price (v0.181.190).
"""

from __future__ import annotations

import json
import math
import time
from typing import Optional

from .db import utcnow

TIER_LETTERS = ["S", "A", "B", "C", "D", "E", "F"]
# below this many scored prompts a tier letter is noise dressed as a grade
MIN_FOR_TIERS = 5


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """A 95% interval for a pass rate, as (low, high) in 0-1.

    Wilson rather than the textbook normal interval because the textbook one
    gives nonsense at the edges - 8 of 8 becomes "100% +/- 0%", which would read
    as certainty from eight prompts.
    """
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def tier_of(rows: list) -> list:
    """`rows` (dicts with score/lo/hi, best first) with a `tier` letter added.

    A row joins the tier above unless its whole interval is below that tier's
    leader - so a tier is "not distinguishable from the best of this group on the
    evidence here", which is what a reader takes a tier to mean.
    """
    out = []
    leader = None
    idx = 0
    for r in rows:
        r = dict(r)
        if r.get("scored", 0) < MIN_FOR_TIERS:
            r["tier"] = ""                      # not enough answers to grade
            out.append(r)
            continue
        if leader is None:
            leader = r
        elif r["hi"] < leader["lo"]:            # clear of the tier above
            idx = min(idx + 1, len(TIER_LETTERS) - 1)
            leader = r
        r["tier"] = TIER_LETTERS[idx]
        out.append(r)
    return out


def repeatability(db, run_ids: list) -> dict:
    """How much of this persona's score would move if the runs were done again.

    Two things, and only the first belongs on a board:

    `verdict` is how often a prompt's pass/fail came out the same in every
    repeat. That is what a score rests on, and 0.81 means about one prompt in
    five changed its mind, so the score moves by roughly that share of a prompt
    each time.

    `answers` is how often the text came back identical, and it is near zero for
    any prose answer, because prose varies. The first version of this board put
    that number up as "repeat-identity" and said every score sat on an unmeasured
    noise floor - on the first real campaign it read 6% and 18% while the
    verdicts agreed 81% and 94% of the time. It was measuring whether a model
    emits the same bytes twice, which is not news and not what anyone was asking.
    It is kept because it explains the other number, and labelled for what it is
    (v0.181.191).

    Only prompts answered more than once count. A prompt asked a single time
    cannot disagree with itself, and counting it as steady is the same fault as a
    check that cannot fail an answer: a campaign with repeats=1, or one read
    before its repeats finish, would have reported perfect stability from
    evidence that contains none. It returns None instead, so the board says the
    number is not measured rather than showing 100%.
    """
    out = {"verdict": None, "answers": None, "prompts": 0, "flipped": 0}
    if not run_ids:
        return out
    q = ",".join("?" * len(run_ids))
    verdicts: dict = {}
    answers: dict = {}
    for r in db.query(f"SELECT prompt_id, shape_pass, answer_hash FROM eval_results "
                      f"WHERE run_id IN ({q})", run_ids):
        verdicts.setdefault(r["prompt_id"], set()).add(int(r["shape_pass"] or 0))
        answers.setdefault(r["prompt_id"], set()).add(r["answer_hash"] or "")
    if not verdicts:
        return out
    # how many times each prompt was actually answered
    seen: dict = {}
    for r in db.query(f"SELECT prompt_id, COUNT(*) AS n FROM eval_results "
                      f"WHERE run_id IN ({q}) GROUP BY prompt_id", run_ids):
        seen[r["prompt_id"]] = int(r["n"] or 0)
    repeated = [p for p, n in seen.items() if n > 1]
    out["prompts"] = len(repeated)
    if not repeated:
        return out            # nothing was repeated, so nothing is measured
    steady = sum(1 for p in repeated if len(verdicts.get(p, {0})) == 1)
    same = sum(1 for p in repeated if len(answers.get(p, {""})) == 1)
    out.update({"verdict": steady / len(repeated),
                "answers": same / len(repeated),
                "flipped": len(repeated) - steady})
    return out


def scoreboard(db, campaign_id: Optional[int] = None, hours: float = 0) -> dict:
    """One row per persona: pass rate with its interval, tier, judge score,
    repeatability, and what it cost in tokens and seconds."""
    clauses, args = ["status='done'"], []
    if campaign_id:
        clauses.append("campaign_id=?")
        args.append(int(campaign_id))
    elif hours:
        clauses.append("ts >= datetime('now', ?)")
        args.append(f"-{float(hours)} hours")
    runs = db.query("SELECT * FROM eval_runs WHERE " + " AND ".join(clauses), args)
    by: dict = {}
    for r in runs:
        by.setdefault(r["persona"], []).append(r)
    rows = []
    for persona, rs in by.items():
        ids = [r["id"] for r in rs]
        q = ",".join("?" * len(ids))
        agg = db.query_one(
            f"SELECT COUNT(*) AS n, "
            f"SUM(COALESCE(shape_pass,0)) AS passed, "
            f"SUM(CASE WHEN COALESCE(error,'')<>'' THEN 1 ELSE 0 END) AS silent, "
            f"AVG(judge_score) AS judge, "
            f"AVG(duration_ms) AS ms, "
            f"SUM(COALESCE(prompt_tokens,0)) AS ptok, "
            f"SUM(COALESCE(completion_tokens,0)) AS ctok "
            f"FROM eval_results WHERE run_id IN ({q})", ids) or {}
        n = int(agg.get("n") or 0)
        passed = int(agg.get("passed") or 0)
        lo, hi = wilson(passed, n)
        rep = repeatability(db, ids)
        rows.append({
            "persona": persona, "runs": len(ids), "scored": n,
            "passed": passed,
            "score": round(100.0 * passed / n, 1) if n else 0.0,
            "lo": lo, "hi": hi,
            "interval": round(100.0 * (hi - lo), 1),
            "silent": int(agg.get("silent") or 0),
            "judge": round(float(agg["judge"]), 2) if agg.get("judge") is not None else None,
            "seconds": round(float(agg.get("ms") or 0) / 1000.0, 1),
            "tokens": int(agg.get("ptok") or 0) + int(agg.get("ctok") or 0),
            # the pass/fail verdict repeating is what a score rests on; the text
            # coming back identical is mostly a fact about prose
            "repeat_verdict": None if rep["verdict"] is None else round(rep["verdict"], 3),
            "repeat_answers": None if rep["answers"] is None else round(rep["answers"], 3),
            "repeat_prompts": rep["prompts"],
            "flipped": rep["flipped"],
        })
    # ungraded rows last whatever their score: a 95% off two prompts is not a
    # result, and putting it above a graded row would read as one
    rows.sort(key=lambda r: (r["scored"] < MIN_FOR_TIERS, -r["score"], r["persona"]))
    rows = tier_of(rows)
    worst = min([r["repeat_verdict"] for r in rows
                 if r["repeat_verdict"] is not None] or [None], default=None)
    notes = []
    if rows and max(r["scored"] for r in rows) < MIN_FOR_TIERS:
        notes.append(f"fewer than {MIN_FOR_TIERS} scored prompts: no tiers given")
    elif rows:
        widest = max(r["interval"] for r in rows)
        notes.append(f"the widest interval is {widest:.0f} points wide - "
                     f"models inside one tier are not separated by this evidence, "
                     f"and more prompts, not a finer ranking, is what narrows it")
    if worst is not None and worst < 1.0:
        flipped = max((r["flipped"] for r in rows), default=0)
        notes.append(f"verdicts repeated {worst:.0%} of the time - up to {flipped} "
                     f"prompt(s) per persona passed in one repeat and failed in "
                     f"another, which is the score's own movement run to run and "
                     f"is separate from the interval")
    return {"rows": rows, "note": " | ".join(notes),
            "min_for_tiers": MIN_FOR_TIERS}


# --------------------------------------------------------------------------- #
# Campaigns: the same prompts, repeated, across several personas              #
# --------------------------------------------------------------------------- #

_state: dict = {"state": "idle", "done": 0, "total": 0, "persona": "",
                "campaign_id": None, "stop": False, "error": ""}


def status() -> dict:
    return dict(_state)


def stop() -> dict:
    _state["stop"] = True
    return dict(_state)


def running() -> bool:
    return _state.get("state") == "running"


async def run_campaign(svc, personas: list, repeats: int = 3,
                       categories: Optional[list] = None,
                       judge_model: str = "", name: str = "") -> int:
    """Every persona over the same prompts, `repeats` times each.

    Alternating persona by repeat rather than finishing one before starting the
    next, so a machine that warms up, throttles or gets busy half way through
    does not hand all of that to whichever model happened to run then.
    """
    from .evalharness import EvalHarness
    reps = max(1, min(10, int(repeats or 1)))
    names = [p for p in personas if p]
    if not names:
        raise ValueError("pick at least one persona")
    cid = svc.db.execute(
        "INSERT INTO eval_campaigns (ts, name, personas, repeats, categories, "
        "judge_model, status) VALUES (?,?,?,?,?,?,'running')",
        (utcnow(), name or f"{len(names)} persona(s)", json.dumps(names), reps,
         json.dumps(categories or []), judge_model))
    _state.update({"state": "running", "done": 0, "total": len(names) * reps,
                   "campaign_id": cid, "stop": False, "error": "", "persona": ""})
    h = EvalHarness(svc)
    try:
        for rep in range(reps):
            for persona in names:
                if _state["stop"]:
                    break
                _state["persona"] = persona
                await h.run(persona, judge_model=judge_model,
                            categories=categories, campaign_id=cid)
                _state["done"] += 1
            if _state["stop"]:
                break
        svc.db.execute("UPDATE eval_campaigns SET status=? WHERE id=?",
                       ("stopped" if _state["stop"] else "done", cid))
    except Exception as e:                                        # noqa: BLE001
        from .errors import describe_exception
        _state["error"] = describe_exception(e)
        svc.db.execute("UPDATE eval_campaigns SET status='failed' WHERE id=?", (cid,))
        _state["state"] = "idle"
        raise
    _state["state"] = "idle"
    return cid


def campaigns(db, limit: int = 20) -> list:
    rows = db.query("SELECT * FROM eval_campaigns ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        for k in ("personas", "categories"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (ValueError, TypeError):
                d[k] = []
        out.append(d)
    return out

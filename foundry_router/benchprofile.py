"""A model profile: every suite, one model, one card.

Asked for: a way to run the suites together, get one score, and read two models
side by side well enough to decide whether a new model or a quantisation
variant will carry this ecosystem. Running them one at a time and holding five
numbers in your head is not that.

Three things here are judgement calls rather than arithmetic, and each is shown
rather than buried, because a composite score is exactly where a benchmark
stops being evidence and starts being an opinion with a number on it.

**The weights.** One score over five suites is a weighted average, and the
weights decide the answer. Equal weight per SUITE is the default - not per
case, which would let the 60-case baseline outvote the 28-case code suite by
sitting still. They are configurable, they are printed next to the number, and
the per-suite bars are the real content: a model that is strong everywhere and
hopeless at tool finding cannot run this ecosystem whatever its average says,
and only the bars show that.

**The noise.** This router's own benchmark moves about five points between
identical runs, so a three-point gap between two models is not a result. Rather
than printing that as a remembered constant, the spread is **measured from this
database**: the standard deviation over previous runs of the same suite and
model, used when there are at least three. A difference inside it is marked as
within noise and not called a win. With no history and one repeat, the card
says so instead of implying precision it has not earned.

**What has no score.** The agent tasks are deliverables with no answer key, so
they are never averaged in. They appear on the card as a count with a link to
the pairwise comparison, which is the only honest thing to say about them.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# Equal weight per suite. Not per case: the number of cases in a suite is a
# fact about how much work it was to build, not about how much it matters.
DEFAULT_WEIGHT = 1.0

# Suites that cannot contribute a score, whatever the weights say.
UNSCORED = ("agent",)

# Below this many past runs of the same suite+model, a standard deviation is
# noise about noise.
MIN_FOR_SD = 3

# What this router has measured about itself before any history exists: runs of
# one suite on one model have moved about this much. Used only to say "a gap
# smaller than this is not a result", never to fill in a number.
ASSUMED_SPREAD = 5.0

# Suites whose scoring changed meaning with bench.SCORER_VERSION 2
# (v0.181.211): a run of one from before that version is a different test.
RESCORED_IN_V2 = ("toolflow",)

_state: dict = {"state": "idle"}
_lock = threading.Lock()


def state() -> dict:
    return dict(_state)


# --------------------------------------------------------------------------- #
# Weights                                                                     #
# --------------------------------------------------------------------------- #

def weights(svc, suites) -> dict:
    """{suite: weight}, from config where set, 1.0 otherwise.

    A suite the operator has weighted 0 is kept in the profile and left out of
    the composite: seeing a suite scored and deliberately not counted is
    different from not knowing it exists."""
    try:
        cfg = dict(getattr(svc.config_store.config.bench, "suite_weights", {}) or {})
    except AttributeError:
        cfg = {}
    out = {}
    for s in suites:
        if s in UNSCORED:
            continue
        try:
            out[s] = max(0.0, float(cfg.get(s, DEFAULT_WEIGHT)))
        except (TypeError, ValueError):
            out[s] = DEFAULT_WEIGHT
    return out


# --------------------------------------------------------------------------- #
# Noise, measured rather than remembered                                      #
# --------------------------------------------------------------------------- #

def spread(db, suite: str, model: str, before_id: int = 0) -> Optional[float]:
    """Standard deviation of past scores for this suite and model.

    None when there is not enough history to say. `before_id` excludes the run
    being reported, so a run is never part of its own noise estimate."""
    sql = ("SELECT score FROM bench_runs WHERE suite=? AND model=? "
           "AND status='done' AND score IS NOT NULL")
    args: list = [suite, model]
    if before_id:
        sql += " AND id<>?"
        args.append(int(before_id))
    rows = [r["score"] for r in db.query(sql + " ORDER BY id DESC LIMIT 25", tuple(args))]
    if len(rows) < MIN_FOR_SD:
        return None
    mean = sum(rows) / len(rows)
    var = sum((x - mean) ** 2 for x in rows) / (len(rows) - 1)
    return round(math.sqrt(var), 2)


def within_noise(a: Optional[float], b: Optional[float],
                 sd_a: Optional[float], sd_b: Optional[float]) -> bool:
    """Whether a difference is too small to call.

    The comparison is against the combined spread of the two sides, and where
    neither side has history the floor this router has measured about itself
    stands in - so an unmeasured pair is treated as noisy rather than as
    precise."""
    if a is None or b is None:
        return True
    s = [x for x in (sd_a, sd_b) if x is not None]
    band = math.sqrt(sum(x * x for x in s)) if s else ASSUMED_SPREAD
    return abs(a - b) <= max(band, 0.1)


# --------------------------------------------------------------------------- #
# Running every suite                                                         #
# --------------------------------------------------------------------------- #

def runnable_suites(svc) -> list:
    """Every built suite, scorable ones first, unscored ones last."""
    from .bench import suites as built
    names = [s["name"] for s in built(svc)]
    return ([n for n in names if n not in UNSCORED]
            + [n for n in names if n in UNSCORED])


def start(svc, model: str, suites=(), label: str = "", repeats: int = 1,
          limit: int = 0) -> dict:
    """Run every named suite against one model, in sequence, as one profile.

    Sequential on purpose: these share one GPU, and two at once would measure
    the contention rather than the model."""
    from . import bench
    if _state.get("state") == "running" or bench.state().get("state") == "running":
        return dict(_state)
    names = [s for s in (suites or runnable_suites(svc))]
    if not names:
        return {"state": "failed", "error": "no suites are built yet"}
    if not _lock.acquire(blocking=False):
        return dict(_state)
    reps = max(1, min(10, int(repeats or 1)))
    db = svc.db
    db.execute("INSERT INTO bench_profiles (ts, model, label, suites, repeats, "
               "status) VALUES (datetime('now'), ?, ?, ?, ?, 'running')",
               (model, label, json.dumps(names), reps))
    pid = db.query_one("SELECT MAX(id) AS id FROM bench_profiles")["id"]
    _state.clear()
    _state.update({"state": "running", "profile_id": pid, "model": model,
                   "suites": names, "repeats": reps, "done": 0,
                   "total": len(names) * reps, "started": time.time()})

    def go():
        import asyncio
        try:
            asyncio.run(_run(svc, pid, model, names, label, reps, limit))
        except Exception as e:                                    # noqa: BLE001
            from .errors import describe_exception
            msg = describe_exception(e)
            _state.update({"state": "failed", "error": msg,
                           "finished": time.time()})
            db.execute("UPDATE bench_profiles SET status='failed', note=? "
                       "WHERE id=?", (msg[:300], pid))
        finally:
            _lock.release()

    threading.Thread(target=go, name="foundry-profile", daemon=True).start()
    time.sleep(0.05)
    return dict(_state)


def stop() -> dict:
    if _state.get("state") == "running":
        _state["stop"] = True
    from . import bench
    bench.stop()
    return dict(_state)


async def _run(svc, pid: int, model: str, names: list, label: str,
               reps: int, limit: int) -> dict:
    from . import bench
    db = svc.db
    done = 0
    for rep in range(1, reps + 1):
        for suite in names:
            if _state.get("stop"):
                break
            _state.update({"suite": suite, "rep": rep})
            out = await bench._run(svc, suite, model,
                                   label or f"profile {pid}", limit)
            db.execute("UPDATE bench_runs SET profile_id=? WHERE id=?",
                       (pid, out["run_id"]))
            done += 1
            _state["done"] = done
        if _state.get("stop"):
            break
    card = build_card(svc, pid)
    db.execute("UPDATE bench_profiles SET status=?, score=?, note=? WHERE id=?",
               ("stopped" if _state.get("stop") else "done",
                card.get("composite"), (card.get("note") or "")[:600], pid))
    _state.update({"state": "done", "finished": time.time(),
                   "composite": card.get("composite")})
    return card


# --------------------------------------------------------------------------- #
# The card                                                                    #
# --------------------------------------------------------------------------- #

def build_card(svc, profile_id: int) -> dict:
    """Everything the page draws for one profile."""
    db = svc.db
    p = db.query_one("SELECT * FROM bench_profiles WHERE id=?", (int(profile_id),))
    if p is None:
        return {"error": f"there is no profile {profile_id}"}
    runs = db.query("SELECT * FROM bench_runs WHERE profile_id=? ORDER BY id",
                    (int(profile_id),))
    by_suite: dict = {}
    for r in runs:
        by_suite.setdefault(r["suite"], []).append(r)
    w = weights(svc, list(by_suite))
    parts, unscored = [], []
    for suite, rows in by_suite.items():
        scores = [r["score"] for r in rows if r["score"] is not None]
        seconds = [r["seconds"] for r in rows if r["seconds"] is not None]
        cases = max((r["cases"] or 0) for r in rows)
        part = {
            "suite": suite, "runs": len(rows), "cases": cases,
            "run_ids": [r["id"] for r in rows],
            "score": round(sum(scores) / len(scores), 1) if scores else None,
            "low": round(min(scores), 1) if scores else None,
            "high": round(max(scores), 1) if scores else None,
            "seconds": round(sum(seconds) / len(seconds), 2) if seconds else None,
            "weight": w.get(suite),
            "sd": spread(db, suite, p["model"], before_id=0) if scores else None,
            "delivered": sum(int(r["cases"] or 0) for r in rows)
            if suite in UNSCORED else 0,
        }
        try:
            # the breakdown, the misses, the speed, the ceiling (v0.181.211)
            from . import benchcard
            benchcard.enrich_part(svc, part, rows)
        except Exception:                                         # noqa: BLE001
            log.exception("profile %s: could not break down %s", profile_id, suite)
        if part["score"] is None or suite in UNSCORED:
            unscored.append(part)
        else:
            parts.append(part)
    card = {
        "profile_id": int(profile_id), "model": p["model"], "ts": p["ts"],
        "label": p["label"] or "", "status": p["status"],
        "repeats": p["repeats"], "parts": parts, "unscored": unscored,
    }
    card.update(composite(parts))
    card["needle"] = _needle_grid(svc, by_suite.get("needle") or [])
    try:
        from . import benchcard
        card["speed"] = benchcard.card_speed(svc, runs)
        card["verdict"] = benchcard.verdict(card)
    except Exception:                                             # noqa: BLE001
        log.exception("profile %s: could not read its speed", profile_id)
        card["speed"], card["verdict"] = {}, {}
    card["note"] = "; ".join(_notes(card))
    return card


def composite(parts: list) -> dict:
    """The one number, and what it is made of.

    Equal weight per suite by default. A suite weighted 0 is counted as
    deliberately excluded rather than missing."""
    used = [p for p in parts if (p.get("weight") or 0) > 0
            and p.get("score") is not None]
    excluded = [p["suite"] for p in parts if not (p.get("weight") or 0)]
    if not used:
        return {"composite": None, "weighted": [], "excluded": excluded}
    total = sum(p["weight"] for p in used)
    value = sum(p["score"] * p["weight"] for p in used) / total
    return {
        "composite": round(value, 1),
        "weighted": [{"suite": p["suite"], "score": p["score"],
                      "weight": p["weight"],
                      "share": round(p["weight"] / total, 3)} for p in used],
        "excluded": excluded,
    }


def _needle_grid(svc, runs: list) -> dict:
    """The context grid, because one number cannot say WHERE it stops finding
    things - which is the only question a context benchmark answers."""
    if not runs:
        return {}
    from .bench import needle_breakdown
    # needle_breakdown returns chars/depth/cases/score - read from it rather
    # than from what this function wished it returned
    cells: dict = {}
    for r in runs:
        for row in needle_breakdown(svc.db, r["id"]):
            key = (int(row["chars"] or 0), int(row["depth"] or 0))
            c = cells.setdefault(key, {"sum": 0.0, "cases": 0})
            if row.get("score") is not None:
                c["sum"] += float(row["score"]) * int(row["cases"] or 0)
                c["cases"] += int(row["cases"] or 0)
    grid = [{"chars": k[0], "depth": k[1], "cases": v["cases"],
             "score": round(v["sum"] / v["cases"], 1) if v["cases"] else None}
            for k, v in sorted(cells.items())]
    return {"cells": grid,
            "lengths": sorted({c["chars"] for c in grid}),
            "depths": sorted({c["depth"] for c in grid})}


def _notes(card: dict) -> list:
    """What has to be read before the big number is."""
    out = []
    if card.get("composite") is None:
        out.append("no suite in this profile produced a score")
        return out
    if int(card.get("repeats") or 1) < 2:
        out.append("one run per suite: this router's benchmark has moved about "
                   f"{ASSUMED_SPREAD:g} points between identical runs, so read "
                   "a gap smaller than that as no gap")
    weak = [p["suite"] for p in card.get("parts") or []
            if p.get("score") is not None and p["score"] < 50]
    if weak:
        out.append("weak where it matters: " + ", ".join(weak)
                   + " - the average hides this, the bars do not")
    if card.get("excluded"):
        out.append("weighted 0 and left out: " + ", ".join(card["excluded"]))
    if card.get("unscored"):
        out.append("; ".join(
            f"{p['suite']} has no answer key ({p['cases']} task(s)) and is "
            "never scored - compare two profiles instead"
            for p in card["unscored"]))
    return out


# --------------------------------------------------------------------------- #
# Two or more profiles, side by side                                          #
# --------------------------------------------------------------------------- #

def compare(svc, profile_ids: list) -> dict:
    """The side-by-side view: every suite, every model, and which differences
    are too small to call."""
    cards = [build_card(svc, int(p)) for p in profile_ids]
    cards = [c for c in cards if not c.get("error")]
    if not cards:
        return {"cards": [], "rows": [], "note": "no profile was found"}
    order, seen = [], set()
    for c in cards:
        for p in c["parts"] + c["unscored"]:
            if p["suite"] not in seen:
                seen.add(p["suite"])
                order.append(p["suite"])
    base = cards[0]
    rows = []
    for suite in order:
        cells = []
        for c in cards:
            p = next((x for x in c["parts"] + c["unscored"]
                      if x["suite"] == suite), None)
            cells.append({"profile_id": c["profile_id"],
                          "score": (p or {}).get("score"),
                          "sd": (p or {}).get("sd"),
                          "low": (p or {}).get("low"),
                          "high": (p or {}).get("high"),
                          "seconds": (p or {}).get("seconds"),
                          "passed": (p or {}).get("passed"),
                          "total": (p or {}).get("total"),
                          "ceiling": (p or {}).get("ceiling"),
                          "speed": (p or {}).get("speed") or {},
                          "facets": (p or {}).get("facets") or [],
                          "suite_hash": (p or {}).get("suite_hash") or [],
                          "cases": (p or {}).get("cases"),
                          "missing": p is None,
                          "unscored": bool(p and p in c["unscored"])})
        first = cells[0]
        for cell in cells[1:]:
            cell["delta"] = (None if cell["score"] is None
                             or first["score"] is None
                             else round(cell["score"] - first["score"], 1))
            cell["tie"] = within_noise(first["score"], cell["score"],
                                       first["sd"], cell["sd"])
            # a suite rebuilt between the two (harder cases, a new scorer) is
            # a different test: the difference is shown and never called a
            # win or a tie, since it is not about the models (v0.181.211)
            ha = {h for h in first["suite_hash"] if h}
            hb = {h for h in cell["suite_hash"] if h}
            # one side from before suite versions were recorded: the same
            # test only if it has as many cases AND its scorer did not change
            one_sided = bool(ha) != bool(hb)
            if (ha and hb and ha != hb) or (one_sided and (
                    first.get("cases") != cell.get("cases")
                    or suite in RESCORED_IN_V2)):
                cell["changed"] = True
                cell["tie"] = False
        try:
            from .benchcard import SUITE_NAMES
            title = SUITE_NAMES.get(suite, suite)
        except Exception:                                         # noqa: BLE001
            title = suite
        rows.append({"suite": suite, "title": title, "cells": cells,
                     "changed": any(c.get("changed") for c in cells)})
    out = {"cards": cards, "rows": rows, "baseline": base["profile_id"],
           "note": ""}
    comps = [c.get("composite") for c in cards]
    sets = [frozenset(w["suite"] for w in c.get("weighted") or []) for c in cards]
    out["same_suites"] = len(set(sets)) <= 1
    if not out["same_suites"]:
        # two averages over different suites are not two measurements of one
        # thing - a profile still running, or one a suite was added to, would
        # be ranked on what it has not been asked yet (v0.181.211)
        missing = sorted(set().union(*sets) - set.intersection(*map(set, sets)))
        out["note"] = ("the composites cover different suites (" + ", ".join(missing)
                       + " not in every profile), so they are not comparable; "
                       "read the suites row by row")
        return out
    if len([x for x in comps if x is not None]) > 1:
        a, b = comps[0], comps[1]
        if within_noise(a, b, _card_sd(cards[0]), _card_sd(cards[1])):
            out["note"] = (f"the two composites ({a} and {b}) are inside the "
                           "spread these runs have shown, so this evidence "
                           "does not separate them - add repeats rather than "
                           "reading the order")
    return out


def _card_sd(card: dict) -> Optional[float]:
    """The spread of a card's composite, from its suites' own spreads."""
    sds = [p["sd"] for p in card.get("parts") or [] if p.get("sd") is not None]
    if not sds:
        return None
    # equal weights: the mean of independent parts narrows by sqrt(n)
    return round(math.sqrt(sum(x * x for x in sds)) / len(sds), 2)


def profiles(db, limit: int = 40) -> list:
    return db.query("SELECT * FROM bench_profiles ORDER BY id DESC LIMIT ?",
                    (int(limit),))

"""The all-local ladder report (v0.181.245, plan A9).

The benchmark measures each model on each kind of work; this puts a rule on
top. Every suite is a kind of work with a threshold (70 unless the operator
sets another). A model passes a kind of work when its score on the current
version of that suite - the mean of its latest runs on it - reaches the
threshold, and it passes "through" a tier when every tier up to that one does.

Per persona, the ladder is the local models in its own chain, in order (or a
list the operator sets), and the kinds of work it is judged on are those of
its benchmark category. For each kind the report names the first rung that is
good enough; where none is, the first cloud model in the chain that passes.
A cloud rung that no kind of work still needs could go - said only when every
kind of work has been measured, and never acted on: this is a report, and no
route changes because of it.
"""
from __future__ import annotations

from typing import Optional

DEFAULT_THRESHOLD = 70.0
RUNS_PER_RUNG = 3
TIER_ORDER = ("easy", "medium", "hard", "expert")
CATEGORY_FAMILIES = {
    "general_chat": ("grounded", "guard", "heldrule", "summaries", "needle", "structured"),
    "coding": ("codereason", "protocol", "structured", "toolsteps", "toolflow", "baseline"),
    "agentic": ("toolflow", "toolpick", "toolsteps", "guard", "protocol", "baseline"),
}


def _cfg(svc):
    try:
        return svc.config_store.config.bench
    except AttributeError:
        return None


def threshold(svc, suite: str) -> float:
    got = dict(getattr(_cfg(svc), "ladder_thresholds", {}) or {}).get(suite)
    try:
        return float(got) if got is not None else DEFAULT_THRESHOLD
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def families_for(svc, category: str) -> list:
    """The kinds of work a persona of this benchmark category is judged on."""
    own = dict(getattr(_cfg(svc), "ladder_families", {}) or {}).get(category or "")
    if own:
        return [str(s) for s in own]
    return list(CATEGORY_FAMILIES.get(category or "", ()))


def _backend_of(svc, model: str) -> tuple:
    """(backend name, paid) for a model, from the pool; ("", False) when no
    backend serves it."""
    try:
        from .budget import is_paid
        for name, st in (getattr(getattr(svc, "pool", None), "backends", None) or {}).items():
            names = list(getattr(st, "models", None) or []) + list(
                getattr(getattr(st, "config", None), "models", None) or [])
            if model in names:
                return str(name), bool(is_paid(st.config))
    except Exception:                                             # noqa: BLE001
        pass
    return "", False


def _tier_scores(svc, suite: str, run_ids: list, hashes: list) -> dict:
    """{tier: mean case score} over these runs: from the cases' own tier tags,
    or for a suite with none, from what every model run on it did."""
    from .benchcard import _tag, _tags, measured_tiers
    rows = []
    for rid in run_ids:
        rows += svc.db.query("SELECT case_id, score, error, tags FROM bench_results "
                             "WHERE run_id=? AND score IS NOT NULL", (rid,))
    own = {r["case_id"]: _tag(_tags(r), "tier") for r in rows}
    if not any(own.values()):
        try:
            own = measured_tiers(svc, suite, hashes)
        except Exception:                                         # noqa: BLE001
            own = {}
    by: dict = {}
    for r in rows:
        t = own.get(r["case_id"])
        if t in TIER_ORDER:
            by.setdefault(t, []).append(float(r["score"]))
    return {t: round(sum(v) / len(v), 1) for t, v in by.items() if v}


def through(tiers: dict, bar: float) -> str:
    """The highest tier such that it and every tier below it that has cases
    reaches the bar; "" when the easiest tier does not."""
    best = ""
    for t in TIER_ORDER:
        if t not in tiers:
            continue
        if tiers[t] < bar:
            break
        best = t
    return best


def evidence(svc) -> dict:
    """{suite: {model: rung}} from finished runs on the current version of each
    suite: the mean of a model's latest RUNS_PER_RUNG runs. A run on an older
    version of a suite is a different test and is not counted."""
    from .bench import suite_hash
    rows = svc.db.query("SELECT id, suite, model, score, seconds, suite_hash, ts "
                        "FROM bench_runs WHERE status='done' AND score IS NOT NULL "
                        "ORDER BY id DESC")
    current: dict = {}
    picked: dict = {}
    older: dict = {}
    for r in rows:
        s = r["suite"]
        if s not in current:
            current[s] = suite_hash(svc, s)
        h = r["suite_hash"] or ""
        if current[s] and h and h != current[s]:
            older.setdefault(s, set()).add(r["model"])
            continue
        got = picked.setdefault(s, {}).setdefault(r["model"], [])
        if len(got) < RUNS_PER_RUNG:
            got.append(r)
    out: dict = {}
    for s, models in picked.items():
        bar = threshold(svc, s)
        for m, runs in models.items():
            scores = [float(x["score"]) for x in runs]
            secs = [float(x["seconds"]) for x in runs if x["seconds"] is not None]
            backend, paid = _backend_of(svc, m)
            tiers = _tier_scores(svc, s, [x["id"] for x in runs],
                                 sorted({x["suite_hash"] or "" for x in runs}))
            score = round(sum(scores) / len(scores), 1)
            out.setdefault(s, {})[m] = {
                "model": m, "backend": backend, "local": not paid, "score": score,
                "runs": len(runs), "low": round(min(scores), 1), "high": round(max(scores), 1),
                "seconds": round(sum(secs) / len(secs), 1) if secs else None,
                "tiers": tiers, "through": through(tiers, bar), "passes": score >= bar,
                "last": max(x["ts"] or "" for x in runs)}
    for s, ms in older.items():
        for m in ms:
            out.setdefault(s, {}).setdefault(m, None)
    return out


def _title(suite: str) -> str:
    from .benchcard import SUITE_NAMES
    return SUITE_NAMES.get(suite, suite)


def _rung_words(r: dict, bar: float) -> str:
    s = f"{r['model']} {'passes' if r['passes'] else 'misses'} at {r['score']:g}"
    if r["through"]:
        s += f", through {r['through']}"
    if r["seconds"] is not None:
        s += f", {r['seconds']:g} s a case"
    if r["runs"] > 1:
        s += f" ({r['runs']} runs, {r['low']:g} to {r['high']:g})"
    return s


def family(svc, suite: str, rungs: dict) -> dict:
    """One kind of work: every model measured on it, and the sentence."""
    bar = threshold(svc, suite)
    measured = [r for r in rungs.values() if r]
    stale = sorted(m for m, r in rungs.items() if r is None)
    local = sorted((r for r in measured if r["local"]), key=lambda r: -r["score"])
    cloud = sorted((r for r in measured if not r["local"]), key=lambda r: -r["score"])
    passing = [r for r in local if r["passes"]]
    words = [_rung_words(r, bar) for r in passing]
    top_local = max((TIER_ORDER.index(r["through"]) for r in passing if r["through"]),
                    default=-1)
    top_cloud = max(((TIER_ORDER.index(r["through"]), r) for r in cloud
                     if r["passes"] and r["through"]), default=(-1, None), key=lambda x: x[0])
    if not passing:
        words.append("no local model passes" + (
            "; " + ", ".join(_rung_words(r, bar) for r in local) if local else
            " (none measured)"))
        if cloud and cloud[0]["passes"]:
            words.append(_rung_words(cloud[0], bar))
    elif top_cloud[1] is not None and top_cloud[0] > top_local:
        words.append(f"{TIER_ORDER[top_local + 1]} needs {top_cloud[1]['model']}")
    sentence = f"{_title(suite)} (threshold {bar:g}): " + "; ".join(words)
    return {"suite": suite, "title": _title(suite), "threshold": bar,
            "rungs": local + cloud, "good_enough": [r["model"] for r in passing],
            "stale": stale, "sentence": sentence}


def persona(svc, p: dict, fams: dict) -> Optional[dict]:
    """One persona: its ladder, the kinds of work it is judged on, the rung that
    is good enough for each, and the cloud rungs nothing needs."""
    import json
    raw = p.get("pinned_models") or "[]"
    try:
        chain = [str(m) for m in (raw if isinstance(raw, list) else json.loads(raw))]
    except (TypeError, ValueError):
        chain = []
    if not chain:
        return None
    name = p.get("virtual_name") or ""
    paid = {m: _backend_of(svc, m)[1] for m in chain}
    own = dict(getattr(_cfg(svc), "local_ladders", {}) or {}).get(name)
    ladder = [str(m) for m in own] if own else [m for m in chain if not paid[m]]
    cloud = [m for m in chain if paid[m] and m not in ladder]
    wanted = families_for(svc, p.get("benchmark_category") or "") or sorted(fams)
    rows, needed, unmeasured = [], set(), []
    for s in wanted:
        rungs = {m: r for m, r in (fams.get(s) or {}).items() if r}
        bar = threshold(svc, s)
        local = next((rungs[m] for m in ladder if m in rungs and rungs[m]["passes"]), None)
        if local:
            rows.append({"suite": s, "title": _title(s), "threshold": bar, "rung": local["model"],
                         "score": local["score"], "through": local["through"], "cloud": "",
                         "sentence": f"{_title(s)}: {local['model']} passes at "
                                     f"{local['score']:g}, threshold {bar:g}"})
            continue
        if not any(m in rungs for m in ladder + cloud):
            unmeasured.append(s)
            rows.append({"suite": s, "title": _title(s), "threshold": bar, "rung": "",
                         "score": None, "through": "", "cloud": "",
                         "sentence": f"{_title(s)}: not measured on this persona's models"})
            continue
        up = next((rungs[m] for m in cloud if m in rungs and rungs[m]["passes"]), None)
        best = max((rungs[m] for m in ladder if m in rungs), key=lambda r: r["score"],
                   default=None)
        said = (f"{best['model']} reaches {best['score']:g}" if best
                else "no local rung measured")
        if up:
            needed.add(up["model"])
        rows.append({"suite": s, "title": _title(s), "threshold": bar, "rung": "",
                     "score": best["score"] if best else None, "through": "",
                     "cloud": up["model"] if up else "",
                     "sentence": f"{_title(s)}: no local rung passes ({said}, threshold "
                                 f"{bar:g})" + (f"; {up['model']} passes at {up['score']:g}"
                                                if up else "; no model in the chain passes")})
    could_go = [] if unmeasured else [m for m in cloud if m not in needed]
    if unmeasured:
        verdict = (f"not every kind of work is measured ({', '.join(_title(s) for s in unmeasured)}),"
                   " so no cloud rung can be called unneeded")
    elif not cloud:
        verdict = "no cloud rung in the chain"
    elif could_go and len(could_go) == len(cloud):
        verdict = "a local rung is good enough for every kind of work; the cloud rungs " \
                  + ", ".join(could_go) + " are not needed for it"
    elif could_go:
        verdict = "needed: " + ", ".join(sorted(needed)) + "; not needed for any kind of " \
                  "work: " + ", ".join(could_go)
    else:
        verdict = "every cloud rung is still needed for some kind of work"
    return {"persona": name, "category": p.get("benchmark_category") or "",
            "ladder": ladder, "ladder_set": bool(own), "cloud": cloud, "families": rows,
            "could_go": could_go, "unmeasured": unmeasured, "verdict": verdict}


def report(svc) -> dict:
    """Every kind of work measured, and every enabled persona with a chain."""
    from .benchcard import SUITE_NAMES
    fams = evidence(svc)
    order = [s for s in SUITE_NAMES if s in fams] + sorted(s for s in fams
                                                           if s not in SUITE_NAMES)
    families = [family(svc, s, fams[s]) for s in order]
    people = []
    try:
        for p in svc.db.query("SELECT * FROM personas WHERE enabled=1 ORDER BY virtual_name"):
            got = persona(svc, dict(p), fams)
            if got:
                people.append(got)
    except Exception:                                             # noqa: BLE001
        people = []
    return {"default_threshold": DEFAULT_THRESHOLD, "runs_per_rung": RUNS_PER_RUNG,
            "families": families, "personas": people,
            "thresholds": {s: threshold(svc, s) for s in SUITE_NAMES},
            "category_families": {c: families_for(svc, c) for c in CATEGORY_FAMILIES},
            "note": "" if families else
            "nothing measured yet: build the suites and run models on them, and this fills in"}

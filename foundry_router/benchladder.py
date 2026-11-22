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


# One run of this router's benchmark moves about five points (bench noise
# floor), so a score this much higher is the least that says a model did better.
NOISE = 5.0


def _tier_scores(svc, suite: str, run_ids: list, hashes: list, source: Optional[list] = None
                 ) -> dict:
    """{tier: mean case score} over these runs: from the cases' own tier tags,
    or for a suite with none, from what every model run on it did. `source`, when
    given, gets "cases" or "runs" appended: which of the two it was."""
    from .benchcard import _tag, _tags, measured_tiers
    rows = []
    for rid in run_ids:
        rows += svc.db.query("SELECT case_id, score, error, tags FROM bench_results "
                             "WHERE run_id=? AND score IS NOT NULL", (rid,))
    own = {r["case_id"]: _tag(_tags(r), "tier") for r in rows}
    if source is not None:
        source.append("cases" if any(own.values()) else "runs")
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
            src: list = []
            tiers = _tier_scores(svc, s, [x["id"] for x in runs],
                                 sorted({x["suite_hash"] or "" for x in runs}), src)
            score = round(sum(scores) / len(scores), 1)
            out.setdefault(s, {})[m] = {
                "model": m, "backend": backend, "local": not paid, "score": score,
                "runs": len(runs), "low": round(min(scores), 1), "high": round(max(scores), 1),
                "seconds": round(sum(secs) / len(secs), 1) if secs else None,
                "tiers": tiers, "through": through(tiers, bar), "passes": score >= bar,
                # v0.181.251: tiers the cases carry can be compared between
                # models; tiers made from which model missed what cannot - each
                # model's own misses would be a tier the other one "holds"
                "tiers_from": src[0] if src else "runs",
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
    # a tier made from which model missed what says nothing on its own (v0.181.251)
    if r["through"] and r.get("tiers_from", "cases") == "cases":
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
    # a harder tier is only "needed" where the cases carry their tiers (v0.181.251)
    top_cloud = max(((TIER_ORDER.index(r["through"]), r) for r in cloud
                     if r["passes"] and r["through"] and r.get("tiers_from") == "cases"),
                    default=(-1, None), key=lambda x: x[0])
    if not passing:
        words.append("no local model passes" + (
            "; " + ", ".join(_rung_words(r, bar) for r in local) if local else
            " (none measured)"))
        if cloud and cloud[0]["passes"]:
            words.append(_rung_words(cloud[0], bar))
        elif cloud:
            # v0.181.251: no cloud model passes either - say so, with the best
            words.append("no cloud model passes either; " + _rung_words(cloud[0], bar))
    elif top_cloud[1] is not None and top_cloud[0] > top_local and             all(r.get("tiers_from") == "cases" for r in passing):
        words.append(f"{TIER_ORDER[top_local + 1]} needs {top_cloud[1]['model']}")
    elif cloud and cloud[0]["score"] - passing[0]["score"] > NOISE:
        words.append(f"{cloud[0]['model']} does better at {cloud[0]['score']:g}")
    sentence = f"{_title(suite)} (threshold {bar:g}): " + "; ".join(words)
    return {"suite": suite, "title": _title(suite), "threshold": bar,
            "rungs": local + cloud, "good_enough": [r["model"] for r in passing],
            "stale": stale, "sentence": sentence}


def _tier_index(r: Optional[dict]) -> int:
    """How far up the tiers a rung holds: -1 for none."""
    return TIER_ORDER.index(r["through"]) if r and r.get("through") in TIER_ORDER else -1


def _beats(cloud: dict, local: Optional[dict]) -> bool:
    """A cloud rung does better on a kind of work than the local rung covering
    it (v0.181.251): it passes where that misses; or, on a suite whose cases
    carry their own tiers, it holds a harder tier; or, on one tiered from the
    runs, it scores more than the noise of one run higher."""
    if local is None:
        return True
    if cloud["passes"] and not local["passes"]:
        return True
    if cloud.get("tiers_from") == "cases" and local.get("tiers_from") == "cases":
        return _tier_index(cloud) > _tier_index(local)
    return cloud["score"] - local["score"] > NOISE


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
    # v0.181.251: a cloud rung is needed for a kind of work when it does better
    # there than the local rung that covers it - passes where the local rung
    # misses, or holds a harder tier - and it can go only when it was measured
    # on every kind of work and never did better. Before, a local rung whose
    # mean reached the threshold covered the whole kind of work even when it
    # held only the easy tier and the cloud rung the hard one, a kind of work
    # nothing passed needed no cloud rung, and a cloud rung never run at all
    # "could go".
    rows, unmeasured = [], []
    needed: dict = {}
    unknown: dict = {}
    runs_seen: list = []
    for s in wanted:
        rungs = {m: r for m, r in (fams.get(s) or {}).items() if r}
        bar = threshold(svc, s)
        if not any(m in rungs for m in ladder + cloud):
            unmeasured.append(s)
            rows.append({"suite": s, "title": _title(s), "threshold": bar, "rung": "",
                         "score": None, "through": "", "cloud": "", "beaten_by": [],
                         "sentence": f"{_title(s)}: not measured on this persona's models"})
            continue
        local = next((rungs[m] for m in ladder if m in rungs and rungs[m]["passes"]), None)
        best = local or max((rungs[m] for m in ladder if m in rungs),
                            key=lambda r: (_tier_index(r), r["score"]), default=None)
        beat = [m for m in cloud if m in rungs and _beats(rungs[m], best)]
        for m in cloud:
            if m not in rungs:
                unknown.setdefault(m, []).append(s)
        for m in beat:
            needed.setdefault(m, []).append(s)
        runs_seen += [rungs[m]["runs"] for m in ladder + cloud if m in rungs]
        if local:
            text = (f"{_title(s)}: {local['model']} passes at {local['score']:g}"
                    + (f", through {local['through']}" if local["through"]
                       and local.get("tiers_from") == "cases" else "")
                    + f", threshold {bar:g}")
            if beat:
                r = rungs[beat[0]]
                if _tier_index(r) > _tier_index(local) and r.get("tiers_from") == "cases":
                    text += f"; {TIER_ORDER[_tier_index(local) + 1]} needs {beat[0]}"
                else:
                    text += f"; {beat[0]} does better at {r['score']:g}"
        else:
            said = (f"{best['model']} reaches {best['score']:g}" if best
                    else "no local rung measured")
            up = next((rungs[m] for m in cloud if m in rungs and rungs[m]["passes"]), None)
            text = (f"{_title(s)}: no local rung passes ({said}, threshold {bar:g})"
                    + (f"; {up['model']} passes at {up['score']:g}" if up
                       else "; no model in the chain passes"))
            if not up and beat:
                r = rungs[beat[0]]
                text += (f", {beat[0]} gets furthest" + (f" (through {r['through']})"
                                                         if r["through"] else ""))
        rows.append({"suite": s, "title": _title(s), "threshold": bar,
                     "rung": local["model"] if local else "",
                     "score": best["score"] if best else None,
                     "through": local["through"] if local else "",
                     "cloud": beat[0] if beat else "", "beaten_by": beat, "sentence": text})
    could_go = [] if unmeasured else [m for m in cloud if m not in needed and m not in unknown]
    if unmeasured:
        verdict = (f"not every kind of work is measured ({', '.join(_title(s) for s in unmeasured)}),"
                   " so no cloud rung can be called unneeded")
    elif not cloud:
        verdict = "no cloud rung in the chain"
    elif could_go and len(could_go) == len(cloud):
        verdict = "a local rung is good enough for every kind of work; the cloud rungs " \
                  + ", ".join(could_go) + " are not needed for it"
    elif all(m in needed for m in cloud):
        verdict = "every cloud rung is still needed for some kind of work: " + ", ".join(
            f"{m} ({', '.join(_title(s) for s in needed[m])})" for m in cloud)
    else:
        bits = []
        if needed:
            bits.append("needed: " + ", ".join(
                f"{m} ({', '.join(_title(s) for s in needed[m])})" for m in cloud if m in needed))
        late = [m for m in cloud if m in unknown and m not in needed]
        if late:
            bits.append("not run on every kind of work, so not judged: " + ", ".join(late))
        if could_go:
            bits.append("not needed for any kind of work: " + ", ".join(could_go))
        verdict = "; ".join(bits)
    if could_go and runs_seen and min(runs_seen) < RUNS_PER_RUNG:
        verdict += (f" - on fewer than {RUNS_PER_RUNG} runs a model, and one run moves about "
                    "5 points, so a lead to check, not a result")
    return {"persona": name, "category": p.get("benchmark_category") or "",
            "ladder": ladder, "ladder_set": bool(own), "cloud": cloud, "families": rows,
            "could_go": could_go, "unmeasured": unmeasured,
            "needed": {m: needed[m] for m in cloud if m in needed},
            "not_run": {m: unknown[m] for m in cloud if m in unknown},
            "verdict": verdict}


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

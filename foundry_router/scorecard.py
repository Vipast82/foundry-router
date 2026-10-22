"""Per-sprint scorecard: what one coordinator task (a sprint) cost and how it went.

Everything here is already in the router database; this joins it by project and
work task:

  work_tasks / work_events   the coordinator's state and its events (gate runs with
                             pass/fail and duration, reviews, vision rounds,
                             helper start/finish with duration, helper feedback)
  usage_events               model calls and tokens by role and backend (the ledger)
  perf_samples               speed: measured (server timings) kept apart from
                             estimated; cache hit
  request_log                est_cost_usd, and `sub_cost_usd` per model call (the
                             operator's per-plan allocation, perf.subscription_rates)

GET /admin/api/work/scorecard?project=&task= returns one row per sprint and
the totals. Wall time runs from the first event, session or model call of the
sprint to the last (or to the coordinator's last update once it is COMPLETE or
CANCELLED). Numbers the coordinator did not report (events from before it sent
durations) show as null, never as zero.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Optional

TERMINAL = ("COMPLETE", "CANCELLED")
_HELPER_END = ("helper_done", "helper_delivered", "helper_failed", "helper_timed_out",
               "helper_cancelled", "helper_expired")
_HELPER_SKIP = ("helper_skipped", "helper_deferred")


def _loads(s) -> dict:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _events(db, project: str, task: str, since: float) -> dict:
    """work_events folded per task."""
    where, args = ["ts >= ?"], [since]
    if project:
        where.append("project_id=?")
        args.append(project)
    if task:
        where.append("task_id=?")
        args.append(task)
    out: dict = defaultdict(lambda: {
        "first": None, "last": None, "gates": [], "reviews": [], "vision_changes": 0,
        "vision_approved": 0, "vision_rounds": 0, "helpers_started": 0, "helper_ended": [],
        "helper_skipped": 0, "helper_feedback": {}, "red_gates": 0})
    for r in db.query("SELECT ts, project_id, task_id, kind, detail FROM work_events WHERE "
                      + " AND ".join(where) + " ORDER BY ts, id", tuple(args)):
        if not r["task_id"]:
            continue
        a = out[(r["project_id"], r["task_id"])]
        a["first"] = r["ts"] if a["first"] is None else min(a["first"], r["ts"])
        a["last"] = r["ts"] if a["last"] is None else max(a["last"], r["ts"])
        k, d = r["kind"], _loads(r["detail"])
        if k == "fast_lane_gate":
            a["gates"].append(d)
        elif k == "fast_lane_red_gate":
            a["red_gates"] += 1
        elif k == "review":
            a["reviews"].append(d)
        elif k == "fast_lane_vision_approved":
            a["vision_approved"] += 1
            a["vision_rounds"] = max(a["vision_rounds"], int(d.get("rounds") or 0))
        elif k == "fast_lane_vision_accepted_with_notes":
            a["vision_rounds"] = max(a["vision_rounds"], int(d.get("rounds") or 0))
        elif k == "fast_lane_vision_changes":
            a["vision_changes"] += 1
            a["vision_rounds"] = max(a["vision_rounds"], int(d.get("round") or 0))
        elif k == "helper_started":
            a["helpers_started"] += 1
        elif k in _HELPER_END:
            a["helper_ended"].append(d)
        elif k in _HELPER_SKIP:
            a["helper_skipped"] += 1
        elif k == "helper_feedback":
            a["helper_feedback"][str(d.get("unit") or "")] = str(d.get("status") or "")
    return out


def _gate_card(gates: list, red: int) -> dict:
    durs = [x for x in (_num(g.get("duration_s")) for g in gates) if x is not None]
    passed = sum(1 for g in gates if str(g.get("result") or "").upper() == "PASS")
    return {"run": len(gates), "passed": passed, "failed": len(gates) - passed, "red": red,
            "total_s": round(sum(durs), 1) if durs else None,
            "avg_s": round(sum(durs) / len(durs), 1) if durs else None}


def _helper_card(a: dict) -> dict:
    durs = [x for x in (_num(h.get("duration_s")) for h in a["helper_ended"]) if x is not None]
    fb = a["helper_feedback"]
    return {"used": a["helpers_started"], "finished": len(a["helper_ended"]),
            "skipped": a["helper_skipped"],
            "integrated": sum(1 for v in fb.values() if v == "integrated"),
            "rejected": sum(1 for v in fb.values() if v == "rejected"),
            "total_s": round(sum(durs), 1) if durs else None,
            "tokens": sum(int((h.get("tokens") or {}).get("total") or 0)
                          for h in a["helper_ended"] if isinstance(h.get("tokens"), dict)) or None}


def _usage(db, project: str, task: str, since: float) -> dict:
    where, args = ["ts >= ?", "status IN ('settled','error')", "COALESCE(work_task_id,'') != ''"], [since]
    if project:
        where.append("project_id=?")
        args.append(project)
    if task:
        where.append("work_task_id=?")
        args.append(task)
    out: dict = {}
    for r in db.query(
            "SELECT project_id, work_task_id AS task, COALESCE(role,'') AS role, backend, paid, "
            "COUNT(*) AS calls, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, "
            "COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o, "
            "MIN(ts) AS first_ts, MAX(COALESCE(settled_ts, ts)) AS last_ts FROM usage_events "
            "WHERE " + " AND ".join(where) + " GROUP BY project_id, work_task_id, role, backend, paid",
            tuple(args)):
        u = out.setdefault((r["project_id"], r["task"]), {
            "calls": 0, "errors": 0, "input": 0, "output": 0, "by_role": {}, "by_backend": {},
            "paid_calls": 0, "first": None, "last": None})
        u["calls"] += r["calls"]
        u["errors"] += r["errors"] or 0
        u["input"] += r["i"]
        u["output"] += r["o"]
        if r["paid"]:
            u["paid_calls"] += r["calls"]
        for key, name in (("by_role", r["role"] or "-"), ("by_backend", r["backend"] or "-")):
            b = u[key].setdefault(name, {"calls": 0, "input": 0, "output": 0})
            b["calls"] += r["calls"]
            b["input"] += r["i"]
            b["output"] += r["o"]
        u["first"] = r["first_ts"] if u["first"] is None else min(u["first"], r["first_ts"])
        u["last"] = r["last_ts"] if u["last"] is None else max(u["last"], r["last_ts"] or 0)
    return out


def _perf(db, project: str, task: str, hours: float) -> dict:
    """Speeds per sprint: measured (server timing) apart from estimated. Samples
    from before perf_samples had a task column are matched through request_log."""
    where, args = ["COALESCE(p.work_task_id, r.work_task_id, '') != ''"], []
    if hours:
        where.append("datetime(p.ts) >= datetime('now', ?)")
        args.append(f"-{hours} hours")
    if project:
        where.append("COALESCE(p.project_id, r.project_id) = ?")
        args.append(project)
    if task:
        where.append("COALESCE(p.work_task_id, r.work_task_id) = ?")
        args.append(task)
    out: dict = {}
    for r in db.query(
            "SELECT COALESCE(p.project_id, r.project_id) AS project_id, "
            "COALESCE(p.work_task_id, r.work_task_id) AS task, "
            "CASE WHEN p.timing_src='server' THEN 'measured' WHEN p.timing_src='estimated' "
            "THEN 'estimated' ELSE 'unknown' END AS bucket, COUNT(*) AS n, "
            "ROUND(AVG(p.decode_tps),1) AS decode_tps, ROUND(AVG(p.prefill_tps),1) AS prefill_tps, "
            "ROUND(AVG(p.ttft_ms),0) AS ttft_ms, ROUND(AVG(p.eff_tps),1) AS eff_tps, "
            "ROUND(AVG(p.cache_hit_pct),1) AS cache_hit_pct, "
            "COALESCE(SUM(p.completion_tokens),0) AS out_tokens, COALESCE(SUM(p.wall_ms),0) AS wall_ms "
            "FROM perf_samples p LEFT JOIN request_log r ON r.request_id = p.request_id "
            "WHERE " + " AND ".join(where) + " GROUP BY 1, 2, 3", tuple(args)):
        out.setdefault((r["project_id"], r["task"]), {})[r["bucket"]] = {
            k: r[k] for k in ("n", "decode_tps", "prefill_tps", "ttft_ms", "eff_tps",
                              "cache_hit_pct", "out_tokens", "wall_ms")}
    return out


def _cost(db, project: str, task: str, hours: float) -> dict:
    where, args = ["COALESCE(work_task_id,'') != ''"], []
    if hours:
        where.append("datetime(ts) >= datetime('now', ?)")
        args.append(f"-{hours} hours")
    if project:
        where.append("project_id=?")
        args.append(project)
    if task:
        where.append("work_task_id=?")
        args.append(task)
    out: dict = {}
    try:
        rows = db.query(
            "SELECT project_id, work_task_id AS task, COUNT(*) AS requests, "
            "COALESCE(SUM(est_cost_usd),0) AS est, "
            "COALESCE(SUM((SELECT SUM(COALESCE(json_extract(j.value,'$.sub_cost_usd'),0)) "
            "FROM json_each(request_log.models_used) j)),0) AS sub "
            "FROM request_log WHERE " + " AND ".join(where) + " GROUP BY 1, 2", tuple(args))
    except Exception:                                  # noqa: BLE001 - no json1 / bad row
        rows = db.query(
            "SELECT project_id, work_task_id AS task, COUNT(*) AS requests, "
            "COALESCE(SUM(est_cost_usd),0) AS est, 0 AS sub FROM request_log WHERE "
            + " AND ".join(where) + " GROUP BY 1, 2", tuple(args))
    for r in rows:
        out[(r["project_id"], r["task"])] = {"requests": r["requests"],
                                             "est_cost_usd": round(r["est"] or 0, 4),
                                             "sub_cost_usd": round(r["sub"] or 0, 4)}
    return out


def build(db, project: str = "", task: str = "", hours: float = 24 * 14,
          limit: int = 100) -> dict:
    """Scorecard rows for the project's sprints active in the window (newest first)."""
    since = time.time() - hours * 3600 if hours else 0.0
    where, args = ["updated_ts >= ?"], [since]
    if project:
        where.append("project_id=?")
        args.append(project)
    if task:
        where.append("task_id=?")
        args.append(task)
    tasks = db.query("SELECT project_id, task_id, state, data, updated_ts FROM work_tasks WHERE "
                     + " AND ".join(where) + " ORDER BY updated_ts DESC LIMIT ?",
                     (*args, max(1, min(500, int(limit)))))
    ev = _events(db, project, task, since)
    use = _usage(db, project, task, since)
    perf = _perf(db, project, task, hours)
    cost = _cost(db, project, task, hours)
    sprints = []
    for t in tasks:
        key = (t["project_id"], t["task_id"])
        e = ev.get(key)
        u = use.get(key) or {}
        data = _loads(t["data"])
        starts = [x for x in ((e or {}).get("first"), u.get("first")) if x]
        ends = [x for x in ((e or {}).get("last"), u.get("last")) if x]
        if t["state"] in TERMINAL and t["updated_ts"]:
            ends.append(t["updated_ts"])
        wall = round(max(ends) - min(starts), 1) if starts and ends else None
        revs = (e or {}).get("reviews") or []
        pf = perf.get(key) or {}
        c = cost.get(key) or {}
        sprints.append({
            "project_id": t["project_id"], "task_id": t["task_id"], "outcome": t["state"],
            "attempts": data.get("attempt"), "first_ts": min(starts) if starts else None,
            "last_ts": max(ends) if ends else None, "wall_s": wall,
            "model_calls": u.get("calls", 0), "model_errors": u.get("errors", 0),
            "tokens": {"input": u.get("input", 0), "output": u.get("output", 0),
                       "by_role": u.get("by_role", {}), "by_backend": u.get("by_backend", {})},
            "gates": _gate_card((e or {}).get("gates") or [], (e or {}).get("red_gates", 0)),
            "reviews": {"total": len(revs),
                        "approved": sum(1 for r in revs if r.get("verdict") == "APPROVED"),
                        "changes": sum(1 for r in revs if r.get("verdict") == "CHANGES_REQUIRED")},
            "vision": {"rounds": (e or {}).get("vision_rounds", 0),
                       "approved": (e or {}).get("vision_approved", 0),
                       "changes": (e or {}).get("vision_changes", 0)},
            "helpers": _helper_card(e) if e else {"used": 0, "finished": 0, "skipped": 0,
                                                  "integrated": 0, "rejected": 0,
                                                  "total_s": None, "tokens": None},
            "speed": {"measured": pf.get("measured"), "estimated": pf.get("estimated"),
                      "unknown": pf.get("unknown")},
            "cost": {"est_cost_usd": c.get("est_cost_usd", 0.0),
                     "sub_cost_usd": c.get("sub_cost_usd", 0.0),
                     "paid_calls": u.get("paid_calls", 0)},
        })
    tot = {"sprints": len(sprints),
           "complete": sum(1 for s in sprints if s["outcome"] == "COMPLETE"),
           "model_calls": sum(s["model_calls"] for s in sprints),
           "input_tokens": sum(s["tokens"]["input"] for s in sprints),
           "output_tokens": sum(s["tokens"]["output"] for s in sprints),
           "gates_run": sum(s["gates"]["run"] for s in sprints),
           "gates_passed": sum(s["gates"]["passed"] for s in sprints),
           "reviews": sum(s["reviews"]["total"] for s in sprints),
           "vision_rounds": sum(s["vision"]["rounds"] for s in sprints),
           "helpers_used": sum(s["helpers"]["used"] for s in sprints),
           "helpers_integrated": sum(s["helpers"]["integrated"] for s in sprints),
           "wall_s": round(sum(s["wall_s"] or 0 for s in sprints), 1)}
    return {"project": project, "hours": hours, "sprints": sprints, "totals": tot,
            "note": "Model calls and tokens are Foundry's ledger; gates, reviews, vision and "
                    "helpers are the coordinator's events (durations only from coordinator "
                    "versions that send them). Speeds: measured = engine timings; estimated "
                    "= derived from the stream, shown apart."}

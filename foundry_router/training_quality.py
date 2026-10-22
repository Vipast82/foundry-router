"""The quality signal on captured calls: what the router itself knows about
each request, joined by `request_id`.

  request_log.outcome   ok | error | cancelled | timeout | denied
  response_feedback     thumbs up / down (+1 / -1), summed per request
  perf_samples          ttft, tokens/s, timing source, cache hit
  training_outcomes     labels the coordinator posted after it verified the
                        work: gate_pass, review_approved, vision_approved (or the
                        negative gate_fail, review_changes, vision_changes)

The join is made when records are read (GET /v1/foundry/capture/records, the
download in the Training tab), never written into the capture store, so a label
that arrives after the call still reaches the training pipeline on the next read.
The pipeline turns it into `outcome.router` on an episode (foundry_training.labels)
and the curate policy tokens `router_ok` / `router_verified` test it. Which
model's output may become a training target is still the project's
`allow_frontier_targets`; nothing here changes it.
"""

from __future__ import annotations

import json
import time
from typing import Iterable, Optional

from foundry_training.labels import NEGATIVE_LABELS as NEGATIVE
from foundry_training.labels import POSITIVE_LABELS as POSITIVE
from foundry_training.labels import quality_state

LABELS = POSITIVE + NEGATIVE
MAX_REQUESTS = 2000
_CHUNK = 400


class OutcomeError(ValueError):
    pass


def _chunks(items: list, n: int = _CHUNK) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def resolve_requests(db, sel: dict) -> list[str]:
    """request ids a coordinator outcome covers when it names the work instead
    of the requests: project + task (+ session ids / run, + the time the
    verified head was committed) over the calls of the given roles (default the
    implementer: a gate certifies the implementer's output, not the reviewer's)."""
    project, task = str(sel.get("project_id") or ""), str(sel.get("task_id") or "")
    if not (project and task):
        return []
    where = ["project_id=?", "work_task_id=?", "COALESCE(request_id,'') != ''"]
    args: list = [project, task]
    roles = sel.get("roles") or ["implementer"]
    where.append("role IN (%s)" % ",".join("?" * len(roles)))
    args += [str(r) for r in roles]
    sessions = [str(s) for s in (sel.get("session_ids") or []) if s]
    if sessions:
        where.append("session_id IN (%s)" % ",".join("?" * len(sessions)))
        args += sessions
    if sel.get("run_id"):
        where.append("run_id=?")
        args.append(str(sel["run_id"]))
    if sel.get("until_ts"):
        where.append("datetime(ts) <= datetime(?, 'unixepoch')")
        args.append(float(sel["until_ts"]))
    where.append("outcome='ok'")
    rows = db.query("SELECT request_id FROM request_log WHERE " + " AND ".join(where)
                    + " ORDER BY id DESC LIMIT ?", (*args, MAX_REQUESTS))
    return [r["request_id"] for r in rows]


def add_outcomes(db, body: dict, source: str = "") -> dict:
    """Record a coordinator label for request ids (or for the work they came
    from). Idempotent per (request, label)."""
    label = str(body.get("label") or "")
    if label not in LABELS:
        raise OutcomeError(f"label must be one of {', '.join(LABELS)}")
    ids = [str(x) for x in (body.get("request_ids") or []) if x]
    if len(ids) > MAX_REQUESTS:
        raise OutcomeError(f"at most {MAX_REQUESTS} request ids per post")
    resolved = False
    if not ids:
        ids = resolve_requests(db, body)
        resolved = True
    if not ids:
        raise OutcomeError("give request_ids, or project_id + task_id of calls that were recorded")
    ev = body.get("evidence")
    evidence = json.dumps(ev, default=str)[:2000] if ev else ""
    now = time.time()
    n = 0
    project, task = str(body.get("project_id") or ""), str(body.get("task_id") or "")
    if not (project and task):
        # request ids given without the work they belong to: take it from the log
        row = db.query_one("SELECT project_id, work_task_id FROM request_log WHERE request_id=? "
                           "AND COALESCE(project_id,'') != ''", (ids[0],))
        if row:
            project, task = project or row["project_id"], task or row["work_task_id"] or ""
    for rid in ids:
        n += db.execute_count(
            "INSERT OR IGNORE INTO training_outcomes (ts, request_id, label, source, project_id, "
            "task_id, evidence) VALUES (?,?,?,?,?,?,?)",
            (now, rid, label, source[:80], project[:160], task[:160], evidence))
    return {"label": label, "requests": len(ids), "recorded": n, "resolved_from_work": resolved}


def attach(db, rows: list[dict]) -> list[dict]:
    """Add row["quality"] to capture rows that carry a request id."""
    rids = sorted({r.get("request_id") for r in rows if r.get("request_id")})
    if not rids:
        return rows
    req: dict = {}
    perf: dict = {}
    labels: dict = {}
    fb: dict = {}
    try:
        for part in _chunks(rids):
            ph = ",".join("?" * len(part))
            for r in db.query(f"SELECT id, request_id, outcome, status, error, duration_ms, "
                              f"est_cost_usd, served_backend FROM request_log "
                              f"WHERE request_id IN ({ph})", tuple(part)):
                req[r["request_id"]] = r
            for r in db.query(f"SELECT request_id, backend, model, ttft_ms, decode_tps, eff_tps, "
                              f"timing_src, cache_hit_pct, wall_ms, completion_tokens "
                              f"FROM perf_samples WHERE request_id IN ({ph}) ORDER BY id",
                              tuple(part)):
                perf.setdefault(r["request_id"], []).append(r)
            for r in db.query(f"SELECT request_id, label, source, ts, evidence FROM "
                              f"training_outcomes WHERE request_id IN ({ph}) ORDER BY id",
                              tuple(part)):
                labels.setdefault(r["request_id"], []).append(
                    {"label": r["label"], "source": r["source"], "ts": r["ts"],
                     "evidence": r["evidence"] or ""})
        log_ids = {v["id"]: k for k, v in req.items()}
        for part in _chunks(sorted(log_ids)):
            ph = ",".join("?" * len(part))
            for r in db.query(f"SELECT request_log_id, SUM(rating) AS score FROM response_feedback "
                              f"WHERE request_log_id IN ({ph}) GROUP BY request_log_id",
                              tuple(part)):
                fb[log_ids[r["request_log_id"]]] = r["score"]
    except Exception:                                   # noqa: BLE001 - quality is best effort
        return rows
    for row in rows:
        rid = row.get("request_id")
        if not rid:
            continue
        rl = req.get(rid) or {}
        mine = perf.get(rid) or []
        p = next((x for x in mine if x["backend"] == row.get("backend")
                  and x["model"] == row.get("model")), mine[0] if mine else None)
        q = {"outcome": rl.get("outcome"), "status": rl.get("status"),
             "feedback": fb.get(rid), "labels": labels.get(rid, []),
             "cost_usd": rl.get("est_cost_usd"), "request_ms": rl.get("duration_ms")}
        if p:
            q["perf"] = {k: p[k] for k in ("ttft_ms", "decode_tps", "eff_tps", "timing_src",
                                           "cache_hit_pct", "wall_ms")}
        if rl or p or q["labels"]:
            row["quality"] = q
    return rows


def chatml_example(call: dict) -> Optional[dict]:
    """One captured, successful backend call as a {"messages", "tools"} line
    (the chatml-tools shape): the effective prompt plus the reply as the final
    assistant turn. Reasoning is left out, as in every training export."""
    body = call.get("body") or {}
    resp = body.get("response") or {}
    if call.get("kind") != "backend_call" or call.get("status") != "ok":
        return None
    if not (resp.get("content") or resp.get("tool_calls")):
        return None
    req = body.get("request") or {}
    msgs = [m for m in req.get("messages") or [] if isinstance(m, dict)]
    final: dict = {"role": "assistant", "content": resp.get("content") or ""}
    if resp.get("tool_calls"):
        final["tool_calls"] = [{"id": t.get("id"), "type": "function", "function": {
            "name": t.get("name"), "arguments": t.get("arguments") if isinstance(
                t.get("arguments"), str) else json.dumps(t.get("arguments") or {})}}
            for t in resp["tool_calls"]]
    out = {"messages": msgs + [final]}
    if req.get("tools"):
        out["tools"] = req["tools"]
    return out


def passes(call: dict, need: str) -> bool:
    """Does a captured row satisfy a download filter: "" (everything), "ok"
    (router_ok: the call succeeded, no thumbs-down, no negative label) or
    "verified" (router_ok plus a positive coordinator label)."""
    if not need:
        return True
    st = quality_state(call.get("quality"))
    return bool(st["ok"] and (need != "verified" or st["positive"]))

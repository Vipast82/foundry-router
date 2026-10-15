"""Coordinator store (foundry_coordinator) -> normalized events, read-only.

Per task: lifecycle transitions with their evidence, sessions (which run,
role, phase, persona), verification results bound to a revision with their
raw-evidence pointers, frontier reviews bound to the reviewed commit,
human approvals bound to revision + artifact hashes, and human corrections
(rejections with findings, answers to questions).

Session -> task links from here are observed facts (the coordinator started
those sessions).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

from ..schema import Event

ADAPTER = "coordinator"


def from_db(path: str) -> Iterator[tuple[str, str, dict, dict]]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        def q(sql, args=()):
            return [dict(r) for r in c.execute(sql, args)]
        for t in q("SELECT * FROM tasks ORDER BY project_id, task_id"):
            key = (t["project_id"], t["task_id"])
            payload = {
                "task": t,
                "transitions": q("SELECT * FROM transitions WHERE project_id=? AND task_id=? "
                                 "ORDER BY id", key),
                "sessions": q("SELECT * FROM sessions WHERE project_id=? AND task_id=? "
                              "ORDER BY created_ts", key),
                "verifications": q("SELECT * FROM verifications WHERE project_id=? AND task_id=? "
                                   "ORDER BY id", key),
                "reviews": q("SELECT review_id, project_id, task_id, reviewed_sha, role, persona, "
                             "verdict, valid, error, result, ts FROM reviews WHERE project_id=? "
                             "AND task_id=? ORDER BY ts", key),
                "approvals": q("SELECT * FROM approvals WHERE project_id=? AND task_id=? "
                               "ORDER BY ts", key),
            }
            rid = f"{t['project_id']}/{t['task_id']}@{t['updated_ts']}"
            yield (rid, t["project_id"], payload,
                   {"adapter": ADAPTER, "store": path, "sanitization": "none"})
    finally:
        c.close()


def _j(v, d):
    try:
        return json.loads(v) if isinstance(v, str) and v else (v if v is not None else d)
    except ValueError:
        return d


def normalize(p: dict, project_id: str, raw_ref: str, redaction: dict) -> list[Event]:
    t = p["task"]
    tid = t["task_id"]
    common = dict(project_id=project_id, task_id=tid, raw_ref=raw_ref,
                  redaction_status=redaction.get("status", "clean"))
    ev = []
    for h in p["transitions"]:
        evid = _j(h["evidence"], {})
        ev.append(Event(ADAPTER, f"{project_id}/{tid}/tr/{h['id']}", "task_transition",
                        timestamp=h["ts"], actor_role="human" if h["actor"] not in ("engine",)
                        else "coordinator", session_id=evid.get("session_id"),
                        source_revision=evid.get("head_sha") or evid.get("reviewed_commit_sha"),
                        facts={"from": h["from_state"], "to": h["to_state"], "actor": h["actor"],
                               "evidence": evid}, **common))
        if h["actor"] not in ("engine", "operator") and evid.get("findings"):
            ev.append(Event(ADAPTER, f"{project_id}/{tid}/tr/{h['id']}", "human_correction",
                            part="human", timestamp=h["ts"], actor_role="human",
                            facts={"by": h["actor"], "findings": evid["findings"],
                                   "to": h["to_state"]}, **common))
        if h["to_state"] in ("IMPLEMENTING", "CLOSING") and evid.get("answered"):
            ev.append(Event(ADAPTER, f"{project_id}/{tid}/tr/{h['id']}", "human_correction",
                            part="answer", timestamp=h["ts"], actor_role="human",
                            session_id=t.get("session_id"),
                            facts={"by": h["actor"], "answer": evid["answered"]}, **common))
    for s in p["sessions"]:
        data = _j(s.get("data"), {})
        ev.append(Event(ADAPTER, f"{project_id}/{tid}/session/{s['session_id']}", "session_start",
                        timestamp=s["created_ts"], session_id=s["session_id"],
                        parent_session_id=data.get("parent"), requested_persona=s["persona"],
                        actor_role="coordinator",
                        facts={"run_id": s["run_id"], "role": s["role"], "phase": s["phase"],
                               "attempt": s["attempt"], "client": s["client"],
                               "outcome": s["outcome"], "ended_ts": s["ended_ts"],
                               "model_report": data.get("report"), "tokens": data.get("tokens"),
                               "observed": "started by the coordinator"}, **common))
    for v in p["verifications"]:
        ev.append(Event(ADAPTER, f"{project_id}/{tid}/ver/{v['id']}", "verification",
                        timestamp=v["executed_ts"], source_revision=v["revision"],
                        verification_status=v["result"] if v["result"] in ("PASS", "FAIL",
                                                                           "UNKNOWN") else "UNKNOWN",
                        actor_role="coordinator",
                        facts={"set_id": v["set_id"], "phase": v["phase"], "check": v["check_name"],
                               "required": bool(v["required"]), "evidence": v["evidence"],
                               "detail": v["detail"],
                               "negative_control": "negative" in (v["check_name"] or "").lower()},
                        **common))
    for r in p["reviews"]:
        res = _j(r["result"], {})
        ev.append(Event(ADAPTER, f"{project_id}/{tid}/rev/{r['review_id']}", "review",
                        timestamp=r["ts"], source_revision=r["reviewed_sha"],
                        actor_role="reviewer", requested_persona=r["persona"],
                        verification_status="n/a",
                        facts={"review_id": r["review_id"], "role": r["role"],
                               "verdict": r["verdict"], "valid": bool(r["valid"]),
                               "error": r["error"], "result": res}, **common))
    for a in p["approvals"]:
        ev.append(Event(ADAPTER, f"{project_id}/{tid}/apr/{a['approval_id']}", "approval",
                        timestamp=a["ts"], source_revision=a["revision"], actor_role="human",
                        facts={"kind": a["kind"], "approver": a["approver"],
                               "artifacts": _j(a["artifacts"], {}), "note": a["note"],
                               "revoked": bool(a["revoked_ts"])}, **common))
    return ev

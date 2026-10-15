"""Correlation: linking records that describe the same thing, with how and
how sure.

Observed links (confidence 1.0, observed=1):
  parent_child   a Kilo session's parentID; a coordinator helper binding
  session_task   Kilo session metadata from the coordinator; the coordinator's
                 own session table
Explicit links (operator mapping, observed=0, method explicit):
  session_task   `correlate --map SESSION=TASK` for sessions nothing labelled
Matched links:
  same_model_call  a Kilo assistant step and a Foundry model_call in the same
                   session: content match (tool calls / text) = 0.95; order
                   only, one candidate = 0.6; more than one candidate =
                   recorded with the ambiguity, never merged
Inferred links (never used for training unless a policy allows them):
  session_task   by time inside one coordinator run window = 0.5

Nothing is ever linked across projects: every query is per project.
"""

from __future__ import annotations

import json
from collections import defaultdict

from .messages import tool_calls as norm_tool_calls
from .store import TrainingStore


def _sig_kilo(step_events: list[dict]) -> str:
    calls = sorted((e["facts"].get("name") or "", json.dumps(e["facts"].get("arguments") or {},
                                                              sort_keys=True))
                   for e in step_events if e["event_type"] == "tool_call")
    txt = "".join((e["facts"].get("text") or "").strip() for e in step_events
                  if e["event_type"] == "assistant_message" and not e["facts"].get("step_finish"))
    return json.dumps([calls, txt.strip()])


def _sig_foundry(e: dict) -> str:
    calls = sorted((tc["name"], json.dumps(tc["arguments"], sort_keys=True))
                   for tc in norm_tool_calls(e["facts"].get("tool_calls")))
    return json.dumps([calls, (e["facts"].get("content") or "").strip()])


def kilo_steps(events: list[dict]) -> dict:
    steps = defaultdict(list)
    for e in events:
        if e["source_adapter"] != "kilo" or e["event_type"] not in ("assistant_message",
                                                                    "tool_call", "reasoning"):
            continue
        key = (e["session_id"], e["facts"].get("message_id"), e["facts"].get("step") or 0)
        steps[key].append(e)
    return steps


def run(store: TrainingStore, project_id: str, explicit: dict | None = None) -> dict:
    ev = store.events(project_id)
    counts = defaultdict(int)
    for e in ev:
        if e["event_type"] == "child_session" and e["source_adapter"] == "kilo" \
                and e["facts"].get("parent"):
            store.correlate("parent_child", project_id, e["facts"]["parent"], e["session_id"],
                            "kilo parentID", 1.0, True)
            counts["parent_child"] += 1
        if e["event_type"] == "session_start" and e["source_adapter"] == "coordinator":
            store.correlate("session_task", project_id, e["session_id"], e["task_id"],
                            "coordinator session table", 1.0, True)
            counts["session_task"] += 1
            if e["parent_session_id"]:
                store.correlate("parent_child", project_id, e["parent_session_id"],
                                e["session_id"], "coordinator helper binding", 1.0, True)
                counts["parent_child"] += 1
        if e["event_type"] == "session_start" and e["source_adapter"] == "kilo" and e["task_id"]:
            store.correlate("session_task", project_id, e["session_id"], e["task_id"],
                            "kilo session metadata (coordinator)", 1.0, True)
            counts["session_task"] += 1
    for sid, tid in (explicit or {}).items():
        store.correlate("session_task", project_id, sid, tid, "explicit operator mapping", 1.0,
                        False)
        counts["explicit"] += 1
    # Kilo step <-> Foundry model_call, per session
    calls_by_session = defaultdict(list)
    for e in ev:
        if e["event_type"] == "model_call" and e["session_id"]:
            calls_by_session[e["session_id"]].append(e)
    for key, step_events in sorted(kilo_steps(ev).items(), key=lambda kv: min(
            x["timestamp"] for x in kv[1])):
        sid = key[0]
        cands = [c for c in calls_by_session.get(sid, []) if c["facts"].get("status") == "ok"]
        if not cands:
            continue
        sig = _sig_kilo(step_events)
        exact = [c for c in cands if _sig_foundry(c) == sig]
        a = f"kilo-step:{sid}:{key[1]}:{key[2]}"
        if len(exact) == 1:
            store.correlate("same_model_call", project_id, a, exact[0]["event_id"],
                            "same session + identical output", 0.95, False)
            counts["same_model_call"] += 1
        elif len(exact) > 1:
            # identical outputs (e.g. a retry): pick nothing, record the ambiguity
            store.correlate("same_model_call", project_id, a, exact[0]["event_id"],
                            "same session + identical output (several)", 0.4, False,
                            ambiguity=json.dumps([c["event_id"] for c in exact]))
            counts["ambiguous"] += 1
    return dict(counts)


def links(store: TrainingStore, project_id: str, kind: str, min_conf: float = 0.9,
          allow_inferred: bool = False) -> list[dict]:
    out = []
    for r in store.correlations(project_id, kind):
        if r["confidence"] < min_conf or (r["ambiguity"] and not allow_inferred):
            continue
        out.append(r)
    return out

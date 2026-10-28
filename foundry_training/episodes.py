"""Episode reconstruction.

trajectory   one coding session (one coordinator attempt, a closeout, a
             helper): the conversation exactly as the model saw it, the
             tools it had, what it changed (base..head diff), and the outcome
             measured at THAT session's head (verification, review, approval).
             Outcomes are labels on the episode, never text inside it, so
             later results, reviewer answers or future file state can't leak
             into an earlier prompt.
correction   attempt k that failed (a check, the reviewer, the studio gate or
             the vision reviewer), the feedback the repair session actually
             received, and the repair (attempt k+1) that was verified;
             authorship kept per part. A fast-lane session that got its red
             gate / vision findings in the SAME session yields an inline
             correction (labels.inline_corrections).

Labels (gate verdict, vision verdict, critiques), tags (fast_lane, ui_v3,
templates, widgets, device sizes) and per-step authorship (helper prework
patches / harvested pre-code models are context, never targets) come from
labels.py.

Only the session's own turns build it: Foundry calls tagged with another
purpose (brain_route, frontier_assist, research, operator_chat, ...) made
during the session are not its conversation. Each assistant turn keeps the
reasoning its call returned in a separate `reasoning` field (never in
content; exported only with include_reasoning). A session that stopped on a
tool call nobody answered (its next call cancelled, or the session ended)
ends at the turn before it (`flags.trimmed_open_tail`). Kilo's tool paging
("Showing lines 1-120 of 304") is what the model saw, not a truncation.

`outcome.attempt_end`: how this attempt ended, from the coordinator's own
task transitions: the last of BLOCKED / FAILED / CANCELLED / COMPLETE /
AWAITING_HUMAN after the session started and before the task's next attempt
started. A later attempt that completes never relabels an earlier one.

Context source, best first:
  foundry-capture     the session's last complete model_call: Foundry's
                      effective request (system prompt, tool definitions, every
                      earlier turn and tool result) + the response
  kilo-reconstruction Kilo's own messages; system prompt and tool definitions
                      are NOT in Kilo's records, so they're marked missing (never
                      invented)
  cline               the same, from Cline's files
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from . import correlate, labels as L
from .messages import canonical_text, neutral, neutral_tools, pairing
from .schema import content_hash, stable_id

LOOP_REPEATS = 3
AGENT_TURN = "agent_turn"
ATTEMPT_END_STATES = ("BLOCKED", "FAILED", "CANCELLED", "COMPLETE", "AWAITING_HUMAN")


def agent_turn(e: dict) -> bool:
    """A Foundry call that is one of the session's own turns (rows captured
    before v0.153.0 carry no purpose: they were all agent turns)."""
    return (e["facts"].get("purpose") or AGENT_TURN) == AGENT_TURN


def _turn_key(content, tool_calls) -> str:
    from .messages import tool_calls as _tcs
    return json.dumps([(content or "").strip(),
                       [[t["name"], t["arguments"]] for t in _tcs(tool_calls)]],
                      sort_keys=True, default=str)


def attempt_end(ev: list[dict], task_id: Optional[str], start_ts: float,
                next_start: Optional[float]) -> Optional[dict]:
    if not task_id:
        return None
    end = None
    for t in _transitions(ev, task_id):
        if t["timestamp"] < start_ts or (next_start is not None and t["timestamp"] >= next_start):
            continue
        to = t["facts"].get("to")
        if to in ATTEMPT_END_STATES:
            evd = t["facts"].get("evidence") or {}
            end = {"state": to, "ts": t["timestamp"],
                   "blocker": str(evd.get("blocker") or evd.get("reason") or "")[:300]}
    return end


def _sessions(ev: list[dict]) -> dict:
    by = defaultdict(list)
    for e in ev:
        if e["session_id"]:
            by[e["session_id"]].append(e)
    return by


def _coordinator_index(ev: list[dict]) -> dict:
    """run facts per session from the coordinator's records (observed)."""
    out = {}
    for e in ev:
        if e["source_adapter"] == "coordinator" and e["event_type"] == "session_start":
            out[e["session_id"]] = dict(e["facts"], task_id=e["task_id"],
                                        persona=e["requested_persona"],
                                        parent=e["parent_session_id"])
    return out


def _transitions(ev, task_id):
    return [e for e in ev if e["event_type"] == "task_transition" and e["task_id"] == task_id]


REVIEW_ROLES = {"reviewer", "deep_reviewer", "visual_reviewer"}


def _revisions(ev, task_id, session_id, phase=None) -> tuple[Optional[str], Optional[str]]:
    base = head = None
    if phase == "closeout":
        # a closeout session starts from the reviewed commit and ends at the
        # revision the closeout checks ran on
        revs = [e for e in ev if e["event_type"] == "review" and e["task_id"] == task_id
                and e["facts"].get("valid") and e["facts"].get("verdict") == "APPROVED"]
        ver = [e for e in ev if e["event_type"] == "verification" and e["task_id"] == task_id
               and e["facts"].get("phase") == "closeout"]
        return (revs[-1]["source_revision"] if revs else None,
                ver[-1]["source_revision"] if ver else None)
    for t in _transitions(ev, task_id):
        evd = t["facts"].get("evidence") or {}
        if evd.get("session_id") != session_id:
            continue
        if t["facts"]["to"] == "IMPLEMENTING" and evd.get("base_sha"):
            base = evd["base_sha"]
        if t["facts"]["to"] == "VERIFYING" and evd.get("head_sha"):
            head = evd["head_sha"]
    return base, head


def outcome(ev: list[dict], task_id: Optional[str], head: Optional[str],
            phase: str = "verify") -> dict:
    """Verification (of `phase`: verify, or closeout for a closeout session)
    / review / approval measured at exactly `head`. A closeout's changes are
    usually not reviewed at its own head, so its review is normally None
    (review_missing): the pre-closeout review isn't relabelled as covering
    them."""
    out = {"verification": {"status": "UNKNOWN", "revision": head, "checks": [],
                            "negative_controls": []},
           "review": None, "approval": None, "task_state": None}
    if not task_id:
        return out
    tr = _transitions(ev, task_id)
    if tr:
        out["task_state"] = tr[-1]["facts"]["to"]
    if not head:
        return out
    ver = [e for e in ev if e["event_type"] == "verification" and e["task_id"] == task_id
           and e["source_revision"] == head and e["facts"].get("phase") == phase]
    if ver:
        # the latest verification set at this revision
        last_set = ver[-1]["facts"]["set_id"]
        checks = [e for e in ver if e["facts"]["set_id"] == last_set]
        req = [c for c in checks if c["facts"]["required"]]
        status = ("FAIL" if any(c["verification_status"] == "FAIL" for c in req) else
                  "UNKNOWN" if any(c["verification_status"] != "PASS" for c in req) else "PASS")
        out["verification"] = {
            "status": status, "revision": head, "set_id": last_set,
            "checks": [{"check": c["facts"]["check"], "result": c["verification_status"],
                        "required": c["facts"]["required"], "evidence": c["facts"]["evidence"]}
                       for c in checks],
            "negative_controls": [c["facts"]["check"] for c in checks
                                  if c["facts"].get("negative_control")]}
    revs = [e for e in ev if e["event_type"] == "review" and e["task_id"] == task_id
            and e["source_revision"] == head and e["facts"].get("valid")]
    if revs:
        r = revs[-1]
        out["review"] = {"verdict": r["facts"]["verdict"], "reviewed_sha": head,
                         "persona": r["requested_persona"], "role": r["facts"].get("role"),
                         "findings": (r["facts"].get("result") or {}).get("findings") or []}
    apr = [e for e in ev if e["event_type"] == "approval" and e["task_id"] == task_id
           and not e["facts"].get("revoked")]
    if apr:
        a = apr[-1]
        out["approval"] = {"kind": a["facts"]["kind"], "revision": a["source_revision"],
                           "approver": a["facts"]["approver"], "artifacts": a["facts"]["artifacts"]}
    return out


def _author(model: Optional[str], persona: Optional[str], cfg) -> dict:
    if (model and model in cfg.local_models) or (persona and persona in cfg.local_personas):
        return {"kind": "local", "model": model or persona}
    if model or persona:
        # anything not configured as the local model is another model's work
        return {"kind": "non-local", "model": model or persona}
    return {"kind": "unknown", "model": None}


def _from_foundry(calls: list[dict], cfg) -> Optional[dict]:
    ok = [c for c in calls if c["facts"].get("status") == "ok"
          and c["capture_status"] == "complete" and c["facts"].get("messages")]
    if not ok:
        return None
    last = ok[-1]
    authors = {}
    reasoning = {}
    for c in ok:
        sig = (c["facts"].get("content") or "").strip()
        authors[sig] = _author(c["actual_model"], c["requested_persona"], cfg)
        if c["facts"].get("reasoning"):
            reasoning[_turn_key(c["facts"].get("content"), c["facts"].get("tool_calls"))] = \
                c["facts"]["reasoning"]
    msgs = []
    for m in last["facts"]["messages"]:
        nm = neutral(m)
        if nm["role"] == "assistant":
            nm["author"] = authors.get((nm.get("content") or "").strip()) or \
                _author(last["actual_model"], last["requested_persona"], cfg)
            r = reasoning.get(_turn_key(nm.get("content"), nm.get("tool_calls")))
            if r:
                nm["reasoning"] = r
        msgs.append(nm)
    final = neutral({"role": "assistant", "content": last["facts"].get("content") or "",
                     "tool_calls": last["facts"].get("tool_calls")},
                    _author(last["actual_model"], last["requested_persona"], cfg))
    if last["facts"].get("reasoning"):
        final["reasoning"] = last["facts"]["reasoning"]
    trimmed = []
    later = [c for c in calls if c["timestamp"] > last["timestamp"]]
    if final.get("tool_calls"):
        # Nobody answered these calls: the next call was cancelled / failed, or
        # the session ended here. End on the turn before (never invent results).
        trimmed = [tc["name"] for tc in final["tool_calls"]]
    else:
        msgs.append(final)
    if trimmed and not any(m["role"] == "assistant" for m in msgs):
        msgs.append(final)                # nothing before it: keep it, flagged open
        trimmed = []
    shrank = any(len(c["facts"]["messages"]) > len(last["facts"]["messages"]) for c in ok[:-1])
    return {"messages": msgs, "tools": neutral_tools(last["facts"].get("tools")),
            "context_source": "foundry-capture", "system_and_tools_known": True,
            "context_replaced": shrank, "event_ids": [c["event_id"] for c in ok],
            "actual_model": last["actual_model"], "actual_backend": last["actual_backend"],
            "final_open_tool_calls": bool(msgs and msgs[-1] is final and final.get("tool_calls")),
            "trimmed_open_tail": trimmed,
            "next_call_status": (later[0]["facts"].get("status") if later else None)}


def _from_client(sev: list[dict], cfg, persona: Optional[str]) -> dict:
    msgs, cur, ids = [], None, []
    truncated, missing = [], []
    for e in sorted(sev, key=lambda e: (e["timestamp"], e["seq"] or 0)):
        if e["source_adapter"] not in ("kilo", "cline"):
            continue
        t = e["event_type"]
        f = e["facts"]
        if t == "user_message":
            if f.get("synthetic"):
                continue
            cur = None
            msgs.append(neutral({"role": "user", "content": f.get("text")}))
            ids.append(e["event_id"])
        elif t == "assistant_message" and not f.get("step_finish"):
            if cur is None or cur.get("_step") != (f.get("message_id"), f.get("step")):
                cur = neutral({"role": "assistant", "content": ""},
                              _author(e["actual_model"], e["requested_persona"] or persona, cfg))
                cur["_step"] = (f.get("message_id"), f.get("step"))
                msgs.append(cur)
            cur["content"] = (cur["content"] + (f.get("text") or "")).strip()
            ids.append(e["event_id"])
        elif t == "tool_call":
            if cur is None or cur.get("_step") != (f.get("message_id"), f.get("step")):
                cur = neutral({"role": "assistant", "content": ""},
                              _author(e["actual_model"], e["requested_persona"] or persona, cfg))
                cur["_step"] = (f.get("message_id"), f.get("step"))
                msgs.append(cur)
            cur.setdefault("tool_calls", []).append(
                {"id": e["tool_call_id"], "name": f.get("name"), "arguments": f.get("arguments") or {}})
            ids.append(e["event_id"])
        elif t == "tool_result":
            # Kilo's `truncated` is the tool paging its output (stored exactly as
            # the model saw it); only a record the capture itself cut counts
            if e["capture_status"] == "truncated" and not (
                    e["source_adapter"] == "kilo" and f.get("truncated")):
                truncated.append(e["tool_call_id"])
            if e["capture_status"] == "missing":
                missing.append(e["tool_call_id"])
                continue                 # never invent a result
            out = f.get("output") if f.get("status") == "completed" else \
                f"ERROR: {f.get('error') or ''}"
            msgs.append({"role": "tool", "tool_call_id": e["tool_call_id"], "content": out or "",
                         "author": {"kind": "tool", "model": None}})
            ids.append(e["event_id"])
    for m in msgs:
        m.pop("_step", None)
    return {"messages": msgs, "tools": [], "context_source": (
        "cline" if any(e["source_adapter"] == "cline" for e in sev) else "kilo-reconstruction"),
            "system_and_tools_known": False, "truncated_results": truncated,
            "missing_results": missing, "event_ids": ids}


def _loop(messages: list[dict]) -> bool:
    seen = defaultdict(int)
    for m in messages:
        for tc in m.get("tool_calls") or []:
            k = tc["name"] + json.dumps(tc["arguments"], sort_keys=True)
            seen[k] += 1
            if seen[k] >= LOOP_REPEATS:
                return True
    return False


def build(store, project_id: str, cfg, diff_fn=None) -> list[dict]:
    ev = store.events(project_id)
    coord = _coordinator_index(ev)
    outcome_events = [e for e in ev if e["event_type"] == "router_outcome"]
    parent_links = correlate.links(store, project_id, "parent_child")
    children = defaultdict(list)
    for l in parent_links:
        children[l["a"]].append(l["b"])
    parent_of = {l["b"]: l["a"] for l in parent_links}
    task_links = {l["a"]: l["b"] for l in correlate.links(store, project_id, "session_task")}
    episodes = []
    for sid, sev in _sessions(ev).items():
        kinds = {e["event_type"] for e in sev}
        if not kinds & {"model_call", "user_message", "assistant_message", "tool_call"}:
            continue
        info = coord.get(sid) or {}
        task_id = task_links.get(sid) or info.get("task_id") or next(
            (e["task_id"] for e in sev if e["task_id"]), None)
        role = info.get("role") or next((e["facts"].get("role") for e in sev
                                         if e["source_adapter"] == "foundry"
                                         and e["facts"].get("role")), None)
        if role in REVIEW_ROLES:
            continue                      # reviews are outcome labels, not trajectories
        calls = [e for e in sev if e["event_type"] == "model_call" and agent_turn(e)]
        persona = info.get("persona") or next((e["requested_persona"] for e in sev
                                               if e["requested_persona"]), None)
        built = _from_foundry(calls, cfg)
        client = _from_client(sev, cfg, persona)
        convo = built or client
        gaps = []
        if not built:
            gaps.append("no complete Foundry capture: system prompt and tool definitions unknown")
        trunc = client.get("truncated_results") or []
        miss = client.get("missing_results") or []
        if built and (trunc or miss):
            # the Foundry context is what the model saw; still flag truncation Kilo recorded
            pass
        # per-step authorship: a local turn that applied a helper's patch or
        # wrote a harvested pre-code model is frontier-authored (context only)
        attrs = L.attributions(ev, task_id, cfg.workspace)
        helper_steps = L.attribute_steps(convo["messages"], attrs)
        unverifiable = sorted({a["patch"] for a in attrs if not a["verifiable"]})
        compaction = any(e["event_type"] == "compaction" for e in sev) or \
            bool(convo.get("context_replaced"))
        phase = info.get("phase")
        base, head = _revisions(ev, task_id, sid, phase) if task_id else (None, None)
        oc = outcome(ev, task_id, head, "closeout" if phase == "closeout" else "verify")
        oc["router"] = L.router_signal(calls, outcome_events)
        pair = pairing(convo["messages"])
        arts = [{"status": e["facts"].get("artifact_status"),
                 "refs": e["artifact_references"]} for e in sev if e["event_type"] == "file_ref"]
        authors = sorted({(m["author"]["kind"], m["author"].get("model") or "")
                          for m in convo["messages"] if m["role"] == "assistant"})
        diff, diff_cut = (diff_fn(base, head) if (diff_fn and base and head and base != head)
                          else ("", False))
        msgs = convo["messages"]
        ep = {
            "episode_id": stable_id(project_id, "trajectory", sid),
            "project_id": project_id, "task_id": task_id, "kind": "trajectory",
            "session_id": sid, "parent_session_id": parent_of.get(sid) or info.get("parent"),
            "child_sessions": children.get(sid, []),
            "run_id": info.get("run_id"), "phase": info.get("phase"),
            "attempt": info.get("attempt"), "role": role or "implementer",
            "group_id": f"{project_id}:{task_id or ('session:' + (parent_of.get(sid) or sid))}",
            "messages": msgs, "tools": convo["tools"],
            "context_source": convo["context_source"],
            "revision": {"base": base, "head": head},
            "diff": {"patch": diff, "truncated": diff_cut,
                     "helper_authored": bool(helper_steps) or L.diff_has_helper(diff, attrs)},
            "outcome": oc,
            "flags": {"complete_tool_sequence": pair["ok"] and not trunc and not miss
                      and not convo.get("final_open_tool_calls"),
                      "trimmed_open_tail": convo.get("trimmed_open_tail") or [],
                      "pairing_problems": pair["problems"][:5], "truncated_results": trunc,
                      "missing_results": miss, "compaction": compaction,
                      "system_and_tools_known": convo["system_and_tools_known"],
                      "authors": [list(a) for a in authors], "loop": _loop(msgs),
                      "reasoning_excluded": sum(1 for e in sev if e["event_type"] == "reasoning"),
                      "reasoning_turns": sum(1 for m in msgs if m.get("reasoning")),
                      "artifacts": arts, "gaps": gaps,
                      "tool_errors": sum(1 for m in msgs if m["role"] == "tool"
                                         and (m.get("content") or "").startswith("ERROR:"))},
            "sources": sorted({e["source_adapter"] for e in sev}),
            "event_ids": sorted(set(convo["event_ids"]) | {e["event_id"] for e in sev
                                                            if e["event_type"] in
                                                            ("session_start", "child_session",
                                                             "file_ref", "compaction")}),
            "first_ts": min(e["timestamp"] for e in sev),
        }
        heads = (L.heads_of_session(ev, task_id, sid) if task_id else []) + \
            ([head] if head else [])
        ep["labels"] = L.episode_labels(ev, task_id, heads, ep["first_ts"])
        ep["flags"]["label_artifacts"] = L.label_artifacts(ep["labels"])
        ep["flags"]["helper_steps"] = helper_steps
        # a helper patch whose content was never captured: which steps used it
        # cannot be checked, so the episode is not trusted (fail closed)
        ep["flags"]["helper_patch_unverifiable"] = unverifiable
        ep["flags"]["gate_label_inconsistent"] = [g["event_id"] for g in ep["labels"]["gate"]
                                                  if g.get("inconsistent")]
        ep["tags"] = L.episode_tags(ep, cfg, ep["labels"], ep["flags"]["label_artifacts"])
        ep["content_hash"] = content_hash(canonical_text(msgs))
        episodes.append(ep)
    # how each attempt ended: bounded by the start of the task's next attempt
    starts = defaultdict(list)
    for ep in episodes:
        if ep["task_id"] and ep["role"] not in ("helper",):
            starts[ep["task_id"]].append((ep["first_ts"], ep.get("run_id")))
    for ep in episodes:
        if not ep["task_id"] or ep["role"] == "helper":
            ep["outcome"]["attempt_end"] = None
            continue
        nxt = [ts for ts, run in starts[ep["task_id"]]
               if ts > ep["first_ts"] and run != ep.get("run_id")]
        ep["outcome"]["attempt_end"] = attempt_end(ev, ep["task_id"], ep["first_ts"],
                                                   min(nxt) if nxt else None)
    inline = []
    for ep in episodes:
        inline += L.inline_corrections(ep, project_id, diff_fn)
    episodes += corrections(ev, episodes, cfg, project_id)
    episodes += inline
    return episodes


def corrections(ev, episodes, cfg, project_id) -> list[dict]:
    """attempt k (failed / changes required) -> feedback -> attempt k+1
    (verified). The feedback is exactly what attempt k+1's prompt carried."""
    by_task = defaultdict(list)
    for e in episodes:
        if e["kind"] == "trajectory" and e["task_id"] and e["phase"] in ("implement", None) \
                and e.get("attempt"):
            by_task[e["task_id"]].append(e)
    out = []
    for task_id, eps in by_task.items():
        eps.sort(key=lambda e: e["attempt"])
        for prev, nxt in zip(eps, eps[1:]):
            vision_red = any(v.get("verdict") == "CHANGES"
                             for v in (prev.get("labels") or {}).get("vision") or [])
            if prev["outcome"]["verification"]["status"] == "PASS" and \
                    (prev["outcome"]["review"] or {}).get("verdict") == "APPROVED" and \
                    not vision_red:
                continue
            fb = None
            for t in _transitions(ev, task_id):
                evd = t["facts"].get("evidence") or {}
                if t["facts"]["to"] == "CHANGES_REQUIRED" and evd.get("findings") and \
                        t["timestamp"] <= nxt["first_ts"] + 1:
                    fb = {"findings": evd["findings"], "from": t["facts"]["from"],
                          "actor": t["facts"]["actor"], "review_id": evd.get("review_id")}
            if not fb:
                # a red studio_gate verdict at the failed attempt's head (fast lane)
                gl = [g for g in (prev.get("labels") or {}).get("gate") or []
                      if g.get("result") == "FAIL" and g.get("head") ==
                      prev["revision"].get("head")]
                if gl:
                    fb = {"findings": [f"{c['id']}: {c.get('result')} {c.get('detail') or ''}"
                                       .strip() for c in gl[-1]["failed_checks"]],
                          "from": "VERIFYING", "actor": "studio_gate", "review_id": None}
            if not fb:
                continue
            actor = str(fb["actor"] or "")
            src = ("reviewer" if fb["from"] == "REVIEWING" else
                   "gate" if actor == "studio_gate" else
                   "vision" if actor.startswith("model:") else
                   "human" if actor not in ("engine", "operator") else "mechanical-checks")
            rev = prev["outcome"].get("review") or {}
            out.append({
                "episode_id": stable_id(project_id, "correction", prev["episode_id"],
                                        nxt["episode_id"]),
                "project_id": project_id, "task_id": task_id, "kind": "correction",
                "group_id": prev["group_id"], "session_id": nxt["session_id"],
                "run_id": nxt["run_id"], "attempt": nxt["attempt"],
                "original": {"episode_id": prev["episode_id"], "revision": prev["revision"],
                             "final_message": next((m for m in reversed(prev["messages"])
                                                    if m["role"] == "assistant"), None),
                             "diff": prev["diff"], "outcome": prev["outcome"],
                             "authors": prev["flags"]["authors"]},
                "feedback": {"source": src, "items": fb["findings"],
                             "author": ({"kind": "frontier", "model": rev.get("persona")}
                                        if src == "reviewer" else
                                        {"kind": "frontier", "model": actor[6:] or None}
                                        if src == "vision" else
                                        {"kind": "coordinator", "model": "studio_gate"}
                                        if src == "gate" else
                                        {"kind": "human" if src == "human" else "coordinator",
                                         "model": None})},
                "correction": {"episode_id": nxt["episode_id"], "messages": nxt["messages"],
                               "tools": nxt["tools"], "revision": nxt["revision"],
                               "diff": nxt["diff"]},
                "messages": nxt["messages"], "tools": nxt["tools"],
                "context_source": nxt["context_source"], "revision": nxt["revision"],
                "outcome": nxt["outcome"], "flags": dict(nxt["flags"]),
                "labels": nxt.get("labels") or {}, "tags": nxt.get("tags") or [],
                "sources": sorted(set(prev["sources"]) | set(nxt["sources"])),
                "event_ids": sorted(set(prev["event_ids"]) | set(nxt["event_ids"])),
                "first_ts": prev["first_ts"],
                "content_hash": content_hash(["correction", prev["content_hash"],
                                              nxt["content_hash"]]),
            })
    return out

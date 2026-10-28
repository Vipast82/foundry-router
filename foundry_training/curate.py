"""Curation: selection by evidence, deduplication, split assignment,
preference pairs, evaluation categories.

Every episode ends in exactly one status, with reasons:
  selected   meets the project's policy for its dataset kind (training candidate)
  eval       its group is assigned to evaluation (never exported for training)
  excluded   one or more reasons below
  duplicate  an exact duplicate, or a near duplicate (prompt AND actions each
             MinHash-Jaccard >= 0.9) of another episode (kept, not exported)

Exclusion reasons (each a requirement that wasn't met):
  unverified        no captured verification at the episode's head ("DONE" isn't evidence)
  verification_failed / verification_unknown
  review_missing / review_not_approved / unresolved_findings
  approval_missing  a required human (e.g. visual) approval isn't recorded
  broken_tool_sequence / truncated_tool_output / missing_tool_result
  missing_context   system prompt / tool definitions unknown, or compaction inside
  secret_exposure   the export-time scan still finds something secret-looking
  non_local_author  assistant turns written by a model that isn't the local model
                    (enforced for every kind while allow_frontier_targets = false)
  no_local_target   every assistant turn is a helper step (frontier code the local
                    model applied): nothing is left to train on
  router_outcome_missing / router_not_ok / router_unverified
                    (policy tokens `router_ok`, `router_verified`) the router's own
                    record of the session's final call: it succeeded, nobody rated it
                    down, no negative gate / review / vision label; `router_verified`
                    also needs a positive label (gate PASS, review APPROVED, vision
                    approved) the coordinator posted. A policy that lists them in
                    place of the verification tokens lets router-verified output
                    qualify; whose output may be a target stays `allow_frontier_targets`.
  loop              the same tool call repeated (blind retries)
  missing_artifact  a referenced image's bytes aren't available
  no_feedback       (correction) the repair prompt carried no feedback
  attempt_blocked / attempt_failed / attempt_abandoned
                    the attempt ended BLOCKED / FAILED / CANCELLED (outcome.attempt_end,
                    from the coordinator's transitions): a labelled negative, kept for
                    evaluation sets and as the rejected side of preference pairs
Failed episodes are kept (status excluded, outcome FAIL) and remain
available to evaluation datasets; they are never positive training data.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import defaultdict

from .messages import canonical_text
from .redact import scan
from .schema import content_hash

NEAR_DUP = 0.9
ATTEMPT_NEGATIVE = {"BLOCKED": "attempt_blocked", "FAILED": "attempt_failed",
                    "CANCELLED": "attempt_abandoned"}


def _shingles(text: str, k: int = 5) -> set:
    toks = re.findall(r"\w+|[^\w\s]", text.lower())
    return {" ".join(toks[i:i + k]) for i in range(max(1, len(toks) - k + 1))}


def minhash(text: str, n: int = 64) -> list[int]:
    sh = _shingles(text) or {""}
    out = []
    for i in range(n):
        salt = str(i).encode()
        out.append(min(int.from_bytes(hashlib.blake2b(salt + s.encode(), digest_size=8).digest(),
                                      "big") for s in sh))
    return out


def similarity(a: list[int], b: list[int]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / max(1, len(a))


def signatures(messages: list[dict]) -> tuple[list[int], list[int]]:
    """(prompt, actions) MinHash signatures over content only (tool-call ids,
    authors and timestamps differ between sessions and would hide a
    duplicate). Kept apart because project prompts share long boilerplate
    (AnimalUprising's rules run to ~17 KB): two different tasks look alike
    on the prompt alone."""
    prompt = [m for m in messages if m["role"] in ("system", "user")]
    actions = [m for m in messages if m["role"] in ("assistant", "tool")]
    return minhash(canonical_text(prompt)), minhash(canonical_text(actions))


def near_duplicate(a, b) -> bool:
    """The same request answered the same way: both halves alike."""
    return similarity(a[0], b[0]) >= NEAR_DUP and similarity(a[1], b[1]) >= NEAR_DUP


def requirements(ep: dict, cfg, known_secrets=()) -> list[str]:
    kind = "correction" if ep["kind"] == "correction" else (
        "multimodal" if any(a.get("refs") for a in ep["flags"].get("artifacts") or []) and
        cfg.policy.get("multimodal") else "sft")
    need = set(cfg.require(kind))
    oc = ep["outcome"]
    f = ep["flags"]
    reasons = []
    v = oc["verification"]["status"]
    if "verification_pass" in need:
        if oc["verification"].get("set_id") is None:
            reasons.append("unverified")
        elif v == "FAIL":
            reasons.append("verification_failed")
        elif v != "PASS":
            reasons.append("verification_unknown")
    if "review_approved_at_revision" in need:
        r = oc.get("review")
        if not r:
            reasons.append("review_missing")
        elif r["verdict"] != "APPROVED":
            reasons.append("review_not_approved")
        elif any(x.get("severity") in ("blocker", "major") for x in r.get("findings") or []):
            reasons.append("unresolved_findings")
    if "approval_if_required" in need and ep.get("approval_required") and not oc.get("approval"):
        reasons.append("approval_missing")
    if "complete_tool_sequence" in need or kind == "correction":
        if f.get("truncated_results"):
            reasons.append("truncated_tool_output")
        if f.get("missing_results"):
            reasons.append("missing_tool_result")
        if not f.get("complete_tool_sequence") and "broken_tool_sequence" not in reasons:
            reasons.append("broken_tool_sequence")
    if "system_and_tools_known" in need and (not f.get("system_and_tools_known")
                                             or f.get("compaction")):
        reasons.append("missing_context")
    if not cfg.export.get("allow_frontier_targets", False):
        # Enforced for every dataset kind, not only where a policy lists
        # local_author: frontier text is never a training target. A "helper"
        # step (a local turn that carried out a helper's prework patch or a
        # harvested pre-code model) stays in the conversation as context and
        # is weighted out at export; any other non-local turn excludes the
        # episode, and an episode with no local turn left has no target.
        kinds = {a[0] for a in f.get("authors") or []}
        if kinds - {"local", "helper"}:
            reasons.append("non_local_author")
        elif "helper" in kinds and "local" not in kinds:
            reasons.append("no_local_target")
    # fail closed, for every kind: a helper patch whose content was never
    # captured (steps can't be checked against it) or a gate verdict whose
    # `result` disagrees with its own checks excludes the episode
    if f.get("helper_patch_unverifiable"):
        reasons.append("helper_patch_unverifiable")
    if f.get("gate_label_inconsistent") or any(
            g.get("inconsistent") for g in (ep.get("labels") or {}).get("gate") or []):
        reasons.append("gate_label_inconsistent")
    if "router_ok" in need or "router_verified" in need:
        rs = oc.get("router") or {}
        if not rs.get("known"):
            reasons.append("router_outcome_missing")
        elif not rs.get("ok"):
            reasons.append("router_not_ok")
        elif "router_verified" in need and not rs.get("positive"):
            reasons.append("router_unverified")
    if "no_loop" in need and f.get("loop"):
        reasons.append("loop")
    if "feedback_present" in need and not (ep.get("feedback") or {}).get("items"):
        reasons.append("no_feedback")
    if "artifacts_present" in need and any(a.get("status") != "present"
                                           for a in f.get("artifacts") or []):
        reasons.append("missing_artifact")
    end = (oc.get("attempt_end") or {}).get("state")
    if end in ATTEMPT_NEGATIVE:
        reasons.append(ATTEMPT_NEGATIVE[end])
    if scan({"m": ep.get("messages"), "d": (ep.get("diff") or {}).get("patch")},
            known_secrets).count:
        reasons.append("secret_exposure")
    return sorted(set(reasons))


def split_of(group_id: str, project_id: str, first_ts: float, cfg) -> str:
    sp = cfg.split
    if project_id in (sp.get("heldout_projects") or []):
        return "eval"
    cutoff = sp.get("time_cutoff")
    if cutoff:
        import datetime as _dt
        try:
            c = _dt.datetime.fromisoformat(str(cutoff)).timestamp()
            if first_ts and first_ts >= c:
                return "eval"
        except ValueError:
            pass
    h = int(hashlib.sha256(f"{sp.get('seed', 'v1')}:{group_id}".encode()).hexdigest()[:8], 16)
    return "eval" if (h / 0xFFFFFFFF) < float(sp.get("eval_fraction", 0.15)) else "train"


EVAL_CATEGORIES = ("coding_correctness", "tool_use", "scope_discipline", "loop_avoidance",
                   "evidence_quality", "visual_diagnosis", "quota_refusal")


def eval_categories(ep: dict, task_events: list[dict]) -> list[str]:
    cats = []
    checks = {c["check"]: c["result"] for c in ep["outcome"]["verification"].get("checks") or []}
    if ep["outcome"]["verification"]["status"] in ("PASS", "FAIL"):
        cats.append("coding_correctness")
    if any(m.get("tool_calls") for m in ep.get("messages") or []):
        cats.append("tool_use")
    if "coordinator.scope" in checks:
        cats.append("scope_discipline")
    if ep["flags"].get("loop") or checks.get("au.kilo_checkpoint") == "FAIL":
        cats.append("loop_avoidance")
    if any(k.startswith(("au.done_check", "au.gate")) for k in checks):
        cats.append("evidence_quality")
    if ep.get("approval_required") or any(a.get("refs") for a in ep["flags"].get("artifacts")
                                          or []):
        cats.append("visual_diagnosis")
    if any(e["event_type"] == "task_transition" and e["facts"]["to"] == "PAUSED"
           and "Foundry" in json.dumps(e["facts"].get("evidence") or {}) for e in task_events):
        cats.append("quota_refusal")
    return cats


def run(store, project_id: str, episodes: list[dict], cfg, known_secrets=()) -> dict:
    ev = store.events(project_id)
    approvals_needed = {e["task_id"] for e in ev if e["event_type"] == "task_transition"
                        and (e["facts"].get("evidence") or {}).get("awaiting", "").startswith(
                            "approval")}
    deleted = {r["episode_id"] for r in store.cur.q(
        "SELECT episode_id FROM episodes WHERE project_id=? AND deleted=1", (project_id,))}
    # dedupe (exact, then near) inside the project, across sources
    by_hash: dict = {}
    sigs = []
    counts = defaultdict(int)
    ph = cfg.policy_hash()
    group_merge: dict = {}
    for ep in sorted(episodes, key=lambda e: (e["first_ts"], e["episode_id"])):
        if ep["episode_id"] in deleted:
            counts["deleted"] += 1
            continue
        ep["approval_required"] = ep["task_id"] in approvals_needed
        sig = signatures(ep.get("messages") or [])
        dup_of = by_hash.get(ep["content_hash"])
        if dup_of is None:
            for other_id, other_sig, other_kind in sigs:
                if other_kind == ep["kind"] and near_duplicate(sig, other_sig):
                    dup_of = other_id
                    break
        if dup_of is None:
            by_hash[ep["content_hash"]] = ep["episode_id"]
            sigs.append((ep["episode_id"], sig, ep["kind"]))
        else:
            # a duplicate joins its canonical's group, so both land in one split
            canon = store.cur.q("SELECT group_id FROM episodes WHERE episode_id=?", (dup_of,))
            group_merge[ep["group_id"]] = canon[0]["group_id"] if canon else ep["group_id"]
        reasons = requirements(ep, cfg, known_secrets)
        group = group_merge.get(ep["group_id"], ep["group_id"])
        split = split_of(group, project_id, ep["first_ts"], cfg)
        if dup_of:
            status = "duplicate"
            reasons = reasons + ["duplicate_episode"]
        elif split == "eval":
            status = "eval"
        elif reasons:
            status = "excluded"
        else:
            status = "selected"
        task_events = [e for e in ev if e["task_id"] == ep["task_id"]] if ep["task_id"] else []
        ep["eval_categories"] = eval_categories(ep, task_events)
        counts[status] += 1
        data = dict(ep)
        store.cur.x(
            "INSERT INTO episodes (episode_id, project_id, task_id, group_id, kind, status, split, "
            "outcome, sources, event_ids, content_hash, near_sig, reasons, data, created_ts, "
            "updated_ts, policy_hash, duplicate_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "strftime('%s','now'),strftime('%s','now'),?,?) ON CONFLICT(episode_id) DO UPDATE SET "
            "status=CASE WHEN episodes.deleted=1 THEN 'deleted' ELSE excluded.status END, "
            "split=excluded.split, outcome=excluded.outcome, sources=excluded.sources, "
            "event_ids=excluded.event_ids, content_hash=excluded.content_hash, "
            "near_sig=excluded.near_sig, reasons=excluded.reasons, data=excluded.data, "
            "group_id=excluded.group_id, updated_ts=excluded.updated_ts, "
            "policy_hash=excluded.policy_hash, duplicate_of=excluded.duplicate_of",
            (ep["episode_id"], project_id, ep["task_id"], group, ep["kind"], status, split,
             json.dumps(ep["outcome"]), json.dumps(ep["sources"]), json.dumps(ep["event_ids"]),
             ep["content_hash"], json.dumps([sig[0][:4], sig[1][:4]]), json.dumps(reasons),
             json.dumps(data, default=str), ph, dup_of))
    pairs = preferences(store, project_id, episodes, cfg)
    counts["preference_pairs"] = len(pairs)
    return dict(counts)


def prompt_prefix(messages: list[dict]) -> list[dict]:
    """The task prompt of a trajectory: leading system turns + the first user turn."""
    out = []
    for m in messages:
        out.append(m)
        if m["role"] == "user":
            return out
        if m["role"] != "system":
            return []
    return []


def _local_only(authors, allow_frontier: bool) -> bool:
    kinds = {a[0] for a in authors or []}
    return allow_frontier or ("local" in kinds and not kinds - {"local", "helper"})


def _attempt_good(ep: dict) -> bool:
    oc = ep["outcome"]
    rv = (oc.get("review") or {}).get("verdict")
    gates = [g.get("result") for g in (ep.get("labels") or {}).get("gate") or []]
    end = (oc.get("attempt_end") or {}).get("state")
    return end not in ATTEMPT_NEGATIVE and oc["verification"]["status"] != "FAIL" and (
        rv == "APPROVED" or (gates and gates[-1] == "PASS")
        or (rv is None and oc["verification"]["status"] == "PASS"))


def _attempt_bad(ep: dict) -> bool:
    oc = ep["outcome"]
    rv = (oc.get("review") or {}).get("verdict")
    gates = [g.get("result") for g in (ep.get("labels") or {}).get("gate") or []]
    end = (oc.get("attempt_end") or {}).get("state")
    return end in ATTEMPT_NEGATIVE or oc["verification"]["status"] == "FAIL" or \
        rv in ("CHANGES_REQUIRED", "BLOCKED") or bool(gates and gates[-1] == "FAIL")


def preferences(store, project_id: str, episodes: list[dict], cfg=None) -> list[dict]:
    """Chosen/rejected only for a justified preference:

      same context   two model calls with an identical request context
                     (messages + tools) and different responses, preferred by
                     opposite verified outcomes of their sessions, by the
                     router's labels on the calls themselves (review APPROVED /
                     gate PASS / vision approved vs review CHANGES / gate FAIL /
                     vision CHANGES, posted by the coordinator after a frontier
                     review or the gate), or a recorded human judgment
      same task      (stored as "ep:<episode id>") two attempts at one task with
                     the identical task prompt (system + first user turn): the
                     attempt the reviewer approved or the gate passed vs one
                     with review CHANGES, gate FAIL, failed verification or an
                     attempt that ended BLOCKED / FAILED / CANCELLED

    Both sides must be the local model's own work unless the project allows
    frontier targets. A later response with more information is never paired
    with an earlier one."""
    from .labels import quality_state
    allow = bool(cfg is not None and cfg.export.get("allow_frontier_targets", False))
    ev = store.events(project_id, "event_type='model_call'")
    outcome_by_session = {e["session_id"]: e["outcome"]["verification"]["status"]
                          for e in episodes if e["kind"] == "trajectory"}
    local = (lambda e: True) if (allow or cfg is None) else (
        lambda e: (e["actual_model"] in cfg.local_models)
        or (e["requested_persona"] in cfg.local_personas))
    labels = defaultdict(list)
    for o in store.events(project_id, "event_type='router_outcome'"):
        if o.get("request_id"):
            labels[o["request_id"]].append({"label": o["facts"].get("label")})

    def router(e):
        q = e["facts"].get("quality") or {}
        return quality_state(q, (q.get("labels") or []) + labels.get(e.get("request_id"), []))
    by_ctx = defaultdict(list)
    for e in ev:
        if e["facts"].get("status") != "ok":
            continue
        if (e["facts"].get("purpose") or "agent_turn") != "agent_turn":
            continue
        ctx = content_hash([e["facts"].get("messages"), e["facts"].get("tools")])
        by_ctx[ctx].append(e)
    out = []
    # derived pairs are rebuilt from the evidence on every pass (a rule change or
    # a corrected label never leaves a stale pair); human judgments are kept
    store.cur.x("DELETE FROM preferences WHERE project_id=? AND judge != 'human'", (project_id,))

    def add(ctx, chosen, rejected, basis, judge):
        store.cur.x("INSERT OR IGNORE INTO preferences (project_id, context_hash, "
                    "chosen_event, rejected_event, basis, judge, ts) VALUES "
                    "(?,?,?,?,?,?,strftime('%s','now'))",
                    (project_id, ctx, chosen, rejected, basis, judge))
        out.append({"context": ctx, "chosen": chosen, "rejected": rejected, "judge": judge})
    for ctx, calls in by_ctx.items():
        if len(calls) < 2:
            continue
        calls = [c for c in calls if local(c)]
        good = [c for c in calls if outcome_by_session.get(c["session_id"]) == "PASS"]
        bad = [c for c in calls if outcome_by_session.get(c["session_id"]) == "FAIL"]
        pairs = [(g, b, "verified outcome (PASS vs FAIL) from the same request context",
                  "verification") for g in good for b in bad]
        rs = {c["event_id"]: router(c) for c in calls}
        rgood = [c for c in calls if rs[c["event_id"]]["positive"] and rs[c["event_id"]]["ok"]]
        rbad = [c for c in calls if rs[c["event_id"]]["negative"]]
        done = {(g["event_id"], b["event_id"]) for g, b, _, _ in pairs}
        pairs += [(g, b, "router labels on the same request context: "
                   + ",".join(rs[g["event_id"]]["positive"]) + " vs "
                   + ",".join(rs[b["event_id"]]["negative"]), "router_label")
                  for g in rgood for b in rbad if (g["event_id"], b["event_id"]) not in done]
        for g, b, basis, judge in pairs:
            if (g["facts"].get("content"), g["facts"].get("tool_calls")) == \
                    (b["facts"].get("content"), b["facts"].get("tool_calls")):
                continue
            add(ctx, g["event_id"], b["event_id"], basis, judge)
    # same task, same task prompt: an approved attempt vs a rejected one
    by_prompt = defaultdict(list)
    for e in episodes:
        if e["kind"] != "trajectory" or not e["task_id"] or e.get("role") == "helper":
            continue
        pre = prompt_prefix(e.get("messages") or [])
        if not pre or len(pre) >= len(e["messages"]):
            continue
        if not _local_only((e.get("flags") or {}).get("authors"), allow):
            continue
        by_prompt[(e["task_id"], content_hash(canonical_text(pre)))].append(e)
    for (task, ph), eps in by_prompt.items():
        good = [e for e in eps if _attempt_good(e)]
        bad = [e for e in eps if _attempt_bad(e) and not _attempt_good(e)]
        for g in good:
            for b in bad:
                if g["content_hash"] == b["content_hash"]:
                    continue
                why = []
                rv = (b["outcome"].get("review") or {}).get("verdict")
                if rv:
                    why.append(f"review {rv}")
                end = (b["outcome"].get("attempt_end") or {}).get("state")
                if end in ATTEMPT_NEGATIVE:
                    why.append(f"attempt ended {end}")
                if b["outcome"]["verification"]["status"] == "FAIL":
                    why.append("verification FAIL")
                add(ph, "ep:" + g["episode_id"], "ep:" + b["episode_id"],
                    f"same task prompt ({task}): approved attempt vs "
                    + (", ".join(why) or "gate FAIL"), "review")
    for r in store.cur.q("SELECT * FROM preferences WHERE project_id=? AND judge='human'",
                         (project_id,)):
        out.append({"context": r["context_hash"], "chosen": r["chosen_event"],
                    "rejected": r["rejected_event"]})
    return out


def record_human_preference(store, project_id: str, chosen_event: str, rejected_event: str,
                            judge: str, note: str) -> None:
    rows = store.norm.q("SELECT event_id, facts FROM events WHERE event_id IN (?,?) AND "
                        "project_id=? AND event_type='model_call'",
                        (chosen_event, rejected_event, project_id))
    if len(rows) != 2:
        raise ValueError("both events must be model_call events of this project")
    f = {r["event_id"]: json.loads(r["facts"]) for r in rows}
    ca = content_hash([f[chosen_event].get("messages"), f[chosen_event].get("tools")])
    cb = content_hash([f[rejected_event].get("messages"), f[rejected_event].get("tools")])
    if ca != cb:
        raise ValueError("not the same decision: the two calls had different request contexts")
    store.cur.x("INSERT OR REPLACE INTO preferences (project_id, context_hash, chosen_event, "
                "rejected_event, basis, judge, ts) VALUES (?,?,?,?,?,?,strftime('%s','now'))",
                (project_id, ca, chosen_event, rejected_event, f"human judgment: {note}", "human"))


__all__ = ["run", "requirements", "split_of", "minhash", "similarity", "random"]

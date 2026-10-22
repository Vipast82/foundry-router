"""Opt-in, revision-bound visual sign-off by an independent vision session.

This is a required review, separate from the optional visual-observation lane.
It never substitutes for the sprint's mechanical checks or code review. An
unavailable route, missing frame, uncertain answer or crash leaves the existing
approval gate unsatisfied.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re

from .adapters.foundry import FoundryDenied, FoundryError
from .profile import matches_any


def enabled(P) -> bool:
    return (P.profile.raw.get("approval") or {}).get("visual_model") is True


def _answer(text: str) -> dict:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw, re.I)
    if fenced:
        raw = fenced.group(1)
    try:
        result = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _required(P, spec, rule, arts: dict) -> tuple[list[tuple[str, str, bytes]], str]:
    pats = [p.replace("{task}", spec.task_id) for p in rule.artifacts]
    named = sorted({p.replace("\\", "/") for p in spec.scope
                    if p.lower().endswith((".png", ".jpg", ".jpeg"))
                    and matches_any(p, pats)})
    if not named:
        return [], "the plan names no required visual frames"
    frames = []
    for rel in named:
        if rel not in arts:
            return [], f"required frame {rel} is missing"
        if not P.profile.in_workspace(rel):
            return [], f"required frame {rel} leaves the workspace"
        path = os.path.join(P.profile.workspace, rel)
        try:
            with open(path, "rb") as fh:
                blob = fh.read(8_000_001)
        except OSError as e:
            return [], f"cannot read {rel}: {e}"
        if len(blob) > 8_000_000 or not blob:
            return [], f"{rel} is empty or over the 8 MB review limit"
        mime = "image/png" if blob.startswith(b"\x89PNG\r\n\x1a\n") else \
            "image/jpeg" if blob.startswith(b"\xff\xd8\xff") else ""
        if not mime:
            return [], f"{rel} is not a readable PNG/JPEG frame"
        if hashlib.sha256(blob).hexdigest() != arts[rel]:
            return [], f"{rel} changed while preparing visual review"
        frames.append((rel, mime, blob))
    return frames, ""


# Exact per-frame field values (UI-D1, 2026-10-20: the prompt named the fields but not their
# values, so a model answered validity "valid" for good frames and every one of them was
# counted as an invalid frame: 800+ bogus CHANGES findings in one round)
FRAME_FIELDS = (
    'Each frame object: "path" and "sha256" exactly as given, "image_seen": true only if you '
    'actually see the image, "subject_present": true when the expected UI is visible, '
    '"validity": exactly one of "ok", "invalid", "occluded", "missing_subject" ("ok" for a '
    'normal, readable capture, even one with defects), "observation": one line on what is '
    'visible, "findings": a list of defect strings (empty when the frame is fine).')
_OK_VALIDITY = {"ok", "valid", "good", "pass", "passed", "yes", "true"}


# B3 (2026-10-21): the structured retry of an inconclusive batch asks for this exact shape
# (Foundry passes it on as a json_schema response format)
VISION_SCHEMA = {
    "type": "object",
    "required": ["verdict", "frames", "limitations"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "changes_required", "uncertain"]},
        "limitations": {"type": "string"},
        "frames": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "sha256", "image_seen", "subject_present", "validity",
                         "observation", "findings"],
            "properties": {
                "path": {"type": "string"}, "sha256": {"type": "string"},
                "image_seen": {"type": "boolean"}, "subject_present": {"type": "boolean"},
                "validity": {"type": "string",
                             "enum": ["ok", "invalid", "occluded", "missing_subject"]},
                "observation": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "string"}}}}}},
}


# ---- clean-frame memory (v0.130.17, shared by every vision path since B3) ------------
# A frame the reviewer judged clean (seen, valid, no finding) at exact bytes is never sent
# to a vision model again for the task. The fast lane keeps its list under
# data.fast_lane.vision_clean; other paths use data.vision_clean. Both are read.

def clean_memory(t: dict) -> set:
    data = (t or {}).get("data") or {}
    return set(data.get("vision_clean") or []) | set(
        ((data.get("fast_lane") or {}).get("vision_clean")) or [])


def _task(store, pid: str, tid: str, default=None):
    fn = getattr(store, "task", None)
    return (fn(pid, tid) if callable(fn) else None) or default or {}


def remember_clean(store, pid: str, tid: str, hashes) -> None:
    hashes = [h for h in hashes if h]
    if not hashes or not callable(getattr(store, "merge_data", None)):
        return
    t = _task(store, pid, tid)
    mem = list(dict.fromkeys(list((t.get("data") or {}).get("vision_clean") or [])
                             + hashes))[-4000:]
    store.merge_data(pid, tid, vision_clean=mem)


def clean_frames(verdict: str, findings: list, rels: list) -> list:
    """The frames a verdict judged clean: all of them on approve; on changes_required only
    those no finding names, and only when every finding names a frame (a finding we can't
    place never lets a frame skip review)."""
    if verdict == "approve":
        return list(rels)
    notes = [str(f) for f in findings or []]
    if verdict != "changes_required" or not notes or \
            not all(any(rel in n for rel in rels) for n in notes):
        return []
    return [rel for rel in rels if not any(rel in n for n in notes)]


def _validity_ok(row: dict) -> bool:
    v = row.get("validity")
    if v is True:
        return True
    return isinstance(v, str) and v.strip().lower() in _OK_VALIDITY


def _valid_verdict(answer: dict, frames: list[tuple[str, str, bytes]], arts: dict) -> tuple[str, list[str]]:
    verdict = str(answer.get("verdict") or "").strip().lower().replace("-", "_").replace(" ", "_")
    verdict = {"approved": "approve", "approval": "approve", "changes": "changes_required",
               "change_required": "changes_required", "request_changes": "changes_required",
               "changes_requested": "changes_required", "unsure": "uncertain"}.get(verdict, verdict)
    if verdict not in ("approve", "changes_required", "uncertain"):
        return "uncertain", ["the vision model returned no valid verdict"]
    rows = answer.get("frames")
    if not isinstance(rows, list) or len(rows) != len(frames):
        return "uncertain", ["the vision model did not account for every required frame"]
    by_path = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            return "uncertain", ["a frame verdict has no path"]
        if row["path"] in by_path:
            return "uncertain", ["duplicate frame verdict"]
        by_path[row["path"]] = row
    if set(by_path) != {rel for rel, _, _ in frames}:
        return "uncertain", ["the frame verdicts name the wrong images"]
    findings = []
    for rel, _, _ in frames:
        row = by_path[rel]
        if row.get("sha256") != arts[rel] or row.get("image_seen") is not True:
            return "uncertain", [f"inspection of {rel} was not verified"]
        if not str(row.get("observation") or "").strip():
            return "uncertain", [f"{rel} has no visual observation"]
        if not isinstance(row.get("findings"), list):
            return "uncertain", [f"{rel} has no structured findings list"]
        for finding in row["findings"]:
            if not isinstance(finding, str) or not finding.strip():
                return "uncertain", [f"{rel} has an invalid finding"]
            findings.append(f"{rel}: {finding[:300]}")
        if row.get("subject_present") is not True or not _validity_ok(row):
            findings.append(f"{rel}: invalid, occluded or wrong UI subject: "
                            f"{row.get('observation', '')[:300]}")
    if verdict == "approve" and findings:
        return "changes_required", findings
    if verdict == "changes_required" and not findings:
        return "uncertain", ["changes required without a visible finding"]
    if verdict == "uncertain" and not findings:
        # every frame above was verified (image seen, sha256 matched, subject present,
        # validity ok, an observation) and none has a defect: that IS an approval, whatever
        # word the model put on top (UI-02a 2026-10-21: gpt-6-astra answered "uncertain" with
        # 20/20 clean frames on every retry, and the task stopped as inconclusive)
        return "approve", []
    return verdict, findings


def prior_approvals(store, pid: str, tid: str) -> list:
    fn = getattr(store, "approvals", None)
    if not callable(fn):
        return []
    try:
        return list(fn(pid, tid, "visual") or [])
    except Exception:                                            # noqa: BLE001
        return []


def accepted_hashes(store, pid: str, tid: str, t: dict) -> set:
    """W1.3: every frame sha a visual approval of this task already covers (approved,
    carried, or accepted with notes by the fast lane) plus the clean-frame memory.
    Such a frame is never sent to a vision model again."""
    out = set(clean_memory(_task(store, pid, tid, t)))
    for a in prior_approvals(store, pid, tid):
        out |= {str(v) for v in (a.get("artifacts") or {}).values() if v}
    return out


def _max_rounds(P) -> int:
    fl = getattr(P.profile, "fast_lane", None)
    try:
        return max(0, int((fl or {}).get("max_vision_rounds", 1)))
    except (TypeError, ValueError, AttributeError):
        return 1


def _file_sha(P, rel: str) -> str:
    path = os.path.join(P.profile.workspace, rel)
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _carry_prior(c, P, t: dict, revision: str, why: str) -> bool:
    """Carry the latest visual approval to `revision` when every frame it covers
    still has the same bytes in the workspace (W1.3: the templated closeout moves
    HEAD, so an exact-revision approval from the fast lane never matched)."""
    pid, tid = P.id, t["task_id"]
    prior = prior_approvals(c.store, pid, tid)
    if not prior:
        return False
    last = prior[-1]
    arts = dict(last.get("artifacts") or {})
    if not arts:
        return False
    if any(_file_sha(P, rel) != sha for rel, sha in arts.items()):
        return False
    c.store.add_approval(pid, tid, "visual", revision, arts, last.get("approver") or "carried",
                         f"carried from the visual approval at "
                         f"{str(last.get('revision') or '')[:12]}: every frame it covers is "
                         f"byte-identical ({len(arts)} frame(s)); {why}; no model call")
    c.store.event(pid, tid, "visual_approval_carried", detail={
        "revision": revision, "from": last.get("revision"), "frames": len(arts), "why": why})
    return True


def _missing_frames(c, P, t: dict, spec, rule, revision: str, problem: str) -> None:
    """W1.3: a required frame that is missing (or a plan naming none) is a
    continuation: one recapture round in CHANGES_REQUIRED with the exact finding,
    then a stop the lead sees. Never a silent AWAITING_HUMAN forever."""
    pid, tid = P.id, t["task_id"]
    if _carry_prior(c, P, t, revision, f"the rule's frames could not be gathered ({problem})"):
        return
    data = dict((_task(c.store, pid, tid, t) or {}).get("data") or {})
    rounds = dict(data.get("visual_missing_rounds") or {})
    n = int(rounds.get(revision[:12]) or 0)
    finding = (f"visual approval: {problem}. Capture and commit the required frame(s) the plan "
               f"names (rule artifacts {list(rule.artifacts)}) at the current revision, by "
               f"explicit path; nothing else changes.")
    if n < 1 and callable(getattr(c, "reject_human", None)):
        rounds[revision[:12]] = n + 1
        if callable(getattr(c.store, "merge_data", None)):
            c.store.merge_data(pid, tid, visual_missing_rounds=rounds)
        c.store.event(pid, tid, "visual_frames_missing", detail={
            "revision": revision, "problem": problem[:300], "round": n + 1,
            "action": "recapture (CHANGES_REQUIRED)"})
        c.reject_human(pid, tid, [finding], "coordinator:visual_approval")
        return
    stop = getattr(c, "_stop", None)
    if callable(stop):
        from . import states as S
        c.store.event(pid, tid, "visual_frames_missing", detail={
            "revision": revision, "problem": problem[:300], "round": n,
            "action": "stop (after the recapture round)"})
        stop(P, _task(c.store, pid, tid, t), S.BLOCKED, {
            "blocker": f"visual approval: {problem} (still missing after the recapture round)"},
            cls="visual_frames_missing")
        return
    c.store.update_task(pid, tid, next_action=f"visual approval: {problem[:350]}")


def _accept_with_notes(c, P, t: dict, revision: str, arts: dict, actor: str, notes: list,
                       why: str) -> None:
    pid, tid = P.id, t["task_id"]
    notes = [str(n)[:400] for n in notes][:40]
    if callable(getattr(c.store, "merge_data", None)):
        cur = dict((_task(c.store, pid, tid, t) or {}).get("data") or {})
        c.store.merge_data(pid, tid, visual_notes=(list(cur.get("visual_notes") or [])
                                                   + notes)[-60:])
    c.store.add_approval(pid, tid, "visual", revision, arts,
                         f"coordinator:visual_approval ({actor})",
                         f"accepted with notes {why}: " + " | ".join(notes)[:1500])
    c.store.event(pid, tid, "visual_approval_accepted_with_notes", detail={
        "revision": revision, "why": why, "notes": notes[:20]})


def step(c, P, t: dict, spec, rule, revision: str) -> None:
    """One fail-closed approval attempt; the caller rechecks artifact hashes."""
    if not enabled(P) or rule.kind != "visual" or P.foundry is None:
        return
    pid, tid = P.id, t["task_id"]
    dirty = {path for _, path in P.ws.status()}
    if P.ws.head() != revision:
        c.store.update_task(pid, tid, next_action="visual approval waits for the exact clean "
                            "closeout revision")
        return
    arts = P.adapter.approval_artifacts(spec, rule.artifacts)
    if any(path in dirty for path in arts):
        c.store.update_task(pid, tid, next_action="visual approval waits for committed "
                            "screenshot bytes")
        return
    frames, problem = _required(P, spec, rule, arts)
    if problem:
        if "missing" in problem or "names no required" in problem:
            return _missing_frames(c, P, t, spec, rule, revision, problem)
        c.store.update_task(pid, tid, next_action=f"visual approval: {problem[:350]}")
        return
    # B3 / W1.3: frames already judged clean at these exact bytes (this path or the fast
    # lane), or covered by ANY earlier visual approval (approved, carried, accepted with
    # notes), are never sent again; when none is left the record itself is the approval
    mem = accepted_hashes(c.store, pid, tid, t)
    todo = [f for f in frames if arts.get(f[0]) not in mem]
    if not todo:
        c.store.add_approval(pid, tid, "visual", revision, arts, "coordinator:vision-memory",
                             f"every required frame was judged clean or accepted at these bytes "
                             f"earlier ({len(frames)} frame(s)); no model call")
        c.store.event(pid, tid, "visual_approval_from_memory", detail={
            "revision": revision, "frames": len(frames)})
        return
    frames = todo
    persona = P.profile.persona("visual_reviewer", tid)
    try:
        info = P.foundry.persona_info(persona)
    except FoundryError as e:
        c.store.update_task(pid, tid, next_action=f"visual approval route unavailable: {e}"[:500])
        return
    routes = (info or {}).get("routes") or []
    if not (info and info.get("exists", True) and info.get("enabled", True)
            and info.get("strict") is True and info.get("client_allowed") is not False
            and routes and all(r.get("vision") is True and r.get("model") for r in routes)):
        c.store.update_task(pid, tid, next_action="visual approval route is not verified "
                            "vision-capable for every possible model")
        return
    fingerprint = hashlib.sha256(json.dumps([revision, arts], sort_keys=True).encode()).hexdigest()
    data0 = dict((_task(c.store, pid, tid, t) or {}).get("data") or {})
    tries = int((data0.get("visual_uncertain") or {}).get(fingerprint[:16]) or 0)
    key = f"visual-approval:{pid}:{tid}:{fingerprint}" + (f":retry{tries}" if tries else "")
    prior = c.store.op_begin(key, pid, tid, "visual_approval")
    if prior:
        result = prior.get("result") or {}
        if prior["status"] == "done":
            actor = f"model:{persona}/{result.get('served_model') or 'unknown'}"
            if result.get("verdict") == "approve" and not c._valid_approval(
                    P, t, spec, rule, revision):
                c.store.add_approval(pid, tid, "visual", revision, arts, actor,
                                     f"recovered recorded model review {result.get('session_id')}; "
                                     "policy visual_model v1")
            elif result.get("verdict") == "changes_required":
                _changes(c, P, t, revision, arts, actor, list(result.get("findings") or []))
            else:
                # W1.3: an inconclusive answer is retried once (structured), then
                # accepted with notes; never a dead end at AWAITING_HUMAN
                _uncertain(c, P, t, revision, arts, actor, fingerprint, tries,
                           list(result.get("findings") or []))
            return
        if prior["status"] == "deferred" and c.clock() >= float(
                result.get("retry_at") or 0):
            pass
        else:
            if prior["status"] == "calling":
                c.store.update_task(pid, tid, next_action="visual review call outcome "
                                    "uncertain after restart; paid call not repeated")
            return  # an in-flight/finished call is never paid for twice
    sid = f"visual-approval-{fingerprint[:20]}"
    if not c.store.session(sid):
        c.store.add_session(sid, pid, tid, t["run_id"], "visual_reviewer", "visual_approval",
                            t["attempt"], "foundry-direct", persona,
                            {"parent": t.get("session_id"), "revision": revision})
    retry_note = ("This is the one retry of a review whose first answer could not be verified: "
                  "answer with exactly the JSON object the response schema describes, one frame "
                  "object per attached image, each sha256 copied exactly. " if tries else "")
    parts = [{"type": "text", "text": retry_note + (
        f"Review ALL {len(frames)} required current screenshots for project {pid}, task {tid}, "
        f"revision {revision}. Assess the sprint acceptance criteria below. Check the expected "
        "UI subject and state, readable text, clipping, overlap, contrast, target sizes, "
        "alignment, safe areas and visible defects in each viewport. Do not infer pixels "
        "from filenames or claim inspection without seeing each image. Return valid JSON "
        'with keys "verdict" (approve, changes_required or uncertain), "frames" (one '
        'object per image with path, sha256, image_seen, subject_present, validity, '
        'observation and findings), and "limitations". ' + FRAME_FIELDS + " "
        "Approve only if every required frame is valid, actually seen and has no visible "
        f"defect.\n\nSprint criteria:\n{spec.spec[:10000]}")}]
    for rel, mime, blob in frames:
        parts.append({"type": "text", "text": f"Required frame {rel}, SHA256 {arts[rel]}"})
        parts.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(blob).decode()}"}})
    try:
        P.foundry.bind(sid, pid, tid, t["run_id"], "visual_reviewer", persona,
                       parent=t.get("session_id") or "", purpose="review")
        c.store.op_update(key, "calling", {"session_id": sid})
        kw = {"schema": VISION_SCHEMA} if tries else {}
        raw, meta = P.foundry.chat(persona, [
            {"role": "system", "content": "You are a read-only, independent vision reviewer. "
             "Judge only the actual attached frames. Never waive test gates or invent evidence."},
            {"role": "user", "content": parts}], session_id=sid, max_tokens=5000, **kw)
    except FoundryDenied as e:
        delay = max(60, int(e.retry_after or 300))
        c.store.op_update(key, "deferred", {"retry_at": c.clock() + delay,
                                            "reason": str(e)[:300]})
        c.store.update_task(pid, tid, next_action=f"visual approval deferred: {e}"[:500])
        return
    except FoundryError as e:
        c.store.op_update(key, "deferred", {"retry_at": c.clock() + 300,
                                            "reason": str(e)[:300]})
        c.store.update_task(pid, tid, next_action=f"visual approval unavailable: {e}"[:500])
        return
    served = str((meta or {}).get("served_model") or "")
    answer = _answer(raw)
    verdict, findings = _valid_verdict(answer, frames, arts)
    if served not in {r["model"] for r in routes}:
        verdict, findings = "uncertain", ["actual serving model was not verified as vision-capable"]
    c.store.op_update(key, "done", {"session_id": sid, "verdict": verdict,
                                    "served_model": served, "findings": findings[:10],
                                    "response_sha256": hashlib.sha256(str(raw).encode()).hexdigest()})
    c.store.update_session(sid, status="ended", outcome=verdict, ended_ts=c.clock())
    c.store.event(pid, tid, "model_visual_review", session_id=sid, detail={
        "verdict": verdict, "persona": persona, "served_model": served,
        "revision": revision, "artifacts": arts, "findings": findings[:10]})
    remember_clean(c.store, pid, tid, [arts[r] for r in clean_frames(
        verdict, findings, [rel for rel, _, _ in frames])])
    if (P.ws.head() != revision or
            P.adapter.approval_artifacts(spec, rule.artifacts) != arts or
            any(path in {p for _, p in P.ws.status()} for path in arts)):
        c.store.update_task(pid, tid, next_action="visual review became stale; recapture "
                            "and review the current revision")
        return
    actor = f"model:{persona}/{served}"
    if verdict == "approve":
        c.store.add_approval(pid, tid, "visual", revision, arts, actor,
                             f"model visual review {sid}; policy visual_model v1")
    elif verdict == "changes_required":
        _changes(c, P, t, revision, arts, actor, findings)
    else:
        _uncertain(c, P, t, revision, arts, actor, fingerprint, tries, findings)


def _uncertain(c, P, t: dict, revision: str, arts: dict, actor: str, fingerprint: str,
               tries: int, findings: list) -> None:
    """W1.3: one structured retry of an inconclusive review, then accept with notes."""
    pid, tid = P.id, t["task_id"]
    if tries < 1 and callable(getattr(c.store, "merge_data", None)):
        cur = dict((_task(c.store, pid, tid, t) or {}).get("data") or {})
        unc = dict(cur.get("visual_uncertain") or {})
        unc[fingerprint[:16]] = tries + 1
        c.store.merge_data(pid, tid, visual_uncertain=unc)
        c.store.event(pid, tid, "visual_approval_retry", detail={
            "revision": revision, "why": findings[:4], "retry": tries + 1})
        c.store.update_task(pid, tid, next_action="visual review inconclusive; one structured "
                            "retry at the current revision")
        return
    _accept_with_notes(c, P, t, revision, arts, actor,
                       [f"inconclusive after one structured retry: {f}" for f in findings[:10]]
                       or ["inconclusive after one structured retry"],
                       "(inconclusive after one structured retry)")


def _changes(c, P, t: dict, revision: str, arts: dict, actor: str, findings: list) -> None:
    """Vision CHANGES after closeout: back into one fix round (CHANGES_REQUIRED), at
    most [fast_lane] max_vision_rounds times per task (default 1); then accepted
    with notes (W1.3), like the fast lane does at the gate."""
    pid, tid = P.id, t["task_id"]
    cur = dict((_task(c.store, pid, tid, t) or {}).get("data") or {})
    rounds = int(cur.get("visual_changes_rounds") or 0)
    mx = _max_rounds(P)
    if rounds < mx and callable(getattr(c, "reject_human", None)):
        if callable(getattr(c.store, "merge_data", None)):
            c.store.merge_data(pid, tid, visual_changes_rounds=rounds + 1)
        c.store.event(pid, tid, "visual_approval_changes", detail={
            "revision": revision, "round": rounds + 1, "of": mx, "findings": findings[:10]})
        c.reject_human(pid, tid, findings, actor)
        return
    _accept_with_notes(c, P, t, revision, arts, actor, findings,
                       f"after {rounds} CHANGES round(s)")

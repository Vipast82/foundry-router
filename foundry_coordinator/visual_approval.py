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
    return verdict, findings


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
        c.store.update_task(pid, tid, next_action=f"visual approval: {problem[:350]}")
        return
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
    key = f"visual-approval:{pid}:{tid}:{fingerprint}"
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
                c.reject_human(pid, tid, list(result.get("findings") or []), actor)
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
    parts = [{"type": "text", "text": (
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
        raw, meta = P.foundry.chat(persona, [
            {"role": "system", "content": "You are a read-only, independent vision reviewer. "
             "Judge only the actual attached frames. Never waive test gates or invent evidence."},
            {"role": "user", "content": parts}], session_id=sid, max_tokens=5000)
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
    if (P.ws.head() != revision or
            P.adapter.approval_artifacts(spec, rule.artifacts) != arts or
            any(path in {p for _, p in P.ws.status()} for path in arts)):
        c.store.update_task(pid, tid, next_action="visual review became stale; recapture "
                            "and review the current revision")
        return
    if verdict == "approve":
        c.store.add_approval(pid, tid, "visual", revision, arts,
                             f"model:{persona}/{served}",
                             f"model visual review {sid}; policy visual_model v1")
    elif verdict == "changes_required":
        c.reject_human(pid, tid, findings, f"model:{persona}/{served}")
    else:
        c.store.update_task(pid, tid, next_action="visual review inconclusive; frames "
                            "must be reviewed again at the current revision")

"""Coordinator store (foundry_coordinator) -> normalized events, read-only.

Per task: lifecycle transitions with their evidence, sessions (which run,
role, phase, persona), verification results bound to a revision with their
raw-evidence pointers, frontier reviews bound to the reviewed commit,
human approvals bound to revision + artifact hashes, and human corrections
(rejections with findings, answers to questions).

UI v3 additions (all optional tables, read only when present):
  authorship  task_memory notes written by `helper:<unit>` (prework drafts:
              refs = [unit, .plan/tmp/prework/<task>/<unit>.patch]) and by
              `helper:precode-harvest` (refs = the harvested model files),
              plus `prework_ready` events. The patch is read whole from the
              workspace at ingest (closeout) and stored as block hashes
              (any size) plus the text while small, so matching never
              depends on the git-ignored tmp file surviving. A patch that is
              already gone is recorded as content 'missing' (fail closed).
  labels      `model_visual_review` events (visual_approval.py: verdict,
              findings, revision, artifact hashes) and operator / playtest
              critiques (`operator_critique` / `playtest_critique` events).

Session -> task links from here are observed facts (the coordinator started
those sessions).
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterator, Optional

from ..schema import Event

ADAPTER = "coordinator"
LABEL_EVENTS = ("model_visual_review", "prework_ready", "operator_critique", "playtest_critique")
MAX_PATCH = 60_000          # patch text kept inline up to this; hashes cover any size
MAX_READ = 50_000_000
VISION = {"approve": "APPROVED", "changes_required": "CHANGES", "uncertain": "UNCERTAIN"}


def from_db(path: str) -> Iterator[tuple[str, str, dict, dict]]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        def q(sql, args=()):
            return [dict(r) for r in c.execute(sql, args)]
        tables = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
        ws = {}
        if "projects" in tables:
            ws = {r["project_id"]: r["workspace"] for r in q("SELECT project_id, workspace "
                                                             "FROM projects")}
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
                "memory": q("SELECT mem_id, kind, text, refs, revision, actor, status, created_ts "
                            "FROM task_memory WHERE project_id=? AND task_id=? AND actor LIKE "
                            "'helper:%' ORDER BY created_ts", key)
                if "task_memory" in tables else [],
                "coord_events": q("SELECT event_id, ts, session_id, kind, detail FROM events "
                                  "WHERE project_id=? AND task_id=? AND kind IN (%s) ORDER BY id"
                                  % ",".join("?" * len(LABEL_EVENTS)), (*key, *LABEL_EVENTS))
                if "events" in tables else [],
            }
            payload["patches"] = _patches(ws.get(t["project_id"]) or "", payload)
            rid = f"{t['project_id']}/{t['task_id']}@{t['updated_ts']}"
            yield (rid, t["project_id"], payload,
                   {"adapter": ADAPTER, "store": path, "sanitization": "none"})
    finally:
        c.close()


def _patches(workspace: str, p: dict) -> dict:
    """{patch path: text} for the prework patches the records name, read
    from the workspace while they still exist (they live in a git-ignored
    tmp dir). Absent files are simply absent (the episode builder then
    matches on the path alone)."""
    paths = set()
    for m in p.get("memory") or []:
        for r in _j(m.get("refs"), []):
            if str(r).endswith(".patch"):
                paths.add(str(r))
    for e in p.get("coord_events") or []:
        if e["kind"] == "prework_ready" and _j(e["detail"], {}).get("patch"):
            paths.add(str(_j(e["detail"], {})["patch"]))
    out = {}
    if not workspace:
        return out
    root = os.path.realpath(workspace)
    for rel in sorted(paths):
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                out[rel] = f.read(MAX_READ)
        except OSError:
            pass
    return out


def _j(v, d):
    try:
        return json.loads(v) if isinstance(v, str) and v else (v if v is not None else d)
    except ValueError:
        return d


def normalize(p: dict, project_id: str, raw_ref: str, redaction: dict, store=None,
              workspace: str = "", artifact_roots: Optional[list] = None) -> list[Event]:
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
    ev += _authorship(p, project_id, tid, common, workspace)
    ev += _labels(p, project_id, tid, common, store, workspace, artifact_roots)
    return ev


def _patch_facts(rel: str, patches: dict, workspace: str) -> dict:
    """The helper patch as captured now (closeout ingest): block hashes over
    the whole text (labels.index_facts, any size), the text inline only while
    it is small. A patch that is already gone is recorded as content
    'missing' so the episode builder fails closed instead of trusting
    path-only matching."""
    from ..labels import index_facts
    if not rel:
        return {"patch_text": "", "blocks": [], "small": [], "content": "none"}
    text = patches.get(rel)
    if text is None and workspace:
        root = os.path.realpath(workspace)
        full = os.path.realpath(os.path.join(root, rel))
        if full.startswith(root + os.sep) and os.path.isfile(full):
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read(MAX_READ)
            except OSError:
                text = None
    facts = index_facts(text or "")
    facts["patch_text"] = (text or "") if len(text or "") <= MAX_PATCH else ""
    facts["patch_text_omitted"] = len(text or "") > MAX_PATCH
    return facts


def _authorship(p: dict, project_id: str, tid: str, common: dict,
                workspace: str = "") -> list[Event]:
    """Code that a helper wrote (prework patch) or that was harvested from a
    frontier pre-code (helper:precode-harvest). Recorded as provenance; the
    episode builder marks the steps that applied it as non-target context.
    Helper guidance without code (plain notes) is not authorship: the local
    primary's code stays eligible."""
    out = []
    patches = p.get("patches") or {}
    seen = set()
    for m in p.get("memory") or []:
        actor = str(m.get("actor") or "")
        refs = [str(r) for r in _j(m.get("refs"), [])]
        patch = next((r for r in refs if r.endswith(".patch")), "")
        unit = actor.split(":", 1)[1] if ":" in actor else ""
        basis = ("precode-harvest" if actor == "helper:precode-harvest" else
                 "prework-patch" if patch else "helper-note")
        if basis == "helper-note":
            continue
        paths = [r for r in refs if r != unit and not r.endswith(".patch")]
        seen.add(patch or m["mem_id"])
        pf = _patch_facts(patch, patches, workspace)
        out.append(Event(ADAPTER, f"{project_id}/{tid}/mem/{m['mem_id']}", "authorship",
                         part="0" if pf["content"] != "missing" else "missing",
                         timestamp=m.get("created_ts") or 0, actor_role="assistant",
                         actor_model=actor, source_revision=m.get("revision") or None,
                         facts={"author": actor, "basis": basis, "unit": unit, "patch": patch,
                                **pf, "paths": paths,
                                "status": m.get("status"),
                                "note": str(m.get("text") or "")[:400]}, **common))
    for e in p.get("coord_events") or []:
        d = _j(e["detail"], {})
        if e["kind"] != "prework_ready" or not d.get("patch") or d["patch"] in seen:
            continue
        pf = _patch_facts(d["patch"], patches, workspace)
        out.append(Event(ADAPTER, f"{project_id}/{tid}/cev/{e['event_id']}", "authorship",
                         part="0" if pf["content"] != "missing" else "missing",
                         timestamp=e["ts"] or 0, actor_role="assistant",
                         actor_model=f"helper:{d.get('unit')}",
                         source_revision=d.get("head") or None,
                         facts={"author": f"helper:{d.get('unit')}", "basis": "prework-patch",
                                "unit": d.get("unit"), "patch": d["patch"],
                                **pf, "paths": [],
                                "status": "active", "note": str(d.get("step") or "")[:400]},
                         **common))
    return out


def _labels(p, project_id, tid, common, store, workspace, roots) -> list[Event]:
    """Vision verdicts (APPROVED / CHANGES with numbered findings) and
    operator / playtest critiques, bound to the revision they judged."""
    out = []
    for e in p.get("coord_events") or []:
        d = _j(e["detail"], {})
        if e["kind"] == "model_visual_review":
            raw = str(d.get("verdict") or "").lower()
            arts = d.get("artifacts") if isinstance(d.get("artifacts"), dict) else {}
            refs, frames = [], []
            for i, (rel, sha) in enumerate(sorted(arts.items())):
                fr = {"path": rel, "sha256_recorded": sha, "status": "missing"}
                if store is not None:
                    from ..artifacts import resolve
                    r = resolve(store, project_id, f"vision:{e['event_id']}:{i}", rel, workspace,
                                roots or [], {"adapter": ADAPTER, "label": "vision_verdict",
                                              "event": e["event_id"]})
                    fr["status"] = r["status"]
                    if r.get("sha256"):
                        fr.update(sha256=r["sha256"], matches_recorded=r["sha256"] == sha)
                        refs.append({"sha256": r["sha256"], "mime": r.get("mime"),
                                     "width": r.get("width"), "height": r.get("height"),
                                     "path": rel})
                frames.append(fr)
            out.append(Event(ADAPTER, f"{project_id}/{tid}/cev/{e['event_id']}", "label",
                             timestamp=e["ts"] or 0, actor_role="reviewer",
                             requested_persona=d.get("persona"),
                             actual_model=d.get("served_model") or None,
                             source_revision=d.get("revision") or None,
                             artifact_references=refs,
                             facts={"label": "vision_verdict", "session": e.get("session_id"),
                                    "verdict": VISION.get(raw, "UNCERTAIN"), "raw_verdict": raw,
                                    "findings": [{"n": i + 1, "text": str(f)}
                                                 for i, f in enumerate(d.get("findings") or [])],
                                    "persona": d.get("persona"),
                                    "served_model": d.get("served_model"), "frames": frames},
                             **common))
        elif e["kind"] in ("operator_critique", "playtest_critique"):
            items = d.get("findings") or ([d["text"]] if d.get("text") else [])
            out.append(Event(ADAPTER, f"{project_id}/{tid}/cev/{e['event_id']}", "label",
                             timestamp=e["ts"] or 0, actor_role="human",
                             source_revision=d.get("revision") or d.get("head") or None,
                             facts={"label": "critique", "kind": e["kind"],
                                    "by": d.get("by") or d.get("actor") or "operator",
                                    "findings": [{"n": i + 1, "text": str(f)}
                                                 for i, f in enumerate(items)]}, **common))
    return out

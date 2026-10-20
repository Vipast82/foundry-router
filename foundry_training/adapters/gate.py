"""studio_gate verdict JSON -> label events, read-only.

The fast-lane runner (foundry_coordinator.projects.animaluprising.studio_gate)
writes one verdict per gated revision and mode to
  <workspace>/.plan/tmp/gate/<SPRINT>/<head12>.<mode>.json   (schema 1; carries mode, run_id)
(older runs: <head12>.json, no mode). Only those files are read here: latest/,
latest.json, status.json, gate_expect*.json, *.raw.log and *.vision.json are copies,
progress or inputs, never verdicts. Only mode == "sprint" verdicts become gate labels
(a legacy file without mode counts as sprint); stories/regression runs are skipped.
A verdict is a label on the episodes of task <SPRINT> whose head matches
`head` (never text inside an episode). Its captures are artifacts: their
bytes are read only from the project's artifact roots (docs/evidence), and a
capture whose file is gone or outside the roots is recorded as such, never
assumed present.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Iterator, Optional

from ..schema import Event, content_hash

ADAPTER = "gate"
SKIP = {"latest.json", "status.json"}
# <head12>.json (legacy) or <head12>.<mode>.json; anything else in the sprint dir is not a verdict
_NAME = re.compile(r"^(?P<head>[0-9a-f]{12})(?:\.(?P<mode>[a-z][a-z0-9_-]*))?\.json$")
NOT_MODES = {"vision", "raw"}
LABEL_MODE = "sprint"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
RESULTS = ("PASS", "FAIL", "SKIP", "UNKNOWN")


def parse_name(name: str) -> Optional[tuple[str, Optional[str]]]:
    """(head12, mode | None) for a real verdict file name, else None."""
    if name in SKIP or name.startswith("gate_expect"):
        return None
    m = _NAME.match(name)
    if not m or m.group("mode") in NOT_MODES:
        return None
    return m.group("head"), m.group("mode")


def verdict_files(workspace: str) -> list[str]:
    """Sprint-mode verdict files only (<head12>.sprint.json and legacy <head12>.json);
    latest/ is a subdirectory and never globbed."""
    root = os.path.join(workspace, ".plan", "tmp", "gate")
    out = []
    for p in glob.glob(os.path.join(root, "*", "*.json")):
        parsed = parse_name(os.path.basename(p))
        if parsed and (parsed[1] or LABEL_MODE) == LABEL_MODE:
            out.append(p)
    return sorted(out)


def records(workspace: str, project_id: str) -> Iterator[tuple[str, str, dict, dict]]:
    for path in verdict_files(workspace):
        rel = os.path.relpath(path, workspace).replace("\\", "/")
        try:
            with open(path, encoding="utf-8") as f:
                v = json.load(f)
        except (OSError, ValueError) as e:
            yield (f"{project_id}:{rel}", project_id,
                   {"path": rel, "unreadable": f"{type(e).__name__}: {e}"[:300]},
                   {"adapter": ADAPTER, "workspace": workspace, "sanitization": "none"})
            continue
        if isinstance(v, dict) and (v.get("mode") or LABEL_MODE) != LABEL_MODE:
            continue                      # a stories/regression run is never a gate label
        problem = _problem(v, path)
        if problem:
            yield (f"{project_id}:{rel}", project_id, {"path": rel, "unreadable": problem},
                   {"adapter": ADAPTER, "workspace": workspace, "sanitization": "none"})
            continue
        # one record per verdict content: a re-run at the same head is a new label
        # one id for records() and normalize(): hashed here, before redaction
        rid = f"{project_id}:{v['sprint']}:{v['head'][:12]}:{content_hash(v)[:12]}"
        yield (rid, project_id, {"path": rel, "verdict": v, "rid": rid},
               {"adapter": ADAPTER, "workspace": workspace, "sanitization": "none"})


def _problem(v, path: str) -> Optional[str]:
    if not isinstance(v, dict) or v.get("schema") != 1:
        return "not a schema-1 gate verdict"
    if not v.get("sprint") or not _HEX40.match(str(v.get("head") or "")):
        return "verdict without sprint or a 40-hex head"
    if os.path.basename(os.path.dirname(path)) != v["sprint"]:
        return f"verdict for {v['sprint']} filed under another sprint"
    parsed = parse_name(os.path.basename(path))
    if not parsed or parsed[0] != v["head"][:12]:
        return "file name does not match the verdict head"
    return None


def overall(checks: list, error=None) -> str:
    """The runner's rule (studio_gate.overall), recomputed here so a label
    never rests on the file's own `result`: PASS only if there is at least
    one required check and every required check is PASS; an error is ERROR."""
    if error:
        return "ERROR"
    req = [c for c in checks if isinstance(c, dict) and c.get("required", True)]
    if not req:
        return "FAIL"
    return "PASS" if all(c.get("result") == "PASS" for c in req) else "FAIL"


def failed_checks(checks: list) -> list[dict]:
    """Required checks that did not pass (a SKIP/UNKNOWN required check keeps
    the gate red, so it is a finding too)."""
    return [{"id": c.get("id"), "result": c.get("result"), "detail": c.get("detail") or "",
             "artifacts": c.get("artifacts") or []}
            for c in checks if isinstance(c, dict) and c.get("required", True)
            and c.get("result") != "PASS"]


def normalize(p: dict, project_id: str, raw_ref: str, redaction: dict, store=None,
              workspace: str = "", artifact_roots: Optional[list] = None) -> list[Event]:
    if p.get("unreadable"):
        return [Event(ADAPTER, f"{project_id}:{p['path']}", "capture_gap", timestamp=0,
                      project_id=project_id, capture_status="missing", raw_ref=raw_ref,
                      facts={"what": "gate verdict", "path": p["path"],
                             "error": p["unreadable"]})]
    v = p["verdict"]
    rid = p.get("rid") or f"{project_id}:{v['sprint']}:{v['head'][:12]}:{content_hash(v)[:12]}"
    checks = [c for c in v.get("checks") or [] if isinstance(c, dict)]
    claimed = str(v.get("result") or "UNKNOWN").upper()
    computed = overall(checks, v.get("error"))
    # a hand-edited / forged verdict (the implementer can write .plan/tmp):
    # the label is marked inconsistent, UNKNOWN, and its episodes are excluded
    inconsistent = claimed != computed
    result = "UNKNOWN" if inconsistent else computed
    refs, captures = [], []
    for i, cap in enumerate(c for c in v.get("captures") or [] if isinstance(c, dict)):
        entry = {"story": cap.get("story"), "size": cap.get("size"), "path": cap.get("path"),
                 "geometry_defects": cap.get("geometry_defects"),
                 "layout_lines": list(cap.get("layout_lines") or [])[:40], "status": "missing"}
        if store is not None and cap.get("path"):
            from ..artifacts import resolve
            r = resolve(store, project_id, f"gate:{rid}:{i}", cap["path"], workspace,
                        artifact_roots or [], {"adapter": ADAPTER, "verdict": p["path"],
                                               "story": cap.get("story"),
                                               "size": cap.get("size")})
            entry["status"] = r["status"]
            if r.get("sha256"):
                entry.update(sha256=r["sha256"], mime=r.get("mime"), width=r.get("width"),
                             height=r.get("height"))
                refs.append({"sha256": r["sha256"], "mime": r.get("mime"),
                             "width": r.get("width"), "height": r.get("height"),
                             "path": cap["path"], "story": cap.get("story"),
                             "size": cap.get("size")})
        captures.append(entry)
    return [Event(ADAPTER, rid, "label", timestamp=float(v.get("finished") or v.get("started")
                                                         or 0),
                  project_id=project_id, task_id=v["sprint"], source_revision=v["head"],
                  actor_role="coordinator", raw_ref=raw_ref, artifact_references=refs,
                  redaction_status=redaction.get("status", "clean"),
                  capture_status="complete" if all(c["status"] == "present" for c in captures)
                  else "partial",
                  verification_status=result if result in ("PASS", "FAIL") else "UNKNOWN",
                  facts={"label": "gate_verdict", "path": p["path"], "mode": v.get("mode") or LABEL_MODE,
                         "run_id": v.get("run_id"),
                         "result": result, "claimed_result": claimed,
                         "computed_result": computed, "inconsistent": inconsistent,
                         "checks": [{"id": c.get("id"), "result": c.get("result"),
                                     "required": bool(c.get("required", True)),
                                     "detail": c.get("detail") or ""} for c in checks],
                         "failed": failed_checks(checks), "captures": captures,
                         "regression": v.get("regression"),
                         "summary_line": v.get("summary_line"), "error": v.get("error"),
                         "started": v.get("started"), "finished": v.get("finished")})]

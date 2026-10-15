"""Frontier review contract: the packet a reviewer gets and the result it
must return, validated before the engine accepts it.

A review is bound to one exact commit. The result is untrusted data: it can
approve, ask for changes or block, but it can never widen a task's scope,
skip a failed mechanical check, or grant itself anything. An APPROVED verdict
that still lists blocking findings or missing evidence is not an approval.
"""

from __future__ import annotations

import json
import re
from typing import Optional

VERDICTS = ("APPROVED", "CHANGES_REQUIRED", "BLOCKED")
SEVERITIES = ("blocker", "major", "minor", "info")

SCHEMA = {
    "type": "object",
    "required": ["sprint_id", "reviewed_commit_sha", "verdict", "findings",
                 "evidence_inspected", "missing_evidence", "required_repairs", "residual_risks"],
    "properties": {
        "sprint_id": {"type": "string"},
        "project_id": {"type": "string"},
        "reviewed_commit_sha": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "findings": {"type": "array", "items": {
            "type": "object", "required": ["severity", "message"],
            "properties": {"severity": {"type": "string", "enum": list(SEVERITIES)},
                           "file": {"type": "string"}, "line": {"type": ["integer", "null"]},
                           "message": {"type": "string"}}}},
        "evidence_inspected": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "required_repairs": {"type": "array", "items": {"type": "string"}},
        "residual_risks": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You are a code reviewer for one development task. You review only the diff and evidence in the packet.
Return ONLY a JSON object (no prose, no code fence) with exactly these keys:
  sprint_id (the task id given), project_id, reviewed_commit_sha (the full head commit given),
  verdict: "APPROVED" | "CHANGES_REQUIRED" | "BLOCKED",
  findings: [{"severity": "blocker|major|minor|info", "file": "<repo-relative path>", "line": <int or null>, "message": "..."}],
  evidence_inspected: [...], missing_evidence: [...], required_repairs: [...], residual_risks: [...]
APPROVED only if the change does what the task asks, stays in its listed files, and the evidence supports it.
CHANGES_REQUIRED when specific repairs inside the task's files would fix it. BLOCKED when it needs a human decision or work outside the task.
You cannot change the task's scope or override a failed check; say what you saw."""


class ReviewInvalid(ValueError):
    pass


def build_packet(*, project_id: str, task_id: str, title: str, spec: str, scope: list[str],
                 base: str, head: str, commits: list, diff: str, diff_truncated: bool,
                 verification: list[dict], extra: str = "", question: str = "") -> str:
    vlines = []
    for v in verification:
        vlines.append(f"- {v['check_name']}: {v['result']}{' (required)' if v['required'] else ''}"
                      f" — evidence: {v.get('evidence') or 'inline'}\n"
                      + "\n".join("    " + l for l in (v.get('detail') or '').splitlines()[-8:]))
    return "\n".join([
        f"# Review packet — project {project_id}, task {task_id}: {title}",
        f"Base commit: {base}", f"Head commit (review exactly this): {head}",
        "", "## Task (what was asked)", spec.strip()[:12000],
        "", "## Files the task may change", "\n".join(f"- {s}" for s in scope) or "- (none)",
        "", "## Commits", "\n".join(f"- {c[0][:12]} {c[1]}" for c in commits) or "- (none)",
        "", "## Mechanical verification at the head commit (already PASSED; listed for context)",
        "\n".join(vlines) or "- (none)",
        *(["", "## Project evidence", extra.strip()[:12000]] if extra else []),
        "", "## Diff" + (" (truncated)" if diff_truncated else ""), "```diff", diff, "```",
        "", "## Question", question or "Does this commit complete the task correctly, within scope?",
    ])


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = [m.group(1)] if m else []
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        cands.append(text[i:j + 1])
    for c in cands:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


def _strs(v, n=40, ln=600) -> list[str]:
    if not isinstance(v, list):
        raise ReviewInvalid("expected a list of strings")
    return [str(x)[:ln] for x in v[:n]]


def validate(text: str, *, project_id: str, task_id: str, head_sha: str,
             scope: list[str]) -> dict:
    """The normalized review, or ReviewInvalid. Checks: JSON shape, verdict,
    the task id, and that it names the exact head commit (full sha, or a
    prefix of at least 12 characters)."""
    d = _extract_json(text)
    if d is None:
        raise ReviewInvalid("reviewer did not return a JSON object")
    missing = [k for k in SCHEMA["required"] if k not in d]
    if missing:
        raise ReviewInvalid(f"review is missing {', '.join(missing)}")
    if str(d["sprint_id"]).strip() != task_id:
        raise ReviewInvalid(f"review is for {d['sprint_id']!r}, not {task_id!r}")
    if d.get("project_id") and str(d["project_id"]).strip() != project_id:
        raise ReviewInvalid(f"review is for project {d['project_id']!r}, not {project_id!r}")
    sha = str(d["reviewed_commit_sha"]).strip().lower()
    if len(sha) < 12 or not head_sha.lower().startswith(sha):
        raise ReviewInvalid(f"review names commit {sha[:16]!r}, not the head {head_sha[:12]}")
    verdict = str(d["verdict"]).strip().upper()
    if verdict not in VERDICTS:
        raise ReviewInvalid(f"verdict {verdict!r} is not one of {VERDICTS}")
    if not isinstance(d["findings"], list):
        raise ReviewInvalid("findings must be a list")
    findings, notes = [], []
    for f in d["findings"][:60]:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "info").lower()
        sev = sev if sev in SEVERITIES else "info"
        path = str(f.get("file") or "").replace("\\", "/").strip()
        if path and (path.startswith("/") or ".." in path.split("/") or re.match(r"^[A-Za-z]:", path)):
            notes.append(f"finding path {path[:80]!r} is outside the repository; dropped the path")
            path = ""
        line = f.get("line")
        findings.append({"severity": sev, "file": path,
                         "line": line if isinstance(line, int) else None,
                         "message": str(f.get("message") or "")[:1500]})
    out = {"sprint_id": task_id, "project_id": project_id, "reviewed_commit_sha": head_sha,
           "verdict": verdict, "findings": findings,
           "evidence_inspected": _strs(d["evidence_inspected"]),
           "missing_evidence": _strs(d["missing_evidence"]),
           "required_repairs": _strs(d["required_repairs"]),
           "residual_risks": _strs(d["residual_risks"]), "validator_notes": notes}
    blocking = [f for f in findings if f["severity"] in ("blocker", "major")]
    if verdict == "APPROVED" and (blocking or out["missing_evidence"]):
        out["verdict"] = "CHANGES_REQUIRED"
        notes.append("APPROVED with blocking findings or missing evidence is not an approval")
    return out


def findings_for_repair(review: dict) -> list[str]:
    """Reviewer requests, quoted as advice for the repair session."""
    out = []
    for f in review.get("findings") or []:
        if f["severity"] in ("blocker", "major", "minor"):
            loc = f"{f['file']}:{f['line']}" if f.get("file") and f.get("line") else f.get("file", "")
            out.append(f"reviewer ({f['severity']}){' ' + loc if loc else ''}: {f['message']}")
    out += [f"reviewer repair: {r}" for r in review.get("required_repairs") or []]
    out += [f"reviewer: missing evidence: {m}" for m in review.get("missing_evidence") or []]
    return out[:30]

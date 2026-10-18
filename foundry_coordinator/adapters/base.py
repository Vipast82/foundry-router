"""Interfaces between the engine and a project / a coding client.

The engine only ever sees these normalized shapes:
  TaskSpec            what a task is allowed to change and how to describe it
  VerificationResult  one check, PASS / FAIL / UNKNOWN, bound to a revision,
                      with a pointer to its raw evidence
  SessionState        what a coding session is doing right now

A project adapter (GenericAdapter here, or a narrow project-specific subclass
such as projects/animaluprising) turns a project's own documents and tools
into these. A coding client (kilo.KiloClient, scripted.ScriptedClient) runs
sessions.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
REPORT_FENCE = "coordinator-report"


@dataclass
class TaskSpec:
    project_id: str
    task_id: str
    title: str
    spec: str                                   # the text the coding session is given
    scope: list[str]                            # files/globs this task may change
    tags: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)   # what the task source says it needs
    allow_no_change: bool = False
    data: dict = field(default_factory=dict)


@dataclass
class VerificationResult:
    check: str
    result: str                                 # PASS | FAIL | UNKNOWN
    required: bool = True
    evidence: str = ""                          # path (or pointer) to the raw output
    detail: str = ""
    revision: str = ""
    executed_ts: float = field(default_factory=time.time)
    duration_ms: int = 0


@dataclass
class SessionState:
    status: str                                 # busy | idle | attention | missing | error
    error: str = ""
    denial: Optional[dict] = None               # Foundry refusal {category, code, retry_after}
    question: Optional[dict] = None             # {request_id, kind, text, options}
    final_text: str = ""
    report: Optional[dict] = None               # the model's own report: advisory only
    tokens: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    unanswered: bool = False                    # idle with no reply to the prompt yet


class CodingClient:
    """A coding agent that runs sessions in a workspace (Kilo, or a scripted
    stand-in for tests)."""
    name = "base"

    def start_session(self, *, directory: str, title: str, persona: str, agent: str,
                      metadata: dict, permissions: list) -> str:
        raise NotImplementedError

    def prompt(self, session_id: str, text: str, *, directory: str, persona: str,
               agent: str) -> None:
        raise NotImplementedError

    def state(self, session_id: str, *, directory: str, since_ts: float) -> SessionState:
        raise NotImplementedError

    def abort(self, session_id: str, *, directory: str) -> None:
        pass

    def answer(self, session_id: str, request_id: str, answers: list, *, directory: str,
               kind: str = "question") -> None:
        raise NotImplementedError

    def transcript(self, session_id: str, *, directory: str) -> list:
        return []

    def children(self, session_id: str, *, directory: str) -> list[dict]:
        return []

    def preflight(self, directory: str) -> list[tuple[str, str, str]]:
        """[(PASS|FAIL|WARN, check, detail)] about the client's configuration."""
        return []


_REPORT_RE = re.compile(r"```" + REPORT_FENCE + r"\s*\n(.*?)```", re.S)


def parse_report(text: str) -> Optional[dict]:
    """The model's own end-of-session report (a fenced JSON block). Advisory:
    stored, shown, used to notice a declared blocker, never to advance."""
    m = None
    for m in _REPORT_RE.finditer(text or ""):
        pass
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {"status": "UNPARSEABLE", "raw": m.group(1)[:500]}
    return d if isinstance(d, dict) else None


def report_instructions() -> str:
    return (f"When you stop, end your final message with this block (it is recorded, but the "
            f"coordinator decides completion only from the verification checks):\n"
            f"```{REPORT_FENCE}\n"
            '{"status": "DONE | NOT DONE | BLOCKED | NEEDS_HUMAN", "summary": "<one line>", '
            '"blocker": "<what you need, if BLOCKED or NEEDS_HUMAN>"}\n```')


def safe_rel(path: str) -> Optional[str]:
    """A repo-relative path, or None if it leaves the workspace."""
    p = (path or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        return None
    return "/".join(parts)


def sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def evidence_dir(state_dir: str, project_id: str, task_id: str, revision: str) -> str:
    d = os.path.join(state_dir, "evidence", project_id, re.sub(r"[^\w.-]", "_", task_id),
                     (revision or "norev")[:12])
    os.makedirs(d, exist_ok=True)
    return d

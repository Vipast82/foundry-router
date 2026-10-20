"""Normalized record schema (versioned).

One `Event` per observed fact from one source. Observed values (what the
source said) are kept in `facts`; anything the pipeline inferred (a
correlation, a guessed project) lives in `inferred`, never mixed in. A missing
value is None or "UNKNOWN", never a default that looks real.

Identity is deterministic: event_id = hash(source_adapter, source_record_id,
part), so re-ingesting the same source record produces the same ids
(idempotent ingestion).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import SCHEMA_VERSION

EVENT_TYPES = (
    "session_start",        # a coding session (Kilo/Cline) and its metadata
    "user_message",         # text a person or the coordinator sent
    "assistant_message",    # model text shown to the client
    "reasoning",            # a reasoning channel / client status text (never a default training field)
    "tool_call",            # tool name, id, arguments
    "tool_result",          # output / error / truncated flag for a tool_call id
    "compaction",           # context was summarized; earlier turns no longer in the prompt
    "file_edit",            # a patch / edit operation the client recorded
    "file_ref",             # a file or image the conversation referenced
    "model_request",        # Foundry: request as received
    "model_call",           # Foundry: effective request + response for one backend attempt
    "child_session",        # a helper session started from a parent
    "task_transition",      # coordinator lifecycle step with its evidence
    "verification",         # one check: PASS / FAIL / UNKNOWN at a revision
    "review",               # frontier review bound to a commit
    "approval",             # human approval bound to a revision / artifacts
    "human_correction",     # human feedback (reject findings, answers)
    "commit",               # git commit with changed files
    "authorship",           # code attributed to a non-local author (helper prework patch,
                            # harvested pre-code model): those steps are context, never targets
    "label",                # an outcome label bound to a revision: studio_gate verdict JSON,
                            # vision verdict, operator / playtest critique
    "capture_gap",          # something the source could not provide
)
CAPTURE = ("complete", "partial", "missing", "truncated")
VERIFICATION = ("PASS", "FAIL", "UNKNOWN", "n/a")


def stable_id(*parts: Any) -> str:
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()
    return h[:32]


def content_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str,
                                     ensure_ascii=False).encode()).hexdigest()


@dataclass
class Event:
    source_adapter: str
    source_record_id: str
    event_type: str
    project_id: str
    timestamp: float
    part: str = "0"
    schema_version: int = SCHEMA_VERSION
    repository_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    request_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    seq: Optional[int] = None              # order within the session as the source gave it
    actor_role: Optional[str] = None       # user | assistant | tool | system | coordinator | human | reviewer
    actor_model: Optional[str] = None      # who actually wrote it (model id), when known
    requested_persona: Optional[str] = None
    actual_backend: Optional[str] = None
    actual_model: Optional[str] = None
    source_revision: Optional[str] = None
    artifact_references: list = field(default_factory=list)
    capture_status: str = "complete"
    redaction_status: str = "clean"
    verification_status: str = "n/a"
    facts: dict = field(default_factory=dict)
    inferred: dict = field(default_factory=dict)
    raw_ref: Optional[str] = None

    @property
    def event_id(self) -> str:
        return stable_id(self.source_adapter, self.source_record_id, self.part)

    def validate(self) -> list[str]:
        p = []
        if self.event_type not in EVENT_TYPES:
            p.append(f"unknown event_type {self.event_type!r}")
        if not self.project_id:
            p.append("project_id is required (records are never merged across projects)")
        if self.capture_status not in CAPTURE:
            p.append(f"capture_status {self.capture_status!r}")
        if self.verification_status not in VERIFICATION:
            p.append(f"verification_status {self.verification_status!r}")
        if self.event_type in ("tool_call", "tool_result") and not self.tool_call_id:
            p.append(f"{self.event_type} without tool_call_id")
        return p

    def row(self) -> dict:
        d = asdict(self)
        d["event_id"] = self.event_id
        for k in ("artifact_references", "facts", "inferred"):
            d[k] = json.dumps(d[k], default=str, ensure_ascii=False)
        return d

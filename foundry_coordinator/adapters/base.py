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

# -- plan checklist marks ----------------------------------------------------------------
# A plan checklist row is "- [x] ..." (complete), "- [~] ..." (in progress: partly
# done, usually with notes on what remains) or "- [ ] ..." (open). "[~]" is NOT
# completion: it is an open step that carries progress information.
CHECK_ROW = re.compile(r"^\s*-\s\[([ xX~])\]\s*(.*)$")
_MARK = {" ": "open", "~": "partial", "x": "done", "X": "done"}
_RANK = {"open": 0, "partial": 1, "done": 2}


def checklist_mark(line: str) -> Optional[str]:
    """'done' | 'partial' | 'open' for a checklist row, None for any other line."""
    m = CHECK_ROW.match(line or "")
    return _MARK[m.group(1)] if m else None


def checklist_transitions(before, after) -> list[str]:
    """Plan progress between two checklist states ([[mark, row text], ...], as
    an adapter's plan_progress() returns them): a step whose mark advanced
    (open -> partial -> done) or an in-progress step whose notes changed.
    Rows are matched by their leading step token when it is unique, else by
    position. A newly added row, a regression (done -> partial) or an edit of a
    complete row is not progress."""
    if not before or not after:
        return []

    def keyed(rows):
        toks = [((str(t).split() or [""])[0]) for _, t in rows]
        if all(toks) and len(set(toks)) == len(toks):
            return {k: (m, str(t)) for k, (m, t) in zip(toks, rows)}
        return {f"#{i + 1}": (m, str(t)) for i, (m, t) in enumerate(rows)}
    b, a = keyed(before), keyed(after)
    out = []
    for k, (mark, text) in a.items():
        if k not in b:
            continue
        pmark, ptext = b[k]
        if _RANK.get(mark, 0) > _RANK.get(pmark, 0):
            out.append(f"step {k}: {pmark} -> {mark}")
        elif mark == pmark == "partial" and text.strip() != ptext.strip():
            out.append(f"step {k}: in-progress notes changed")
    return out


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
    retry: bool = False                         # the provider call is being retried (Kilo retry)
    compaction: Optional[dict] = None           # turn ended on Kilo's compaction summary
    context: Optional[int] = None               # context tokens of the last turn (B12)


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
            '"blocker": "<what you need, if BLOCKED or NEEDS_HUMAN>", '
            '"options": ["<2-4 concrete ways to resolve the blocker>"], '
            '"recommended": "<the option you recommend>", '
            '"blocker_kind": "product | tooling"}\n```\n'
            "Nobody answers questions during a session (the question tool is off): decide "
            "within the plan; if a decision the plan does not settle truly blocks you, commit "
            "safe partial work and report BLOCKED with 2-4 options and the one you recommend. "
            "The coordinator decides (the recommended option applies after its timeout) and "
            "continues the sprint. "
            "Need a file outside the task's file list (e.g. a harness row)? Don't edit it: "
            "add a ```SCOPE_REQUEST``` block with {\"path\": \"<file>\", \"reason\": "
            "\"<one line>\"} and stop; the coordinator grants allow-listed harness lines "
            "itself and continues this session.")


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


REVIEW_PHASES = ("build", "full")
EVIDENCE_STATUSES = ("ok", "missing", "stale", "unverified")
EVIDENCE_ITEM_CHARS = 12000

_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff",
               ".pdf", ".zip", ".gz", ".7z", ".tar", ".exe", ".dll", ".so", ".dylib", ".bin",
               ".ttf", ".otf", ".woff", ".woff2", ".mp3", ".ogg", ".wav", ".mp4", ".mov",
               ".rbxl", ".rbxm", ".psd", ".blend", ".fbx", ".pyc")
_CONFIG_EXT = (".toml", ".json", ".jsonc", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".xml")
_DOC_EXT = (".md", ".rst", ".txt", ".adoc")
_LOCK_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
               "cargo.lock", "gemfile.lock", "composer.lock")


def is_binary_path(path: str) -> bool:
    return (path or "").lower().endswith(_BINARY_EXT)


def generic_diff_priority(path: str) -> int:
    """0 = code a reviewer must see .. 9 = binary. A generic guess from the path;
    project adapters refine it (diff_priority)."""
    p = (path or "").replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if is_binary_path(p):
        return 9
    if p.endswith(".log") or "/evidence/" in "/" + p or name in _LOCK_NAMES:
        return 8
    if (p.startswith(("tests/", "test/")) or "/tests/" in p or "/test/" in p
            or name.startswith("test_") or "_test." in name or ".spec." in name
            or ".test." in name):
        return 1
    if p.startswith(("src/", "lib/", "app/", "pkg/")):
        return 0                      # a source tree is code whatever the extension
    if p.startswith(("docs/", "doc/")) or name.endswith(_DOC_EXT):
        return 5
    if name.endswith(_CONFIG_EXT) or p.startswith(("tools/", "scripts/", ".github/")) \
            or name in ("makefile", "dockerfile"):
        return 2
    return 0


def bound_text(text: str, max_chars: int = EVIDENCE_ITEM_CHARS, keep: str = "head"
               ) -> tuple[str, bool, int]:
    """Cut `text` to at most max_chars on whole lines. keep="head" keeps the
    start, "tail" the end. Returns (text, truncated, lines_omitted)."""
    text = text or ""
    if len(text) <= max_chars:
        return text, False, 0
    lines = text.splitlines()
    kept, size = [], 0
    seq = lines if keep == "head" else list(reversed(lines))
    for line in seq:
        if size + len(line) + 1 > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    if not kept and seq:                       # one enormous line: cut inside it
        kept = [seq[0][:max_chars] if keep == "head" else seq[0][-max_chars:]]
    omitted = len(lines) - len(kept)
    if keep != "head":
        kept.reverse()
    return "\n".join(kept), True, omitted


def evidence_item(title: str, *, path: str = "", revision: str = "", blob: str = "",
                  lines: str = "", text: str = "", status: str = "ok", truncated: bool = False,
                  note: str = "", priority: int = 5) -> dict:
    """One review-evidence item (see ReviewContractDefaults.review_evidence)."""
    if status not in EVIDENCE_STATUSES:
        raise ValueError(f"evidence status {status!r}")
    return {"title": title, "path": path, "revision": revision, "blob": blob or "",
            "lines": lines, "text": text, "status": status, "truncated": bool(truncated),
            "note": note, "priority": max(0, min(9, int(priority)))}


def duty_bullets(steps_text: str) -> list[str]:
    """A project's free-text closeout steps as one duty per bullet / numbered
    line (continuation lines folded in); unstructured text is one duty."""
    duties: list[str] = []
    for raw in (steps_text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        m = re.match(r"^(?:[-*+•]|\d+[.)]|\(\w\))\s+(.*)$", s)
        if m:
            duties.append(m.group(1).strip())
        elif duties:
            duties[-1] += " " + s
        else:
            duties.append(s)
    return [d for d in duties if d]


class ReviewContractDefaults:
    """The review contract the engine/review packet asks a project adapter for
    (each method has a safe default; callers use getattr with fallbacks).

    review_contract(spec)            what this review covers and what a later
                                     phase does: {"phase": "build"|"full",
                                     "reviewed": [..], "deferred": [..]}
    describe_at(task_id, rev, ws)    the TaskSpec as of a commit
    review_evidence(spec, rev, ws)   [evidence_item(..)] bound to that commit
    diff_priority(path, spec)        0 essential code .. 9 binary
    """

    def closeout_mode(self) -> str:                      # overridden by GenericAdapter
        return "none"

    def closeout_duties(self, spec: TaskSpec) -> list[str]:
        return []

    def review_contract(self, spec: TaskSpec) -> dict:
        if self.closeout_mode() == "session":
            duties = self.closeout_duties(spec) or [
                "The project's closeout steps (a separate closeout session after approval)"]
            return {"phase": "build",
                    "reviewed": ["The implementation (base..reviewed commit) against the task spec",
                                 "Tests and checks as run at the reviewed commit",
                                 "Scope: only the task's declared files changed"],
                    "deferred": duties}
        return {"phase": "full",
                "reviewed": ["The whole task: implementation, tests/checks at the reviewed "
                             "commit, scope and any task deliverables"],
                "deferred": []}

    def describe_at(self, task_id: str, revision: str, ws) -> TaskSpec:
        spec = self.describe(task_id)                    # type: ignore[attr-defined]
        spec.data["spec_source"] = "working tree"
        return spec

    def review_evidence(self, spec: TaskSpec, revision: str, ws) -> list[dict]:
        extra = self.review_extra(spec, revision) if hasattr(self, "review_extra") else ""
        if not (extra or "").strip():
            return []
        text, cut, omitted = bound_text(extra)
        return [evidence_item("Project review context", revision=revision, text=text,
                              status="unverified", truncated=cut, priority=3,
                              note="read from the working tree by review_extra, not from the "
                                   "reviewed commit" + (f"; {omitted} lines cut" if cut else ""))]

    def diff_priority(self, path: str, spec: TaskSpec) -> int:
        return generic_diff_priority(path)

    # -- evidence / lifecycle contract (v0.122). Safe "nothing known" defaults: a
    # project without these hooks credits nothing from transcripts, has no
    # dependency or runtime knowledge (freshness 'unknown' / 'runtime unverified'),
    # and gets the callers' own obligation fallback (obligations() -> None).

    def classify_tool_result(self, view: dict, ctx: Optional[dict] = None) -> list[dict]:
        """[{check_id, chain_id, seq, prereqs, result, counts, exclusion,
        runtime_hint, artifact_path}] parsed from one tool call; [] = nothing."""
        return []

    def required_checks(self, spec: TaskSpec, phase: str = "BUILD") -> list[dict]:
        """[{check_id, chain_id, seq, prereqs, runtime, min_tier}] for a phase."""
        return []

    def check_dependencies(self, spec: TaskSpec, check_id: str) -> Optional[list[str]]:
        """Path globs a check depends on; None = unknown."""
        return None

    def runtime_identity(self, view_or_text) -> Optional[dict]:
        """What code a runtime reported it loaded; None = not reported."""
        return None

    def runtime_expected(self, spec: TaskSpec, revision: str) -> Optional[str]:
        """The loaded fingerprint `revision` should produce; None = not derivable."""
        return None

    def void_runtime(self, view: dict) -> bool:
        return False

    def obligations(self, spec: TaskSpec) -> Optional[list[dict]]:
        """None = no adapter obligation model (callers fall back to required
        checks + review_contract()['deferred'])."""
        return None

    def readiness(self, spec: TaskSpec, phase: str = "implement",
                  ctx: Optional[dict] = None) -> list[dict]:
        """[{name, ok, cls, detail, blocking}] before a session; [] = none."""
        return []

    # -- committed BUILD progress (v0.122.5) --------------------------------------------

    def intermediate_outputs(self, spec: TaskSpec) -> list[str]:
        """Exact workspace paths the project's own tools regenerate for THIS task
        while BUILD is in progress (e.g. a gate's expectation/verdict files).
        Untracked files at exactly these paths never block a committed
        partial-BUILD continuation; final verification still requires them
        committed (or absent). [] = none known."""
        return []

    def plan_progress(self, spec: TaskSpec, revision: str, ws) -> Optional[list]:
        """The committed plan's checklist at `revision` as [[mark, row text]]
        (mark: done | partial | open, see checklist_mark); None = no structured
        checklist. Used only to recognize progress, never to pass a check."""
        return None


def evidence_dir(state_dir: str, project_id: str, task_id: str, revision: str) -> str:
    d = os.path.join(state_dir, "evidence", project_id, re.sub(r"[^\w.-]", "_", task_id),
                     (revision or "norev")[:12])
    os.makedirs(d, exist_ok=True)
    return d

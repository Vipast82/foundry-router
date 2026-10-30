"""Failure classification, repeat detection and bounded repair assignments.

- `classify` puts every failure into one of ten classes (first match wins), so
  an environment scratch file, a stale capture or a 429 is not treated as a
  product defect that burns an implementation attempt.
- `fingerprint` / `match` recognise the same finding across reworded attempts
  (no embeddings: normalized tokens, Jaccard >= 0.6).
- `Recovery.assignment` turns the open obligations, repeats and evidence into
  one bounded repair assignment. A repeat with an unchanged strategy goes to
  the adviser once, else to an operator decision; never a third blind repair.
  Only an OPEN escalation blocks. An answered one is acted on: a strategy
  proceeds with it, "authorize more budget" proceeds once, "stop" returns
  {'stop': True, 'reason', 'decision_id'} (the caller keeps the task
  blocked); anything else, or a repeat after the answer was used, opens a new
  decision superseding it with the attempt in the question.
- `gate_submission` refuses a review while BUILD evidence is missing.
- `progress` detects sessions that change nothing.

Guidance and assignments can never waive a check, move a BUILD obligation to
CLOSE, or offer CLOSE paperwork in place of BUILD validation: such lines are
refused (`obligations.directive_violations`) and BUILD checks always stay in
`affected_checks_to_rerun`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Callable, Optional

from .evidence import glob_match
from .obligations import (Obligations, _j, _sha, _tid, _uj, directive_violations,
                          migrate as _migrate_obligations, migrate_schema)

CLASSES = ("product_defect", "test_harness", "env_sync", "transport", "quota",
           "missing_evidence", "packet_omission", "docs_inconsistency", "review_disagreement",
           "scope_decision")

# Per class: (action, counts an implementation attempt)
RECOVERY = {
    "quota": ("pause", False),
    "transport": ("free_retry_then_decision", False),
    "env_sync": ("quarantine_owned_scratch_or_decision", False),
    "missing_evidence": ("same_session_continuation_then_repair", True),
    "packet_omission": ("rebuild_packet_or_block", False),
    "docs_inconsistency": ("closeout_retry", False),
    "test_harness": ("rerun_then_decision", False),
    "product_defect": ("repair", True),
    "review_disagreement": ("second_reviewer_then_decision", False),
    "scope_decision": ("decision", False),
}

JACCARD = 0.6
PROMPT_CAP = 6000
DOCS_ROWS = ("PLAN", "SUMMARY", "PATHS", "API", "STATE")
DEFAULT_SCRATCH = (".kilo/*", ".kilo/**")

SCHEMA_NAME = "schema:recovery"
_V1 = """
CREATE TABLE IF NOT EXISTS progress_marks (
  id INTEGER PRIMARY KEY, project_id TEXT, task_id TEXT, session_id TEXT, attempt INT,
  prev_head TEXT, head TEXT, verified TEXT, findings_fp TEXT, action_fp TEXT,
  reported INT, ts REAL, UNIQUE (project_id, task_id, session_id));
"""
# v2: committed BUILD progress per session -- work_files (in-scope, non-bookkeeping
# files changed prev_head..head; NULL = unknown, an older row) and checklist
# ({"before": rows at prev_head, "after": rows at head}, the adapter's plan_progress)
_STEPS: list = [(2, [("progress_marks", "work_files", "INT"),
                     ("progress_marks", "checklist", "TEXT")])]

STOP = frozenset("""
the and for with from since this that these those was were are is not has have had been
still its into onto than then there their they them which while when where what will would
should could must also only just but any all each per via over under after before again
same such very more most less least other some our out off yet may might can cannot did does
doing done being because about above below between both few own too nor now here how why
who whom whose your you his her him she task attempt attempts run runs session sessions
revision rev head commit commits line lines file files found shows show see seen got get
""".split())

_RE_QUOTED_PATH = re.compile(r"[`'\"]([\w.\-]+(?:[/\\][\w.\-]+)+)[`'\"]")
_RE_PATH = re.compile(r"(?<![\w/])((?:[\w.\-]+[/\\])+[\w.\-]+\.\w{1,6})\b")
_RE_SHA = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
_RE_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?z?\b")
_RE_IDS = re.compile(r"\b(?:attempt|run|session|ses|sid|try|pass)[\s#:_\-]*[\w\-]*\d[\w\-]*\b")
_RE_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_RE_LINENO = re.compile(r"\b(?:line|ln|l)\s*\d+\b|:\d+(?::\d+)?\b")
_RE_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")

_EVIDENCE_NOUN = r"(?:evidence|captures?|screenshots?|attachments?|logs?|frames?|artifacts?|" \
                 r"images?|recordings?|gate\s+output|tally|tallies)"
_RE_MISSING_EVIDENCE = re.compile(
    rf"\b(?:missing|absent|stale|not\s+(?:attached|included|provided|present|shown|in\s+the\s+"
    rf"packet)|no|without|lacks?|cannot\s+see|could\s+not\s+(?:find|see))\b[^.;\n]{{0,40}}?"
    rf"\b{_EVIDENCE_NOUN}\b|\b{_EVIDENCE_NOUN}\b[^.;\n]{{0,40}}?\b(?:is|are|was|were)\s+"
    rf"(?:missing|absent|stale|not\s+(?:attached|included|provided|present))\b", re.I)
_RE_QUOTA = re.compile(r"\b429\b|rate[\s_-]?limit|usage[\s_-]?quota|quota[\s_-]?(?:exceeded|"
                       r"exhausted)|insufficient_quota|too\s+many\s+requests", re.I)
_RE_TRANSPORT = re.compile(r"foundryerror|review_call_failed|connection\s+(?:reset|refused|"
                           r"aborted)|remote\s+disconnected|\b50[234]\b|bad\s+gateway|"
                           r"service\s+unavailable|read\s+timed?\s*out", re.I)
_RE_ENV_UNKNOWN = re.compile(r"not\s+on\s+path|\brojo\b|studio[^.\n]{0,30}not\s+reachable|"
                             r"not\s+reachable[^.\n]{0,30}studio", re.I)
_RE_CRASH = re.compile(r"printed\s+no\s+rows|traceback\s+\(most\s+recent|crashed|"
                       r"segmentation\s+fault|no\s+verdict\s+rows|exit\s+code\s+\d+[^.\n]{0,40}"
                       r"no\s+(?:verdict|rows|output)", re.I)
_RE_OUT_OF_SCOPE = re.compile(r"outside\s+(?:the\s+)?(?:task'?s?\s+)?scope|not\s+in\s+scope|"
                              r"scope\s+(?:change|expansion)|needs?\s+to\s+(?:change|edit)\s+"
                              r"[^.\n]{0,40}outside", re.I)
_RE_ACCESS = re.compile(r"access\s+denied|permission\s+denied|forbidden|\b40[13]\b|"
                        r"unauthori[sz]ed|could\s+not\s+(?:open|access|download)|"
                        r"cannot\s+(?:open|access)|not\s+accessible", re.I)


class AssignmentInvariant(RuntimeError):
    """An assignment would waive a check, drop BUILD work or move it to CLOSE."""


def _text(item: dict) -> str:
    return " ".join(str(item.get(k) or "") for k in ("detail", "text", "title", "issue",
                                                     "message", "error", "reason", "summary"))


def _check_name(item: dict) -> str:
    return str(item.get("check") or item.get("check_name") or item.get("check_id") or "")


def _paths_from_detail(detail: str) -> list[str]:
    out = []
    for seg in re.findall(r":\s*([^;:]+)", detail or ""):
        for p in seg.split(","):
            p = p.strip()
            if p and " " not in p and ("/" in p or "." in p):
                out.append(p)
    return out


def _glob_any(path: str, globs) -> bool:
    """The ledger's path-aware glob (`*` never crosses `/`), or a directory prefix."""
    return any(glob_match(path, g) or path.startswith(g.rstrip("*/") + "/") for g in globs)


# Words that place a "missing ..." review finding in the packet/evidence context
# (as opposed to product prose such as "renders without image caching").
_RE_PACKET_CTX = re.compile(r"\b(?:packet|attach\w*|evidence|submitted|provided|included|"
                            r"bundle|review\s+(?:input|material))\b", re.I)
_RE_EVIDENCE_PATH = re.compile(r"(?:^|/)docs/evidence/|\.(?:png|jpe?g|gif|webp|mp4|log)$", re.I)


def _error_text(item: dict) -> str:
    """Only the error/transport fields of an item (never finding prose or check detail)."""
    return " ".join(str(item.get(k) or "") for k in ("error", "raw_error", "transport_error",
                                                    "http_error", "status_text"))


def classify(source: str, item: dict, ctx: Optional[dict] = None) -> tuple[str, str]:
    """(class, rationale). `source` is check|review|session|denial|closeout|
    evidence|obligation|guidance. First match wins (GAP_B rules).

    Quota and transport are matched only on denials, sessions and a review's
    error fields; a check's detail or a review finding's prose never is ("Ran
    429 cases", "maps a 503 to success" are product facts)."""
    ctx = ctx or {}
    item = item or {}
    text = _text(item)
    clean = _RE_PATH.sub(" path ", _RE_QUOTED_PATH.sub(" path ", text))
    name = _check_name(item)
    short = name.rsplit(".", 1)[-1]
    low = short.lower()
    result = str(item.get("result") or "").upper()
    status = str(item.get("status") or "").lower()
    # the text a quota/transport rule may read
    if source in ("denial", "session"):
        err = text + " " + _error_text(item)
    elif source == "review":
        err = _error_text(item)
    else:
        err = ""

    # quota
    if (source == "denial" and item.get("category") == "budget") or \
            (err.strip() and _RE_QUOTA.search(err)):
        return "quota", "provider budget/429/usage quota"
    # transport
    if source == "denial" and item.get("category") == "route":
        return "transport", "route denial"
    if source == "session" and status in ("missing", "error", "timeout"):
        return "transport", f"session {status}"
    if source in ("review", "session", "denial") and (
            item.get("kind") == "review_call_failed" or
            (err.strip() and _RE_TRANSPORT.search(err))):
        return "transport", "provider/transport failure"
    # env_sync
    if source in ("check", "closeout") and result in ("FAIL", "UNKNOWN"):
        if low in ("unrelated_preserved", "clean") and "pre-existing" not in text:
            paths = list(item.get("paths") or _paths_from_detail(item.get("detail") or ""))
            new = ctx.get("untracked_new")
            scratch = ctx.get("scratch_globs") or DEFAULT_SCRATCH
            if paths and all((p in new) if new is not None else _glob_any(p, scratch)
                             for p in paths):
                return "env_sync", "only untracked files the session created: " + \
                    ", ".join(paths[:6])
        if low == "history" and result == "FAIL":
            return "env_sync", "history rewritten under the task"
        if result == "UNKNOWN" and _RE_ENV_UNKNOWN.search(text):
            return "env_sync", "tool or runtime not reachable"
    # missing_evidence
    if source == "evidence" and status in ("missing", "stale"):
        return "missing_evidence", f"evidence {status}"
    if source in ("check", "closeout") and result in ("FAIL", "UNKNOWN") and (
            low == "gate_bound_to_head" or "freshness" in name.lower()):
        return "missing_evidence", f"{name}: evidence not bound/fresh at head"
    if source == "obligation" and item.get("kind") in ("path", "blob_changed"):
        return "missing_evidence", f"required {item.get('kind')} evidence unmet"
    if source == "review":
        f = str(item.get("file") or item.get("path") or "").replace("\\", "/")
        evidence_paths = set(ctx.get("evidence_paths") or [])
        in_context = bool(_RE_PACKET_CTX.search(clean)) or (
            f and (f in evidence_paths or bool(_RE_EVIDENCE_PATH.search(f))))
        if item.get("kind") == "missing_evidence" or item.get("missing_evidence") or (
                in_context and _RE_MISSING_EVIDENCE.search(clean)):
            return "missing_evidence", "reviewer reports missing evidence"
    # packet_omission
    if source == "review":
        omitted = set(ctx.get("coverage_omitted") or [])
        if ctx.get("insufficient") or (item.get("file") and item.get("file") in omitted):
            return "packet_omission", "the review packet omitted what the finding cites"
    # docs_inconsistency
    if source in ("check", "closeout") and (short in DOCS_ROWS or
                                           low == "closeout_consistency"):
        return "docs_inconsistency", f"closeout document row {short}"
    # test_harness
    if item.get("flaky") or ctx.get("flaky"):
        return "test_harness", "mixed results at one revision"
    if source in ("check", "closeout") and (_RE_CRASH.search(text) or (
            item.get("exit_code") not in (None, 0) and item.get("rows") == 0)):
        return "test_harness", "the check tool crashed or printed no verdict"
    # scope_decision
    if source in ("check", "closeout") and low == "scope" and result == "FAIL":
        return "scope_decision", "changes outside the declared scope"
    if source == "guidance" and item.get("action") == "needs_human":
        return "scope_decision", "adviser says a human is needed"
    if source == "review":
        scope = ctx.get("scope")
        f = item.get("file") or ""
        if _RE_OUT_OF_SCOPE.search(clean) or (scope and f and not _glob_any(f, scope)):
            return "scope_decision", "the finding needs a file outside scope"
    # review_disagreement
    if source == "review" and ctx.get("checks_green") and item.get("file") and \
            item.get("file") in set(ctx.get("repair_touched") or []):
        fp = fingerprint("product_defect", "review:" + str(item.get("severity") or ""),
                         item.get("file"), text)
        if fp in set(ctx.get("prev_fingerprints") or []):
            return "review_disagreement", "same reviewer finding after a repair of that file"
    return "product_defect", "default"


# -- fingerprints ---------------------------------------------------------------------

def _stem(w: str) -> str:
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxh":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2] if w[-3] != "e" else w[:-1]
    return w


def normalize(text: str) -> tuple[str, str]:
    """(normalized text, first path). Strips shas, uuids, timestamps, line
    numbers, numbers, attempt/run/session ids and paths (the path is kept
    apart: it is part of the fingerprint key, not of the wording)."""
    t = (text or "").lower().replace("\\", "/")
    path = ""
    m = _RE_QUOTED_PATH.search(t) or _RE_PATH.search(t)
    if m:
        path = m.group(1)
    t = _RE_QUOTED_PATH.sub(" ", t)
    t = _RE_PATH.sub(" ", t)
    for rx in (_RE_UUID, _RE_TS, _RE_SHA, _RE_IDS, _RE_LINENO, _RE_NUM):
        t = rx.sub(" ", t)
    return t, path


def tokens(text: str) -> list[str]:
    """The FULL normalized token set (sorted for a stable hash). No cap: an
    alphabetical cut would make long findings that differ only after the
    24th token collide, and Jaccard must see every token."""
    t, _ = normalize(text)
    return sorted({_stem(w) for w in re.findall(r"[a-z][a-z0-9_]{2,}", t) if w not in STOP}
                  - STOP)


def fingerprint(cls: str, check_id: Optional[str], path: Optional[str], text: str) -> str:
    _, p = normalize(text)
    path = (path or p or "").replace("\\", "/").lower()
    return hashlib.sha1(f"{cls}|{check_id or ''}|{path}|{' '.join(tokens(text))}"
                        .encode()).hexdigest()[:16]


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else 1.0


def match(cls: str, check_id: Optional[str], path: Optional[str], text: str,
          candidates: list[dict]) -> Optional[str]:
    """The fingerprint of a stored finding that this one repeats: the exact
    fingerprint, or the same (class, check_id, path) with token Jaccard >= 0.6."""
    fp = fingerprint(cls, check_id, path, text)
    _, p = normalize(text)
    path = (path or p or "").replace("\\", "/").lower()
    toks = tokens(text)
    for c in candidates:
        if c["fingerprint"] == fp:
            return fp
    best, score = None, 0.0
    for c in candidates:
        if (c.get("class"), c.get("check_id") or "", c.get("path") or "") != \
                (cls, check_id or "", path):
            continue
        s = jaccard(toks, _uj(c.get("tokens"), []) if isinstance(c.get("tokens"), str)
                    else c.get("tokens") or [])
        if s >= JACCARD and s > score:
            best, score = c["fingerprint"], s
    return best


def strategy_fp(text: str) -> str:
    return _sha(" ".join(tokens(text)) or (text or "").strip().lower(), 16)


# -- review routing ---------------------------------------------------------------------

def review_route(review: dict, packet_info: Optional[dict] = None,
                 ledger_state: Optional[dict] = None) -> str:
    """product_changes | evidence_incomplete | access_blocked.

    access_blocked: the reviewer could not get at the packet (quota, transport,
    permissions). evidence_incomplete: everything the review asks for is
    evidence (missing/stale/omitted), so the fix is evidence, not product code.
    Otherwise product_changes."""
    review = review or {}
    packet_info = packet_info or {}
    ledger_state = ledger_state or {}
    err = " ".join(str(review.get(k) or "") for k in ("error", "raw_error"))
    if ledger_state.get("access_blocked") or review.get("access_blocked"):
        return "access_blocked"
    if err.strip():
        cls, _ = classify("review", {"error": err}, {})
        if cls in ("quota", "transport") or _RE_ACCESS.search(err):
            return "access_blocked"
    findings = list(review.get("findings") or [])
    ctx = {"coverage_omitted": packet_info.get("omitted") or [],
           "insufficient": packet_info.get("insufficient")}
    classes = [classify("review", f if isinstance(f, dict) else {"text": str(f)}, ctx)[0]
               for f in findings]
    if findings and all(_RE_ACCESS.search(_text(f if isinstance(f, dict) else {"text": str(f)}))
                        for f in findings):
        return "access_blocked"
    evidence_only = all(c in ("missing_evidence", "packet_omission") for c in classes)
    has_evidence_gap = bool(review.get("missing_evidence") or packet_info.get("insufficient")
                            or ledger_state.get("missing") or
                            any(c in ("missing_evidence", "packet_omission") for c in classes))
    if evidence_only and has_evidence_gap:
        return "evidence_incomplete"
    return "product_changes"


# -- stateful part ------------------------------------------------------------------------

def migrate(store) -> int:
    _migrate_obligations(store)
    return migrate_schema(store, SCHEMA_NAME, _V1, _STEPS)


class Recovery:
    def __init__(self, store, clock=time.time, **obligation_kw):
        self.store, self.clock = store, clock
        migrate(store)
        self._obligations = Obligations(store, clock, **obligation_kw)

    # -- findings ------------------------------------------------------------------

    def findings(self, project, task) -> list[dict]:
        rows = self.store.q("SELECT * FROM findings WHERE project_id=? AND task_id=? ORDER BY "
                            "first_seen_attempt, fingerprint", (project, task))
        for r in rows:
            r["tokens"] = _uj(r["tokens"], [])
            r["samples"] = _uj(r["samples"], [])
            r["attempts"] = _uj(r.get("attempts"), [])
        return rows

    def record_findings(self, P, t, attempt: int, items: list[dict]) -> list[dict]:
        """Record this attempt's findings. Each item: {source, item, ctx} or
        {class, check_id, path, text, obligation_id}. A finding seen in two or
        more distinct attempts is a repeat; returns every repeat touched now."""
        task = _tid(t)
        attempt = int(attempt)
        touched = []
        with self.store.tx():
            stored = self.findings(P.id, task)
            for it in items or []:
                raw = it.get("item") or {}
                cls = it.get("class")
                rationale = ""
                if cls not in CLASSES:
                    cls, rationale = classify(it.get("source") or "check", raw or it,
                                              it.get("ctx") or {})
                text = it.get("text") or _text(raw)
                check_id = it.get("check_id") or _check_name(raw) or (
                    "review:" + str(raw.get("severity") or "") if it.get("source") == "review"
                    else "")
                _, p = normalize(text)
                path = (it.get("path") or raw.get("file") or raw.get("path") or p or ""
                        ).replace("\\", "/").lower()
                hit = match(cls, check_id, path, text, stored)
                sample = {"attempt": attempt, "text": text[:400], "rationale": rationale}
                if hit:
                    row = next(r for r in stored if r["fingerprint"] == hit)
                    attempts = sorted(set(row["attempts"] or [row["first_seen_attempt"]]) |
                                      {attempt})
                    samples = (row["samples"] + [sample])[-3:] \
                        if attempt not in (row["attempts"] or []) else row["samples"]
                    self.store.x("UPDATE findings SET last_seen_attempt=?, count=?, attempts=?, "
                                 "samples=? WHERE project_id=? AND task_id=? AND fingerprint=?",
                                 (max(attempts), len(attempts), _j(attempts), _j(samples),
                                  P.id, task, hit))
                    row.update(attempts=attempts, count=len(attempts),
                               last_seen_attempt=max(attempts), samples=samples)
                    fp = hit
                else:
                    fp = fingerprint(cls, check_id, path, text)
                    row = {"fingerprint": fp, "project_id": P.id, "task_id": task,
                           "class": cls, "check_id": check_id, "path": path,
                           "tokens": tokens(text), "first_seen_attempt": attempt,
                           "last_seen_attempt": attempt, "count": 1,
                           "obligation_id": it.get("obligation_id") or "",
                           "samples": [sample], "attempts": [attempt]}
                    self.store.x("INSERT OR IGNORE INTO findings (fingerprint, project_id, "
                                 "task_id, class, check_id, path, tokens, first_seen_attempt, "
                                 "last_seen_attempt, count, obligation_id, samples, attempts) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (fp, P.id, task, cls, check_id, path, _j(row["tokens"]),
                                  attempt, attempt, 1, row["obligation_id"], _j(row["samples"]),
                                  _j([attempt])))
                    stored.append(row)
                oid = it.get("obligation_id") or self._obligation_for(P.id, task, check_id)
                if oid:
                    self._obs().link_finding(P.id, task, oid, fp)
                if fp not in [r["fingerprint"] for r in touched]:
                    touched.append(row)
        return [{"fingerprint": r["fingerprint"], "class": r["class"],
                 "check_id": r["check_id"], "path": r["path"], "count": r["count"],
                 "attempts": r["attempts"], "first_seen_attempt": r["first_seen_attempt"],
                 "last_seen_attempt": r["last_seen_attempt"],
                 "text": (r["samples"][-1]["text"] if r["samples"] else "")}
                for r in touched if r["count"] >= 2]

    def _obs(self) -> Obligations:
        return self._obligations

    def _obligation_for(self, project, task, check_id) -> str:
        if not check_id:
            return ""
        for ob in self._obs().all(project, task):
            if any(e.get("kind") == "check" and e.get("ref") == check_id
                   for e in ob["required_evidence"]):
                return ob["obligation_id"]
        return ""

    def repeats(self, project, task, min_count: int = 2) -> list[dict]:
        return [r for r in self.findings(project, task) if r["count"] >= min_count]

    def live_repeats(self, project, task, *, passing: frozenset = frozenset(),
                     min_count: int = 2) -> list[dict]:
        """Repeats that are still happening: seen again in the latest round of findings
        and not on a check that passes now. 2026-10-28 GF-GATE1 15:11 / 15:36: scope and
        gate-binding findings fixed attempts ago (both PASS at the head) kept counting as
        repeats, so a review with two new doc points stopped the sprint as "findings repeat
        with no materially different strategy"."""
        rows = self.findings(project, task)
        if not rows:
            return []
        latest = max(int(r.get("last_seen_attempt") or 0) for r in rows)
        return [r for r in rows if r["count"] >= min_count
                and int(r.get("last_seen_attempt") or 0) >= latest
                and not (r.get("check_id") and r["check_id"] in passing)]

    # -- repairs / assignment ---------------------------------------------------------------

    def repairs(self, project, task) -> list[dict]:
        rows = self.store.q("SELECT * FROM repairs WHERE project_id=? AND task_id=? ORDER BY "
                            "attempt, ts", (project, task))
        for r in rows:
            for k in ("obligations", "validation_plan", "diff_files", "intended_files",
                      "assignment"):
                r[k] = _uj(r.get(k), None)
        return rows

    def assignment(self, P, t, *, ledger=None, obligations: Optional[Obligations] = None,
                   ws=None, strategy: str = "", trusted_strategy: bool = False,
                   adviser: Optional[Callable] = None,
                   evidence_items: Optional[list] = None, last_report: str = "",
                   spec=None, max_items: int = 3, cap: int = PROMPT_CAP,
                   free: bool = False) -> dict:
        """One bounded repair assignment for attempt t['attempt'], or
        {'decision': id} when findings repeat with an unchanged strategy and no
        adviser offers a different one. Idempotent per attempt (a restart
        returns the stored assignment; the adviser is never asked twice)."""
        obs = obligations or self._obs()
        task = _tid(t)
        trow = t if isinstance(t, dict) else (self.store.task(P.id, task) or {"task_id": task})
        attempt = int(trow.get("attempt") or 0)
        # idempotent per run (a restart returns the same assignment); a free
        # evidence-only retry keeps the attempt number but gets a new run id,
        # so it never reuses the previous attempt's stale assignment
        run = str(trow.get("run_id") or "")
        rid = "rep-" + _sha(f"{P.id}|{task}|{attempt}|{run}", 16)
        rows = self.repairs(P.id, task)
        prior = [r for r in rows if r["repair_id"] != rid]
        mine = next((r for r in rows if r["repair_id"] == rid), None)
        if mine and mine.get("assignment"):
            return mine["assignment"]
        ws = ws or getattr(P, "ws", None)
        head = ws.head() if ws is not None and callable(getattr(ws, "head", None)) else ""
        real = [r for r in prior if r.get("outcome") != "escalated"]
        prev = real[-1] if real else None

        # what changed since the previous repair began
        changed = []
        if prev and prev.get("start_rev") and head and callable(getattr(ws, "changed_files", None)):
            try:
                changed = list(ws.changed_files(prev["start_rev"], head))
            except Exception:                                  # noqa: BLE001 - advisory only
                changed = []
        open_now = [o["obligation_id"] for o in obs.open_items(P, task)]
        prev_open = set((prev or {}).get("obligations") or [])
        what_changed = {"files": changed[:60],
                        "closed": sorted(prev_open - set(open_now)),
                        "reopened": sorted(set(open_now) - prev_open) if prev else []}

        # unresolved BUILD work only (phase read from the DB, never from text)
        package = obs.next_package(P, task, "BUILD", max_items)
        unresolved = [{"obligation_id": o["obligation_id"], "title": o["title"],
                       "acceptance": o["acceptance"], "required_evidence": o["required_evidence"],
                       "blocking_reason": o.get("blocking_reason") or "", "phase": "BUILD"}
                      for o in package if o["phase"] == "BUILD"]
        build_open = obs.open_items(P, task, "BUILD")

        # overlooked evidence related to open obligations and not cited
        cited = (last_report or "") + " " + " ".join(changed)
        refs = [str(e["ref"]) for o in build_open for e in o["required_evidence"]]
        overlooked = []
        for it in evidence_items or []:
            if not isinstance(it, dict):
                continue
            path = str(it.get("path") or "")
            title = str(it.get("title") or "")
            rel = any((path and (path == r or _glob_match(path, r))) or (r and r in title)
                      for r in refs)
            if rel and not (path and path in cited):
                overlooked.append({"title": title[:200], "path": path,
                                   "status": it.get("status") or "", "note":
                                   str(it.get("note") or "")[:300]})
        overlooked.sort(key=lambda x: (x["status"] == "ok", x["path"]))

        passing: set = set()
        if ledger is not None and spec is not None and callable(getattr(ledger, "current", None)):
            try:
                cur = ledger.current(P, spec, head) or {}
                passing = {cid for cid, v in cur.items()
                           if isinstance(v, dict) and v.get("result") == "PASS"
                           and not v.get("flaky")}
            except Exception:                                     # noqa: BLE001
                passing = set()
        reps = self.live_repeats(P.id, task, passing=frozenset(passing))
        repeats = [{"fingerprint": r["fingerprint"], "class": r["class"],
                    "check_id": r["check_id"], "path": r["path"], "count": r["count"],
                    "text": (r["samples"][-1]["text"] if r["samples"] else "")[:300]}
                   for r in reps]

        affected = []
        for o in build_open:
            for e in o["required_evidence"]:
                if e["kind"] == "check" and e["ref"] not in affected:
                    affected.append(e["ref"])
        for r in reps:
            cid = r["check_id"] or ""
            if cid and not cid.startswith("review:") and cid not in affected:
                affected.append(cid)
        if ledger is not None and spec is not None and callable(getattr(ledger, "missing", None)):
            for m in ledger.missing(P, spec, head) or []:
                if m.get("check_id") and m["check_id"] not in affected:
                    affected.append(m["check_id"])

        # strategy: refused if it waives/moves/substitutes; must differ on repeats
        rejected = []
        strategy = (strategy or "").strip()
        if strategy and directive_violations(strategy):
            rejected += directive_violations(strategy)
            strategy = ""
        if not strategy:
            strategy = ("Repair the unresolved obligations " +
                        ", ".join(u["obligation_id"] for u in unresolved))
        sfp = strategy_fp(strategy)
        # A failed quota admission never contacted the adviser. Keep its audit
        # row, but do not spend the one real consultation on that outage.
        adviser_used = any(r.get("adviser") and r.get("adviser") != "adviser:unavailable"
                           for r in prior)
        same = bool(prev and prev.get("strategy_fp") == sfp)
        max_count = max([r["count"] for r in reps] or [0])
        adviser_note = ""
        esc_used = ""                  # the answered escalation this assignment acts on
        # A lead case has already investigated the repeat and supplied one
        # bounded, read-only-reviewed assignment. Let a genuinely new strategy
        # reach the primary once instead of immediately asking the same
        # recovery question again. Its fingerprint enters `prior` on this
        # assignment, so a subsequent repeat cannot reuse this exception.
        # Only the lead's own text qualifies: a rejected (waiving) lead text was
        # replaced by the default above and never counts as fresh.
        fresh_lead_strategy = (trusted_strategy and bool(strategy) and not rejected
                               and sfp not in {r.get("strategy_fp") for r in prior})
        lead_withdrawn: list[str] = []
        if reps and (same or max_count >= 3) and fresh_lead_strategy:
            # the lead's strategy answers this repeat set's still-open question
            # (kept as withdrawn for the audit, as the lead's resume does)
            base_q = self._escalation_question(reps)
            for d in self._escalations(obs, P.id, task, base_q, _sha(base_q, 16)):
                if d["status"] == "open":
                    self.store.x("UPDATE decisions SET status='withdrawn' WHERE "
                                 "decision_id=? AND status='open'", (d["decision_id"],))
                    self.store.event(P.id, task, "decision_withdrawn", detail={
                        "decision_id": d["decision_id"], "by": "lead_manager",
                        "reason": "answered by a fresh lead recovery strategy",
                        "strategy_fp": sfp})
                    lead_withdrawn.append(d["decision_id"])
        # a free continuation (context handoff, infra retry, operator reset) is the same
        # work carrying on, not the same strategy failing again: never ask (2026-10-28
        # GF-GATE1 14:13: the question opened and was withdrawn in the same second, but its
        # notice had already gone out)
        if reps and (same or max_count >= 3) and not fresh_lead_strategy and not free:
            base_q = self._escalation_question(reps)
            key = _sha(base_q, 16)
            chain = self._escalations(obs, P.id, task, base_q, key)
            latest = chain[-1] if chain else None
            if latest and latest["status"] == "open":
                link_prior_escalation(obs, P.id, task, latest["decision_id"])
                # still waiting on the operator: block on THIS open decision
                return {"decision": latest["decision_id"], "repeats": repeats,
                        "rejected_guidance": rejected}
            new, granted, supersedes = "", False, None
            if latest is None:
                # a new repeat set: its question replaces the task's previous
                # answered escalation (an older repeat set), so answering it
                # supersedes that answer instead of contradicting it
                supersedes = (prior_answered_escalation(obs, P.id, task, key) or
                              {}).get("decision_id")
            if latest is not None:                     # answered: act on the answer
                supersedes = latest["decision_id"]
                act, val = self._escalation_answer(latest)
                # an answer is good for one proceeding assignment (then a repeat
                # with the same strategy asks again, superseding it)
                unused = not any((r.get("assignment") or {}).get("escalation_decision") ==
                                 latest["decision_id"] for r in prior)
                if act == "stop":
                    return {"stop": True, "decision_id": latest["decision_id"],
                            "reason": f"operator stopped the task ({latest['decision_id']}): "
                                      f"{val}"[:500], "repeats": repeats,
                            "rejected_guidance": rejected}
                if act == "strategy":
                    v = directive_violations(val)
                    if v:
                        rejected += v
                    elif unused or strategy_fp(val) != (prev or {}).get("strategy_fp"):
                        new = val
                elif act == "budget" and unused:
                    granted = True                     # one more try with the strategy as is
            if not new and not granted and adviser is not None and not adviser_used:
                adviser_note = "consulted"
                try:
                    g = adviser({"project": P.id, "task": task, "attempt": attempt,
                                 "repeats": repeats, "unresolved": unresolved,
                                 "last_strategy": (prev or {}).get("strategy") or "",
                                 "rule": "propose a materially different strategy; never waive "
                                         "checks or move BUILD work to CLOSE"}) or {}
                except Exception as e:                         # noqa: BLE001
                    g = {"action": "unavailable", "summary": str(e)[:200]}
                if g.get("action") == "deferred":
                    return {"defer": True, "reason": str(g.get("summary") or "quota source "
                            "temporarily unavailable")[:300], "repeats": repeats}
                if g.get("action") == "repair":
                    steps = [str(s) for s in g.get("steps") or []]
                    kept = []
                    for s in steps:
                        v = directive_violations(s)
                        (rejected.extend(v) if v else kept.append(s))
                    cand = "\n".join(kept)
                    if kept and strategy_fp(cand) != (prev or {}).get("strategy_fp"):
                        new = (str(g.get("summary") or "")[:500] + "\n" + cand).strip()
                adviser_note = f"adviser:{g.get('action') or 'none'}"
            operator_said = bool(new) and latest is not None and not adviser_note
            if not granted and (not new or (strategy_fp(new) == (prev or {}).get("strategy_fp")
                                            and not operator_said)):
                q = base_q if latest is None else \
                    f"{base_q} (attempt {attempt}; the answer to {supersedes} did not resolve it)"
                did = ""
                from .decision_policy import MORE_BUDGET, PARK, STRATEGY
                for n in range(4):
                    qq = q if n == 0 else f"{q} [re-ask {n}]"
                    # W1.2 (Victor 2026-10-22): the default after the timeout is to park
                    # the sprint (BLOCKED with a summary; the queue moves on). The lead,
                    # when on, takes the question first (engine._lead_from_wait)
                    did = obs.open_decision(
                        P.id, task, "recovery_escalation", qq,
                        evidence={"repeats": repeats,
                                  "last_strategy": (prev or {}).get("strategy"),
                                  "adviser": adviser_note or "unavailable",
                                  "rejected_guidance": rejected, "attempt": attempt,
                                  "recommended": PARK},
                        options=[STRATEGY, MORE_BUDGET, PARK],
                        recommendation=PARK,
                        blocks=[task], scope={"escalation_key": key, "attempt": attempt},
                        supersedes=supersedes)
                    if (obs.decision(did) or {}).get("status") == "open":
                        break                      # never hand back an answered decision
                if adviser_note:
                    self._record_adviser_use(P, task, rid, attempt, adviser_note)
                return {"decision": did, "repeats": repeats, "rejected_guidance": rejected}
            if new:
                strategy, sfp = new, strategy_fp(new)
            if latest is not None and (granted or operator_said):
                esc_used = latest["decision_id"]

        budget = obs.effective_limits(P, trow)
        out = {"attempt": attempt, "repair_id": rid, "head": head,
               "what_changed": what_changed, "unresolved": unresolved,
               "overlooked": overlooked[:12], "repeats": repeats,
               "strategy_required_different": bool(reps), "strategy": strategy[:2000],
               "strategy_fp": sfp, "budget": budget, "affected_checks_to_rerun": affected,
               "escalation_decision": esc_used,
               "rejected_guidance": rejected}
        if fresh_lead_strategy and reps and (same or max_count >= 3):
            out["lead_strategy"] = True             # crossed the repeat guard once
            out["withdrawn_decisions"] = lead_withdrawn
        out["prompt_text"] = self._prompt(out, cap)
        self._enforce(out, build_open)
        with self.store.tx():
            self.store.x(
                "INSERT OR REPLACE INTO repairs (repair_id, project_id, task_id, attempt, "
                "run_id, start_rev, obligations, diagnosis, strategy, strategy_fp, "
                "validation_plan, adviser, assignment, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, P.id, task, attempt, trow.get("run_id") or "", head, _j(open_now),
                 _j([r["fingerprint"] for r in reps]), strategy[:4000], sfp, _j(affected),
                 adviser_note, _j(out), self.clock()))
        return out

    def _record_adviser_use(self, P, task, rid, attempt, note):
        self.store.x("INSERT OR IGNORE INTO repairs (repair_id, project_id, task_id, attempt, "
                     "adviser, outcome, ts) VALUES (?,?,?,?,?,?,?)",
                     (rid + "-esc", P.id, task, attempt, note, "escalated", self.clock()))

    @staticmethod
    def _escalations(obs, project, task, base_q: str, key: str) -> list[dict]:
        """The active (open/answered) recovery_escalation decisions for this
        repeat set, oldest first."""
        out = []
        for d in obs.active_decisions(project, task, "recovery_escalation"):
            if (d.get("scope") or {}).get("escalation_key") == key or d["question"] == base_q:
                out.append(d)
        out.sort(key=lambda d: (d.get("opened_ts") or 0, d["decision_id"]))
        return out

    _STOP = re.compile(r"^\s*(?:stop|abort|cancel|give\s+up|halt)\b", re.I)
    _BUDGET = re.compile(r"\b(?:budget|more\s+attempts?|another\s+attempt|extra\s+attempts?|"
                         r"authori[sz]e)\b", re.I)

    @classmethod
    def _escalation_answer(cls, d: dict) -> tuple[str, str]:
        """(stop|strategy|budget|unknown, text) from an answered escalation."""
        ans = d.get("answer")
        if isinstance(ans, dict):
            strategy = str(ans.get("strategy") or "").strip()
            option = str(ans.get("option") or ans.get("choice") or "").strip()
            if ans.get("stop") or cls._STOP.match(option) or cls._STOP.match(strategy):
                return "stop", str(ans.get("reason") or strategy or option or "stop")
            if strategy and not (len(strategy) < 60 and cls._BUDGET.search(strategy)) and \
                    not strategy.lower().startswith("answer with a different strategy"):
                return "strategy", strategy
            if ans.get("budget") or cls._BUDGET.search(option) or cls._BUDGET.search(strategy):
                return "budget", option or strategy
            return "unknown", option or strategy
        text = str(ans or "").strip()
        if cls._STOP.match(text):
            return "stop", text
        if cls._BUDGET.search(text) and len(text) < 60:
            return "budget", text
        return ("strategy", text) if text else ("unknown", "")

    @staticmethod
    def _escalation_question(reps) -> str:
        return ("Findings repeat across attempts with no materially different strategy: " +
                ", ".join(sorted(r["fingerprint"] for r in reps)))

    @staticmethod
    def _enforce(a: dict, build_open: list[dict]) -> None:  # raises AssignmentInvariant
        """Hard invariants: only BUILD work is assigned, every open BUILD check is
        rerun, and no line of the assignment waives/moves/substitutes."""
        if any(u.get("phase") != "BUILD" for u in a["unresolved"]):
            raise AssignmentInvariant("an assignment may only carry BUILD obligations")
        for o in build_open:
            for e in o["required_evidence"]:
                if e["kind"] == "check" and e["ref"] not in a["affected_checks_to_rerun"]:
                    raise AssignmentInvariant(f"BUILD check {e['ref']} dropped from the assignment")
        if directive_violations(a["strategy"]):
            raise AssignmentInvariant("assignment strategy waives or moves BUILD work")

    @staticmethod
    def _prompt(a: dict, cap: int) -> str:
        L = [f"## Repair assignment (attempt {a['attempt']})",
             "Rules: no check may be waived, skipped or moved to CLOSE. CLOSE paperwork "
             "(SUMMARY, STATE, done_check, the closeout commit) is no substitute for BUILD "
             "validation. Rerun the affected checks below at your final commit."]
        if a["unresolved"]:
            L.append("### Unresolved BUILD obligations (only these)")
            for u in a["unresolved"]:
                ev = ", ".join(f"{e['kind']}:{e['ref']}" for e in u["required_evidence"])
                L.append(f"- {u['obligation_id']}: {u['title'][:200]}"
                         + (f" | acceptance: {u['acceptance'][:200]}" if u["acceptance"] else "")
                         + (f" | evidence: {ev}" if ev else "")
                         + (f" | still: {u['blocking_reason'][:240]}" if u["blocking_reason"] else ""))
        wc = a["what_changed"]
        if wc["files"] or wc["closed"] or wc["reopened"]:
            L.append("### Since the last repair")
            if wc["files"]:
                L.append("- changed: " + ", ".join(wc["files"][:20]))
            if wc["closed"]:
                L.append("- now verified (do not redo): " + ", ".join(wc["closed"]))
            if wc["reopened"]:
                L.append("- reopened: " + ", ".join(wc["reopened"]))
        if a["overlooked"]:
            L.append("### Evidence you have not addressed")
            for o in a["overlooked"]:
                L.append(f"- [{o['status']}] {o['path'] or o['title']} {o['note'][:160]}".rstrip())
        if a["repeats"]:
            L.append("### Repeated findings (your previous approach did not fix these; use a "
                     "materially different strategy)")
            for r in a["repeats"]:
                L.append(f"- x{r['count']} [{r['class']}] {r['check_id']} {r['path']}: "
                         f"{r['text'][:200]}")
        L.append("### Strategy\n" + a["strategy"][:1200])
        if a["affected_checks_to_rerun"]:
            L.append("### Checks to rerun: " + ", ".join(a["affected_checks_to_rerun"]))
        b = a["budget"]
        L.append("### Budget: attempts {}/{}, sessions {}/{}, evidence continuations {}/{}".format(
            b["attempts"]["used"], b["attempts"]["max"], b["sessions"]["used"],
            b["sessions"]["max"], b["missing_evidence_continuations"]["used"],
            b["missing_evidence_continuations"]["max"]))
        text = "\n".join(L)
        return text if len(text) <= cap else text[:cap - 20] + "\n[...truncated]"

    def close_repair(self, P, t, attempt: int, *, end_rev: str = "", diff_files=None,
                     verification_id: str = "", outcome: str = "") -> None:
        rid = "rep-" + _sha(f"{P.id}|{_tid(t)}|{int(attempt)}", 16)
        self.store.x("UPDATE repairs SET end_rev=?, diff_files=?, verification_id=?, outcome=? "
                     "WHERE repair_id=? AND project_id=?",
                     (end_rev, _j(list(diff_files or [])), verification_id, outcome, rid, P.id))

    # -- submission gate ---------------------------------------------------------------------

    def gate_submission(self, P, t, head: str, ledger=None,
                        obligations: Optional[Obligations] = None, *, spec=None,
                        evidence_items: Optional[list] = None,
                        deferred=None) -> list[dict]:
        """Everything that must hold before a paid review: every BUILD obligation
        verified at `head`, no missing/stale required evidence (ledger and
        priority <= 2 packet items), no open obligation_conflict decision.
        Empty list = submit.

        `deferred(kind, ref) -> bool` names evidence another stage owns (a track
        sprint's Studio evidence: the merge gate runs it in the main checkout);
        an obligation all of whose evidence is deferred is not asked for here."""
        owns = deferred or (lambda kind, ref: False)
        obs = obligations or self._obs()
        task = _tid(t)
        trow = t if isinstance(t, dict) else (self.store.task(P.id, task) or {"task_id": task})
        if spec is None and callable(getattr(P.adapter, "describe", None)):
            spec = P.adapter.describe(task)
        current, missing = {}, []
        if ledger is not None and spec is not None:
            current = ledger.current(P, spec, head) or {}
            missing = list(ledger.missing(P, spec, head) or [])
        obs.evaluate(P, trow, head, current, missing, phases=("BUILD",))
        lim = obs.effective_limits(P, trow)["missing_evidence_continuations"]
        act = "continue_same_session" if lim["remaining"] > 0 else "changes_required"
        out = []
        for o in obs.all(P.id, task):
            if o["phase"] != "BUILD" or o["status"] in ("waived", "superseded"):
                continue
            req = o.get("required_evidence") or []
            if isinstance(req, str):
                try:
                    req = json.loads(req)
                except ValueError:
                    req = []
            # 2026-10-29 S52: a capture obligation pairs a Studio check (capture:<png>) with its
            # PNG path; only the check matched, so the track sprint was asked for a Studio
            # capture it cannot take. An obligation that needs a deferred check is deferred.
            if deferred and req and (
                    all(isinstance(r, dict) and owns(r.get("kind"), r.get("ref")) for r in req)
                    or any(isinstance(r, dict) and r.get("kind") == "check"
                           and owns("check", r.get("ref")) for r in req)):
                continue
            if o["status"] != "verified" or o.get("verified_rev") != head:
                out.append({"class": "missing_evidence", "action": act,
                            "item": {"obligation_id": o["obligation_id"], "title": o["title"],
                                     "required_evidence": o["required_evidence"],
                                     "reason": o.get("blocking_reason") or "not verified at head"}})
        for m in missing:
            if owns("check", m.get("check_id")):
                continue
            out.append({"class": "missing_evidence", "action": act,
                        "item": {"check_id": m.get("check_id"), "reason": m.get("reason"),
                                 "detail": str(m.get("detail") or "")[:300]}})
        if evidence_items:
            from .review import normalize_evidence
            for it in normalize_evidence(evidence_items):
                if it["status"] in ("missing", "stale") and it["priority"] <= 2                         and not owns("file", it["path"])                         and not (deferred and owns("check", str(it.get("title") or "")
                                                   .split(" ")[0].strip("()"))):
                    out.append({"class": "missing_evidence", "action": act,
                                "item": {"path": it["path"], "title": it["title"],
                                         "status": it["status"], "note": it["note"]}})
        for d in obs.active_decisions(P.id, task, "obligation_conflict"):
            if d["status"] == "open":
                out.append({"class": "scope_decision", "action": "decision",
                            "item": {"decision_id": d["decision_id"],
                                     "question": d["question"][:300]}})
        return out

    # -- progress ------------------------------------------------------------------------------

    def mark_session(self, P, t, session_id: str, *, prev_head: str, head: str,
                     reported: bool = True, actions: str = "",
                     verified: Optional[list[str]] = None,
                     work_files: Optional[int] = None,
                     checklist: Optional[dict] = None) -> None:
        """One row per session: the head before and after, the verified
        obligations and open finding set after it, and its committed BUILD
        progress (work_files: in-scope non-bookkeeping files changed
        prev_head..head; checklist: {"before", "after"} plan rows).

        A session can go idle more than once (a helper handoff, a continuation
        turn): the row keeps the head and checklist it STARTED from and is
        brought up to date at every later idle, so commits made after the first
        idle are never lost (they were, before v0.122.5: the first idle's
        INSERT OR IGNORE froze the row)."""
        task = _tid(t)
        attempt = int((t.get("attempt") if isinstance(t, dict) else 0) or 0)
        if verified is None:
            verified = [o["obligation_id"] for o in self._obs().all(P.id, task)
                        if o["status"] == "verified"]
        fps = sorted(r["fingerprint"] for r in self.findings(P.id, task)
                     if r["last_seen_attempt"] >= attempt)
        row = (_j(sorted(verified)), _sha(_j(fps)),
               strategy_fp(actions) if actions else "", 1 if reported else 0)
        with self.store.tx():
            old = self.store.q("SELECT id, checklist FROM progress_marks WHERE project_id=? "
                               "AND task_id=? AND session_id=?", (P.id, task, session_id))
            if not old:
                self.store.x(
                    "INSERT INTO progress_marks (project_id, task_id, session_id, attempt, "
                    "prev_head, head, verified, findings_fp, action_fp, reported, ts, "
                    "work_files, checklist) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (P.id, task, session_id, attempt, prev_head or "", head or "", *row,
                     self.clock(), work_files, _j(checklist) if checklist else None))
                return
            # a later idle of the same session: keep where it started (prev_head,
            # checklist "before") and its first reported actions (the repeat
            # detector compares sessions, not turns); record where it is now
            cl = _uj(old[0]["checklist"], None) if old[0]["checklist"] else None
            if checklist and isinstance(cl, dict) and cl.get("before") is not None:
                checklist = {**checklist, "before": cl["before"]}
            self.store.x(
                "UPDATE progress_marks SET head=?, verified=?, findings_fp=?, "
                "action_fp=CASE WHEN action_fp='' THEN ? ELSE action_fp END, "
                "reported=MAX(reported, ?), work_files=COALESCE(?, work_files), "
                "checklist=COALESCE(?, checklist) WHERE id=?",
                (head or "", *row, work_files, _j(checklist) if checklist else None,
                 old[0]["id"]))

    def progress(self, P, t, resolve: Optional[Callable[[dict], dict]] = None) -> dict:
        """Stall detection over the session marks. A session STALLS when it
        verified nothing new, made no committed BUILD progress, and either
        committed nothing or left the same findings. Committed BUILD progress
        is HEAD advancing with in-scope work files, or a plan checklist step
        advancing ([ ] -> [~] -> [x]) or an in-progress step's notes changing.

        resolve(mark) -> {"work_files", "checklist"} fills a mark recorded
        before those columns existed (or with them unknown); without it such a
        mark is judged by the older rule alone."""
        from .adapters.base import checklist_transitions
        task = _tid(t)
        marks = self.store.q("SELECT * FROM progress_marks WHERE project_id=? AND task_id=? "
                             "ORDER BY id", (P.id, task))
        # revamp B11: a session that died on infrastructure (Kilo did not admit the
        # prompt, a 5xx, Rojo/Studio down) says nothing about the sprint's progress,
        # and an operator resolution (unblock, answered decision) between two
        # sessions starts a fresh count: before, both raised false no-progress stops
        infra = self._infra_sessions(P.id, task, [m["session_id"] for m in marks])
        resets = self._progress_resets(P.id, task)
        need = self._stall_limit(P)
        skipped = 0
        stalls = summary_only = repeated = 0
        delta = 0
        prev = None
        last_build: dict = {}
        for m in marks:
            if m["session_id"] in infra:
                skipped += 1
                continue
            if prev is not None and any(float(prev["ts"] or 0) < r <= float(m["ts"] or 0)
                                        for r in resets):
                stalls = summary_only = repeated = 0
                prev = None
            v = set(_uj(m["verified"], []))
            pv = set(_uj(prev["verified"], [])) if prev else set()
            d = len(v - pv) - len(pv - v)
            committed = bool(m["head"]) and m["head"] != m["prev_head"]
            same_findings = bool(prev) and prev["findings_fp"] == m["findings_fp"]
            work = m.get("work_files")
            cl = _uj(m.get("checklist"), None) if m.get("checklist") else None
            if resolve is not None and (work is None or cl is None):
                try:
                    got = resolve(dict(m)) or {}
                except Exception:                                 # noqa: BLE001
                    got = {}
                work = got.get("work_files") if work is None else work
                cl = got.get("checklist") if cl is None else cl
            steps = checklist_transitions((cl or {}).get("before"), (cl or {}).get("after")) \
                if isinstance(cl, dict) else []
            build = (committed and bool(work)) or bool(steps)
            stall = d <= 0 and not build and (not committed or same_findings)
            summ = bool(m["reported"]) and not committed and d <= 0
            rep = bool(prev) and bool(m["action_fp"]) and m["action_fp"] == prev["action_fp"] \
                and m["head"] == prev["head"]
            stalls = stalls + 1 if stall else 0
            summary_only = summary_only + 1 if summ else 0
            repeated = repeated + 1 if rep else 0
            delta = d
            last_build = {"committed": committed, "work_files": work, "plan_steps": steps[:8]}
            prev = m
        no_progress = stalls >= need
        rec = "decision" if stalls >= need + 1 else "consult" if stalls >= need else "continue"
        out = {"sessions": len(marks), "verified_delta": delta, "repeated_actions": repeated,
               "summary_only_sessions": summary_only, "stalls": stalls,
               "no_progress": no_progress, "recommendation": rec,
               "last_session_build": last_build}
        if skipped:
            out["infra_sessions_skipped"] = skipped
        return out

    # revamp B11 ---------------------------------------------------------------------------
    INFRA_OUTCOMES = ("prompt_not_admitted", "lost", "error", "compaction_loop", "unbound",
                      "refused")

    @staticmethod
    def _stall_limit(P) -> int:
        try:
            from .automation import get as auto_get
            return max(1, int(auto_get(P, "no_progress_sessions") or 2))
        except Exception:                                        # noqa: BLE001
            return 2

    def _infra_sessions(self, project_id: str, task_id: str, sids: list) -> set:
        """Sessions whose end was an infrastructure failure: their outcome, or a
        BLOCKED report whose blocker carries a known infra signature."""
        from .autoheal import infra_signature
        out = set()
        for sid in set(sids):
            try:
                s = self.store.session(sid) or {}
            except Exception:                                    # noqa: BLE001
                s = {}
            if str(s.get("outcome") or "") in self.INFRA_OUTCOMES:
                out.add(sid)
                continue
            rep = ((s.get("data") or {}).get("report") or {}) if isinstance(
                s.get("data"), dict) else {}
            if isinstance(rep, dict) and str(rep.get("status") or "").upper() in (
                    "BLOCKED", "NEEDS_HUMAN") and infra_signature(
                    f"{rep.get('blocker') or ''} {rep.get('summary') or ''}"):
                out.add(sid)
        return out

    def _progress_resets(self, project_id: str, task_id: str) -> list[float]:
        """When an operator (or the decision timeout) resolved something for the
        task: a BLOCKED/FAILED -> READY transition not made by the engine, or an
        answered decision."""
        out = []
        try:
            for h in self.store.history(project_id, task_id):
                if h["to_state"] == "READY" and h["from_state"] in ("BLOCKED", "FAILED") \
                        and h.get("actor") not in ("engine", None, ""):
                    out.append(float(h["ts"] or 0))
        except Exception:                                        # noqa: BLE001
            pass
        try:
            out += [float(r["answered_ts"]) for r in self.store.q(
                "SELECT answered_ts FROM decisions WHERE project_id=? AND task_id=? AND "
                "answered_ts IS NOT NULL", (project_id, task_id))]
        except Exception:                                        # noqa: BLE001
            pass
        return out


def _glob_match(path: str, ref: str) -> bool:
    return glob_match(path, ref)


def _blocks(d: dict) -> set:
    b = d.get("blocks")
    return set(b if isinstance(b, list) else [])


def prior_answered_escalation(obs, project: str, task: str, key: str,
                              before: Optional[dict] = None) -> Optional[dict]:
    """The task's newest answered recovery_escalation of ANOTHER repeat set
    (escalation key), opened before `before` when given: the answer a new
    repeat set's question replaces. None when there is none."""
    best = None
    for d in obs.active_decisions(project, task, "recovery_escalation"):
        if d["status"] != "answered" or (d.get("scope") or {}).get("escalation_key") == key:
            continue
        if before is not None:
            if d["decision_id"] == before["decision_id"] or                     not (_blocks(d) & _blocks(before)) or                     (d.get("opened_ts") or 0) >= (before.get("opened_ts") or 0):
                continue
        if best is None or (d.get("opened_ts") or 0) >= (best.get("opened_ts") or 0):
            best = d
    return best


def link_prior_escalation(obs, project: str, task: str, decision_id: str) -> str:
    """Reconcile an OPEN recovery_escalation that was opened without
    `supersedes` while an older repeat set's escalation still stands answered
    (rows from before v0.123.1, or a repeat set that changed under it): record
    that older answer as the one it supersedes, so the operator's answer
    replaces it explicitly. Nothing else changes: the old decision becomes
    `superseded` only when the operator answers this one, the question (and
    its hash) is untouched, and a decision of another kind, task or blocked
    item is never linked, so the contradiction guard stays as strict.
    Returns the linked decision id ('' when nothing was linked)."""
    d = obs.decision(decision_id)
    if not d or d["kind"] != "recovery_escalation" or d["status"] != "open" or             d.get("supersedes") or d["project_id"] != project or d["task_id"] != task:
        return ""
    old = prior_answered_escalation(obs, project, task,
                                    (d.get("scope") or {}).get("escalation_key"), before=d)
    if not old:
        return ""
    n = obs.store.x("UPDATE decisions SET supersedes=? WHERE decision_id=? AND status='open' "
                    "AND (supersedes IS NULL OR supersedes='')", (old["decision_id"], decision_id))
    if n == 0 and (obs.decision(decision_id) or {}).get("supersedes") != old["decision_id"]:
        return ""
    obs.store.event(project, task, "decision_linked", detail={
        "decision_id": decision_id, "supersedes": old["decision_id"], "by": "coordinator",
        "reason": "a new repeat set's recovery question replaces the older answered one"})
    return old["decision_id"]

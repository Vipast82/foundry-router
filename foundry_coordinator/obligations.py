"""Structured task obligations, operator decisions and the budget ledger.

An obligation is one thing a task owes (a BUILD check, a required capture, a
CLOSE duty, a visual approval). Its status changes only from evidence
(`evaluate`) or a review (`review_link`), never from a model's report. Its
phase is locked: stop summaries, plan NEXT lines, helper notes, adviser
guidance and repair assignments can never move it. Only an answered
`obligation_phase` decision can, and moving BUILD work to CLOSE needs the
operator himself.

A BUILD obligation that declares no machine evidence (an acceptance prose
line) is stored as REVIEW: an approving independent review verifies it.
Unknown evidence kinds are kept as unmet requirements and open an
obligation_conflict; an unknown dependency is unmet and flagged.

Decisions are typed questions for the operator. The authority that answers
one is `operator` or `delegated_policy`, never a model. `operator` is a
positive rule: answer(..., channel='control') from the control center's
decision_answer path, or an actor in `operator_actors`; a model-name denylist
is an extra layer. Conflicting answers go through supersession; both rows
stay for the audit.

The budget ledger is append-only. Counters are sums of rows, so compaction
and restarts never reset them. An operator reset or extension is an explicit
`authorized:<counter>` row tied to an answered budget_extension/reset
decision naming that counter (and n), or to an applied `unblock:<n>` reset;
each reference authorizes a counter once.

Every query filters on project_id. Tables are created by `migrate(store)`
(versioned in meta `schema:obligations`), safe on an existing DB.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Iterable, Optional

from .evidence import glob_match
from .profile import matches_any

SCHEMA_NAME = "schema:obligations"
SCHEMA_VERSION = 2

PHASES = ("BUILD", "REVIEW", "CLOSE", "APPROVAL")
STATUSES = ("open", "in_progress", "evidenced", "verified", "waived", "superseded")
LIVE = ("open", "in_progress", "evidenced", "verified")
EVIDENCE_KINDS = ("check", "path", "blob_changed", "log_marker")
AUTHORITIES = ("operator", "delegated_policy")
# A delegated policy may never answer these (they need the operator).
OPERATOR_ONLY_KINDS = frozenset({"scope_change", "approval", "expectation_change",
                                 "quarantine_check", "policy"})
# An extra layer only: operator authority is a POSITIVE rule (the control
# channel or an operator allowlist, see Obligations._check_authority).
MODEL_ACTOR_PREFIXES = ("model", "session", "helper", "assistant", "persona", "kilo",
                        "cline", "foundry-", "adviser", "reviewer", "implementer", "agent",
                        "claude", "gpt", "qwen", "codex", "opus", "sonnet", "haiku", "gemini",
                        "llm", "coordinator", "engine", "repo", "plan", "policy-model")
# The channel the control center's decision_answer path passes; only it carries
# a human operator session.
OPERATOR_CHANNEL = "control"
# Decision kinds the coordinator knows (others are allowed; these are typed).
DECISION_KINDS = ("budget_extension", "reset", "obligation_phase", "obligation_conflict",
                  "recovery_escalation", "no_progress", "review_access", "scope_change",
                  "approval", "expectation_change", "quarantine_check", "policy", "flaky_check")
BUDGET_KINDS = ("budget_extension", "reset")

# Ledger counters (raw usage, only ever charged upward).
COUNTERS = ("attempt_ordinary", "attempt_guided", "sessions", "closeout", "review_calls",
            "review_errors", "provider_retries", "same_session_continuations")
# The design's `attempt_authorized` is `authorized:attempt_ordinary` here.
AUTH_PREFIX = "authorized:"

# effective_limits key -> (counter, profile limit key, default max)
LIMITS = {
    "ordinary": ("attempt_ordinary", "max_attempts", 3),
    "guided_extra": ("attempt_guided", None, 0),
    "sessions": ("sessions", "max_sessions_per_task", 8),
    "closeout": ("closeout", "max_closeout_attempts", 2),
    "review_calls": ("review_calls", "max_review_calls", 4),
    "review_errors": ("review_errors", "max_review_errors", 3),
    "transport_retries": ("provider_retries", "recovery.transport", 2),
    "missing_evidence_continuations": ("same_session_continuations",
                                       "recovery.missing_evidence", 2),
}

_V1 = """
CREATE TABLE IF NOT EXISTS obligations (
  obligation_id TEXT, project_id TEXT, task_id TEXT, phase TEXT, title TEXT, source TEXT,
  deps TEXT, acceptance TEXT, required_evidence TEXT, status TEXT, verified_by TEXT,
  verified_rev TEXT, findings TEXT, blocking_reason TEXT, next_action TEXT, package TEXT,
  created_ts REAL, updated_ts REAL, PRIMARY KEY (project_id, task_id, obligation_id));
CREATE TABLE IF NOT EXISTS findings (
  fingerprint TEXT, project_id TEXT, task_id TEXT, class TEXT, check_id TEXT, path TEXT,
  tokens TEXT, first_seen_attempt INT, last_seen_attempt INT, count INT, obligation_id TEXT,
  samples TEXT, PRIMARY KEY (project_id, task_id, fingerprint));
CREATE TABLE IF NOT EXISTS repairs (
  repair_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, attempt INT, run_id TEXT,
  start_rev TEXT, preexisting_dirty TEXT, intended_files TEXT, obligations TEXT,
  diagnosis TEXT, strategy TEXT, strategy_fp TEXT, validation_plan TEXT, end_rev TEXT,
  diff_files TEXT, verification_id TEXT, unexpected TEXT, quarantined TEXT, outcome TEXT,
  ts REAL);
CREATE TABLE IF NOT EXISTS budget_ledger (
  id INTEGER PRIMARY KEY, project_id TEXT, task_id TEXT, counter TEXT, delta INT,
  reason TEXT, decision_id TEXT, actor TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, kind TEXT, question TEXT,
  evidence TEXT, options TEXT, recommendation TEXT, blocks TEXT, status TEXT, answer TEXT,
  authority TEXT, actor TEXT, scope TEXT, supersedes TEXT, superseded_by TEXT,
  opened_ts REAL, answered_ts REAL);
"""

# Numbered steps after v1: (version, [(table, column, type) | "SQL"]).
_STEPS = [
    (2, [("decisions", "qhash", "TEXT"),
         ("obligations", "phase_decision", "TEXT"),
         ("findings", "attempts", "TEXT"),
         ("repairs", "adviser", "TEXT"),
         ("repairs", "assignment", "TEXT"),
         "CREATE INDEX IF NOT EXISTS idx_dec_task ON decisions(project_id, task_id, status)",
         "CREATE INDEX IF NOT EXISTS idx_ledger_task ON budget_ledger(project_id, task_id)",
         "CREATE INDEX IF NOT EXISTS idx_find_task ON findings(project_id, task_id)"]),
]


class DecisionRefused(ValueError):
    """A decision answer from a non-authority (a model, a session) or out of policy."""


class DecisionConflict(ValueError):
    """An answer contradicts an active answered decision; supersede it instead."""


class PhaseLocked(ValueError):
    """An obligation's phase can only change through an answered obligation_phase decision."""


class LedgerRefused(ValueError):
    pass


# -- helpers -----------------------------------------------------------------------

def _j(v) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def _uj(v, default=None):
    if v in (None, ""):
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _sha(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


def _tid(t) -> str:
    return t if isinstance(t, str) else str(t["task_id"])


def _columns(store, table: str) -> set[str]:
    return {r["name"] for r in store.q(f"PRAGMA table_info({table})")}


def migrate_schema(store, name: str, create_sql: str, steps: list) -> int:
    """Versioned, idempotent migration: CREATE IF NOT EXISTS, then the numbered
    steps above the recorded version. ADD COLUMN steps are skipped when the
    column already exists, so a DB in any earlier shape converges. Runs in one
    store.tx() with individual statements (executescript would COMMIT any
    transaction the caller has open)."""
    with store.tx():
        for sql in (s.strip() for s in create_sql.split(";")):
            if sql:
                store.x(sql)
        try:
            current = int(store.meta(name) or 0)
        except ValueError:
            current = 0
        current = max(current, 1)
        for version, ops in steps:
            if version <= current:
                continue
            for op in ops:
                if isinstance(op, tuple):
                    table, col, typ = op
                    if col not in _columns(store, table):
                        store.x(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                else:
                    store.x(op)
            current = version
        # Always re-assert the columns of every step (an older DB may have had the
        # meta row but lost a column); cheap and idempotent.
        for _, ops in steps:
            for op in ops:
                if isinstance(op, tuple) and op[1] not in _columns(store, op[0]):
                    store.x(f"ALTER TABLE {op[0]} ADD COLUMN {op[1]} {op[2]}")
        store.set_meta(name, str(current))
    return current


def migrate(store) -> int:
    return migrate_schema(store, SCHEMA_NAME, _V1, _STEPS)


def is_model_actor(actor: str) -> bool:
    a = (actor or "").strip().lower()
    return not a or a.startswith(MODEL_ACTOR_PREFIXES)


def _limit(P, key: Optional[str], default: int) -> int:
    if not key:
        return default
    limits = getattr(getattr(P, "profile", None), "limits", None) or {}
    node: Any = limits
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    try:
        return max(0, int(node))
    except (TypeError, ValueError):
        return default


def _is_glob(ref: str) -> bool:
    return any(c in ref for c in "*?[")


# -- phase-move detection (shared with recovery) -------------------------------------

CLOSE_TARGET = re.compile(
    r"\b(?:to|in|into|for|until|at|during|the|by)\s+(?:the\s+)?(?:close(?:out)?|closing)\b"
    r"|\bclose(?:out)?[\s-]+(?:session|phase|step|stage)\b|\bcloseout\b", re.I)
DEFER = re.compile(
    r"\b(?:defer\w*|mov(?:e|es|ed|ing)|postpon\w*|belong\w*|reserv\w*|leav\w*|left|later|"
    r"push\w*\s+(?:back|out)|wait\w*|handl\w*|delegat\w*|hand\w*\s+(?:off|over)|"
    r"shift\w*|carr\w*\s+over|do(?:ne)?\s+(?:in|at|during))\b", re.I)
WAIVE = re.compile(
    r"\b(?:waiv|skip|bypass|ignor|disabl|relax|suppress|quarantin|downgrad)\w*\b[^.;,\n]{0,30}?"
    r"\b(?:checks?|tests?|gates?|asserts?|assertions?|validations?|requirements?|captures?|"
    r"verifications?|evidence)\b"
    r"|\b(?:checks?|tests?|gates?|captures?)\b[^.;,\n]{0,25}\b(?:as|is|are)\s+(?:optional|"
    r"not\s+required|non-?blocking|passed|waived)\b"
    r"|\bmark\w*\b[^.;,\n]{0,30}\b(?:optional|not\s+required|as\s+pass(?:ed)?)\b", re.I)
SUBSTITUTE = re.compile(
    r"\b(?:summary|state(?:\.md)?\s*(?:row|ledger)?|done_check|closeout|close\s+session|"
    r"close\s+paperwork|paperwork|ledger\s+row|evidence\s+log\s+(?:line|row))\b[^.;\n]{0,60}?"
    r"\b(?:instead\s+of|in\s+place\s+of|substitut\w*|replac\w*|satisf\w*|suffic\w*|"
    r"counts?\s+as|covers?|stands?\s+in\s+for)\b[^.;\n]{0,60}?"
    r"\b(?:build|reruns?|tests?|gates?|captures?|validation|verification|checks?)\b", re.I)

CATEGORIES = {
    "capture": ("capture", "captures", "screenshot", "screenshots", "frame", "frames",
                "image", "images", "png", "recording"),
    "rerun": ("rerun", "reruns", "re-run", "re-runs", "rerunning", "test", "tests", "suite",
              "suites", "gate", "gates", "check", "checks", "validation", "verify",
              "verification"),
    "evidence": ("evidence", "proof", "artifact", "artifacts"),
}


def directive_violations(text: str) -> list[str]:
    """What a guidance step, strategy or assignment line may never say:
    waive/skip a check, move BUILD work to CLOSE, or treat CLOSE paperwork as
    a substitute for BUILD validation."""
    out = []
    for raw in re.split(r"[\n]+", text or ""):
        line = raw.strip()
        if not line:
            continue
        # dotted ids and paths (au.gate, docs/x.png) would stop the [^.] windows
        flat = re.sub(r"[\w\-]+(?:[./\\][\w\-]+)+", "item", line)
        if WAIVE.search(line) or WAIVE.search(flat):
            out.append("waives_check: " + line[:240])
        if CLOSE_TARGET.search(line) and DEFER.search(line):
            out.append("moves_build_to_close: " + line[:240])
        if SUBSTITUTE.search(line) or SUBSTITUTE.search(flat):
            out.append("close_substitutes_build: " + line[:240])
    return out


def phase_move_lines(text: str) -> list[str]:
    return [l.strip() for l in (text or "").splitlines()
            if l.strip() and CLOSE_TARGET.search(l) and DEFER.search(l)]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9_\-]*", (text or "").lower()))


def _ob_categories(ob: dict) -> set[str]:
    blob = " ".join([ob["obligation_id"], ob.get("title") or "",
                     " ".join(str(e.get("ref", "")) for e in ob.get("required_evidence") or [])]
                    ).lower()
    cats = set()
    if any(w in blob for w in ("cap-", "capture", "screenshot", "frame", ".png", ".jpg",
                               "docs/evidence/")):
        cats.add("capture")
    kinds = {e.get("kind") for e in ob.get("required_evidence") or []}
    if "check" in kinds or ob["obligation_id"].startswith("check:"):
        cats.add("rerun")
    if kinds & {"path", "blob_changed", "log_marker"}:
        cats.add("evidence")
    return cats


# -- the class ---------------------------------------------------------------------

class Obligations:
    def __init__(self, store, clock=time.time, *, operator_actors: Optional[Iterable[str]] = None,
                 unblock_validator=None):
        """operator_actors: names allowed to carry `operator` authority outside
        the control channel (default none: only channel='control' answers
        carry it). unblock_validator(project, task, ref, actor) -> bool checks
        an `unblock:<ref>` budget authorization; without it the reference must
        name an applied reset_attempts transition of the task by that actor."""
        self.store, self.clock = store, clock
        self.operator_actors = frozenset(str(a).strip().lower() for a in operator_actors or ()
                                         if str(a).strip())
        self.unblock_validator = unblock_validator
        migrate(store)

    # -- rows ---------------------------------------------------------------------

    @staticmethod
    def _row(r: dict) -> dict:
        r = dict(r)
        for k, d in (("deps", []), ("required_evidence", []), ("findings", []),
                     ("acceptance", "")):
            r[k] = _uj(r.get(k), d) if k != "acceptance" else (r.get(k) or "")
        return r

    def get(self, project: str, task: str, obligation_id: str) -> Optional[dict]:
        r = self.store.q("SELECT * FROM obligations WHERE project_id=? AND task_id=? AND "
                         "obligation_id=?", (project, task, obligation_id))
        return self._row(r[0]) if r else None

    def all(self, project: str, task: str) -> list[dict]:
        rows = self.store.q("SELECT rowid AS _n, * FROM obligations WHERE project_id=? AND "
                            "task_id=? ORDER BY created_ts, _n", (project, task))
        return [self._row(r) for r in rows]

    @staticmethod
    def _split_evidence(ob: dict) -> tuple[list[dict], list]:
        """(known required evidence, entries with an unknown kind or no ref)."""
        req, bad = [], []
        for e in ob.get("required_evidence") or []:
            if isinstance(e, dict) and e.get("kind") in EVIDENCE_KINDS and e.get("ref"):
                req.append({k: e[k] for k in ("kind", "ref", "marker", "min_tier") if k in e})
            else:
                bad.append(e)
        return req, bad

    @classmethod
    def seed_phase(cls, ob: dict) -> str:
        """The phase an adapter obligation is stored with. Unknown phases never
        slip to CLOSE (BUILD). A BUILD obligation that declares no machine
        evidence at all (an acceptance prose line) can never verify from
        evidence, so it is stored as REVIEW: it is verified by an approving
        independent review of the revision (review_link), and its source and
        title are preserved. One with unknown evidence kinds stays BUILD and
        is flagged (an obligation_conflict), never silently relaxed."""
        phase = str(ob.get("phase") or "BUILD").upper()
        phase = phase if phase in PHASES else "BUILD"
        req, bad = cls._split_evidence(ob)
        if phase == "BUILD" and not req and not bad:
            return "REVIEW"
        return phase

    def _put(self, project, task, ob: dict) -> bool:
        phase = self.seed_phase(ob)
        req, bad = self._split_evidence(ob)
        oid = str(ob["obligation_id"])
        now = self.clock()
        if self.get(project, task, oid):
            return False
        # unknown evidence kinds are kept (as unmet requirements) and flagged
        stored = req + [{"kind": "unknown", "ref": _j(e)[:300]} for e in bad]
        self.store.x(
            "INSERT OR IGNORE INTO obligations (obligation_id, project_id, task_id, phase, "
            "title, source, deps, acceptance, required_evidence, status, findings, "
            "blocking_reason, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (oid, project, task, phase, str(ob.get("title") or "")[:500],
             str(ob.get("source") or ""), _j(list(ob.get("deps") or [])),
             str(ob.get("acceptance") or "")[:2000], _j(stored), "open", _j([]),
             ("no machine evidence declared: verified by an approving independent review"
              if phase == "REVIEW" and str(ob.get("phase") or "").upper() == "BUILD" else ""),
             now, now))
        if bad:
            self.open_decision(
                project, task, "obligation_conflict",
                f"obligation {oid} declares required evidence the coordinator cannot check: "
                + ", ".join(_j(e)[:120] for e in bad[:5]),
                evidence={"obligation_id": oid, "unknown_evidence": bad[:10],
                          "source": str(ob.get("source") or "")},
                options=["fix the adapter's evidence declaration", "operator decision"],
                recommendation="declare check/path/blob_changed/log_marker evidence",
                blocks=[oid])
        return True

    # -- seed / reconcile -------------------------------------------------------------

    def _adapter_obligations(self, P, spec) -> list[dict]:
        hook = getattr(P.adapter, "obligations", None)
        model = hook(spec) if callable(hook) else None
        if model is not None:
            # the adapter has an obligation model ([] = genuinely none required)
            return [o for o in model if isinstance(o, dict) and o.get("obligation_id")]
        out = []          # None: no obligation model -> checks + deferred duties fallback
        req = getattr(P.adapter, "required_checks", None)
        checks = req(spec, "BUILD") if callable(req) else None
        if checks:
            for c in checks:
                cid = c.get("check_id") if isinstance(c, dict) else str(c)
                if cid:
                    out.append({"obligation_id": f"check:{cid}", "phase": "BUILD",
                                "title": f"Check {cid} passes at the reviewed revision",
                                "source": f"check:{cid}",
                                "required_evidence": [{"kind": "check", "ref": cid}]})
        else:
            for c in getattr(getattr(P, "profile", None), "checks", None) or []:
                if getattr(c, "required", True) and getattr(c, "phase", "verify") == "verify":
                    out.append({"obligation_id": f"check:{c.name}", "phase": "BUILD",
                                "title": f"Check {c.name} passes at the reviewed revision",
                                "source": f"check:{c.name}",
                                "required_evidence": [{"kind": "check", "ref": c.name}]})
        contract = getattr(P.adapter, "review_contract", None)
        deferred = (contract(spec) or {}).get("deferred") or [] if callable(contract) else []
        for i, d in enumerate(deferred):
            did = d.get("id") if isinstance(d, dict) else None
            title = (d.get("duty") or d.get("title") or did) if isinstance(d, dict) else str(d)
            oid = f"close.{did or i + 1}"
            out.append({"obligation_id": oid, "phase": "CLOSE", "title": title,
                        "source": f"adapter:{did or i + 1}", "required_evidence": []})
        return out

    def seed(self, P, spec) -> list[str]:
        """Idempotent on (task, obligation_id). An existing row keeps its status
        and its phase (the adapter can't move a phase either; reconcile flags it)."""
        task = spec.task_id
        added = []
        with self.store.tx():
            for ob in self._adapter_obligations(P, spec):
                if self._put(P.id, task, ob):
                    added.append(str(ob["obligation_id"]))
        return added

    def screen_text(self, P, task: str, text: str, source: str) -> list[dict]:
        """Refuse any line of a model/plan/helper text that moves BUILD work to
        CLOSE: the obligation keeps phase BUILD and an obligation_conflict
        decision is opened. Returns the conflicts."""
        conflicts = []
        builds = [o for o in self.all(P.id, task) if o["phase"] == "BUILD"
                  and o["status"] in LIVE]
        for line in phase_move_lines(text):
            words = _words(line)
            cats = {c for c, ws in CATEGORIES.items() if words & set(ws)}
            for ob in builds:
                refs = {str(e.get("ref", "")).lower() for e in ob["required_evidence"]}
                direct = ob["obligation_id"].lower() in line.lower() or any(
                    r and r in line.lower() for r in refs)
                if not direct and not (cats & _ob_categories(ob)):
                    continue
                q = (f"{source} asks to move BUILD obligation {ob['obligation_id']} to CLOSE "
                     f"(refused; phase stays BUILD): {line[:300]}")
                did = self.open_decision(
                    P.id, task, "obligation_conflict", q,
                    evidence={"source": source, "line": line[:500],
                              "obligation": ob["obligation_id"]},
                    options=["keep BUILD (default)", "operator moves it with an "
                             "obligation_phase decision"],
                    recommendation="keep BUILD: captures and reruns are BUILD evidence",
                    blocks=[ob["obligation_id"]])
                conflicts.append({"kind": "phase_move", "obligation_id": ob["obligation_id"],
                                  "source": source, "line": line[:500], "decision_id": did})
        return conflicts

    def reconcile(self, P, spec, plan_text_at_rev: str = "") -> list[dict]:
        task = spec.task_id
        conflicts = self.screen_text(P, task, plan_text_at_rev, "plan")
        stored = {o["obligation_id"]: o for o in self.all(P.id, task)}
        scope = list(getattr(spec, "scope", None) or [])

        def conflict(kind, q, evidence, blocks):
            did = self.open_decision(P.id, task, "obligation_conflict", q, evidence=evidence,
                                     options=["fix the plan or scope", "operator decision"],
                                     recommendation="", blocks=blocks)
            conflicts.append({"kind": kind, "decision_id": did, **evidence})

        # the adapter now proposes a different phase for an existing obligation
        for ob in self._adapter_obligations(P, spec):
            cur = stored.get(str(ob["obligation_id"]))
            want = self.seed_phase(ob)
            if cur and want in PHASES and want != cur["phase"] and not cur.get("phase_decision"):
                conflict("phase_drift", f"adapter phase {want} differs from locked phase "
                         f"{cur['phase']} for {cur['obligation_id']}",
                         {"obligation_id": cur["obligation_id"], "adapter_phase": want,
                          "phase": cur["phase"]}, [cur["obligation_id"]])
        # required evidence the task may not even write
        if scope:
            for ob in stored.values():
                for e in ob["required_evidence"]:
                    if e["kind"] in ("path", "blob_changed") and not _is_glob(e["ref"]) \
                            and not matches_any(e["ref"], scope):
                        conflict("evidence_outside_scope",
                                 f"required evidence {e['ref']} of {ob['obligation_id']} is "
                                 f"outside the task scope",
                                 {"obligation_id": ob["obligation_id"], "ref": e["ref"]},
                                 [ob["obligation_id"]])
        # plan acceptance captures that no obligation tracks
        refs = {str(e.get("ref")) for o in stored.values() for e in o["required_evidence"]}
        for line in (plan_text_at_rev or "").splitlines():
            if not re.search(r"acceptance|capture|screenshot", line, re.I):
                continue
            for path in re.findall(r"[\w.\-/]+\.(?:png|jpe?g|gif|webp|mp4|log)\b", line):
                if not any(path == r or (_is_glob(r) and glob_match(path, r))
                           for r in refs):
                    conflict("plan_requirement_untracked",
                             f"the plan requires {path} but no obligation tracks it",
                             {"ref": path, "line": line.strip()[:300]}, [path])
        # dependencies on obligations that don't exist (next_package treats them as unmet)
        for ob in stored.values():
            for dep in ob["deps"]:
                if dep not in stored:
                    conflict("unknown_dependency",
                             f"obligation {ob['obligation_id']} depends on unknown obligation "
                             f"{dep}", {"obligation_id": ob["obligation_id"], "dep": dep},
                             [ob["obligation_id"]])
        # answered decisions that reference unknown obligations
        for d in self.active_decisions(P.id, task):
            scope_obs = (d.get("scope") or {}).get("obligations") or []
            ans = d.get("answer") if isinstance(d.get("answer"), dict) else {}
            if ans.get("obligation"):
                scope_obs = [*scope_obs, ans["obligation"]]
            for oid in scope_obs:
                if oid not in stored:
                    conflict("unknown_obligation", f"decision {d['decision_id']} references "
                             f"unknown obligation {oid}",
                             {"obligation_id": oid, "decision": d["decision_id"]}, [oid])
        return conflicts

    # -- evaluation -------------------------------------------------------------------

    def _evidence_met(self, P, t, ob, e, revision, current, missing_ids):
        kind, ref = e["kind"], str(e["ref"])
        ws = getattr(P, "ws", None)
        if kind == "check":
            rec = current.get(ref)
            at = (revision or "")[:12]
            if not isinstance(rec, dict) or ref in missing_ids:
                return False, f"check {ref}: no fresh verified record at {at}"
            # the record's phase is its session's phase (EvidenceLedger.current sets it)
            if ob["phase"] == "BUILD" and \
                    str(rec.get("phase") or "").upper() in ("CLOSE", "CLOSEOUT"):
                return False, f"check {ref}: only CLOSE-phase evidence (never credited at BUILD)"
            if str(rec.get("result") or "").upper() != "PASS":
                return False, f"check {ref}: newest fresh result is " \
                              f"{rec.get('result') or 'unknown'} at {at}"
            if rec.get("flaky"):
                return False, f"check {ref}: flaky (a fresh FAIL and a fresh PASS) at {at}"
            if rec.get("truncated"):
                return False, f"check {ref}: only truncated output (never credited)"
            need = e.get("min_tier") or "verified"
            tiers = ("claim", "observed", "verified", "reviewed", "approved")
            have = str(rec.get("tier") or "")
            if have not in tiers or tiers.index(have) < tiers.index(
                    need if need in tiers else "verified"):
                return False, f"check {ref}: tier {have or 'none'} below {need}"
            if rec.get("freshness") != "fresh":
                return False, f"check {ref}: record not fresh at {at} " \
                              f"({rec.get('freshness') or 'freshness unknown'})"
            return True, ""
        files = self._files_at(ws, revision, ref)
        if files is None:
            return False, f"{kind} {ref}: unknown (workspace cannot read {revision[:12]})"
        if kind == "path":
            return (True, "") if files else (False, f"path {ref}: absent at {revision[:12]}")
        if kind == "blob_changed":
            data = (t.get("data") or {}) if isinstance(t, dict) else {}
            start = data.get("task_base") or (t.get("base_sha") if isinstance(t, dict) else None)
            if not start:
                return False, f"blob_changed {ref}: no start revision"
            if not files:
                return False, f"blob_changed {ref}: absent at {revision[:12]}"
            same = [p for p, b in files if b and ws.blob_at(start, p) == b]
            if same:
                return False, (f"blob_changed {ref}: {len(same)} file(s) unchanged since "
                               f"{start[:12]}: " + ", ".join(same[:8]))
            return True, ""
        if kind == "log_marker":
            marker = e.get("marker") or ""
            path = ref
            if not marker and "::" in ref:
                path, marker = ref.split("::", 1)
            show = getattr(ws, "show_at", None)
            text = show(revision, path) if callable(show) else None
            if text is None:
                return False, f"log_marker {path}: absent at {revision[:12]}"
            return (True, "") if marker and marker in text else \
                (False, f"log_marker {path}: marker {marker!r} not found")
        return False, f"unknown evidence kind {kind}"

    @staticmethod
    def _files_at(ws, revision, ref) -> Optional[list[tuple[str, str]]]:
        if ws is None:
            return None
        if _is_glob(ref):
            ls = getattr(ws, "ls_tree_at", None)
            if not callable(ls):
                return None
            parts = ref.split("/")
            base = "/".join(p for p in parts[:next(i for i, p in enumerate(parts)
                                                     if _is_glob(p))])
            return [(p, b) for p, b in ls(revision, base or ".") if glob_match(p, ref)]
        blob = getattr(ws, "blob_at", None)
        if not callable(blob):
            return None
        b = blob(revision, ref)
        return [(ref, b)] if b else []

    def evaluate(self, P, t, revision: str, evidence_current: Optional[dict] = None,
                 evidence_missing: Optional[Iterable[dict]] = None, phases=None) -> dict:
        """Status changes from evidence only: all required evidence met at
        `revision` → verified; a verified obligation whose evidence no longer
        holds → open (regressed). Obligations without required evidence are
        left alone (review_link or an operator decision handles those)."""
        task = _tid(t)
        if not isinstance(t, dict):
            t = self.store.task(P.id, task) or {"task_id": task, "data": {}}
        # evidence_missing None = "not computed": the records in evidence_current
        # must carry the verdict on their own (result/tier/freshness/flaky)
        current = dict(evidence_current or {})
        missing_ids = {m.get("check_id") for m in (evidence_missing or [])
                       if isinstance(m, dict)}
        verified, open_, regressed = [], [], []
        now = self.clock()
        with self.store.tx():
            for ob in self.all(P.id, task):
                if ob["status"] not in LIVE or (phases and ob["phase"] not in phases):
                    continue
                if not ob["required_evidence"]:
                    (verified if ob["status"] == "verified" else open_).append(ob["obligation_id"])
                    continue
                unmet = []
                for e in ob["required_evidence"]:
                    ok, why = self._evidence_met(P, t, ob, e, revision, current, missing_ids)
                    if not ok:
                        unmet.append(why)
                if not unmet:
                    verified.append(ob["obligation_id"])
                    self.store.x("UPDATE obligations SET status='verified', verified_rev=?, "
                                 "verified_by=?, blocking_reason='', updated_ts=? WHERE "
                                 "project_id=? AND task_id=? AND obligation_id=?",
                                 (revision, "evidence", now, P.id, task, ob["obligation_id"]))
                else:
                    open_.append(ob["obligation_id"])
                    if ob["status"] == "verified":
                        regressed.append(ob["obligation_id"])
                    self.store.x("UPDATE obligations SET status='open', blocking_reason=?, "
                                 "updated_ts=? WHERE project_id=? AND task_id=? AND "
                                 "obligation_id=?", ("; ".join(unmet)[:2000], now, P.id, task,
                                                     ob["obligation_id"]))
        return {"verified": verified, "open": open_, "regressed": regressed}

    def review_link(self, P, t, review_id: str, verdict: str, revision: str,
                    cited: Optional[dict] = None) -> list[str]:
        """APPROVED at `revision` verifies the REVIEW-phase obligations. A
        CHANGES_REQUIRED that cites obligations ({obligation_id: [fingerprints]})
        reopens them and links the findings. Returns changed ids."""
        task, changed, now = _tid(t), [], self.clock()
        with self.store.tx():
            for ob in self.all(P.id, task):
                if ob["status"] not in LIVE:
                    continue
                oid = ob["obligation_id"]
                if verdict == "APPROVED" and ob["phase"] == "REVIEW" and ob["status"] != "verified":
                    self.store.x("UPDATE obligations SET status='verified', verified_rev=?, "
                                 "verified_by=?, updated_ts=? WHERE project_id=? AND task_id=? "
                                 "AND obligation_id=?", (revision, f"review:{review_id}", now,
                                                         P.id, task, oid))
                    changed.append(oid)
                elif verdict != "APPROVED" and cited and oid in cited:
                    fps = sorted(set(ob["findings"]) | set(cited[oid] or []))
                    self.store.x("UPDATE obligations SET status='open', findings=?, "
                                 "blocking_reason=?, updated_ts=? WHERE project_id=? AND "
                                 "task_id=? AND obligation_id=?",
                                 (_j(fps), f"review {review_id}: {verdict}", now, P.id, task, oid))
                    changed.append(oid)
        return changed

    def link_finding(self, project, task, obligation_id, fingerprint) -> None:
        ob = self.get(project, task, obligation_id)
        if ob and fingerprint not in ob["findings"]:
            self.store.x("UPDATE obligations SET findings=? WHERE project_id=? AND task_id=? "
                         "AND obligation_id=?", (_j([*ob["findings"], fingerprint]), project,
                                                 task, obligation_id))

    # -- packages / phase --------------------------------------------------------------

    def open_items(self, P, t, phase: Optional[str] = None) -> list[dict]:
        return [o for o in self.all(P.id, _tid(t)) if o["status"] in ("open", "in_progress",
                "evidenced") and (phase is None or o["phase"] == phase)]

    def next_package(self, P, t, phase: str = "BUILD", max_items: int = 3) -> list[dict]:
        """The first dep-ready open obligations of `phase`. CLOSE work is never
        offered while any BUILD obligation is open (CLOSE paperwork is no
        substitute for BUILD validation)."""
        task = _tid(t)
        obs = self.all(P.id, task)
        if phase in ("CLOSE", "APPROVAL") and any(
                o["phase"] == "BUILD" and o["status"] in ("open", "in_progress", "evidenced")
                for o in obs):
            return []
        state = {o["obligation_id"]: o["status"] for o in obs}
        # an unknown dependency is unmet (reconcile flags it), never assumed done
        ready = [o for o in obs if o["phase"] == phase
                 and o["status"] in ("open", "in_progress", "evidenced")
                 and all(state.get(d) in ("verified", "waived", "superseded")
                         for d in o["deps"])]
        return ready[:max(1, int(max_items))]

    def set_phase(self, P, t, obligation_id: str, phase: str, decision_id: str) -> dict:
        task = _tid(t)
        phase = str(phase).upper()
        ob = self.get(P.id, task, obligation_id)
        if not ob or phase not in PHASES:
            raise PhaseLocked(f"unknown obligation {obligation_id} or phase {phase}")
        d = self.decision(decision_id)
        if not d or d["project_id"] != P.id or d["task_id"] != task:
            raise PhaseLocked("set_phase needs an answered obligation_phase decision of this task")
        if d["kind"] != "obligation_phase" or d["status"] != "answered":
            raise PhaseLocked(f"decision {decision_id} is not an answered obligation_phase")
        if d["authority"] not in AUTHORITIES:
            raise PhaseLocked("the decision was not answered by an authority")
        if ob["phase"] == "BUILD" and phase != "BUILD" and d["authority"] != "operator":
            raise PhaseLocked("moving BUILD work out of BUILD needs the operator")
        ans = d["answer"] if isinstance(d["answer"], dict) else {}
        if ans.get("obligation") != obligation_id or str(ans.get("phase", "")).upper() != phase:
            raise PhaseLocked("the decision's typed answer does not name this obligation/phase")
        self.store.x("UPDATE obligations SET phase=?, phase_decision=?, updated_ts=? WHERE "
                     "project_id=? AND task_id=? AND obligation_id=?",
                     (phase, decision_id, self.clock(), P.id, task, obligation_id))
        return self.get(P.id, task, obligation_id)

    def checklist(self, P, t) -> list[dict]:
        task = _tid(t)
        obs = self.all(P.id, task)
        ready = {o["obligation_id"] for ph in PHASES for o in self.next_package(P, task, ph, 999)}
        counts = {r["fingerprint"]: r["count"] for r in self.store.q(
            "SELECT fingerprint, count FROM findings WHERE project_id=? AND task_id=?",
            (P.id, task))}
        return [{"obligation_id": o["obligation_id"], "phase": o["phase"], "title": o["title"],
                 "status": o["status"], "verified_rev": o.get("verified_rev") or "",
                 "verified_by": o.get("verified_by") or "", "source": o["source"],
                 "required_evidence": o["required_evidence"], "deps": o["deps"],
                 "blocking_reason": o.get("blocking_reason") or "",
                 "findings": [{"fingerprint": f, "count": counts.get(f, 0)} for f in o["findings"]],
                 "ready": o["obligation_id"] in ready,
                 "phase_decision": o.get("phase_decision") or ""} for o in obs]

    # -- decisions ----------------------------------------------------------------------

    @staticmethod
    def _drow(r: dict) -> dict:
        r = dict(r)
        for k, d in (("evidence", {}), ("options", []), ("blocks", []), ("answer", None),
                     ("scope", {})):
            r[k] = _uj(r.get(k), d)
        return r

    def decision(self, decision_id: str) -> Optional[dict]:
        r = self.store.q("SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
        return self._drow(r[0]) if r else None

    def open_decision(self, project, task, kind, question, evidence=None, options=None,
                      recommendation="", blocks=None, scope=None, supersedes=None) -> str:
        """Idempotent on (project, task, kind, question hash): an open or answered
        decision with the same question is returned as is."""
        qhash = _sha(f"{kind}|{question}")
        with self.store.tx():
            rows = self.store.q("SELECT decision_id, status FROM decisions WHERE project_id=? "
                                "AND task_id=? AND kind=? AND qhash=? ORDER BY opened_ts",
                                (project, task, kind, qhash))
            for r in rows:
                if r["status"] in ("open", "answered"):
                    return r["decision_id"]
            did = "dec-" + _sha(f"{project}|{task}|{kind}|{qhash}|{len(rows)}", 16)
            self.store.x(
                "INSERT INTO decisions (decision_id, project_id, task_id, kind, question, "
                "evidence, options, recommendation, blocks, status, scope, supersedes, qhash, "
                "opened_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, project, task, kind, str(question)[:2000], _j(evidence or {}),
                 _j(list(options or [])), str(recommendation or "")[:1000],
                 _j(list(blocks or [])), "open", _j(scope or {}), supersedes, qhash,
                 self.clock()))
        return did

    def is_operator(self, actor: str, channel: Optional[str]) -> bool:
        """Positive rule: operator authority comes from the control center's
        operator session (channel 'control') or a configured operator name.
        The model-name denylist is an extra layer on top, never the rule."""
        if is_model_actor(actor):
            return False
        if channel == OPERATOR_CHANNEL:
            return True
        return str(actor or "").strip().lower() in self.operator_actors

    def _check_authority(self, d: dict, actor: str, authority: str, policy_id=None,
                         channel: Optional[str] = None) -> None:
        if authority not in AUTHORITIES:
            raise DecisionRefused(f"authority {authority!r} may not answer decisions "
                                  f"(operator or delegated_policy only; never a model)")
        if is_model_actor(actor):
            raise DecisionRefused(f"actor {actor!r} is a model/session; it cannot answer")
        if authority == "operator" and not self.is_operator(actor, channel):
            raise DecisionRefused(f"operator authority needs the control channel's operator "
                                  f"session (channel={channel!r}, actor={actor!r})")
        if authority == "delegated_policy":
            if d["kind"] in OPERATOR_ONLY_KINDS:
                raise DecisionRefused(f"a delegated policy cannot answer {d['kind']}")
            pol = self.decision(policy_id) if policy_id else None
            if not pol or pol["kind"] != "policy" or pol["status"] != "answered" or \
                    pol["authority"] != "operator" or pol["project_id"] != d["project_id"]:
                raise DecisionRefused("delegated_policy needs an operator-answered policy "
                                      "decision of this project")
            kinds = (pol.get("scope") or {}).get("kinds") or \
                ((pol.get("answer") or {}).get("kinds") if isinstance(pol.get("answer"), dict)
                 else None) or []
            tasks = (pol.get("scope") or {}).get("tasks")
            if d["kind"] not in kinds or (tasks and d["task_id"] not in tasks):
                raise DecisionRefused(f"policy {policy_id} does not cover {d['kind']} "
                                      f"for {d['task_id']}")

    def answer(self, decision_id: str, answer, actor: str, authority: str,
               policy_id: Optional[str] = None, *, channel: Optional[str] = None) -> dict:
        """Answer an open decision. authority='operator' is accepted only with
        channel='control' (the control center's decision_answer path) or an
        actor in operator_actors; 'delegated_policy' needs an operator-answered
        policy decision covering the kind."""
        d = self.decision(decision_id)
        if not d:
            raise DecisionRefused(f"unknown decision {decision_id}")
        self._check_authority(d, actor, authority, policy_id, channel)
        if d["status"] != "open":
            raise DecisionRefused(f"decision {decision_id} is {d['status']}; open a "
                                  f"superseding decision instead")
        # A contradicting active answer must be superseded explicitly.
        blocks = set(d["blocks"])
        for other in self.active_decisions(d["project_id"], d["task_id"]):
            if other["decision_id"] == decision_id or other["status"] != "answered" or \
                    other["kind"] != d["kind"] or other["decision_id"] == d.get("supersedes"):
                continue
            if (blocks & set(other["blocks"])) and _j(other["answer"]) != _j(answer):
                raise DecisionConflict(
                    f"{decision_id} contradicts answered {other['decision_id']}; open a "
                    f"decision with supersedes={other['decision_id']}")
        with self.store.tx():
            self.store.x("UPDATE decisions SET status='answered', answer=?, actor=?, "
                         "authority=?, answered_ts=? WHERE decision_id=?",
                         (_j(answer), actor, authority, self.clock(), decision_id))
            if d.get("supersedes"):
                self._mark_superseded(d["supersedes"], decision_id, d["project_id"],
                                      d["task_id"])
        return self.decision(decision_id)

    def _mark_superseded(self, old_id, new_id, project, task):
        old = self.decision(old_id)
        if not old or old["project_id"] != project or old["task_id"] != task:
            raise DecisionRefused(f"{old_id} is not a decision of {project}/{task}")
        self.store.x("UPDATE decisions SET status='superseded', superseded_by=? WHERE "
                     "decision_id=?", (new_id, old_id))

    def supersede(self, old_id: str, new_id: str, actor: str, authority: str, *,
                  channel: Optional[str] = None) -> None:
        new = self.decision(new_id)
        if not new:
            raise DecisionRefused(f"unknown decision {new_id}")
        self._check_authority(new, actor, authority if authority != "delegated_policy"
                              else "operator-required", channel=channel)
        with self.store.tx():
            self.store.x("UPDATE decisions SET supersedes=? WHERE decision_id=?",
                         (old_id, new_id))
            self._mark_superseded(old_id, new_id, new["project_id"], new["task_id"])

    def withdraw(self, decision_id: str, actor: str, authority: str, *,
                 channel: Optional[str] = None) -> None:
        d = self.decision(decision_id)
        if d:
            self._check_authority(d, actor, authority if authority != "delegated_policy"
                                  else "operator-required", channel=channel)
            self.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=?",
                         (decision_id,))

    def active_decisions(self, project, task, kind: Optional[str] = None) -> list[dict]:
        rows = self.store.q("SELECT * FROM decisions WHERE project_id=? AND task_id=? AND "
                            "status IN ('open','answered') ORDER BY opened_ts, decision_id",
                            (project, task))
        out = [self._drow(r) for r in rows]
        return [d for d in out if kind is None or d["kind"] == kind]

    def all_decisions(self, project, task) -> list[dict]:
        return [self._drow(r) for r in self.store.q(
            "SELECT * FROM decisions WHERE project_id=? AND task_id=? ORDER BY opened_ts, "
            "decision_id", (project, task))]

    def decisions_block(self, project, task, cap: int = 3000) -> str:
        """'Authorized decisions' for every prompt of the task (answered, active)."""
        ans = [d for d in self.active_decisions(project, task) if d["status"] == "answered"]
        if not ans:
            return ""
        lines = ["## Authorized decisions (operator; these stand until superseded)"]
        for d in ans:
            lines.append(f"- [{d['kind']}] {d['question'][:300]} -> {_j(d['answer'])[:300]} "
                         f"({d['authority']}, {d['decision_id']})")
        return "\n".join(lines)[:cap]

    # -- budget ledger -------------------------------------------------------------------

    def charge(self, project, task, counter: str, delta: int, reason: str, actor: str,
               decision_id: Optional[str] = None) -> int:
        delta = int(delta)
        if counter.startswith(AUTH_PREFIX):
            base = counter[len(AUTH_PREFIX):]
            if base not in COUNTERS or delta <= 0:
                raise LedgerRefused(f"bad authorization row {counter} {delta}")
            with self.store.tx():                  # check + row in one transaction
                self._require_authorization(project, task, decision_id, actor, base, delta)
                return self.store.x(
                    "INSERT INTO budget_ledger (project_id, task_id, counter, delta, reason, "
                    "decision_id, actor, ts) VALUES (?,?,?,?,?,?,?,?)",
                    (project, task, counter, delta, str(reason)[:500], decision_id, actor,
                     self.clock()))
        elif counter not in COUNTERS:
            raise LedgerRefused(f"unknown counter {counter}")
        elif delta < 0:
            raise LedgerRefused("usage counters never go down; record an authorized:<counter> "
                                "row tied to a decision or unblock instead")
        return self.store.x("INSERT INTO budget_ledger (project_id, task_id, counter, delta, "
                            "reason, decision_id, actor, ts) VALUES (?,?,?,?,?,?,?,?)",
                            (project, task, counter, delta, str(reason)[:500], decision_id,
                             actor, self.clock()))

    def _used(self, project, task, ref: str, counter: str) -> bool:
        return bool(self.store.q(
            "SELECT 1 FROM budget_ledger WHERE project_id=? AND task_id=? AND decision_id=? "
            "AND counter=?", (project, task, ref, AUTH_PREFIX + counter)))

    def _unblock_applied(self, project, task, ref: str, actor: str) -> bool:
        """`unblock:<n>`: the n-th (0-based) transition of the task is an
        applied operator unblock with reset_attempts, by `actor`."""
        num = ref.split(":", 1)[1] if ":" in ref else ""
        if not num.isdigit():
            return False
        rows = self.store.q("SELECT to_state, evidence, actor FROM transitions WHERE "
                            "project_id=? AND task_id=? ORDER BY id LIMIT 1 OFFSET ?",
                            (project, task, int(num)))
        if not rows:
            return False
        ev = _uj(rows[0].get("evidence"), {}) or {}
        return bool(isinstance(ev, dict) and ev.get("reset_attempts")) and \
            str(rows[0].get("actor") or "") == str(actor or "") and \
            str(rows[0].get("to_state") or "").upper() == "READY"

    def _require_authorization(self, project, task, decision_id, actor, counter: str = "",
                               n: int = 0) -> None:
        """An authorized:<counter> row needs (a) an answered budget_extension or
        reset decision of this task whose typed answer names `counter` and an
        n >= the requested n, used at most once per counter, or (b) an
        `unblock:<ref>` naming a real applied reset unblock (validated by the
        engine-provided unblock_validator, else a transitions lookup), used at
        most once per counter. Anything unvalidated is refused."""
        if not decision_id:
            raise LedgerRefused("an authorization needs a decision id or unblock:<action id>")
        if is_model_actor(actor):
            raise LedgerRefused(f"actor {actor!r} cannot authorize budget")
        if counter and self._used(project, task, decision_id, counter):
            raise LedgerRefused(f"{decision_id} already authorized {counter} once")
        if decision_id.startswith("unblock:"):
            if callable(self.unblock_validator):
                try:
                    ok = bool(self.unblock_validator(project, task, decision_id, actor))
                except Exception:                                 # noqa: BLE001
                    ok = False
            else:
                ok = self._unblock_applied(project, task, decision_id, actor)
            if not ok:
                raise LedgerRefused(f"{decision_id} is not an applied operator unblock with "
                                    f"reset_attempts of {project}/{task} by {actor!r}")
            return
        d = self.decision(decision_id)
        if not d or d["status"] != "answered" or d["authority"] not in AUTHORITIES or \
                d["project_id"] != project or d["task_id"] != task:
            raise LedgerRefused(f"{decision_id} is not an answered decision of {project}/{task}")
        if d["kind"] not in BUDGET_KINDS:
            raise LedgerRefused(f"{decision_id} is a {d['kind']} decision; budget needs "
                                f"{' or '.join(BUDGET_KINDS)}")
        ans = d["answer"] if isinstance(d["answer"], dict) else {}
        names = ans.get("counters") if isinstance(ans.get("counters"), list) else \
            [ans.get("counter")]
        names = {LIMITS[x][0] if x in LIMITS else x for x in names if x}
        if counter and counter not in names:
            raise LedgerRefused(f"{decision_id}'s answer does not name counter {counter}")
        if d["kind"] == "budget_extension" or "n" in ans:
            try:
                granted = int(ans.get("n"))
            except (TypeError, ValueError):
                raise LedgerRefused(f"{decision_id}'s answer grants no n") from None
            if granted < int(n):
                raise LedgerRefused(f"{decision_id} grants {granted} {counter}, {n} requested")

    def authorize(self, project, task, counter: str, n: int, reason: str, actor: str,
                  decision_id: str) -> int:
        """`n` extra uses of `counter`: needs an answered budget_extension (or
        reset) decision whose typed answer is {counter, n>=requested}, or an
        applied `unblock:<n>` reset. Each reference authorizes a counter once."""
        return self.charge(project, task, AUTH_PREFIX + counter, n, reason, actor, decision_id)

    def reset(self, P, t, counter: str, reason: str, actor: str, decision_id: str) -> int:
        """An operator reset: authorize exactly enough that the remaining room
        equals the base limit again. The raw count stays."""
        key = next(k for k, v in LIMITS.items() if v[0] == counter)
        lim = self.effective_limits(P, t)[key]
        n = lim["used"] - lim["authorized"]
        return self.authorize(P.id, _tid(t), counter, n, reason, actor, decision_id) if n > 0 else 0

    def counters(self, project, task) -> dict:
        rows = self.store.q("SELECT counter, SUM(delta) AS n FROM budget_ledger WHERE "
                            "project_id=? AND task_id=? GROUP BY counter", (project, task))
        return {r["counter"]: int(r["n"] or 0) for r in rows}

    def ledger(self, project, task) -> list[dict]:
        return self.store.q("SELECT * FROM budget_ledger WHERE project_id=? AND task_id=? "
                            "ORDER BY id", (project, task))

    def effective_limits(self, P, t) -> dict:
        task = _tid(t)
        trow = t if isinstance(t, dict) else (self.store.task(P.id, task) or {"data": {}})
        c = self.counters(P.id, task)
        out = {}
        for key, (counter, lim_key, default) in LIMITS.items():
            if key == "guided_extra":
                from .retry_guidance import RetryGuidance
                base = RetryGuidance.allowance(trow)
            else:
                base = _limit(P, lim_key, default)
            used, auth = c.get(counter, 0), c.get(AUTH_PREFIX + counter, 0)
            mx = base + auth
            out[key] = {"counter": counter, "used": used, "base_max": base, "authorized": auth,
                        "max": mx, "remaining": max(0, mx - used), "exhausted": used >= mx}
        o, g = out["ordinary"], out["guided_extra"]
        out["attempts"] = {"used": o["used"] + g["used"], "max": o["max"] + g["max"],
                           "remaining": o["remaining"] + g["remaining"]}
        return out

"""Playbook promotion pipeline (A1): deterministic triggers, no model call.

Two counters, cross-project, in playbook_candidates:

  fail:<stop class>|<signature>   bumped once per auto-heal key (the first retry
                                  of a head/class/signature) and once per resolved
                                  lead case; at FAIL_THRESHOLD (2) a fixes/ draft
                                  is rendered from the evidence and a low-priority
                                  decision opens ("add to playbook?", recommended
                                  "add", operator-only, blocks nothing)
  op:<normalized text hash>       bumped per operator instruction; at OP_THRESHOLD
                                  (3) the same flow drafts a howto/ entry

Counting always runs while a playbook is configured (so enabling the pipeline
later uses the history); a decision only opens when [automation]
playbook_promote is on for the observing project. On "add" the coordinator
commits the draft by explicit path; on "skip" the candidate is dismissed and
re-opens only after two further repeats. The draft text is template-rendered —
the trigger, the draft and the commit are all deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

FAIL_THRESHOLD = 2
OP_THRESHOLD = 3
REOPEN_EXTRA = 2                       # repeats after a "skip" before re-proposing
KIND = "playbook_promotion"
OPTION_ADD = "add it to the playbook"
OPTION_SKIP = "skip (ask again after further repeats)"
_SLUG = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"\S*\d\S*")      # tokens with digits: sprint ids, dates, counts
_WS = re.compile(r"\s+")


def _slug(text: str, fallback: str = "entry") -> str:
    s = _SLUG.sub("-", (text or "").lower()).strip("-")[:48].strip("-")
    return s or fallback


def normalize_instruction(text: str) -> str:
    """Lower-cased, whitespace-collapsed, digit-bearing tokens (sprint ids,
    dates, counts) removed: the same guidance phrased for another sprint still
    counts as a repeat."""
    s = _DIGITS.sub(" ", (text or "").lower())
    return _WS.sub(" ", s).strip()[:500]


class Promotion:
    def __init__(self, pb):
        self.pb = pb
        self.store = pb.store
        self.clock = getattr(pb, "clock", None) or time.time

    # ------------------------------------------------------------------ observing

    def observe_failure(self, P, task_id: str, signature: str, cls: str, reason: str) -> None:
        """One occurrence of a repaired failure (an auto-heal key's first retry,
        or a resolved lead case)."""
        if not self.pb.configured():
            return
        sig = (signature or "").strip().lower() or "unknown"
        cls = (cls or "").strip().lower() or "stop"
        key = f"fail:{cls}|{sig}"
        self._bump(P, task_id, key, "fix", FAIL_THRESHOLD, {
            "signature": sig, "class": cls, "example": (reason or "")[:300]})

    def observe_instruction(self, P, task_id: str, text: str, actor: str = "") -> None:
        """One operator instruction; the third repeat of the same normalized text
        proposes a howto/ entry."""
        if not self.pb.configured():
            return
        norm = normalize_instruction(text)
        if len(norm) < 12:                       # "continue", "go on": not knowledge
            return
        key = "op:" + hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]
        self._bump(P, task_id, key, "howto", OP_THRESHOLD, {
            "text": (text or "")[:600], "normalized": norm[:300], "actor": (actor or "")[:80]})

    def _bump(self, P, task_id: str, key: str, kind: str, threshold: int, ev: dict) -> None:
        now = self.clock()
        row = self.store.q("SELECT * FROM playbook_candidates WHERE key=?", (key,))
        if row:
            r = row[0]
            projects = sorted(set(json.loads(r["projects"] or "[]")) | {P.id})
            self.store.x("UPDATE playbook_candidates SET count=count+1, projects=?, project_id=?, "
                         "task_id=?, last_ts=?, evidence=?, updated_ts=? WHERE key=?",
                         (json.dumps(projects), P.id, task_id, now, json.dumps(ev), now, key))
            r = self.store.q("SELECT * FROM playbook_candidates WHERE key=?", (key,))[0]
        else:
            self.store.x("INSERT INTO playbook_candidates (key, kind, count, threshold, projects, "
                         "project_id, task_id, first_ts, last_ts, evidence, draft_file, "
                         "draft_heading, draft_text, decision_id, status, updated_ts) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (key, kind, 1, threshold, json.dumps([P.id]), P.id, task_id, now, now,
                          json.dumps(ev), "", "", "", "", "counting", now))
            r = self.store.q("SELECT * FROM playbook_candidates WHERE key=?", (key,))[0]
        if (r["status"] == "counting" and int(r["count"]) >= int(r["threshold"])
                and self.pb.promote_enabled(P)):
            self._propose(P, r)

    # ------------------------------------------------------------------ proposing

    def _draft(self, r: dict) -> tuple[str, str, str]:
        """(file, heading, text) of the template-rendered draft entry."""
        ev = json.loads(r["evidence"] or "{}")
        projects = ", ".join(json.loads(r["projects"] or "[]")) or "?"
        n = int(r["count"])
        if r["kind"] == "fix":
            sig, cls = ev.get("signature") or "unknown", ev.get("class") or "stop"
            file = f"fixes/{_slug(sig if sig != 'unknown' else cls)}.md"
            heading = f"{sig}: repaired {cls} stop"
            text = (f"Failure `{cls}` with signature `{sig}` was repaired {n} times "
                    f"(projects: {projects}). Example: {ev.get('example') or '?'} "
                    f"The coordinator's auto-heal / lead repair resolved it each time; "
                    f"verify the fix and record the cause and prevention here. "
                    f"(drafted by the promotion pipeline)")
        else:
            instr = (ev.get("text") or "").strip()
            file = "howto/operator-guidance.md"
            heading = " ".join(instr.split())[:80] or "repeated operator instruction"
            text = (f"The operator gave this instruction {n} times (projects: {projects}): "
                    f"\"{instr[:400]}\" Apply it as standing guidance. "
                    f"(drafted by the promotion pipeline)")
        return file, heading, text

    def _propose(self, P, r: dict) -> None:
        file, heading, text = self._draft(r)
        what = "repaired failure" if r["kind"] == "fix" else "operator instruction"
        question = (f"Playbook: the same {what} repeated {r['count']} times "
                    f"({r['key']}). Add the drafted entry to {file} > {heading}? "
                    f"Draft: {text[:400]}")
        try:
            did = self.pb.c.obligations.open_decision(
                r["project_id"], r["task_id"] or "", KIND, question,
                evidence={"candidate": r["key"], "file": file, "heading": heading,
                          "draft": text, "count": r["count"],
                          "projects": json.loads(r["projects"] or "[]")},
                options=[OPTION_ADD, OPTION_SKIP], recommendation=OPTION_ADD, blocks=[])
        except Exception as e:                                    # noqa: BLE001
            self.pb.log(f"playbook promotion decision failed: {type(e).__name__}: {e}")
            return
        self.store.x("UPDATE playbook_candidates SET draft_file=?, draft_heading=?, draft_text=?, "
                     "decision_id=?, status='proposed', updated_ts=? WHERE key=?",
                     (file, heading, text, did, self.clock(), r["key"]))
        self.pb._event(r["project_id"], r["task_id"] or "", "playbook_promotion_proposed", {
            "candidate": r["key"], "decision_id": did, "file": file, "heading": heading[:120],
            "count": r["count"]})

    # ------------------------------------------------------------------ applying answers

    def sweep(self) -> list[str]:
        """Apply answered playbook_promotion decisions: "add" commits the draft,
        "skip" dismisses the candidate until it repeats further. Returns the
        candidate keys that changed."""
        rows = self.store.q("SELECT * FROM playbook_candidates WHERE status='proposed' AND "
                            "decision_id != ''")
        if not rows:
            return []
        changed = []
        for r in rows:
            try:
                d = self.pb.c.obligations.decision(r["decision_id"])
            except Exception:                                     # noqa: BLE001
                continue
            if not d or d["status"] not in ("answered",):
                if d and d["status"] not in ("open", "answered"):
                    # withdrawn / superseded: count again from here
                    self.store.x("UPDATE playbook_candidates SET status='counting', "
                                 "decision_id='', updated_ts=? WHERE key=?",
                                 (self.clock(), r["key"]))
                    changed.append(r["key"])
                continue
            ans = d.get("answer")
            if isinstance(ans, dict):
                ans = ans.get("option") or ans.get("answer") or ""
            add = "add" in str(ans or "").strip().lower()[:24]
            if add:
                sha, err, _ = self.pb._apply(
                    [(r["draft_file"], r["draft_heading"], r["draft_text"], "append")],
                    f"promoted {r['key']} to {r['draft_file']}",
                    actor=str(d.get("actor") or "operator"),
                    project_id=r["project_id"] or "", task_id=r["task_id"] or "")
                if err:
                    self.pb.log(f"playbook promotion commit failed for {r['key']}: {err}")
                    continue                     # retried on the next sweep
                self.store.x("UPDATE playbook_candidates SET status='promoted', updated_ts=? "
                             "WHERE key=?", (self.clock(), r["key"]))
                self.pb._event(r["project_id"], r["task_id"] or "", "playbook_promoted", {
                    "candidate": r["key"], "file": r["draft_file"],
                    "heading": r["draft_heading"][:120], "sha": sha})
            else:
                self.store.x("UPDATE playbook_candidates SET status='counting', decision_id='', "
                             "threshold=count+?, updated_ts=? WHERE key=?",
                             (REOPEN_EXTRA, self.clock(), r["key"]))
                self.pb._event(r["project_id"], r["task_id"] or "", "playbook_promotion_skipped", {
                    "candidate": r["key"], "asks_again_at": int(r["count"]) + REOPEN_EXTRA})
            changed.append(r["key"])
        return changed

    # ------------------------------------------------------------------ views

    def view(self, limit: int = 30) -> list[dict]:
        rows = self.store.q("SELECT * FROM playbook_candidates ORDER BY updated_ts DESC LIMIT ?",
                            (limit,))
        out = []
        for r in rows:
            out.append({"key": r["key"], "kind": r["kind"], "count": r["count"],
                        "threshold": r["threshold"],
                        "projects": json.loads(r["projects"] or "[]"),
                        "status": r["status"], "decision_id": r["decision_id"],
                        "file": r["draft_file"], "heading": r["draft_heading"],
                        "draft": (r["draft_text"] or "")[:400],
                        "first_ts": r["first_ts"], "last_ts": r["last_ts"]})
        return out

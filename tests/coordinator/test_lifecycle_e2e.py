"""[lifecycle] end to end: evidence recovery, the submission gate, a bounded
repair assignment, independent review, closeout and advancement.

Deterministic and offline: isolated git repos, a scripted coding client whose
sessions leave Kilo-shaped tool parts in their transcripts (ToolClient), a
generic adapter subclass that declares required checks, chains and dependencies
and parses only the structured JSON a check tool returns (never prose), and a
fake Foundry reviewer. test_lifecycle_engine.py reuses these fakes."""

from __future__ import annotations

import json
import os
import textwrap
import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.project import GenericAdapter
from foundry_coordinator.adapters.scripted import ScriptedClient
from tests.coordinator.fixtures_evidence import msg, text, tool
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo, review_json,
                                       run_until, write_profile)

# task id -> [(check_id, chain_id, seq, [dependency globs])]
CHECKS = {
    "T1": [("core.tests", "", None, ["src/core.txt"]),
           ("ui.tests", "ui", 1, ["src/ui.txt"]),
           ("ui.shot_desktop", "ui", 2, ["src/ui.txt"]),
           ("ui.shot_mobile", "ui", 3, ["src/ui.txt"])],
    "T2": [("t2.tests", "", None, ["src/t2.txt"])],
}


class LifecycleAdapter(GenericAdapter):
    """The generic adapter plus the v0.122 evidence contract."""

    readiness_items: list = []           # class-level knob for readiness tests

    def _checks(self, task_id):
        return CHECKS.get(task_id, [])

    def required_checks(self, spec, phase="BUILD"):
        return [{"check_id": c, "chain_id": ch, "seq": seq, "prereqs": [], "runtime": False,
                 "min_tier": "verified"} for c, ch, seq, _ in self._checks(spec.task_id)]

    def check_dependencies(self, spec, check_id):
        for c, _, _, deps in self._checks(spec.task_id):
            if c == check_id:
                return list(deps)
        return None

    def classify_tool_result(self, view, ctx=None):
        out = view.get("output") or ""
        last = out.splitlines()[-1] if out else ""
        try:
            body = json.loads(last)
        except ValueError:
            return []
        if not isinstance(body, dict) or not body.get("suite"):
            return []
        cid = body["suite"]
        known = {c: (ch, seq) for cs in CHECKS.values() for c, ch, seq, _ in cs}
        ch, seq = known.get(cid, ("", None))
        res = "PASS" if body.get("fail") == 0 and body.get("pass", 0) > 0 else "FAIL"
        return [{"check_id": cid, "chain_id": ch, "seq": seq, "prereqs": [], "result": res,
                 "counts": {"pass": body.get("pass"), "fail": body.get("fail")},
                 "revision": body.get("head") or "", "exclusion": "", "runtime_hint": "",
                 "artifact_path": body.get("artifact") or ""}]

    def readiness(self, spec, phase="implement", ctx=None):
        return [dict(i) for i in type(self).readiness_items]


class ToolClient(ScriptedClient):
    """ScriptedClient whose script may return
        tools: [(check_id, passed, failed[, git ref the check ran at])]
        claim: "assistant prose"            (recorded as text, never credited)
    and every turn becomes one assistant message of tool parts in the session's
    transcript (the way Kilo returns them)."""

    def __init__(self, script, project_id=""):
        super().__init__(script, project_id)
        self.tx: dict[str, list] = {}
        self.outputs: dict[str, list] = {}
        self._n = 0

    def state(self, session_id, *, directory, since_ts):
        s = self.sessions.get(session_id)
        if s is None or not s.get("pending"):
            return super().state(session_id, directory=directory, since_ts=since_ts)
        captured: dict = {}
        orig = self.script

        def wrapped(ctx):
            out = orig(ctx) or {}
            captured.update(out)
            return out
        self.script = wrapped
        try:
            st = super().state(session_id, directory=directory, since_ts=since_ts)
        finally:
            self.script = orig
        self._record(session_id, directory, captured)
        return st

    def _record(self, sid, ws, out):
        parts = []
        for spec in out.get("tools") or []:
            cid, passed, failed = spec[:3]
            at = git(ws, "rev-parse", spec[3] if len(spec) > 3 else "HEAD")
            self._n += 1
            body = json.dumps({"suite": cid, "pass": passed, "fail": failed, "head": at})
            output = f"running {cid} ({passed + failed} cases)\n{body}"
            self.outputs.setdefault(cid, []).append(output)
            parts.append(tool(f"p{self._n}", cmd=f"run-check {cid}", output=output,
                              start=time.time() + self._n * 0.01))
        if out.get("claim"):
            self._n += 1
            parts.append(text(f"t{self._n}", out["claim"]))
        msgs = self.tx.setdefault(sid, [])
        msgs.append(msg(f"m{len(msgs) + 1}-{sid}", parts))

    def transcript(self, session_id, *, directory):
        return list(self.tx.get(session_id, []))


def lifecycle_extra(continuations: int = 2, extra: str = "") -> str:
    return textwrap.dedent(f'''\
        [adapter]
        module = "tests.coordinator.test_lifecycle_e2e:LifecycleAdapter"

        [lifecycle]
        enabled = true
        readiness_retry_seconds = 60

        [limits.recovery]
        missing_evidence = {continuations}
        ''') + textwrap.dedent(extra)


def report(status="DONE", summary="did it", **kw):
    return "```coordinator-report\n" + json.dumps({"status": status, "summary": summary, **kw}) \
        + "\n```"


# ======================================================================= the scenario

T1 = {"id": "T1", "title": "Gallery screen",
      "spec": "Build the gallery screen in src/ui.txt and the core in src/core.txt; keep the "
              "progress log docs/progress/T1_LOG.md current. The required checks are "
              "core.tests, ui.tests, ui.shot_desktop and ui.shot_mobile.",
      "scope": ["src/ui.txt", "src/core.txt", "docs/progress/T1_LOG.md"]}
T2 = {"id": "T2", "title": "Follow-up", "spec": "Write src/t2.txt containing OK.",
      "scope": ["src/t2.txt"], "deps": ["T1"]}


def _state(c, tid):
    return c.store.task("game", tid)["state"]


def _commit(ws, files: dict, message: str) -> str:
    for rel, body in files.items():
        p = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        git(ws, "add", rel)
    git(ws, "commit", "-q", "-m", message)
    return git(ws, "rev-parse", "HEAD")


ALL_GREEN = [("core.tests", 1, 0), ("ui.tests", 1, 0), ("ui.shot_desktop", 1, 0),
             ("ui.shot_mobile", 1, 0)]


def green_t1(ctx):
    """A T1 session that does everything right in one turn."""
    if ctx["task_id"] == "T2":
        return {"writes": {"src/t2.txt": "OK"}, "commit": "T2: x",
                "tools": [("t2.tests", 1, 0)], "final": report()}
    return {"writes": {"src/ui.txt": "OK", "src/core.txt": "OK",
                       "docs/progress/T1_LOG.md": "OK"}, "commit": "T1: work",
            "tools": list(ALL_GREEN), "final": report()}


class Scenario:
    """T1 attempt 1: the worker runs core.tests, ui.tests and ui.shot_desktop at
    c1, then changes src/ui.txt (c2) together with an abbreviated progress log,
    and never captures ui.shot_mobile. T1 attempt 2 (the repair) reruns only the
    ui chain in order and records the real results. T2 is trivial."""

    def __init__(self):
        self.prompts: dict = {}
        self.heads: dict = {}

    def __call__(self, ctx):
        tid, attempt, phase = ctx["task_id"], ctx["attempt"], ctx["phase"]
        self.prompts.setdefault((tid, attempt, phase), []).append(ctx["prompt"])
        ws = ctx["workspace"]
        if tid == "T2":
            return {"writes": {"src/t2.txt": "OK"}, "commit": "T2: follow-up",
                    "tools": [("t2.tests", 2, 0)], "final": report()}
        if attempt == 1:
            c1 = _commit(ws, {"src/ui.txt": "OK gallery v1", "src/core.txt": "OK core"},
                         "T1: gallery and core")
            self.heads["c1"] = c1
            return {"writes": {"src/ui.txt": "OK gallery v2 (spacing)",
                               "docs/progress/T1_LOG.md": "OK log\n- tests: all pass\n"},
                    "commit": "T1: spacing tweak and log",
                    "tools": [("core.tests", 12, 0, c1), ("ui.tests", 7, 0, c1),
                              ("ui.shot_desktop", 1, 0, c1)],
                    "claim": "All tests PASS and both screenshots are captured.",
                    "final": report(summary="all tests pass; screenshots done")}
        # the repair: rerun the ui chain at the current code, record the results
        log = ("OK log\n- core.tests 12/12 (reused, attempt 1)\n- ui.tests 7/7\n"
               "- ui.shot_desktop, ui.shot_mobile captured at HEAD\n")
        return {"writes": {"docs/progress/T1_LOG.md": log}, "commit": "T1: evidence log",
                "tools": [("ui.tests", 7, 0), ("ui.shot_desktop", 1, 0),
                          ("ui.shot_mobile", 1, 0)],
                "final": report(summary="reran the ui chain")}


@pytest.fixture()
def scenario(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1, T2])
    prof = write_profile(tmp_path, "game", root, extra=lifecycle_extra(continuations=0))
    script = Scenario()
    client = ToolClient(script, project_id="game")
    packets = []

    def reviewer(packet):
        packets.append(packet)
        return review_json(packet)
    ff = FakeFoundry(reviewer)
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    return c, client, ff, script, packets, root


def test_e2e_recovery_gate_repair_review_and_advancement(scenario):
    c, client, ff, script, packets, root = scenario
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")

    # -- attempt 1 ends; the gate refuses the review -------------------------------------
    assert run_until(c, lambda: _state(c, "T1") == S.CHANGES_REQUIRED)
    t = c.store.task("game", "T1")
    head = git(root, "rev-parse", "HEAD")
    # F: premature review is rejected - no review call happened
    assert ff.chats == [] and c.store.reviews("game", "T1") == []
    hist = c.store.history("game", "T1")
    assert hist[-1]["from_state"] == S.VERIFYING and \
        hist[-1]["evidence"]["submission_gate"] == "refused"
    findings = " ".join(t["data"]["findings"])
    # A: the report/log abbreviate the results ("all pass"); that prose is a claim,
    # never credited to any check
    spec = P.adapter.describe("T1")
    claims = [r for r in c.ledger.records("game", "T1") if r["tier"] == "claim"]
    assert claims and all(not r["check_id"] for r in claims)
    # B: the exact original results were recovered from the session's tool parts
    core, = [r for r in c.ledger.records("game", "T1", "core.tests")
             if r["source"] == "transcript"]
    assert c.ledger.read_blob(core["output_sha"]).decode() == client.outputs["core.tests"][0]
    assert core["source_revision"] == script.heads["c1"]
    # C: the ui chain is incomplete at head
    chains = {ch["chain_id"]: ch for ch in c.ledger.chain_status(P, spec, head)}
    assert chains["ui"]["status"] != "complete"
    # D: src/ui.txt changed after the capture -> ui.* stale; core.tests depends only on
    # src/core.txt, which the later commit didn't touch -> still fresh
    cur = c.ledger.current(P, spec, head)
    assert "core.tests" in cur and "ui.shot_desktop" not in cur and "ui.tests" not in cur
    shot = c.ledger.records("game", "T1", "ui.shot_desktop")[0]
    st, why = c.ledger.freshness(P, shot, head)
    assert st == "stale" and "src/ui.txt" in why
    miss = {m["check_id"]: m["reason"] for m in c.ledger.missing(P, spec, head)}
    assert miss["ui.shot_mobile"] == "missing" and miss["ui.shot_desktop"] == "stale"
    assert "core.tests" not in miss
    assert "ui.shot_mobile" in findings and "check core.tests" not in findings

    # -- attempt 2: the assignment covers only the missing/invalid work ------------------
    assert run_until(c, lambda: c.store.task("game", "T1")["attempt"] == 2)
    prompt, = [s["prompts"][0] for s in client.sessions.values()
               if s["metadata"]["task"] == "T1" and s["metadata"]["attempt"] == 2]
    assert "## Repair assignment (attempt 2)" in prompt
    assert "Already verified (reusable; do not rerun):\n- core.tests: PASS" in prompt
    a = c.recovery.repairs("game", "T1")[-1]["assignment"]
    rerun = set(a["affected_checks_to_rerun"])
    assert {"ui.tests", "ui.shot_desktop", "ui.shot_mobile"} <= rerun          # E
    assert "core.tests" not in rerun
    assert all(u["obligation_id"] != "check:core.tests" for u in a["unresolved"])

    # -- G/H: the repair produces valid evidence, an independent review, closeout --------
    assert run_until(c, lambda: _state(c, "T1") == S.COMPLETE, limit=60)
    head = git(root, "rev-parse", "HEAD")
    assert c.ledger.missing(P, P.adapter.describe("T1"), head) == []
    rev, = c.store.reviews("game", "T1")
    assert rev["verdict"] == "APPROVED" and rev["persona"] == "Foundry-Kilo-Sonnet"
    impl = {s["persona"] for s in c.store.sessions("game", "T1") if s["role"] == "implementer"}
    assert rev["persona"] not in impl
    # the packet carried the ledger's exact stored outputs
    assert len(packets) == 1 and "ui.shot_mobile: PASS" in packets[0]
    assert client.outputs["ui.shot_mobile"][-1].splitlines()[-1] in packets[0]
    lim = c.obligations.effective_limits(P, c.store.task("game", "T1"))
    assert lim["ordinary"]["used"] == 2 and lim["review_calls"]["used"] == 1
    assert all(o["status"] == "verified" for o in c.obligations.all("game", "T1")
               if o["phase"] == "BUILD")

    # -- I: the next approved dependent task starts automatically ------------------------
    assert run_until(c, lambda: _state(c, "T2") == S.COMPLETE, limit=40)
    path = [h["to_state"] for h in c.store.history("game", "T2")]
    assert path[:3] == [S.QUEUED, S.READY, S.IMPLEMENTING]


def test_e2e_same_session_continuation_before_review(tmp_path):
    """With continuations allowed, a missing capture is produced in the SAME
    session (one journaled prompt), no attempt is charged, no premature review."""
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root, extra=lifecycle_extra(continuations=2))
    turns = []

    def script(ctx):
        turns.append(ctx["session_id"])
        if len(turns) == 1:
            return {"writes": {"src/ui.txt": "OK v1", "src/core.txt": "OK",
                               "docs/progress/T1_LOG.md": "OK"}, "commit": "T1: work",
                    "tools": [("core.tests", 3, 0), ("ui.tests", 2, 0),
                              ("ui.shot_desktop", 1, 0)], "final": report()}
        assert "ui.shot_mobile" in ctx["prompt"] and "Missing before review" in ctx["prompt"]
        return {"tools": [("ui.shot_mobile", 1, 0)], "final": report(summary="captured")}
    client = ToolClient(script, project_id="game")
    ff = FakeFoundry()
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c, "T1") == S.COMPLETE, limit=40)
    assert len(set(turns)) == 1 and len(turns) == 2            # same session, two turns
    t = c.store.task("game", "T1")
    assert t["attempt"] == 1
    path = [h["to_state"] for h in c.store.history("game", "T1")]
    assert path[:6] == [S.QUEUED, S.READY, S.IMPLEMENTING, S.VERIFYING, S.IMPLEMENTING,
                        S.VERIFYING]
    lim = c.obligations.effective_limits(c.projects["game"], t)
    assert lim["missing_evidence_continuations"]["used"] == 1
    assert lim["ordinary"]["used"] == 1 and len(ff.chats) == 1


VISUAL_RULE = """
[[approval.rules]]
match = "visual"
kind = "visual"
stage = "after_close"
"""


@pytest.mark.parametrize("accept", [False, True])
def test_e2e_visual_pending_dependency_advances_only_by_policy(tmp_path, accept):
    t1 = {**T1, "tags": ["visual"]}
    root, _ = make_repo(tmp_path, "game", [t1, T2])
    accept_line = 'deps_accept_pending = ["visual"]' if accept else ""
    rules = "\n".join(["[approval]", accept_line, VISUAL_RULE])
    prof = write_profile(tmp_path, "game", root, extra=lifecycle_extra(extra=rules))
    c = coordinator(tmp_path, [prof], {"game": ToolClient(green_t1, project_id="game")})
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")
    assert run_until(c, lambda: _state(c, "T1") == S.AWAITING_HUMAN, limit=40)
    assert c.store.task("game", "T1")["data"]["stage"] == "after_close"
    for _ in range(4):
        c.tick()
    if accept:
        assert _state(c, "T2") != S.QUEUED
    else:
        assert _state(c, "T2") == S.QUEUED
        assert "waiting for T1" in (c.store.task("game", "T2")["next_action"] or "")


def test_e2e_state_doc_never_advances_a_coordinator_owned_dependency(tmp_path, monkeypatch):
    """T2's task source names T1, the queue doesn't: with [lifecycle] on, a
    'DONE' answer from adapter.dependency_done (a STATE.md row) does not satisfy
    a dependency the coordinator owns; only its own COMPLETE does."""
    t2 = {**T2}
    root, _ = make_repo(tmp_path, "game", [T1, t2])
    prof = write_profile(tmp_path, "game", root, extra=lifecycle_extra())
    c = coordinator(tmp_path, [prof], {"game": ToolClient(green_t1, project_id="game")})
    monkeypatch.setattr(LifecycleAdapter, "dependency_done", lambda self, d: True)
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", [], "victor")          # dependency NOT declared on the queue
    c.pause("game", "T1", "hold")                   # T1 exists but is not complete
    c.tick()
    t2row = c.store.task("game", "T2")
    assert t2row["state"] == S.BLOCKED and "needs T1" in t2row["blocker"]

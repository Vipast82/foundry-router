"""Obligations, decisions and the budget ledger (lifecycle, plain Store + fakes)."""
from types import SimpleNamespace

import pytest

from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.obligations import (DecisionConflict, DecisionRefused, LedgerRefused,
                                             Obligations, PhaseLocked, directive_violations,
                                             migrate)
from foundry_coordinator.store import Store


class FakeWS:
    def __init__(self, trees=None, texts=None, head="h2"):
        self.trees, self.texts, self._head = trees or {}, texts or {}, head

    def head(self):
        return self._head

    def blob_at(self, rev, path):
        return self.trees.get(rev, {}).get(path)

    def ls_tree_at(self, rev, d):
        d = d.rstrip("/") + "/"
        return sorted((p, b) for p, b in self.trees.get(rev, {}).items() if p.startswith(d))

    def show_at(self, rev, path):
        return self.texts.get(rev, {}).get(path)

    def changed_files(self, a, b):
        ta, tb = self.trees.get(a, {}), self.trees.get(b, {})
        return sorted(p for p in set(ta) | set(tb) if ta.get(p) != tb.get(p))


class AUAdapter:
    """An adapter with the obligations hook (a capture and a gate check)."""

    def obligations(self, spec):
        return [
            {"obligation_id": "cap-gallery_desktop", "phase": "BUILD",
             "title": "Gallery desktop capture", "source": "plan:12",
             "acceptance": "fresh capture of the gallery",
             "required_evidence": [{"kind": "blob_changed",
                                    "ref": "docs/evidence/UI-01/gallery_desktop.png"}]},
            {"obligation_id": "gate", "phase": "BUILD", "title": "GATE UI-01 PASS",
             "required_evidence": [{"kind": "check", "ref": "au.gate"}]},
            {"obligation_id": "close.summary", "phase": "CLOSE", "title": "SUMMARY doc",
             "required_evidence": []},
        ]

    def describe(self, task_id):
        return spec(task_id)


class PlainAdapter:
    """No obligations hook: the fallback seeds from required checks + CLOSE duties."""

    def required_checks(self, spec, phase):
        return [{"check_id": "au.tests", "chain_id": "c", "seq": 1, "prereqs": []},
                {"check_id": "au.gate", "chain_id": "c", "seq": 2, "prereqs": []}]

    def review_contract(self, spec):
        return {"phase": "build", "reviewed": [], "deferred": ["Write the SUMMARY", "Push"]}


# what EvidenceLedger.current() returns for a creditable check: a fresh,
# verified, untruncated PASS
GATE_PASS = {"result": "PASS", "tier": "verified", "truncated": 0, "freshness": "fresh",
             "record_id": "ev-gate"}


def spec(task_id="UI-01", scope=None):
    return TaskSpec(project_id="game", task_id=task_id, title="t", spec="",
                    scope=scope or ["src/**", "docs/evidence/UI-01/*"])


def project(pid="game", adapter=None, ws=None, limits=None):
    return SimpleNamespace(id=pid, adapter=adapter or AUAdapter(), ws=ws or FakeWS(),
                           profile=SimpleNamespace(limits=limits or {}, checks=[], raw={}))


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.sqlite"))


def task_row(store, pid="game", tid="UI-01", data=None, base="h1"):
    store.x("INSERT OR REPLACE INTO tasks (project_id, task_id, state, attempt, sessions, "
            "base_sha, data) VALUES (?,?,?,?,?,?,?)",
            (pid, tid, "IMPLEMENTING", 1, 1, base, __import__("json").dumps(data or {})))
    return store.task(pid, tid)


# -- seeding -----------------------------------------------------------------------------

def test_seed_from_adapter_hook_is_idempotent(store):
    ob, P = Obligations(store), project()
    assert ob.seed(P, spec()) == ["cap-gallery_desktop", "gate", "close.summary"]
    assert ob.seed(P, spec()) == []
    rows = {o["obligation_id"]: o for o in ob.all("game", "UI-01")}
    assert rows["cap-gallery_desktop"]["phase"] == "BUILD"
    assert rows["close.summary"]["phase"] == "CLOSE"
    assert all(o["status"] == "open" for o in rows.values())


def test_seed_fallback_checks_and_close_duties(store):
    ob, P = Obligations(store), project(adapter=PlainAdapter())
    ob.seed(P, spec())
    rows = {o["obligation_id"]: o for o in ob.all("game", "UI-01")}
    assert set(rows) == {"check:au.tests", "check:au.gate", "close.1", "close.2"}
    assert rows["check:au.gate"]["required_evidence"] == [{"kind": "check", "ref": "au.gate"}]
    assert rows["close.1"]["phase"] == rows["close.2"]["phase"] == "CLOSE"


# -- phase lock -----------------------------------------------------------------------------

STALE_NEXT = "NEXT: defer the reruns/captures to the CLOSE session (they belong to CLOSE)"


def test_stale_next_cannot_move_build_capture_to_close(store):
    ob, P = Obligations(store), project()
    ob.seed(P, spec())
    conflicts = ob.reconcile(P, spec(), "## Steps\n- [x] build gallery\n" + STALE_NEXT)
    moved = {c["obligation_id"] for c in conflicts if c["kind"] == "phase_move"}
    assert {"cap-gallery_desktop", "gate"} <= moved
    assert ob.get("game", "UI-01", "cap-gallery_desktop")["phase"] == "BUILD"
    decs = ob.active_decisions("game", "UI-01", "obligation_conflict")
    assert decs and all(d["status"] == "open" for d in decs)
    # idempotent: the same stale line does not open a second decision
    again = ob.reconcile(P, spec(), STALE_NEXT)
    assert {c["decision_id"] for c in again if c["kind"] == "phase_move"} <= \
        {d["decision_id"] for d in decs}
    # CLOSE work is never offered while BUILD is open
    assert ob.next_package(P, "UI-01", "CLOSE") == []


def test_summary_text_is_refused_too(store):
    ob, P = Obligations(store), project()
    ob.seed(P, spec())
    c = ob.screen_text(P, "UI-01", "Captures belong to the CLOSE session.", "last_report")
    assert [x["obligation_id"] for x in c] == ["cap-gallery_desktop"]
    assert ob.get("game", "UI-01", "cap-gallery_desktop")["phase"] == "BUILD"


def test_set_phase_needs_answered_operator_decision(store):
    ob, P = Obligations(store), project()
    ob.seed(P, spec())
    with pytest.raises(PhaseLocked):
        ob.set_phase(P, "UI-01", "gate", "CLOSE", "nope")
    d = ob.open_decision("game", "UI-01", "obligation_phase", "move gate?",
                         blocks=["gate"])
    with pytest.raises(PhaseLocked):          # still open
        ob.set_phase(P, "UI-01", "gate", "CLOSE", d)
    ob.answer(d, {"obligation": "gate", "phase": "CLOSE"}, "victor", "operator", channel="control")
    with pytest.raises(PhaseLocked):          # answer names another phase
        ob.set_phase(P, "UI-01", "gate", "REVIEW", d)
    assert ob.set_phase(P, "UI-01", "gate", "CLOSE", d)["phase"] == "CLOSE"


def test_reconcile_flags_capture_outside_scope_and_untracked_plan_capture(store):
    ob, P = Obligations(store), project()
    ob.seed(P, spec(scope=["src/**"]))
    c = ob.reconcile(P, spec(scope=["src/**"]),
                     "Acceptance: capture docs/evidence/UI-01/gallery_mobile.png")
    kinds = {x["kind"] for x in c}
    assert {"evidence_outside_scope", "plan_requirement_untracked"} <= kinds


# -- evaluation -------------------------------------------------------------------------------

def test_evaluate_blob_changed_and_check(store):
    ws = FakeWS(trees={"h1": {"docs/evidence/UI-01/gallery_desktop.png": "b-old"},
                       "h2": {"docs/evidence/UI-01/gallery_desktop.png": "b-old"},
                       "h3": {"docs/evidence/UI-01/gallery_desktop.png": "b-new"}})
    ob, P = Obligations(store), project(ws=ws)
    t = task_row(store)
    ob.seed(P, spec())
    r = ob.evaluate(P, t, "h2", {"au.gate": GATE_PASS}, [])
    assert "gate" in r["verified"] and "cap-gallery_desktop" in r["open"]
    assert "unchanged" in ob.get("game", "UI-01", "cap-gallery_desktop")["blocking_reason"]
    r = ob.evaluate(P, t, "h3", {"au.gate": GATE_PASS}, [])
    assert "cap-gallery_desktop" in r["verified"]
    r = ob.evaluate(P, t, "h3", {}, [{"check_id": "au.gate", "reason": "stale"}])
    assert r["regressed"] == ["gate"]


def test_close_phase_evidence_never_credits_build(store):
    ob, P = Obligations(store), project()
    t = task_row(store)
    ob.seed(P, spec())
    r = ob.evaluate(P, t, "h2", {"au.gate": {**GATE_PASS, "phase": "CLOSE"}}, [])
    assert "gate" in r["open"]


def test_guidance_lines_that_waive_or_substitute_are_detected():
    assert directive_violations("Skip the gallery capture check, it is flaky")
    assert directive_violations("Defer the captures to the CLOSE session")
    assert directive_violations("The SUMMARY and done_check output count as the BUILD validation")
    assert directive_violations("Mark au.gate as optional")
    assert not directive_violations("Never waive a check or change a baseline without raw runs.")
    assert not directive_violations("Do not skip the gate; run every test.")
    assert directive_violations("Never waive a check; skip the gallery capture test.")
    assert not directive_violations("Rerun the gate at the final commit and re-capture frame 3")
    assert not directive_violations("Remove the .kilo scratch file, then rerun the checks")


# -- decisions ----------------------------------------------------------------------------------

def test_decision_answers_need_operator_authority(store):
    ob = Obligations(store)
    d = ob.open_decision("game", "UI-01", "budget_extension", "two more attempts?",
                         blocks=["attempts"])
    assert ob.open_decision("game", "UI-01", "budget_extension", "two more attempts?") == d
    for actor, auth in (("victor", "model"), ("victor", "coordinator"),
                        ("model:qwen", "operator"), ("session-ses_1", "operator"),
                        ("", "operator")):
        with pytest.raises(DecisionRefused):
            ob.answer(d, {"counter": "attempt_ordinary", "n": 2}, actor, auth,
                      channel="control")
    assert ob.decision(d)["status"] == "open"
    ob.answer(d, {"counter": "attempt_ordinary", "n": 2}, "victor", "operator", channel="control")
    with pytest.raises(DecisionRefused):          # answered once; supersede to change
        ob.answer(d, {"n": 3}, "victor", "operator", channel="control")


def test_delegated_policy_scope(store):
    ob = Obligations(store)
    pol = ob.open_decision("game", "UI-01", "policy", "auto-answer flaky checks",
                           scope={"kinds": ["flaky_check"]})
    with pytest.raises(DecisionRefused):          # a policy itself needs the operator
        ob.answer(pol, {"kinds": ["flaky_check"]}, "policybot", "delegated_policy",
                  policy_id=pol)
    ob.answer(pol, {"kinds": ["flaky_check"]}, "victor", "operator", channel="control")
    f = ob.open_decision("game", "UI-01", "flaky_check", "au.tests flaky?")
    ob.answer(f, {"rerun": 2}, "policy:flaky", "delegated_policy", policy_id=pol)
    s = ob.open_decision("game", "UI-01", "scope_change", "touch src/x?")
    with pytest.raises(DecisionRefused):
        ob.answer(s, {"ok": True}, "policy:flaky", "delegated_policy", policy_id=pol)


def test_conflicting_decisions_use_supersession(store):
    ob = Obligations(store)
    d1 = ob.open_decision("game", "UI-01", "budget_extension", "extend attempts by 2?",
                          blocks=["attempts"])
    ob.answer(d1, {"counter": "attempt_ordinary", "n": 2}, "victor", "operator", channel="control")
    d2 = ob.open_decision("game", "UI-01", "budget_extension", "extend attempts by 4?",
                          blocks=["attempts"])
    with pytest.raises(DecisionConflict):
        ob.answer(d2, {"counter": "attempt_ordinary", "n": 4}, "victor", "operator", channel="control")
    d3 = ob.open_decision("game", "UI-01", "budget_extension", "replace: extend by 4",
                          blocks=["attempts"], supersedes=d1)
    ob.answer(d3, {"counter": "attempt_ordinary", "n": 4}, "victor", "operator", channel="control")
    active = {d["decision_id"] for d in ob.active_decisions("game", "UI-01")}
    assert d3 in active and d1 not in active
    audit = {d["decision_id"]: d for d in ob.all_decisions("game", "UI-01")}
    assert audit[d1]["status"] == "superseded" and audit[d1]["superseded_by"] == d3
    assert "replace: extend by 4" in ob.decisions_block("game", "UI-01")
    assert "extend attempts by 2" not in ob.decisions_block("game", "UI-01")


def test_decisions_carry_across_instances(store, tmp_path):
    d = Obligations(store).open_decision("game", "UI-01", "approval", "ok?")
    Obligations(store).answer(d, "yes", "victor", "operator", channel="control")
    again = Obligations(Store(str(tmp_path / "c.sqlite")))
    assert again.decision(d)["answer"] == "yes"
    assert "ok?" in again.decisions_block("game", "UI-01")


# -- budget ledger -------------------------------------------------------------------------------

def test_effective_limits_with_guided_extra_attempt(store):
    ob = Obligations(store)
    P = project(limits={"max_attempts": 3, "recovery": {"transport": 1}})
    t = task_row(store, data={"retry_guidance": {"status": "repair", "extra_attempts": 1}})
    for _ in range(3):
        ob.charge("game", "UI-01", "attempt_ordinary", 1, "attempt", "coordinator")
    lim = ob.effective_limits(P, t)
    assert lim["ordinary"] == {**lim["ordinary"], "used": 3, "max": 3, "exhausted": True}
    assert lim["guided_extra"]["max"] == 1 and lim["guided_extra"]["remaining"] == 1
    assert lim["attempts"] == {"used": 3, "max": 4, "remaining": 1}
    assert lim["transport_retries"]["max"] == 1
    assert lim["missing_evidence_continuations"]["max"] == 2
    ob.charge("game", "UI-01", "attempt_guided", 1, "guided", "coordinator")
    assert ob.effective_limits(P, t)["attempts"]["remaining"] == 0
    # without a granted repair there is no guided room
    t2 = task_row(store, tid="UI-02")
    assert ob.effective_limits(P, t2)["guided_extra"]["max"] == 0


def test_ledger_never_goes_down_without_a_decision(store):
    ob, P = Obligations(store), project()
    t = task_row(store)
    ob.charge("game", "UI-01", "sessions", 1, "start", "coordinator")
    with pytest.raises(LedgerRefused):
        ob.charge("game", "UI-01", "sessions", -1, "compaction", "coordinator")
    with pytest.raises(LedgerRefused):
        ob.authorize("game", "UI-01", "attempt_ordinary", 2, "please", "coordinator", None)
    with pytest.raises(LedgerRefused):
        ob.authorize("game", "UI-01", "attempt_ordinary", 2, "x", "model:qwen", "unblock:a1")
    d = ob.open_decision("game", "UI-01", "budget_extension", "2 more?")
    with pytest.raises(LedgerRefused):            # not answered yet
        ob.authorize("game", "UI-01", "attempt_ordinary", 2, "x", "victor", d)
    ob.answer(d, {"counter": "attempt_ordinary", "n": 2}, "victor", "operator", channel="control")
    ob.authorize("game", "UI-01", "attempt_ordinary", 2, "operator grant", "victor", d)
    for _ in range(3):
        ob.charge("game", "UI-01", "attempt_ordinary", 1, "attempt", "coordinator")
    lim = ob.effective_limits(P, t)["ordinary"]
    assert (lim["used"], lim["max"], lim["remaining"]) == (3, 5, 2)
    with pytest.raises(LedgerRefused):            # names no applied unblock
        ob.reset(P, t, "attempt_ordinary", "unblock --reset-attempts", "victor", "unblock:act-9")
    ref = apply_unblock(store, actor="victor")
    ob.reset(P, t, "attempt_ordinary", "unblock --reset-attempts", "victor", ref)
    lim = ob.effective_limits(P, t)["ordinary"]
    assert (lim["used"], lim["remaining"]) == (3, 3)
    assert ob.ledger("game", "UI-01")[-1]["decision_id"] == ref


def apply_unblock(store, pid="game", tid="UI-01", actor="victor", reset=True) -> str:
    """Record an applied operator unblock transition; returns its unblock:<n> ref
    (n = the number of transitions before it, as Coordinator.unblock numbers it)."""
    n = store.q("SELECT COUNT(*) AS n FROM transitions WHERE project_id=? AND task_id=?",
                (pid, tid))[0]["n"]
    store.x("INSERT INTO transitions (ts, project_id, task_id, from_state, to_state, evidence, "
            "actor) VALUES (?,?,?,?,?,?,?)",
            (1.0, pid, tid, "BLOCKED", "READY",
             __import__("json").dumps({"resolution": "x", "reset_attempts": reset}), actor))
    return f"unblock:{n}"


def test_ledger_survives_restart_and_compaction(store, tmp_path):
    ob, P = Obligations(store), project()
    t = task_row(store)
    for c in ("attempt_ordinary", "sessions", "closeout", "review_calls", "provider_retries",
              "same_session_continuations"):
        ob.charge("game", "UI-01", c, 1, "x", "coordinator")
    before = ob.effective_limits(P, t)
    # "compaction": a new session row and a re-seed change nothing
    ob.seed(P, spec())
    store2 = Store(str(tmp_path / "c.sqlite"))          # "restart"
    after = Obligations(store2).effective_limits(P, store2.task("game", "UI-01"))
    assert after == before
    assert after["closeout"]["used"] == 1 and after["sessions"]["used"] == 1


# -- isolation / migration ----------------------------------------------------------------------------

def test_cross_project_isolation(store):
    ob = Obligations(store)
    A, B = project("a"), project("b")
    ob.seed(A, spec())
    ob.charge("a", "UI-01", "attempt_ordinary", 1, "x", "coordinator")
    d = ob.open_decision("a", "UI-01", "approval", "same question")
    assert ob.all("b", "UI-01") == []
    assert ob.counters("b", "UI-01") == {}
    assert ob.active_decisions("b", "UI-01") == []
    db = ob.open_decision("b", "UI-01", "approval", "same question")
    assert db != d
    ob.answer(d, "yes", "victor", "operator", channel="control")
    assert ob.decision(db)["status"] == "open"
    with pytest.raises(LedgerRefused):     # a decision of project a can't authorize b
        ob.authorize("b", "UI-01", "attempt_ordinary", 1, "x", "victor", d)


def test_migration_on_existing_db(tmp_path):
    path = str(tmp_path / "old.sqlite")
    s = Store(path)
    # an older shape: v1 tables without the v2 columns, with data
    s.conn.executescript("""
      CREATE TABLE decisions (decision_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT,
        kind TEXT, question TEXT, evidence TEXT, options TEXT, recommendation TEXT, blocks TEXT,
        status TEXT, answer TEXT, authority TEXT, actor TEXT, scope TEXT, supersedes TEXT,
        superseded_by TEXT, opened_ts REAL, answered_ts REAL);
      INSERT INTO decisions (decision_id, project_id, task_id, kind, question, status, blocks)
        VALUES ('dec-old', 'game', 'UI-01', 'approval', 'old?', 'open', '[]');
    """)
    s.set_meta("schema:obligations", "1")
    ob = Obligations(s)
    assert s.meta("schema:obligations") == "2"
    cols = {r["name"] for r in s.q("PRAGMA table_info(decisions)")}
    assert "qhash" in cols
    assert ob.decision("dec-old")["question"] == "old?"
    assert migrate(s) == 2 and migrate(s) == 2          # idempotent
    ob.answer("dec-old", "yes", "victor", "operator", channel="control")
    # existing core tables are untouched
    assert s.meta("schema_version") == "1"


# -- v0.122 review regressions ------------------------------------------------------------

class GateOnly:
    def __init__(self, obs):
        self._obs = obs

    def obligations(self, spec):
        return self._obs


def _gate_ob(min_tier=None):
    e = {"kind": "check", "ref": "au.gate"}
    if min_tier:
        e["min_tier"] = min_tier
    return {"obligation_id": "gate", "phase": "BUILD", "title": "gate", "required_evidence": [e]}


@pytest.mark.parametrize("rec,min_tier", [
    ({**GATE_PASS, "result": "FAIL"}, None),
    ({**GATE_PASS, "result": "UNKNOWN"}, None),
    ({**GATE_PASS, "tier": "observed"}, None),
    ({**GATE_PASS, "truncated": 1}, None),
    ({**GATE_PASS, "freshness": "stale"}, None),
    ({k: v for k, v in GATE_PASS.items() if k != "freshness"}, None),
    ({**GATE_PASS, "flaky": True}, None),
    (GATE_PASS, "reviewed"),
])
def test_check_evidence_needs_a_fresh_untruncated_pass_at_min_tier(store, rec, min_tier):
    # review item 4: any record in current used to verify (FAIL/observed included)
    ob, P = Obligations(store), project(adapter=GateOnly([_gate_ob(min_tier)]))
    t = task_row(store)
    ob.seed(P, spec())
    for missing in (None, []):                       # evidence_missing None too
        r = ob.evaluate(P, t, "h2", {"au.gate": rec}, missing)
        assert r["verified"] == [] and r["open"] == ["gate"]
    assert ob.evaluate(P, t, "h2", {"au.gate": {**GATE_PASS, "tier": "reviewed"}},
                       None)["verified"] == ["gate"]


def test_close_session_evidence_never_credits_build(store, tmp_path):
    from foundry_coordinator.evidence import EvidenceLedger
    from tests.coordinator.fixtures_evidence import (FakeAdapter, make_P, make_ws, msg,
                                                     suite_output, tool)
    ws, r1 = make_ws(tmp_path, {"src/a.lua": "1"})
    store.add_session("sc", "game", "UI-01", "run1", "closer", "CLOSE", 1, "kilo", "p",
                      data={"head": r1})
    led = EvidenceLedger(store, str(tmp_path / "st"))
    LP = make_P("game", ws, {"sc": [msg("m", [tool("a", "run", suite_output("au.gate", 3))])]},
                FakeAdapter(required=[{"check_id": "au.gate"}], deps={"au.gate": ["src/**"]}))
    led.recover(LP, {"task_id": "UI-01"})
    cur = led.current(LP, spec(), r1)
    assert cur["au.gate"]["result"] == "PASS" and cur["au.gate"]["phase"] == "CLOSE"
    ob, P = Obligations(store), project(adapter=GateOnly([_gate_ob()]))
    ob.seed(P, spec())
    r = ob.evaluate(P, task_row(store), r1, cur, led.missing(LP, spec(), r1))
    assert r["open"] == ["gate"]
    assert "CLOSE-phase" in ob.get("game", "UI-01", "gate")["blocking_reason"]


def test_budget_needs_a_budget_decision_naming_the_counter_once(store):
    # review item 6
    ob = Obligations(store)
    task_row(store)
    other = ob.open_decision("game", "UI-01", "obligation_conflict", "keep BUILD?")
    ob.answer(other, {"choice": "keep BUILD"}, "victor", "operator", channel="control")
    with pytest.raises(LedgerRefused):            # an answered decision of another kind
        ob.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "victor", other)
    d = ob.open_decision("game", "UI-01", "budget_extension", "2 more sessions?")
    ob.answer(d, {"counter": "sessions", "n": 2}, "victor", "operator", channel="control")
    with pytest.raises(LedgerRefused):            # names another counter
        ob.authorize("game", "UI-01", "attempt_ordinary", 2, "x", "victor", d)
    with pytest.raises(LedgerRefused):            # more than granted
        ob.authorize("game", "UI-01", "sessions", 1000, "x", "victor", d)
    ob.authorize("game", "UI-01", "sessions", 2, "grant", "victor", d)
    with pytest.raises(LedgerRefused):            # used once
        ob.authorize("game", "UI-01", "sessions", 2, "again", "victor", d)
    assert ob.counters("game", "UI-01") == {"authorized:sessions": 2}
    assert [r["decision_id"] for r in ob.ledger("game", "UI-01")] == [d]
    # a typed reset decision names its counter(s); n is computed
    rs = ob.open_decision("game", "UI-01", "reset", "reset attempts?")
    ob.answer(rs, {"counters": ["ordinary"]}, "victor", "operator", channel="control")
    ob.authorize("game", "UI-01", "attempt_ordinary", 3, "reset", "victor", rs)


def test_unblock_reference_must_name_an_applied_reset_unblock(store):
    ob = Obligations(store)
    task_row(store)
    for ref in ("unblock:anything", "unblock:0", "unblock:"):
        with pytest.raises(LedgerRefused):
            ob.authorize("game", "UI-01", "attempt_ordinary", 1000, "x", "operator", ref)
    no_reset = apply_unblock(store, actor="victor", reset=False)
    with pytest.raises(LedgerRefused):
        ob.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "victor", no_reset)
    ref = apply_unblock(store, actor="victor")
    with pytest.raises(LedgerRefused):            # another actor
        ob.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "mallory", ref)
    ob.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "victor", ref)
    ob.authorize("game", "UI-01", "sessions", 1, "x", "victor", ref)    # once per counter
    with pytest.raises(LedgerRefused):
        ob.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "victor", ref)
    # an engine-provided validator decides instead of the transitions lookup
    seen = []
    ob2 = Obligations(store, unblock_validator=lambda *a: seen.append(a) or a[2] == "unblock:x1")
    ob2.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "victor", "unblock:x1")
    with pytest.raises(LedgerRefused):
        ob2.authorize("game", "UI-01", "attempt_ordinary", 1, "x", "victor", "unblock:x2")
    assert seen[0] == ("game", "UI-01", "unblock:x1", "victor")


@pytest.mark.parametrize("actor,kw", [
    ("victor", {}),                                   # no channel: not the operator session
    ("victor", {"channel": "engine"}),
    ("claude-opus", {"channel": "control"}),
    ("Claude-Opus-4", {"channel": "control"}),
    ("gpt-5", {"channel": "control"}),
    ("qwen3-coder", {"channel": "control"}),
    ("codex", {"channel": "control"}),
    ("coordinator", {"channel": "control"}),
    ("", {"channel": "control"}),
])
def test_operator_authority_is_a_positive_rule(store, actor, kw):
    # review item 7
    ob = Obligations(store)
    d = ob.open_decision("game", "UI-01", "scope_change", "widen?")
    with pytest.raises(DecisionRefused):
        ob.answer(d, {"ok": True}, actor, "operator", **kw)
    assert ob.decision(d)["status"] == "open"


def test_operator_allowlist_and_control_channel_accept(store):
    ob = Obligations(store, operator_actors=["Victor"])
    d1 = ob.open_decision("game", "UI-01", "scope_change", "widen a?")
    assert ob.answer(d1, {"ok": True}, "victor", "operator")["status"] == "answered"
    d2 = ob.open_decision("game", "UI-01", "scope_change", "widen b?")
    with pytest.raises(DecisionRefused):            # not on the allowlist, no channel
        ob.answer(d2, {"ok": True}, "alice", "operator")
    assert ob.answer(d2, {"ok": True}, "alice", "operator",
                     channel="control")["status"] == "answered"
    assert not ob.is_operator("gpt-5", "control") and ob.is_operator("victor", None)


def test_evidence_free_build_obligation_is_seeded_as_review(store):
    # review item 8: a BUILD acceptance prose line with no evidence blocked forever
    from foundry_coordinator.recovery import Recovery
    obs = [_gate_ob(),
           {"obligation_id": "build.accept.A1", "phase": "BUILD", "title": "Player opens the shop",
            "source": "plan:docs/p.md#Acceptance", "required_evidence": []}]
    ob, P = Obligations(store), project(adapter=GateOnly(obs))
    t = task_row(store)
    ob.seed(P, spec())
    a1 = ob.get("game", "UI-01", "build.accept.A1")
    assert a1["phase"] == "REVIEW" and a1["source"] == "plan:docs/p.md#Acceptance"
    assert a1["title"] == "Player opens the shop" and "independent review" in a1["blocking_reason"]
    assert not [c for c in ob.reconcile(P, spec(), "") if c["kind"] == "phase_drift"]
    P.adapter.describe = lambda tid: spec(tid)

    class Led:
        def current(self, P, spec, rev):
            return {"au.gate": GATE_PASS}

        def missing(self, P, spec, rev):
            return []
    assert Recovery(store).gate_submission(P, t, "h2", Led(), ob) == []
    assert ob.review_link(P, t, "rev-1", "APPROVED", "h2") == ["build.accept.A1"]
    assert ob.get("game", "UI-01", "build.accept.A1")["status"] == "verified"


def test_unknown_evidence_kind_is_an_explicit_conflict(store):
    obs = [{"obligation_id": "build.x", "phase": "BUILD", "title": "x",
            "required_evidence": [{"kind": "screenshot_diff", "ref": "a.png"},
                                  {"kind": "check", "ref": "au.gate"}]}]
    ob, P = Obligations(store), project(adapter=GateOnly(obs))
    t = task_row(store)
    ob.seed(P, spec())
    row = ob.get("game", "UI-01", "build.x")
    assert row["phase"] == "BUILD" and [e["kind"] for e in row["required_evidence"]] == \
        ["check", "unknown"]
    (d,) = ob.active_decisions("game", "UI-01", "obligation_conflict")
    assert "screenshot_diff" in d["question"] and d["blocks"] == ["build.x"]
    r = ob.evaluate(P, t, "h2", {"au.gate": GATE_PASS}, [])
    assert r["open"] == ["build.x"]                  # never silently relaxed
    assert ob.seed(P, spec()) == [] and \
        len(ob.active_decisions("game", "UI-01", "obligation_conflict")) == 1


def test_unknown_dependency_is_unmet_and_flagged(store):
    # review item 9
    obs = [{"obligation_id": "build.dep", "phase": "BUILD", "title": "d",
            "deps": ["does.not.exist"], "required_evidence": [{"kind": "check", "ref": "y"}]},
           _gate_ob()]
    ob, P = Obligations(store), project(adapter=GateOnly(obs))
    ob.seed(P, spec())
    assert [o["obligation_id"] for o in ob.next_package(P, "UI-01", "BUILD", 5)] == ["gate"]
    c = [x for x in ob.reconcile(P, spec(), "") if x["kind"] == "unknown_dependency"]
    assert c and c[0]["obligation_id"] == "build.dep" and c[0]["dep"] == "does.not.exist"


def test_migration_does_not_commit_the_callers_transaction(tmp_path):
    s = Store(str(tmp_path / "m.sqlite"))
    with pytest.raises(RuntimeError):
        with s.tx():
            s.set_meta("probe", "1")
            Obligations(s)                           # migrates inside the open tx
            raise RuntimeError("roll back")
    assert s.meta("probe") is None


def test_obligation_globs_do_not_cross_a_slash(store):
    ws = FakeWS(trees={"h2": {"docs/evidence/UI-01/a.png": "b1",
                              "docs/evidence/UI-01/old/b.png": "b2"}})
    obs = [{"obligation_id": "caps", "phase": "BUILD", "title": "caps",
            "required_evidence": [{"kind": "path", "ref": "docs/evidence/UI-01/*.png"}]}]
    ob, P = Obligations(store), project(adapter=GateOnly(obs), ws=ws)
    files = ob._files_at(ws, "h2", "docs/evidence/UI-01/*.png")
    assert files == [("docs/evidence/UI-01/a.png", "b1")]


def test_closeout_files_are_never_an_outside_scope_conflict(store):
    """UI-D1 / UI-02a (2026-10-20): the plan, SUMMARY and STATE.md are written by the CLOSE
    session for every task; their obligations asked the operator every sprint."""
    class CloseoutAdapter(AUAdapter):
        def exempt_paths(self, spec):
            return ["docs/evidence/UI-01/*"]          # here: the capture's folder
    ob, P = Obligations(store), project(adapter=CloseoutAdapter())
    ob.seed(P, spec(scope=["src/**"]))
    kinds = {x["kind"] for x in ob.reconcile(P, spec(scope=["src/**"]), "")}
    assert "evidence_outside_scope" not in kinds
    # without the exemption the same evidence is still a conflict
    ob2, P2 = Obligations(store), project(pid="other")
    ob2.seed(P2, spec(scope=["src/**"]))
    assert "evidence_outside_scope" in {x["kind"] for x in ob2.reconcile(P2, spec(scope=["src/**"]), "")}

"""2026-11-14 time-save item 0: [studio_calls] defer_guard_suites. Mid-build, the repair
assignment asks only for the sprint's own and red suites; stale regression guards wait for the
final gate, which still requires and requests them (default_request / verify_hold) and the
submission gate (obligations build.suite.*) is untouched."""
from types import SimpleNamespace

from foundry_coordinator import studio_calls as SC
from foundry_coordinator.obligations import Obligations
from foundry_coordinator.recovery import Recovery
from foundry_coordinator.store import Store
from tests.coordinator.test_obligations import FakeWS, project, spec, task_row

INDEX = ('local entries = { { id = "UX_1", file = "checks/UX_1_check" }, '
         '{ id = "G1", file = "checks/G1_check" }, { id = "G2", file = "checks/G2_check" }, '
         '{ id = "G3", file = "checks/G3_check" }, { id = "G4", file = "G4_check" }, '
         '{ id = "G5", file = "checks/G5_check" } }')
SUITES = ["UX_1", "G1", "G2", "G3", "G4", "G5"]


class Ledger:
    """EvidenceLedger's read side: current/missing plus _cands (newest first)."""

    def __init__(self, results, missing=()):
        self.results, self._missing = results, list(missing)

    def current(self, P, spec, revision):
        return {}

    def missing(self, P, spec, revision):
        return [{"check_id": c} for c in self._missing]

    def _cands(self, P, tid, cid):
        r = self.results.get(cid.split(":", 1)[1])
        return [{"result": r, "truncated": 0}] if r else []


class Adapter:
    def __init__(self, pending=("step 3",), done=1, total=4):
        self.pending = pending
        self.rows = [["done", "r"]] * done + [["open", "r"]] * (total - done) + [["open", "CLOSE"]]

    def obligations(self, spec):
        out = [{"obligation_id": f"build.suite.{s}", "phase": "BUILD", "title": f"Suite {s}",
                "required_evidence": [{"kind": "check", "ref": f"suite:{s}"}]} for s in SUITES]
        return out + [{"obligation_id": "close.summary", "phase": "CLOSE", "title": "SUMMARY",
                       "required_evidence": []}]

    def required_checks(self, spec, phase="BUILD"):
        return [{"check_id": f"suite:{s}"} for s in SUITES]

    def pending_build_steps(self, spec, revision, ws):
        return self.pending

    def plan_progress(self, spec, revision, ws):
        return self.rows

    def describe(self, tid):
        return spec(tid)


def world(tmp_path, *, on=True, results=None, changed=("tests/checks/G2_check.lua",), **akw):
    ws_dir = tmp_path / "au"
    (ws_dir / "tests").mkdir(parents=True)
    (ws_dir / "tests" / "RegressionIndex.lua").write_text(INDEX, encoding="utf-8")
    store = Store(str(tmp_path / "c.sqlite"))
    ws = FakeWS(trees={"h1": {c: "a" for c in changed}, "h2": {c: "b" for c in changed}})
    P = project(adapter=Adapter(**akw), ws=ws)
    P.profile.workspace = str(ws_dir)
    P.profile.studio_calls = {"defer_guard_suites": on}
    t = task_row(store, tid="UX-1")
    ob = Obligations(store)
    ob.seed(P, spec("UX-1"))
    led = Ledger(results if results is not None else
                 {"G1": "PASS", "G2": "PASS", "G3": "FAIL", "G5": "PASS"})   # G4: never run
    return store, ob, Recovery(store), P, t, led


def cands():
    return [f"suite:{s}" for s in SUITES]


def test_switch_defaults_off_in_code_and_schema():
    assert SC.defaults()["defer_guard_suites"] is False
    assert SC.settings(SimpleNamespace(profile=SimpleNamespace(studio_calls=None)))[
        "defer_guard_suites"] is False


def test_classification_own_vs_guard(tmp_path):
    store, ob, rec, P, t, led = world(tmp_path)
    got = SC.deferred_guards(P, t, spec("UX-1"), "h2", cands(), led)
    # UX_1: (a) the sprint's own id; G2: (b) its check file changed in base..head; G3: (c)
    # newest verdict FAIL; G4: (c) never run; G1 and G5 passed earlier: the guards
    assert got == {"deferred": ["suite:G1", "suite:G5"], "halfway": False}
    # (a) is case-insensitive and maps "-" to "_"
    t2 = {**t, "task_id": "ux-1"}
    assert "suite:UX_1" not in SC.deferred_guards(P, t2, spec("ux-1"), "h2", cands(),
                                                  Ledger({**led.results, "UX_1": "PASS"}))["deferred"]
    # (c) a guard that went red is asked for again
    led.results["G1"] = "UNKNOWN"
    assert SC.deferred_guards(P, t, spec("UX-1"), "h2", cands(), led)["deferred"] == ["suite:G5"]


def test_switch_off_and_every_doubt_defer_nothing(tmp_path):
    off = world(tmp_path / "a", on=False)
    assert SC.deferred_guards(off[3], off[4], spec("UX-1"), "h2", cands(), off[5])["deferred"] == []
    store, ob, rec, P, t, led = world(tmp_path / "b")
    sp = spec("UX-1")
    P.adapter.pending = []                                    # BUILD complete: final stretch
    assert SC.deferred_guards(P, t, sp, "h2", cands(), led)["deferred"] == []
    P.adapter.pending = None                                  # no checklist
    assert SC.deferred_guards(P, t, sp, "h2", cands(), led)["deferred"] == []
    P.adapter.pending = ["x"]
    assert SC.deferred_guards(P, t, sp, "h2", cands(), None)["deferred"] == []   # no verdicts
    assert SC.deferred_guards(P, {**t, "base_sha": ""}, sp, "h2", cands(), led)["deferred"] == []
    P.profile.workspace = str(tmp_path / "missing")           # index unreadable
    assert SC.deferred_guards(P, t, sp, "h2", cands(), led)["deferred"] == []


def test_assignment_lists_own_and_red_and_names_the_deferred(tmp_path):
    store, ob, rec, P, t, led = world(tmp_path)
    a = rec.assignment(P, t, ledger=led, obligations=ob, ws=P.ws, spec=spec("UX-1"))
    assert a["affected_checks_to_rerun"] == ["suite:UX_1", "suite:G2", "suite:G3", "suite:G4"]
    assert a["deferred_guard_suites"] == ["suite:G1", "suite:G5"]
    assert "### Checks to rerun: suite:UX_1, suite:G2, suite:G3, suite:G4" in a["prompt_text"]
    assert ("### Deferred to the final gate (regression guards, do not request them now): "
            "G1, G5") in a["prompt_text"]
    # the obligations stay open: nothing is waived
    assert all(ob.get("game", "UX-1", f"build.suite.{s}")["status"] == "open" for s in SUITES)


def test_assignment_unchanged_with_the_switch_off(tmp_path):
    store, ob, rec, P, t, led = world(tmp_path, on=False)
    a = rec.assignment(P, t, ledger=led, obligations=ob, ws=P.ws, spec=spec("UX-1"))
    assert a["affected_checks_to_rerun"] == cands() and a["deferred_guard_suites"] == []
    assert "Deferred to the final gate" not in a["prompt_text"]


def test_halfway_asks_the_guards_once(tmp_path):
    store, ob, rec, P, t, led = world(tmp_path, done=2, total=4)      # 2 of 4 rows ticked
    a = rec.assignment(P, t, ledger=led, obligations=ob, ws=P.ws, spec=spec("UX-1"))
    assert a["affected_checks_to_rerun"] == cands() and a["deferred_guard_suites"] == []
    assert store.task("game", "UX-1")["data"]["guards_halfway"] == "h2"
    t2 = store.task("game", "UX-1")
    b = rec.assignment(P, {**t2, "run_id": "r2"}, ledger=led, obligations=ob, ws=P.ws,
                       spec=spec("UX-1"))
    assert b["deferred_guard_suites"] == ["suite:G1", "suite:G5"]    # not asked a second time


def _gate_world(tmp_path):
    ws = tmp_path / "au"
    (ws / "tests").mkdir(parents=True)
    (ws / "docs" / "progress").mkdir(parents=True)
    (ws / "tests" / "RegressionIndex.lua").write_text(INDEX, encoding="utf-8")
    P = SimpleNamespace(id="game", profile=SimpleNamespace(workspace=str(ws),
                                                studio_calls={"defer_guard_suites": True,
                                                              "auto_verify": True}),
                        adapter=Adapter())
    sc = SC.StudioCalls.__new__(SC.StudioCalls)
    sc._tier = lambda P, tid, spec=None: "A"
    return P, sc


def test_final_gate_request_still_carries_every_required_suite(tmp_path):
    """The final gate is StudioCalls.verify_hold (engine._verifying, entered only once the
    plan's BUILD rows are all ticked) -> default_request: every required suite, guards
    included, with the switch ON."""
    P, sc = _gate_world(tmp_path)
    assert sc.default_request(P, "UX-1", object())["suites"] == SUITES
    d, started = {}, []
    sc.on = lambda P, tid, spec: True
    sc._d = lambda P, tid: d
    sc._save = lambda P, tid, dd: None
    sc.gate_fresh = lambda *a: False
    sc._failed_at_same_code = lambda *a, **k: False
    sc.store = SimpleNamespace(event=lambda *a, **k: None, task=lambda *a: {})
    sc._start = lambda P, t, pend: started.append(pend) or True
    assert sc.verify_hold(P, {"task_id": "UX-1"}, object(), "h" * 40) is True
    assert started[0]["auto"] and started[0]["req"]["suites"] == SUITES


def test_submission_gate_still_requires_the_deferred_guards(tmp_path):
    store, ob, rec, P, t, led = world(tmp_path)
    led._missing = ["suite:G1"]
    refused = rec.gate_submission(P, t, "h2", led, ob, spec=spec("UX-1"))
    assert "suite:G1" in [r["item"].get("check_id") for r in refused]
    assert "build.suite.G1" in [r["item"].get("obligation_id") for r in refused]

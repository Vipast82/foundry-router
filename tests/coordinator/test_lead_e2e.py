"""Lead sprint manager end to end, wired through the engine (v0.123).

Every final BLOCKED / FAILED stop of a project with [lead_manager] enabled runs
deterministic recovery first, then opens a recovery case; the engine steps the
case one bounded step per tick and applies its resume action. Fakes only: a
scripted coding client whose lead sessions reply with a fenced `lead-report`, a
fake Foundry (persona routes, served models, reviews), temp git repos for the
game and for a stand-in Foundry-Router checkout, the real RepairWorkspace, and
the real supervisor driving the fake coordinator process from fakes_supervisor/.
No model calls, no live services, never the live DB or the game repository."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator import supervisor as SV
from foundry_coordinator.adapters.foundry import FoundryError
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.lead import OPEN_STATES, is_read_only
from foundry_coordinator.telemetry import CoordReader, lead_settings, lead_view
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo, review_json,
                                       run_until, write_profile)

LEAD = "Foundry-Kilo-Lead-Manager"
OPUS, ASTRA = "claude-opus-5-5", "gpt-6-astra"
SONNET = "Foundry-Kilo-Sonnet"
FAKE_COORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes_supervisor",
                          "fake_coordinator.py")
TID = "UI-01"
TASK = {"id": TID, "title": "Gallery", "spec": "Build the gallery file.", "scope": ["src/a.txt"]}
UI_OVERRIDE = f'''
[role_overrides.ui]
tasks = ["{TID}"]
implementer = "{SONNET}"
reviewer = "Foundry-Kilo-Opus"
'''
DONE = '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'


def lead_toml(enabled=True, expected=(OPUS, ASTRA), **kw) -> str:
    vals = {"max_diagnoses": 3, "max_candidates": 2, "max_upgrades": 1, "max_quota_calls": 12,
            "defer_seconds": 60, "extra_attempts": 1, "full_authority": False, **kw}
    lines = ["[lead_manager]", f"enabled = {str(bool(enabled)).lower()}",
             f'persona = "{LEAD}"', "expected_models = " + json.dumps(list(expected)),
             'agent = "lead"']
    for k, v in vals.items():
        lines.append(f"{k} = " + (json.dumps(str(v).replace(chr(92), "/"))
                                  if isinstance(v, str) else str(v).lower()
                                  if isinstance(v, bool) else str(v)))
    return "\n" + "\n".join(lines) + "\n"


# ------------------------------------------------------------------ fakes

class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class E2EFoundry(FakeFoundry):
    """FakeFoundry plus the lead endpoints (persona routes, served models) and
    a reviewer that also answers coordinator repair reviews."""

    def __init__(self):
        super().__init__(reviewer=self._review)
        self.info = {"name": LEAD, "exists": True, "enabled": True, "strict": True,
                     "routes": [{"backend": "meridian", "model": OPUS, "source": "subscription"},
                                {"backend": "cliproxy", "model": ASTRA,
                                 "source": "subscription"}],
                     "client_allowed": True, "client": "kilo", "reason": "ready"}
        self.info_error = None
        self.served = {}
        self.default_served = [OPUS]
        self.repair_verdict = "APPROVED"
        self.repair_reviews = []

    def persona_info(self, persona, client=""):
        if self.info_error:
            raise FoundryError(self.info_error)
        return json.loads(json.dumps(self.info))

    def served_models(self, session_id):
        models = self.served.get(session_id, self.default_served)
        chain = {r["model"]: r["backend"] for r in self.info.get("routes") or []}
        return {"session_id": session_id, "calls": len(models), "served_models": list(models),
                "routes": [{"backend": chain.get(m) or "llamacpp", "model": m} for m in models]}

    def _review(self, packet):
        if packet.startswith("# Coordinator repair review"):
            vals = {}
            for line in packet.splitlines():
                for key in ("case id (sprint_id): ", "project_id: ",
                            "candidate commit (reviewed_commit_sha): "):
                    if line.startswith(key):
                        vals[key] = line[len(key):].strip()
            self.repair_reviews.append(vals)
            return json.dumps({"sprint_id": vals["case id (sprint_id): "],
                               "project_id": vals["project_id: "],
                               "reviewed_commit_sha": vals["candidate commit (reviewed_commit_sha): "],
                               "verdict": self.repair_verdict, "findings": [],
                               "evidence_inspected": ["diff"], "missing_evidence": [],
                               "required_repairs": [], "residual_risks": []})
        return review_json(packet)


class E2EClient(ScriptedClient):
    """One fake Kilo for the primary and the lead: sessions whose metadata role
    is "lead" (the lead's diagnosis and repair sessions) follow `lead`, every
    other session follows `primary`. Exposes what lead readiness and restart
    reconciliation read (effective config, has_prompt, find_session)."""

    provider = "foundry"

    def __init__(self, primary, lead=None):
        super().__init__(self._dispatch, project_id="game")
        self.primary, self.lead = primary, lead or (lambda ctx: {"final": "nothing"})
        self.starts, self.titles, self.aborts = [], {}, []
        self.lose_next_lead_prompt = False

    def _dispatch(self, ctx):
        md = self.sessions[ctx["session_id"]]["metadata"]
        if md.get("role") == "lead":
            return self.lead({**ctx, "op": md.get("op") or "", "case": md.get("case") or ""})
        return self.primary(ctx)

    def start_session(self, *, directory, title, persona, agent, metadata, permissions):
        sid = super().start_session(directory=directory, title=title, persona=persona,
                                    agent=agent, metadata=metadata, permissions=permissions)
        self.titles[sid] = title
        self.starts.append({"session_id": sid, "directory": directory, "title": title,
                            "persona": persona, "agent": agent, "metadata": dict(metadata),
                            "permissions": list(permissions or [])})
        return sid

    def prompt(self, session_id, text, *, directory, persona, agent):
        super().prompt(session_id, text, directory=directory, persona=persona, agent=agent)
        if self.lose_next_lead_prompt and \
                self.sessions[session_id]["metadata"].get("role") == "lead":
            self.lose_next_lead_prompt = False
            raise RuntimeError("POST prompt_async: connection reset (the prompt was delivered)")

    def abort(self, session_id, *, directory):
        self.aborts.append(session_id)

    def has_prompt(self, session_id, directory=""):
        return bool(self.sessions.get(session_id, {}).get("prompts"))

    def find_session(self, directory, marker):
        return next((s for s, t in self.titles.items() if marker in t), "")

    def effective_config(self, directory):
        return {"provider": {"foundry": {"models": {LEAD: {"limit": {"context": 200000,
                                                                     "output": 32000}}}}},
                "agent": {"lead": {"mode": "primary"}, "code": {"mode": "primary"}}}

    def model_config_problem(self, workspace, persona, cfg=None):
        return ""

    # what tests read
    def lead_starts(self):
        return [s for s in self.starts if s["metadata"].get("role") == "lead"]

    def primary_starts(self):
        return [s for s in self.starts if s["metadata"].get("role") != "lead"]

    def lead_prompts(self):
        return [p for s in self.lead_starts() for p in self.sessions[s["session_id"]]["prompts"]]

    def diag_prompts(self):
        """Prompts of the lead's diagnosis sessions only (not repair sessions)."""
        return [p for s in self.lead_starts() if s["metadata"].get("op", "").startswith("lead:")
                for p in self.sessions[s["session_id"]]["prompts"]]


def lead_reply(rep) -> dict:
    body = rep if isinstance(rep, str) else json.dumps(rep)
    return {"final": "I looked at the workspace.\n```lead-report\n" + body + "\n```"}


def game_assignment(text="Write the word OK at the start of src/a.txt; the check needs it."):
    return {"classification": "product",
            "hypothesis": "The primary keeps writing 'broken' into src/a.txt; the project "
                          "check requires the word OK in every task file.",
            "evidence_refs": ["verify.py:9", "src/a.txt:1"],
            "action": {"kind": "assign_game_repair", "assignment": text, "files": ["src/a.txt"],
                       "tests": ["python verify.py"]}}


def coordinator_repair(files=("checker.py", "tests/test_checker_regression.py"), hyp=None):
    return {"classification": "coordinator",
            "hypothesis": hyp or "checker.py accepts only the exact text 'OK!', so every valid "
                                 "game file fails the coordinator check.",
            "evidence_refs": ["checker.py:4"],
            "action": {"kind": "coordinator_repair",
                       "plan": "Accept any task file whose text starts with OK in checker.py.",
                       "files": list(files),
                       "reproducing_test": "tests/test_checker_regression.py::test_accepts_ok_line",
                       "focused_tests": ["tests/test_checker_regression.py",
                                         "tests/test_checker.py"],
                       "broader_tests": []}}


def primary_script(log, good_when=("Lead manager assignment",
                                   "Operator answer to lead recovery case"),
                   status_first=None):
    """Writes 'broken' until a prompt carries `good_when`, then 'OK'. Each
    attempt commits distinct content."""
    def script(ctx):
        log.append(ctx)
        n = len(log)
        if status_first and n == 1:
            return {"final": '```coordinator-report\n' + json.dumps(
                {"status": "BLOCKED", "blocker": status_first}) + '\n```'}
        good = good_when is None or any(m in (ctx.get("prompt") or "") for m in good_when)
        return {"writes": {"src/a.txt": ("OK" if good else "broken") + f" attempt {n}\n"},
                "commit": f"{TID}: attempt {n}", "final": DONE}
    return script


def make_game(tmp, primary, lead=None, extra="", checks=None, clock=None, ff=None):
    root, _ = make_repo(tmp, "game", [TASK], extra_files={"src/a.txt": "start\n"})
    prof = write_profile(tmp, "game", root, extra=UI_OVERRIDE + extra, checks=checks)
    client = E2EClient(primary, lead)
    ff = ff or E2EFoundry()
    c = coordinator(tmp, [prof], {"game": client}, foundry={"game": ff}, clock=clock,
                    store_path=tmp / "state" / "coordinator.sqlite")
    return {"c": c, "client": client, "ff": ff, "root": root, "prof": prof, "tmp": tmp,
            "P": c.projects["game"], "clock": clock}


def restart(e):
    """A new Coordinator over the same store, profile, client and Foundry."""
    c = coordinator(e["tmp"], [e["prof"]], {"game": e["client"]}, foundry={"game": e["ff"]},
                    clock=e["clock"], store_path=e["tmp"] / "state" / "coordinator.sqlite")
    e.update(c=c, P=c.projects["game"])
    return c


def task(e):
    return e["c"].store.task("game", TID)


def state(e):
    return task(e)["state"]


def cases(e):
    try:
        return e["c"].store.q("SELECT * FROM recovery_cases WHERE project_id='game' ORDER BY "
                              "created_ts, rowid")
    except Exception:                                            # noqa: BLE001
        return []


def case(e):
    cid = (task(e)["data"] or {}).get("recovery_case") or (cases(e)[-1]["case_id"]
                                                           if cases(e) else "")
    return e["c"].lead.case(cid) if cid else None


def acts(e, cid):
    return e["c"].lead.actions(cid)


def _ev(h) -> dict:
    ev = h.get("evidence")
    return (json.loads(ev) if isinstance(ev, str) else ev) or {}


def hist(e):
    return [(h["from_state"], h["to_state"], _ev(h).get("blocker") or _ev(h).get("resolution")
             or _ev(h).get("reason")) for h in e["c"].store.history("game", TID)]


def transitions(e):
    return [{**h, "evidence": _ev(h)} for h in e["c"].store.history("game", TID)]


def assert_lead_sessions_read_only(e):
    for s in e["client"].lead_starts():
        if s["metadata"].get("op", "").startswith("lead:"):
            assert s["directory"] == e["P"].profile.workspace
            assert s["persona"] == LEAD and s["agent"] == "lead"
            assert is_read_only(s["permissions"]), s["permissions"]
        else:                                   # a coordinator repair session
            assert os.path.realpath(s["directory"]) != os.path.realpath(e["P"].profile.workspace)
            assert "repairs" in s["directory"]


def assert_primary_never_lead(e):
    for s in e["client"].primary_starts():
        assert s["persona"] == SONNET, s
    impl = [b for b in e["ff"].binds if b["role"] == "implementer"]
    assert impl and {b["persona"] for b in impl} == {SONNET}
    assert all(b["persona"] != LEAD for b in impl)
    lead_binds = [b for b in e["ff"].binds if b["persona"] == LEAD]
    assert all(b["role"] == "coordinator" and b["purpose"] == "lead_manager" for b in lead_binds)


# ------------------------------------------------------------------ 1. false blocker

def test_false_blocker_resumes_by_deterministic_recovery_without_a_lead_call(tmp_path):
    """A committed partial BUILD with an older no_progress/recovery question is
    withdrawn by the deterministic re-check (commit 8809871): no case, no lead
    session and no paid call, though the lead manager is on."""
    from tests.coordinator.test_lifecycle_e2e import lifecycle_extra
    from tests.coordinator.test_ui02_partial_build import (BASELINE, CAPTURE, PLAN, S1, TASK as
                                                           UTASK, TID as UTID, _untracked)
    from tests.coordinator.test_ui02_partial_build import ToolClient, report as ureport
    turns = []

    def script(ctx):
        turns.append(ctx)
        if len(turns) > 1:
            return None
        _untracked(ctx["workspace"], [BASELINE, CAPTURE])
        return {"writes": {"src/ui02/kit.lua": "-- partial build\n", PLAN: S1},
                "commit": f"{UTID}: c5b partial", "final": ureport(status="NOT DONE")}

    root, _ = make_repo(tmp_path, "game", [UTASK])
    body = lifecycle_extra().replace(
        'module = "tests.coordinator.test_lifecycle_e2e:LifecycleAdapter"',
        'module = "tests.coordinator.test_ui02_partial_build:UI02Adapter"')
    prof = write_profile(tmp_path, "game", root, extra=body + lead_toml())
    ff = E2EFoundry()
    c = coordinator(tmp_path, [prof], {"game": ToolClient(script, project_id="game")},
                    foundry={"game": ff})
    c.approve("game", UTID, [], "victor")
    assert run_until(c, lambda: c.store.task("game", UTID)["state"] == S.CHANGES_REQUIRED,
                     limit=25)
    P = c.projects["game"]
    assert c.lead is not None and c.lead.enabled(P)
    t = c.store.task("game", UTID)
    did = c.obligations.open_decision("game", UTID, "recovery_escalation",
                                      "premature final checks repeated",
                                      evidence={"repeats": []}, blocks=[UTID])
    c._lc_wait(P, t, [did], "findings repeat", S.READY)
    chats = len(ff.chats)
    c.tick()
    assert c.obligations.decision(did)["status"] == "withdrawn"
    after = c.store.task("game", UTID)
    assert after["state"] == S.READY and after["data"]["free_retry"] is True
    assert "recovery_case" not in after["data"]
    assert after["attempt"] == t["attempt"] and after["sessions"] == t["sessions"]
    assert c.store.q("SELECT COUNT(*) AS n FROM recovery_cases")[0]["n"] == 0
    assert len(ff.chats) == chats                       # no paid call
    assert not [b for b in ff.binds if b["persona"] == LEAD]


def _lifecycle_game(tmp_path, lead_script, answer_original=False, lead_kw=None):
    """The UI-02 partial-BUILD game with a genuine (not reevaluable) stall
    decision: an unrelated untracked file keeps the continuation closed."""
    from tests.coordinator.test_lifecycle_e2e import lifecycle_extra
    from tests.coordinator.test_ui02_partial_build import (BASELINE, CAPTURE, PLAN, S1, TASK as
                                                           UTASK, TID as UTID, _untracked)
    from tests.coordinator.test_ui02_partial_build import ToolClient, report as ureport
    turns = []
    holder = {}

    def script(ctx):
        md = holder["client"].sessions[ctx["session_id"]]["metadata"]
        if md.get("role") == "lead":
            return lead_script({**ctx, "op": md.get("op") or ""})
        turns.append(ctx)
        if len(turns) > 1:
            return None
        _untracked(ctx["workspace"], [BASELINE, CAPTURE])
        return {"writes": {"src/ui02/kit.lua": "-- partial build\n", PLAN: S1},
                "commit": f"{UTID}: c5b partial", "final": ureport(status="NOT DONE")}

    root, _ = make_repo(tmp_path, "game", [UTASK])
    body = lifecycle_extra().replace(
        'module = "tests.coordinator.test_lifecycle_e2e:LifecycleAdapter"',
        'module = "tests.coordinator.test_ui02_partial_build:UI02Adapter"')
    prof = write_profile(tmp_path, "game", root, extra=body + lead_toml(**(lead_kw or {})))
    client = ToolClient(script, project_id="game")
    holder["client"] = client
    for name in ("effective_config", "model_config_problem", "has_prompt", "find_session"):
        setattr(client, name, getattr(E2EClient, name).__get__(client))
    client.provider, client.titles = "foundry", {}
    ff = E2EFoundry()
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", UTID, [], "victor")
    assert run_until(c, lambda: c.store.task("game", UTID)["state"] == S.CHANGES_REQUIRED,
                     limit=25)
    _untracked(str(root), ["docs/progress/UI-01_gate_verdicts.json"])
    P = c.projects["game"]
    did = c.obligations.open_decision("game", UTID, "recovery_escalation",
                                      "findings repeat with no different strategy",
                                      evidence={}, blocks=[UTID])
    c._lc_wait(P, c.store.task("game", UTID), [did], "findings repeat", S.READY)
    return c, client, ff, did, UTID


def test_genuine_stall_decision_goes_to_the_lead_before_victor(tmp_path):
    rep = {"classification": "product",
           "hypothesis": "The BUILD plan's next step was never started; the primary keeps "
                         "re-running final checks instead of building the HUD screens.",
           "evidence_refs": ["docs/progress/UI-02_PLAN.md:7"],
           "action": {"kind": "assign_game_repair",
                      "assignment": "Continue BUILD step 4 in src/ui02/hud.lua before any final "
                                    "gate run.", "files": ["src/ui02/hud.lua"]}}
    c, client, ff, did, utid = _lifecycle_game(tmp_path, lambda ctx: lead_reply(rep))
    c.tick()                     # the deterministic re-check runs first and keeps it open
    t = c.store.task("game", utid)
    assert c.obligations.decision(did)["status"] == "open"
    cid = t["data"]["recovery_case"]
    assert t["state"] == S.BLOCKED and t["blocker"].startswith("lead recovery: DIAGNOSING")
    assert did in t["data"]["auto_reevaluate_checked"]
    assert not [s for s in client.sessions.values() if s["metadata"].get("role") == "lead"]
    assert run_until(c, lambda: c.store.task("game", utid)["state"] != S.BLOCKED, limit=10)
    t = c.store.task("game", utid)
    assert t["state"] in (S.READY, S.IMPLEMENTING)
    assert c.obligations.decision(did)["status"] == "withdrawn"
    assert c.lead.case(cid)["state"] == "RESOLVED"
    # the delegated extra attempt is an authorized ledger row referencing the case
    rows = c.store.q("SELECT counter, delta, decision_id, actor FROM budget_ledger WHERE "
                     "decision_id=?", (f"unblock:lead:{cid}",))
    assert {r["counter"] for r in rows} == {"authorized:attempt_ordinary", "authorized:sessions"}
    assert all(r["delta"] == 1 and r["actor"] == "delegated_policy:lead_manager" for r in rows)
    ev = [e["detail"] for e in c.store.events("game", 200) if e["kind"] == "decision_withdrawn"]
    assert any(d.get("decision_id") == did and d.get("by") == "lead_manager" for d in ev)


def test_lead_strategy_after_three_repeats_reaches_exactly_one_primary_session(tmp_path):
    """UI-02 rc-fef1...: the findings had repeated three times when the lead
    case resolved. Its new strategy must start ONE implementer session instead
    of reopening the repeated-strategy escalation; the same text afterwards is
    not a free pass."""
    hud = "Continue BUILD step 4 in src/ui02/hud.lua before any final gate run."
    rep = {"classification": "product",
           "hypothesis": "The primary keeps re-running final checks instead of building.",
           "evidence_refs": ["docs/progress/UI-02_PLAN.md:7"],
           "action": {"kind": "assign_game_repair", "assignment": hud,
                      "files": ["src/ui02/hud.lua"]}}
    c, client, ff, did, utid = _lifecycle_game(tmp_path, lambda ctx: lead_reply(rep))
    P = c.projects["game"]
    f = {"class": "missing_evidence", "check_id": "au.gate_bound_to_head",
         "text": "gate verdicts were recorded at an older commit than HEAD"}
    for n in (1, 2, 3):
        c.recovery.record_findings(P, c.store.task("game", utid), n, [f])
    assert max(r["count"] for r in c.recovery.repeats("game", utid)) >= 3

    def with_lead():
        return [s for s in client.sessions.values() if s["metadata"].get("role") != "lead"
                and any(hud in p for p in s.get("prompts") or [])]
    assert run_until(c, lambda: with_lead(), limit=25),         [d["question"][:80] for d in c.obligations.active_decisions("game", utid)]
    for _ in range(4):
        c.tick()
    assert len(with_lead()) == 1
    a = next(r["assignment"] for r in c.recovery.repairs("game", utid)
             if (r.get("assignment") or {}).get("strategy") == hud)
    assert a["lead_strategy"] is True
    ev = [e["detail"] for e in c.store.events("game", 300) if e["kind"] == "lead_strategy"]
    assert len(ev) == 1
    # the lead text was consumed with the session start
    assert "lead_assignment" not in (c.store.task("game", utid)["data"] or {})


def test_case_opened_on_an_unused_lead_strategy_is_reconciled_after_the_upgrade(tmp_path):
    """The live UI-02 state: an old coordinator re-asked the repeat question
    (dec-d046...) before the resolved case's strategy ever ran, opening a
    second case (rc-826...). After the upgrade that case is cancelled, the
    question withdrawn (both kept), and the pending strategy reaches exactly
    one implementer session -- no operator edit, no further lead call."""
    hud = "Continue BUILD step 4 in src/ui02/hud.lua before any final gate run."
    rep = {"classification": "product",
           "hypothesis": "The primary keeps re-running final checks instead of building.",
           "evidence_refs": ["docs/progress/UI-02_PLAN.md:7"],
           "action": {"kind": "assign_game_repair", "assignment": hud,
                      "files": ["src/ui02/hud.lua"]}}
    c, client, ff, did, utid = _lifecycle_game(tmp_path, lambda ctx: lead_reply(rep))
    P = c.projects["game"]
    f = {"class": "missing_evidence", "check_id": "au.gate_bound_to_head",
         "text": "gate verdicts were recorded at an older commit than HEAD"}
    for n in (1, 2, 3):
        c.recovery.record_findings(P, c.store.task("game", utid), n, [f])
    real = c.recovery.assignment
    c.recovery.assignment = lambda *a, **kw: real(*a, **{**kw, "trusted_strategy": False})
    first = []

    def second_case():
        k = c.lead.active_case("game", utid)
        if k and not first:
            first.append(k["case_id"])
        return bool(k and first and k["case_id"] != first[0])
    assert run_until(c, second_case, limit=30)                # the old loop
    old_case = c.lead.active_case("game", utid)
    t = c.store.task("game", utid)
    wait = t["data"]["decision_wait"]["ids"]
    assert t["data"]["lead_assignment"]["assignment"] == hud
    assert c.obligations.decision(wait[0])["kind"] == "recovery_escalation"
    lead_calls = len(client.diag_prompts()) if hasattr(client, "diag_prompts") else None
    c.recovery.assignment = real                              # the upgrade

    def with_lead():
        return [x for x in client.sessions.values() if x["metadata"].get("role") != "lead"
                and any(hud in p for p in x.get("prompts") or [])]
    assert run_until(c, lambda: with_lead(), limit=12)
    for _ in range(4):
        c.tick()
    assert len(with_lead()) == 1
    assert c.lead.case(old_case["case_id"])["state"] == "CANCELLED"
    assert c.obligations.decision(wait[0])["status"] == "withdrawn"
    assert not [s for s in client.sessions.values() if s["metadata"].get("role") == "lead"
                and s["metadata"].get("case") == old_case["case_id"] and s.get("prompts")]
    if lead_calls is not None:
        assert len(client.diag_prompts()) == lead_calls
    ev = [e["detail"] for e in c.store.events("game", 300) if e["kind"] == "lead_case_cancelled"]
    assert ev and ev[0]["case_id"] == old_case["case_id"]


def test_lead_escalation_replaces_the_stall_decision_with_one_question(tmp_path):
    """The lead cannot resolve a stall and asks Victor: the inbox shows ONE
    question (the lead_escalation, carrying the dossier reference and the
    attempts), the original is withdrawn with a linking event (audit), the
    case is not mistaken for an operator answer, and Victor's answer to the
    escalation resumes the task through the case."""
    rep = {"classification": "scope_core",
           "hypothesis": "The BUILD plan asks for HUD screens the approved scope does not "
                         "list; continuing needs a scope decision.",
           "evidence_refs": ["docs/progress/UI-02_PLAN.md:7"],
           "action": {"kind": "ask_victor"},
           "question": "Should UI-02 include the HUD screens, or stop after the kit?",
           "options": ["include the HUD screens", "stop after the kit"],
           "recommended": "stop after the kit"}
    c, client, ff, did, utid = _lifecycle_game(tmp_path, lambda ctx: lead_reply(rep))
    O = c.obligations
    assert run_until(c, lambda: (c.lead.active_case("game", utid) or {}).get("state")
                     == "NEEDS_VICTOR", limit=12)
    t = c.store.task("game", utid)
    k = c.lead.case(t["data"]["recovery_case"])
    new = O.decision(k["decision_id"])
    assert [d["decision_id"] for d in O.active_decisions("game", utid)
            if d["status"] == "open"] == [new["decision_id"]]
    assert new["kind"] == "lead_escalation" and new["supersedes"] == did
    assert new["evidence"]["attempted"] and new["evidence"]["dossier_sha"]
    assert new["evidence"]["original_decisions"][0]["decision_id"] == did
    assert "HUD screens" in new["question"] and did in new["question"]
    old = O.decision(did)
    assert old["status"] == "withdrawn"                           # kept, not deleted
    ev = [x["detail"] for x in c.store.events("game", 200) if x["kind"] == "decision_withdrawn"
          and x["detail"].get("decision_id") == did]
    assert ev and ev[0]["superseded_by"] == new["decision_id"] and ev[0]["by"] == "lead_manager"
    # ticks while Victor has not answered: the case is NOT cancelled by the withdrawal
    for _ in range(3):
        c.tick()
    k = c.lead.case(k["case_id"])
    assert k["state"] == "NEEDS_VICTOR"
    t = c.store.task("game", utid)
    assert t["state"] == S.BLOCKED and t["data"]["recovery_case"] == k["case_id"]
    # the lead session ended once its report was parsed
    lead_sids = [a["detail"]["session_id"] for a in c.lead.actions(k["case_id"])
                 if a["kind"] == "lead_call"]
    assert lead_sids and all(c.store.session(x)["status"] == "ended" for x in lead_sids)
    assert {x for x, _ in ff.ended} >= set(lead_sids)
    # Victor answers the ONE question: the original is superseded by it and the
    # case resumes the task with his instruction
    c.decision_answer("game", utid, new["decision_id"],
                      "Include the HUD screens; build them in src/ui02/hud.lua next.",
                      actor="victor", channel="control")
    assert O.decision(did)["status"] == "superseded"
    assert O.decision(did)["superseded_by"] == new["decision_id"]
    assert run_until(c, lambda: c.store.task("game", utid)["state"] != S.BLOCKED, limit=6)
    assert c.lead.case(k["case_id"])["state"] == "RESOLVED"
    resume = next(h for h in c.store.history("game", utid)
                  if h["from_state"] == S.BLOCKED and _ev(h).get("recovery_case") == k["case_id"])
    assert _ev(resume)["action"] == "victor_answer"
    assert _ev(resume)["decision_id"] == new["decision_id"]


def test_operator_answer_to_the_original_decision_wins_over_the_case(tmp_path):
    c, client, ff, did, utid = _lifecycle_game(tmp_path, lambda ctx: {"final": "thinking"})
    c.tick()
    cid = c.store.task("game", utid)["data"]["recovery_case"]
    c.decision_answer("game", utid, did, "Build the HUD screens next, then gate.",
                      actor="victor", channel="control")
    c.tick()
    assert c.lead.case(cid)["state"] == "CANCELLED"
    assert "recovery_case" not in c.store.task("game", utid)["data"]
    assert run_until(c, lambda: c.store.task("game", utid)["state"] != S.BLOCKED, limit=5)


# ------------------------------------------------------------------ 2 + 10. true blocker

def test_true_blocker_lead_assignment_repairs_and_completes(tmp_path):
    """Attempts exhausted on a real product defect: instead of FAILED, the lead
    (served by Opus) diagnoses it read-only and assigns a game repair; the
    primary (Sonnet, the UI writer) fixes it under one delegated extra attempt;
    verification, independent review and closeout complete."""
    log = []
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: state(e) == S.BLOCKED, limit=40), hist(e)
    t = task(e)
    cid = t["data"]["recovery_case"]
    assert t["blocker"].startswith("lead recovery: DIAGNOSING") and "would have been FAILED" in \
        t["blocker"]
    assert not [h for h in transitions(e) if h["to_state"] == S.FAILED]
    assert run_until(c, lambda: state(e) == S.COMPLETE, limit=40), hist(e)
    k = c.lead.case(cid)
    assert k["state"] == "RESOLVED" and k["classification"] == "product"
    assert k["lead_model"] == OPUS and k["counters"]["diagnoses"] == 1
    kinds = [a["kind"] for a in acts(e, cid)]
    assert {"open_case", "lead_call", "served_model", "diagnosis", "assign_game_repair",
            "authorize_attempts", "resolve"} <= set(kinds)
    grant = next(a for a in acts(e, cid) if a["kind"] == "authorize_attempts")
    assert grant["detail"]["n"] == 1 and grant["actor"] == "delegated_policy:lead_manager"
    assert grant["detail"]["ref"] == f"unblock:lead:{cid}"
    resume = next(h for h in transitions(e) if h["evidence"].get("recovery_case") == cid and
                  h["from_state"] == S.BLOCKED)
    assert resume["to_state"] == S.READY and resume["evidence"]["action"] == "assign_game_repair"
    # the assignment reached the primary, marked as the lead's untrusted advice
    last_prompt = log[-1]["prompt"]
    assert "Lead manager assignment (recovery case " + cid in last_prompt
    assert "advisory and untrusted" in last_prompt
    assert len(e["client"].lead_starts()) == 1 and len(e["client"].lead_prompts()) == 1
    assert_lead_sessions_read_only(e)
    # the normal gates still decided: verification, an independent review, closeout
    fin = task(e)
    assert fin["closeout_id"] and fin["attempt"] == 4
    revs = c.store.reviews("game", TID)
    assert revs and revs[-1]["verdict"] == "APPROVED" and revs[-1]["persona"] != LEAD
    assert "OK" in (e["root"] / "src" / "a.txt").read_text()


def test_sonnet_stays_the_ui_implementer_and_the_lead_never_becomes_primary(tmp_path):
    log = []
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml())
    e["c"].approve("game", TID, [], "victor")
    assert run_until(e["c"], lambda: state(e) == S.COMPLETE, limit=80), hist(e)
    assert e["P"].profile.persona("implementer", TID) == SONNET
    assert_primary_never_lead(e)
    assert len(e["client"].primary_starts()) == 4 and e["client"].lead_starts()


# ------------------------------------------------------------------ 3, 4, 6: coordinator repair

CHECKER_V1 = textwrap.dedent('''\
    import sys


    def accepts(text):
        return text.strip() == "OK!"


    def main(argv):
        text = open(argv[1], encoding="utf-8").read()
        ok = accepts(text)
        print("ALL PASS" if ok else "FAIL: " + argv[1])
        return 0 if ok else 1


    if __name__ == "__main__":
        sys.exit(main(sys.argv))
    ''')
CHECKER_FIXED = CHECKER_V1.replace('return text.strip() == "OK!"',
                                   'return text.strip().startswith("OK")')
CHECKER_TOO_LOOSE = CHECKER_V1.replace('return text.strip() == "OK!"', 'return True')
REGRESSION = textwrap.dedent('''\
    from checker import accepts


    def test_accepts_ok_line():
        assert accepts("OK attempt 3\\n")
    ''')


def make_router(tmp):
    repo = tmp / "router"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    files = {"checker.py": CHECKER_V1, "BUILD": "v1\n", "README.md": "router\n",
             "foundry_router/__init__.py": '__version__ = "1.0.0"\n',
             "tests/conftest.py": "import os, sys\nsys.path.insert(0, os.path.dirname("
                                  "os.path.dirname(os.path.abspath(__file__))))\n",
             "tests/test_checker.py": "from checker import accepts\n\n\ndef test_rejects_empty():"
                                      "\n    assert not accepts('')\n"}
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "router v1")
    return repo


def repair_lead(writes_by_round, reports):
    """Lead script: diagnosis calls pop `reports`; repair session n writes
    writes_by_round[n-1] into its worktree."""
    rounds = []

    def script(ctx):
        if ctx["op"].startswith("lead:"):
            rep = reports.pop(0) if reports else "no report"
            return lead_reply(rep)
        rounds.append(ctx["workspace"])
        return {"writes": writes_by_round[min(len(rounds), len(writes_by_round)) - 1],
                "final": "Repaired the checker in the isolated worktree."}
    script.rounds = rounds
    return script


def coordinator_game(tmp_path, lead, **lead_kw):
    router = make_router(tmp_path)
    checks = textwrap.dedent(f'''\
        [[verify.checks]]
        name = "router-check"
        command = ["{{python}}", "{str(router / 'checker.py').replace(chr(92), '/')}", "src/a.txt"]
        pass_regex = "^ALL PASS$"
        ''')
    log = []
    # the stand-in router has no tests/coordinator: its broader suite is tests/
    lead_kw.setdefault("repair_broader_tests", ["tests"])
    e = make_game(tmp_path, primary_script(log, good_when=None), lead=lead, checks=checks,
                  extra=lead_toml(repair_root=str(tmp_path / "repairs"), router_repo=str(router),
                                  **lead_kw))
    e.update(router=router, log=log, state_dir=str(tmp_path / "state"))
    return e


def supervisor(e, **kw):
    cmd = f'"{sys.executable}" "{FAKE_COORD}" --state "{e["state_dir"]}" --max-seconds 120'
    kw.setdefault("stop_timeout", 5)
    kw.setdefault("health_timeout", 20)
    kw.setdefault("poll", 0.05)
    kw.setdefault("test_timeout", 180)
    kw.setdefault("log", lambda m: None)
    sup = SV.Supervisor(e["state_dir"], str(e["router"]), cmd, **kw)
    e.setdefault("sups", []).append(sup)
    return sup


def kill_fakes(e):
    path = os.path.join(e.get("state_dir", ""), "fake_events.log")
    try:
        lines = open(path, encoding="utf-8").read().split("\n")
    except OSError:
        lines = []
    try:
        with open(os.path.join(e["state_dir"], "coordinator.stop"), "w") as fh:
            fh.write("stop")
    except OSError:
        pass
    time.sleep(0.3)
    for line in lines:
        if line.startswith("start "):
            pid = int(line.split()[1])
            if SV.pid_alive(pid):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True)
                else:
                    os.kill(pid, 9)
    for s in e.get("sups", []):
        s.close()


def to_upgrading(e, limit=80):
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: (case(e) or {}).get("state") in ("UPGRADING", "NEEDS_VICTOR"),
                     limit=limit), (hist(e), case(e) and acts(e, case(e)["case_id"]))
    return case(e)


def test_coordinator_defect_isolated_repair_supervisor_upgrade_and_resume(tmp_path):
    lead = repair_lead([{"checker.py": CHECKER_FIXED,
                         "tests/test_checker_regression.py": REGRESSION}],
                       [coordinator_repair()])
    e = coordinator_game(tmp_path, lead)
    try:
        k = to_upgrading(e)
        assert k["state"] == "UPGRADING", acts(e, k["case_id"])
        cid = k["case_id"]
        assert task(e)["state"] == S.BLOCKED and "UPGRADING" in task(e)["blocker"]
        kinds = [a["kind"] for a in acts(e, cid)]
        assert {"diagnosis", "coordinator_repair", "repair_worktree", "repair_session",
                "repair_commit", "candidate_validation", "upgrade_request"} <= set(kinds)
        # the repair happened in an isolated worktree on lead/<case>-c1, never in the
        # router checkout or the game workspace
        wt = next(a for a in acts(e, cid) if a["kind"] == "repair_worktree")["detail"]["path"]
        assert os.path.isdir(wt) and str(tmp_path / "repairs") in wt
        assert open(e["router"] / "checker.py").read() == CHECKER_V1
        assert git(e["root"], "status", "--porcelain") == ""
        assert_lead_sessions_read_only(e)
        val = next(a for a in acts(e, cid) if a["kind"] == "candidate_validation")
        assert val["status"] == "done" and val["detail"]["manifest"]["files"] == [
            "checker.py", "tests/test_checker_regression.py"]
        assert e["ff"].repair_reviews and \
            e["ff"].chats[-1]["persona"] == "Foundry-Kilo-Opus" != LEAD
        up = SV.get_request(e["c"].store, k["upgrade_id"])
        assert up["status"] == "requested" and up["manifest"]["tests"]["regression"] == \
            "tests/test_checker_regression.py::test_accepts_ok_line"
        # the old coordinator build runs; the supervisor installs the candidate
        sup = supervisor(e)
        old = sup.start_coordinator("old")
        sup.health(old["started_ts"], old["pid"], 20)
        prior = git(e["router"], "rev-parse", "HEAD")
        res = sup.run_once()
        assert res and res[-1]["status"] == "done", SV.get_request(e["c"].store,
                                                                   k["upgrade_id"])["log"]
        steps = [x["step"] for x in SV.get_request(e["c"].store, k["upgrade_id"])["log"][
            "entries"] if x["event"] == "done"]
        assert steps == list(SV.STEPS)
        assert git(e["router"], "rev-parse", "HEAD") == up["candidate_sha"] != prior
        assert os.path.isfile(os.path.join(e["state_dir"], "upgrade_backups", k["upgrade_id"],
                                           "coordinator.sqlite"))
        assert not e["c"].store.project("game")["paused"]
        # the supervisor reported on its request only: the case is the lead's
        assert SV.get_request(e["c"].store, k["upgrade_id"])["status"] == "done"
        assert e["c"].lead.case(cid)["state"] == "UPGRADING"
        assert not [a for a in acts(e, cid) if a["kind"] == "upgrade_result"]
        # the restarted coordinator (same store): the lead's UPGRADING step sees
        # `done`, journals it and resumes the task; the blocker is gone
        restart(e)
        assert run_until(e["c"], lambda: state(e) == S.COMPLETE, limit=40), hist(e)
        assert e["c"].lead.case(cid)["state"] == "RESOLVED"
        res = [a for a in acts(e, cid) if a["kind"] == "upgrade_result"]
        assert len(res) == 1 and res[0]["status"] == "done"
        assert res[0]["detail"]["upgrade_id"] == k["upgrade_id"]
        assert res[0]["revision"] == up["candidate_sha"]
        # every lead / repair session of the case was ended (store and Foundry)
        sids = [a["detail"]["session_id"] for a in acts(e, cid)
                if a["kind"] in ("lead_call", "repair_session")]
        assert len(sids) == 2
        for sid in sids:
            row = e["c"].store.session(sid)
            assert row["status"] == "ended" and row["ended_ts"], row
        assert {sid for sid, _ in e["ff"].ended} >= set(sids)
        resume = next(h for h in transitions(e) if h["evidence"].get("recovery_case") == cid
                      and h["from_state"] == S.BLOCKED)
        assert resume["to_state"] == S.VERIFYING
        assert resume["evidence"]["action"] == "coordinator_repair"
        assert resume["evidence"]["upgrade_id"] == k["upgrade_id"]
        ver = e["c"].store.verification(task(e)["verification_id"], "game", TID)
        assert all(r["result"] == "PASS" for r in ver)
        assert len(e["log"]) == 3                      # no extra attempt was needed
    finally:
        kill_fakes(e)


@pytest.mark.parametrize("mode", ["tests_fail", "dirty_scope"])
def test_rejected_candidate_goes_back_to_diagnosis(tmp_path, mode):
    bad = {"tests_fail": {"checker.py": CHECKER_TOO_LOOSE,
                          "tests/test_checker_regression.py": REGRESSION},
           "dirty_scope": {"checker.py": CHECKER_FIXED,
                           "tests/test_checker_regression.py": REGRESSION,
                           "README.md": "edited outside the repair scope\n"}}[mode]
    second = coordinator_repair(hyp="A materially different hypothesis: the checker's exact "
                                    "match should be a prefix match on OK.")
    lead = repair_lead([bad], [coordinator_repair(), second])
    e = coordinator_game(tmp_path, lead)
    c = e["c"]
    c.approve("game", TID, [], "victor")

    def rejected():
        k = case(e)
        return bool(k and any(a["kind"] == "candidate_rejected" for a in acts(e, k["case_id"])))
    assert run_until(c, rejected, limit=80), hist(e)
    k = case(e)
    rej = next(a for a in acts(e, k["case_id"]) if a["kind"] == "candidate_rejected")
    if mode == "tests_fail":
        assert rej["detail"]["phase"] == "validate"
        assert "focused or broader tests failed" in rej["result"]
    else:
        assert rej["detail"]["phase"] == "commit"
        assert "dirty scope" in rej["result"] and "README.md" in rej["result"]
    assert k["state"] == "DIAGNOSING"
    assert k["validation"]["last_rejection"]["phase"] == rej["detail"]["phase"]
    assert not c.store.q("SELECT 1 FROM upgrade_requests") if \
        c.store.q("SELECT 1 FROM sqlite_master WHERE name='upgrade_requests'") else True
    assert open(e["router"] / "checker.py").read() == CHECKER_V1      # nothing installed
    assert state(e) == S.BLOCKED
    # back to diagnosis: the next lead call gets the rejection in its dossier
    assert run_until(c, lambda: len(e["client"].diag_prompts()) >= 2, limit=10)
    assert "candidate_rejected" in e["client"].diag_prompts()[1]


def test_failed_health_check_rolls_back_then_rediagnoses_once_then_victor(tmp_path):
    lead = repair_lead([{"checker.py": CHECKER_FIXED,
                         "tests/test_checker_regression.py": REGRESSION,
                         "FAIL_HEALTH": "the new build reports unhealthy\n"}],
                       [coordinator_repair(files=("checker.py", "FAIL_HEALTH",
                                                  "tests/test_checker_regression.py")),
                        coordinator_repair(hyp="After the rollback: the checker defect needs a "
                                               "fix that keeps the build healthy.")])
    e = coordinator_game(tmp_path, lead, max_candidates=1)
    try:
        k = to_upgrading(e)
        assert k["state"] == "UPGRADING", acts(e, k["case_id"])
        cid = k["case_id"]
        sup = supervisor(e, health_timeout=10)
        old = sup.start_coordinator("old")
        sup.health(old["started_ts"], old["pid"], 20)
        prior = git(e["router"], "rev-parse", "HEAD")
        res = sup.run_once()
        req = SV.get_request(e["c"].store, k["upgrade_id"])
        assert res[-1]["status"] == "rolled_back", req["log"]
        assert req["log"]["state"]["failure"]["step"] == "checking"
        # the prior build and DB are back, the old build runs again
        assert git(e["router"], "rev-parse", "HEAD") == prior
        assert open(e["router"] / "checker.py").read() == CHECKER_V1
        assert not os.path.exists(e["router"] / "FAIL_HEALTH")
        assert not e["c"].store.project("game")["paused"]
        assert SV.pid_alive(int(open(os.path.join(e["state_dir"], "coordinator.pid")).read()))
        # reported on the request only; the restored DB holds the case as it was
        assert req["rollback_result"]["failure"]["step"] == "checking"
        # the DB is restored from the backup, or (supervisor v0.123 review fix)
        # kept because the failed build never migrated it
        kept = [x for x in req["log"]["entries"] if x["step"] == "rolling_back"
                and x["event"] == "db_kept"]
        assert req["rollback_result"]["db_restored"] is True or \
            (kept and kept[-1]["detail"]["restored"] is False)
        assert e["c"].lead.case(cid)["state"] == "UPGRADING"
        # back to diagnosis exactly once (the lead journals the rollback); the
        # bounded retry then reaches Victor
        restart(e)
        assert run_until(e["c"], lambda: e["c"].lead.case(cid)["state"] != "UPGRADING",
                         limit=5), acts(e, cid)
        k = e["c"].lead.case(cid)
        assert k["state"] == "DIAGNOSING" and k["rollback_result"]["status"] == "rolled_back"
        assert k["rollback_result"]["failure"]["step"] == "checking"
        res = [a for a in acts(e, cid) if a["kind"] == "upgrade_result"]
        assert len(res) == 1 and res[0]["status"] == "failed" and "checking" in res[0]["result"]
        assert run_until(e["c"], lambda: e["c"].lead.case(cid)["state"] == "NEEDS_VICTOR",
                         limit=20), acts(e, cid)
        k = e["c"].lead.case(cid)
        assert len(e["client"].diag_prompts()) == 2                          # two diagnoses
        d = e["c"].obligations.decision(k["decision_id"])
        assert d["kind"] == "lead_escalation" and d["status"] == "open"
        assert "exhausted max_candidates (1/1)" in d["question"]
        assert state(e) == S.BLOCKED and "NEEDS_VICTOR" in task(e)["blocker"]
        assert len(lead.rounds) == 1                    # no second candidate was built
    finally:
        kill_fakes(e)


# ------------------------------------------------------------------ 5. restart mid lead call

def test_restart_mid_lead_call_never_repeats_the_paid_call(tmp_path):
    log = []
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml())
    e["client"].lose_next_lead_prompt = True
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: len(e["client"].lead_prompts()) == 1, limit=40), hist(e)
    cid = task(e)["data"]["recovery_case"]
    call = next(a for a in acts(e, cid) if a["kind"] == "lead_call")
    assert call["detail"]["phase"] == "prompting" and call["detail"].get("prompt_error")
    # the coordinator dies here: a new process on the same store
    restart(e)
    assert run_until(e["c"], lambda: state(e) == S.COMPLETE, limit=40), hist(e)
    assert len(e["client"].lead_prompts()) == 1 and len(e["client"].lead_starts()) == 1
    rec = [a for a in acts(e, cid) if a["kind"] == "lead_call_reconcile"]
    assert len(rec) == 1 and rec[0]["status"] == "uncertain"
    assert rec[0]["detail"]["kilo_has_prompt"] is True
    k = e["c"].lead.case(cid)
    assert k["counters"]["diagnoses"] == 1 and k["counters"]["quota_calls"] == 1


# ------------------------------------------------------------------ 7. quota deferral

def test_quota_and_readiness_deferral_wait_without_paid_calls_then_resume(tmp_path):
    clock = Clock()
    log = []
    ff = E2EFoundry()
    ff.info_error = "GET /v1/foundry/personas: 503 quota source unavailable"

    def admission_down(**need):
        raise FoundryError("helper admission: 503")
    ff.admission[LEAD] = admission_down
    e = make_game(tmp_path, primary_script(log, good_when=None,
                                           status_first="provider quota exhausted (429)"),
                  lead=lambda ctx: lead_reply({
                      "classification": "provider_quota",
                      "hypothesis": "The subscription quota window is exhausted until reset.",
                      "evidence_refs": ["session report: 429"],
                      "action": {"kind": "wait", "seconds": 600}}),
                  extra=lead_toml(), clock=clock, ff=ff)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: (case(e) or {}).get("state") == "DEFERRED", limit=20), hist(e)
    k = case(e)
    assert k["next_check_ts"] > clock() and not e["client"].lead_starts()
    rd = next(a for a in acts(e, k["case_id"]) if a["kind"] == "readiness")
    assert all(b["cls"] == "transient" for b in rd["detail"]["failed"])
    for _ in range(5):                                   # backoff: no call while deferred
        c.tick()
    assert not e["client"].lead_starts() and case(e)["state"] == "DEFERRED"
    ff.info_error = None
    del ff.admission[LEAD]
    clock.advance(3600)
    assert run_until(c, lambda: case(e)["state"] == "DEFERRED" and e["client"].lead_starts(),
                     limit=10), acts(e, k["case_id"])
    k = case(e)
    assert k["classification"] == "provider_quota" and k["counters"]["quota_calls"] == 1
    assert k["validation"]["deferrals"] == 2 and k["next_check_ts"] >= clock() + 600
    for _ in range(5):
        c.tick()
    assert len(e["client"].lead_prompts()) == 1 and state(e) == S.BLOCKED
    clock.advance(3600)
    assert run_until(c, lambda: state(e) == S.COMPLETE, limit=40), hist(e)
    k = e["c"].lead.case(k["case_id"])
    assert k["state"] == "RESOLVED" and len(e["client"].lead_prompts()) == 1
    resume = next(h for h in transitions(e) if h["evidence"].get("recovery_case") == k["case_id"]
                  and h["from_state"] == S.BLOCKED)
    assert resume["evidence"]["action"] == "wait" and resume["to_state"] == S.READY


# ------------------------------------------------------------------ 8. bounded -> Victor

def test_bounds_exhausted_reach_victor_with_a_precise_decision_and_alert(tmp_path):
    log = []
    # revamp B9: an invalid report first goes back into the same session; this test
    # is about exhausting the bound with invalid reports, so that repair is off
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: {"final": "I could not tell."},
                  extra=lead_toml(max_diagnoses=2) + "\n[automation]\nlead_report_repairs = 0\n")
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60), \
        hist(e)
    k = case(e)
    assert len(e["client"].lead_prompts()) == 2 and k["counters"]["diagnoses"] == 2
    d = c.obligations.decision(k["decision_id"])
    assert d["kind"] == "lead_escalation" and d["status"] == "open"
    assert "exhausted max_diagnoses (2/2)" in d["question"] and k["case_id"] in d["question"]
    assert d["evidence"]["bound"] == ["max_diagnoses", 2, 2]
    assert [a["kind"] for a in d["evidence"]["attempted"]].count("lead_call") == 2
    assert d["evidence"]["user_action"] == "" and not k["needs_human"]
    assert state(e) == S.BLOCKED and "NEEDS_VICTOR" in task(e)["blocker"]
    # the bridge's `lead` field (what raises the control center's alert)
    reader = CoordReader(c.store.path)
    lv = lead_view(reader.lead_case("game", TID), reader.lead_actions(k["case_id"]),
                   reader.lead_upgrade(reader.lead_case("game", TID)),
                   lead_settings(e["P"]), time.time())
    assert lv["state"] == "NEEDS_VICTOR" and lv["active"] and lv["decision_id"] == d["decision_id"]
    assert lv["counters"]["diagnoses"] == {"used": 2, "max": 2}
    for _ in range(3):
        c.tick()
    assert len(e["client"].lead_prompts()) == 2                     # bounded: no more calls
    # Victor answers the precise question: the case resumes with his instruction
    c.decision_answer("game", TID, d["decision_id"],
                      "Write OK at the start of src/a.txt (the check needs it).",
                      actor="victor", channel="control")
    assert run_until(c, lambda: state(e) == S.COMPLETE, limit=40), hist(e)
    assert c.lead.case(k["case_id"])["state"] == "RESOLVED"
    assert "Operator answer to lead recovery case " + k["case_id"] in log[-1]["prompt"]


def test_named_user_action_marks_the_case_needs_human(tmp_path):
    e = make_game(tmp_path, primary_script([]), lead=lambda ctx: lead_reply({
        "classification": "external",
        "hypothesis": "Roblox Studio is signed out, so no gallery capture can run.",
        "evidence_refs": ["studio log"],
        "action": {"kind": "ask_victor", "user_action": "Open Roblox Studio and sign in"},
        "question": "Please sign Roblox Studio in; should the task then retry the capture?",
        "options": ["signed in: retry the capture", "stop the task"],
        "recommended": "stop the task"}),
        extra=lead_toml())
    e["c"].approve("game", TID, [], "victor")
    assert run_until(e["c"], lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60)
    k = case(e)
    assert k["needs_human"] is True
    d = e["c"].obligations.decision(k["decision_id"])
    assert d["evidence"]["user_action"] == "Open Roblox Studio and sign in"
    assert "sign Roblox Studio in" in d["question"]


# ------------------------------------------------------------------ 9. served-model mismatch

@pytest.mark.parametrize("mode", ["astra_not_expected", "local_served", "local_route"])
def test_served_model_mismatch_fails_closed_without_fallback(tmp_path, mode):
    ff = E2EFoundry()
    expected = (OPUS, ASTRA)
    if mode == "astra_not_expected":
        expected = (OPUS,)
        ff.info["routes"] = ff.info["routes"][:1]
        ff.default_served = [ASTRA]                      # the fallback served the call
    elif mode == "local_served":
        ff.default_served = [OPUS, "qwen3-coder-local"]
    else:
        ff.info["routes"][1]["source"] = "local"
    e = make_game(tmp_path, primary_script([]), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml(expected=expected), ff=ff)
    e["c"].approve("game", TID, [], "victor")
    assert run_until(e["c"], lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60), \
        hist(e)
    k = case(e)
    assert k["proposed_action"] == {}                    # nothing from the report was used
    d = e["c"].obligations.decision(k["decision_id"])
    if mode == "local_route":
        assert not e["client"].lead_starts()             # readiness refused before any call
        assert "no_local_route" in d["question"]
    else:
        assert len(e["client"].lead_starts()) == 1       # never re-routed / retried
        sm = next(a for a in acts(e, k["case_id"]) if a["kind"] == "served_model")
        assert sm["detail"]["verdict"] == "mismatch"
        assert e["client"].lead_starts()[0]["session_id"] in e["client"].aborts
        assert "no fallback" in next(a for a in acts(e, k["case_id"])
                                     if a["kind"] == "lead_call")["result"]
    for _ in range(3):
        e["c"].tick()
    assert state(e) == S.BLOCKED and len(e["client"].primary_starts()) == 3


# ------------------------------------------------------------------ operator controls

def test_project_pause_and_takeover_hold_the_case(tmp_path):
    e = make_game(tmp_path, primary_script([]), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: state(e) == S.BLOCKED, limit=40)
    c.store.set_project_paused("game", True, "operator pause")
    for _ in range(3):
        c.tick()
    assert not e["client"].lead_starts()                 # a paused project starts no lead call
    assert "project paused" in task(e)["next_action"]
    c.store.set_project_paused("game", False, "")
    cid = task(e)["data"]["recovery_case"]
    c.lead.takeover(cid, "victor (control center)", "I'll handle it", P=e["P"])
    for _ in range(3):
        c.tick()
    assert not e["client"].lead_starts() and state(e) == S.BLOCKED
    assert "taken over by victor" in task(e)["next_action"]
    c.lead.instruct(cid, "Check the OK marker first.", "victor (control center)")
    c.lead.pause_case(cid, "victor (control center)", paused=False)
    assert run_until(c, lambda: state(e) == S.COMPLETE, limit=40), hist(e)
    assert "Check the OK marker first." in e["client"].lead_prompts()[0]
    # an operator unblock instead of the lead: covered by unblock() cancelling the case
    assert c.lead.case(cid)["state"] == "RESOLVED"


def test_operator_unblock_cancels_the_case(tmp_path):
    e = make_game(tmp_path, primary_script([]), lead=lambda ctx: {"final": "thinking"},
                  extra=lead_toml())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: state(e) == S.BLOCKED, limit=40)
    cid = task(e)["data"]["recovery_case"]
    c.unblock("game", TID, "I fixed it by hand", reset_attempts=False, actor="victor")
    assert c.lead.case(cid)["state"] == "CANCELLED"
    assert "recovery_case" not in task(e)["data"]


# ------------------------------------------------------------------ 11. disabled

def test_disabled_lead_manager_keeps_the_old_behavior(tmp_path):
    log = []
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml(enabled=False))
    c = e["c"]
    assert c.lead is None
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: state(e) == S.FAILED, limit=60), hist(e)
    assert "3 implementation attempts used" in [h for h in transitions(e)
                                               if h["to_state"] == S.FAILED][0]["evidence"]["reason"]
    assert not e["client"].lead_starts() and "recovery_case" not in task(e)["data"]
    assert not c.store.q("SELECT 1 FROM sqlite_master WHERE name='recovery_cases'")
    assert c.lead is None


# ------------------------------------------------------------------ review A regressions

def test_lead_bash_allowlist_denies_writers_and_executors(tmp_path):
    """Review A #3: `--output=`, `rg --pre=`, `git grep -O`, redirects and
    secret paths are denied after the read-only allows (last match wins)."""
    from foundry_coordinator.lead import kilo_decision, lead_permissions
    e = make_game(tmp_path, primary_script([]), extra=lead_toml())
    rules = lead_permissions(e["P"], e["P"].adapter.describe(TID))
    assert is_read_only(rules)
    for cmd in ("git log -1 --output=src/a.txt", "git diff --output=src/a.txt HEAD~1",
                "git show --output=src/a.txt HEAD", "rg --pre=python x .",
                "git grep --open-files-in-pager=python x", "git grep -Opython x",
                "cat src/a.txt > src/b.txt", "cat ../_secrets/key", "cat C:/Users/x/.ssh/id",
                "git log $(rm -rf src)"):
        assert kilo_decision(rules, "bash", cmd) == "deny", cmd
    for cmd in ("git status", "git log -5 --oneline", "git diff HEAD~1", "cat src/a.txt",
                "grep -n OK src/a.txt"):
        assert kilo_decision(rules, "bash", cmd) == "allow", cmd
    # the lead stays inside the workspace and never reads secret stores
    ext = [r for r in rules if r["permission"] == "external_directory"]
    assert {"permission": "external_directory", "pattern": "*", "action": "deny"} in ext
    assert all(r["action"] == "deny" for r in ext)        # W2: hard denies add secret-store denies
    assert kilo_decision(rules, "read", "C:/foundry/_secrets/kilo.key") == "deny"
    # a later allow of a writer makes the rules not read-only
    assert not is_read_only(rules + [{"permission": "bash", "pattern": "rg *", "action": "allow"}])
    assert not is_read_only(rules + [{"permission": "external_directory", "pattern": "*",
                                      "action": "allow"}])


def test_lead_refuses_to_start_when_its_rules_are_not_read_only(tmp_path, monkeypatch):
    from foundry_coordinator import lead as LM
    real = LM.lead_permissions
    monkeypatch.setattr(LM, "lead_permissions", lambda P, spec=None: real(P, spec) + [
        {"permission": "bash", "pattern": "python *", "action": "allow"}])
    e = make_game(tmp_path, primary_script([]), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml())
    e["c"].approve("game", TID, [], "victor")
    assert run_until(e["c"], lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60)
    assert not e["client"].lead_starts()
    d = e["c"].obligations.decision(case(e)["decision_id"])
    assert "not read-only" in d["question"]


def test_repair_session_bash_is_restricted(tmp_path):
    """Review A #2: the repair session shell is deny-all except the venv's
    `python -m pytest` and read-only git."""
    from foundry_coordinator.lead import kilo_decision, repair_permissions
    e = make_game(tmp_path, primary_script([]), extra=lead_toml())
    py = sys.executable
    rules = repair_permissions(e["P"], e["P"].adapter.describe(TID), python=py)
    for cmd in (f"git -C {e['root']} commit -am x", "git -c x=y commit -am x",
                "python -c \"open('C:/Roblox/game/x','w')\"", "sqlite3 state/c.sqlite x",
                f"{py} -c x", f"{py} -m pytest tests -p evil", f"{py} -m pytest {e['root']}",
                f"{py} -m pytest ../game", "git diff --output=x", "git log > x", "rm -rf .",
                "git commit -am x", "git push"):
        assert kilo_decision(rules, "bash", cmd) == "deny", cmd
    for cmd in (f"{py} -m pytest tests/test_checker.py -q",
                f"{py.replace(chr(92), '/')} -m pytest tests/test_checker.py::test_x",
                "git status", "git diff", "git log -3", "git show HEAD"):
        assert kilo_decision(rules, "bash", cmd) == "allow", cmd
    assert [r for r in rules if r["permission"] == "external_directory"][-1]["action"] == "deny"


class _GameWriter(E2EClient):
    """A lead session that (despite its rules) writes the game workspace."""

    def _dispatch(self, ctx):
        md = self.sessions[ctx["session_id"]]["metadata"]
        if md.get("role") == "lead":
            with open(os.path.join(ctx["workspace"], "src", "a.txt"), "a") as fh:
                fh.write("lead was here\n")
        return super()._dispatch(ctx)


class _Hanging(E2EClient):
    """Lead sessions stay busy (the call is in flight)."""

    def state(self, session_id, *, directory, since_ts):
        from foundry_coordinator.adapters.base import SessionState
        if self.sessions.get(session_id, {}).get("metadata", {}).get("role") == "lead":
            return SessionState(status="busy")
        return super().state(session_id, directory=directory, since_ts=since_ts)


def _game_with(tmp_path, client, **lead_kw):
    root, _ = make_repo(tmp_path, "game", [TASK], extra_files={"src/a.txt": "start\n"})
    prof = write_profile(tmp_path, "game", root, extra=UI_OVERRIDE + lead_toml(**lead_kw))
    ff = E2EFoundry()
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff},
                    store_path=tmp_path / "state" / "coordinator.sqlite")
    return {"c": c, "client": client, "ff": ff, "root": root, "P": c.projects["game"]}


def test_lead_session_that_changes_the_game_is_discarded(tmp_path):
    """Review A #2/#3 mechanical guard: game HEAD/status, router checkout and
    the task's coordinator rows are snapshotted before the lead session and
    compared after; any change aborts it and discards the report."""
    client = _GameWriter(primary_script([]), lambda ctx: lead_reply(game_assignment()))
    e = _game_with(tmp_path, client)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60), hist(e)
    k = case(e)
    assert k["proposed_action"] == {}                       # the report was never used
    gv = [a for a in acts(e, k["case_id"]) if a["kind"] == "guard_violation"]
    assert gv and "game workspace" in gv[0]["result"]
    assert client.lead_starts()[0]["session_id"] in client.aborts
    assert "changed what it must never touch" in c.obligations.decision(k["decision_id"])["question"]
    # the workspace lock was held for the session and released after it
    assert not [lk for lk in c.store.locks() if lk["task_id"] == TID]


def test_write_access_lead_may_change_the_game_and_its_report_is_used(tmp_path):
    """Victor 2026-10-21: with [lead_manager] write_access the lead fixes blockers
    itself. A game-workspace change is recorded as a lead_fix, not a guard
    violation, and the report is used."""
    client = _GameWriter(primary_script([]), lambda ctx: lead_reply(game_assignment()))
    e = _game_with(tmp_path, client, write_access=True)
    c = e["c"]
    c.approve("game", TID, [], "victor")

    def fixed():
        k = case(e)
        return bool(k) and any(a["kind"] == "lead_fix" for a in acts(e, k["case_id"]))
    assert run_until(c, fixed, limit=60), hist(e)
    k = case(e)
    assert not [a for a in acts(e, k["case_id"]) if a["kind"] == "guard_violation"]
    assert "lead was here" in (e["root"] / "src" / "a.txt").read_text()


def test_write_access_permissions_allow_everything_but_secrets_and_questions(tmp_path):
    from foundry_coordinator.lead import kilo_decision, lead_write_permissions, is_read_only
    e = make_game(tmp_path, primary_script([]), extra=lead_toml(write_access=True))
    rules = lead_write_permissions(e["P"], e["P"].adapter.describe(TID))
    assert not is_read_only(rules)
    for perm, val in (("bash", "git commit -m x -- src/a.txt"), ("edit", "src/a.txt"),
                      ("task", "explore"), ("robloxstudio-mcp_run_code", "*"),
                      ("external_directory", "C:/elsewhere/x")):
        assert kilo_decision(rules, perm, val) == "allow", (perm, val)
    for perm, val in (("read", "config/_secrets.toml"), ("read", "app.env"),
                      ("edit", "my_secret_key.txt"), ("question", "anything"),
                      # revamp B6: no subagents of its own beyond explore
                      ("task", "au-helper"), ("task", "au-planner")):
        assert kilo_decision(rules, perm, val) == "deny", (perm, val)


def test_victor_authorizing_another_round_runs_one_more_bounded_round(tmp_path):
    """Review A #4: 'authorize another bounded round' raises the exhausted
    bound once (journaled) and diagnoses again; it never wedges in RESUMING."""
    log = []
    # revamp B9: an invalid report first goes back into the same session; this test
    # is about exhausting the bound with invalid reports, so that repair is off
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: {"final": "I could not tell."},
                  extra=lead_toml(max_diagnoses=1) + "\n[automation]\nlead_report_repairs = 0\n")
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60), hist(e)
    k = case(e)
    assert len(e["client"].diag_prompts()) == 1
    c.decision_answer("game", TID, k["decision_id"], "authorize another bounded round",
                      actor="victor", channel="control")
    for _ in range(15):
        c.tick()
    k2 = c.lead.case(k["case_id"])
    assert k2["state"] == "NEEDS_VICTOR" and k2["decision_id"] != k["decision_id"], acts(
        e, k["case_id"])
    assert state(e) == S.BLOCKED
    assert len(e["client"].diag_prompts()) == 2                 # exactly one more round
    rnd = [a for a in acts(e, k["case_id"]) if a["kind"] == "extra_round"]
    assert len(rnd) == 1 and rnd[0]["detail"]["raised"] == {"max_diagnoses": 1}
    assert rnd[0]["detail"]["to"] == "DIAGNOSING"
    d2 = c.obligations.decision(k2["decision_id"])
    assert "exhausted max_diagnoses (2/2)" in d2["question"]
    # the round's question is answerable again: an instruction resumes the task
    c.decision_answer("game", TID, k2["decision_id"],
                      "Write OK at the start of src/a.txt (the check needs it).",
                      actor="victor", channel="control")
    assert run_until(c, lambda: state(e) == S.COMPLETE, limit=40), hist(e)


def test_cross_case_repair_loop_is_bounded_per_task(tmp_path):
    """Review A #1: each game repair commit moves HEAD; the returning blocker is
    a REPEAT (repeat key without the revision), so cases, lead calls and
    granted extra attempts are bounded per task and Victor gets one precise
    question."""
    log = []
    e = make_game(tmp_path, primary_script(log, good_when=()),
                  lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml(max_diagnoses=1, max_candidates=1))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    for _ in range(250):
        c.tick()
    cs = cases(e)
    assert len(e["client"].lead_prompts()) <= 3, [r["state"] for r in cs]
    assert len(cs) <= 3
    assert len({r["repeat_key"] for r in cs}) == 1 and len({r["fingerprint"] for r in cs}) > 1
    last = c.lead.case(cs[-1]["case_id"])
    assert last["state"] == "NEEDS_VICTOR" and state(e) == S.BLOCKED
    d = c.obligations.decision(last["decision_id"])
    assert d["kind"] == "lead_escalation" and d["status"] == "open"
    assert any(b in d["question"] for b in ("max_fingerprint_repeats", "max_cases_per_task",
                                            "max_extra_attempts_per_task",
                                            "max_lead_calls_per_task"))
    totals = c.lead.task_totals("game", TID)
    assert totals["extra_attempts"] <= 2 and totals["lead_calls"] <= 6
    assert len(log) <= 3 + 2                     # ordinary attempts + at most 2 lead extras


def test_reverify_with_a_moved_head_verifies_again_without_a_paid_session(tmp_path):
    """A reverify resume after HEAD moved (committed work, nothing uncommitted)
    re-runs verification instead of starting a paid primary session."""
    log = []
    e = make_game(tmp_path, primary_script(log, good_when=()),
                  lead=lambda ctx: lead_reply({
                      "classification": "evidence",
                      "hypothesis": "The last verification ran before the fix landed.",
                      "evidence_refs": ["git log"], "action": {"kind": "reverify"}}),
                  extra=lead_toml())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: state(e) == S.BLOCKED, limit=60), hist(e)
    with open(os.path.join(str(e["root"]), "src", "a.txt"), "w") as fh:
        fh.write("OK fixed by hand\n")
    git(e["root"], "commit", "-qam", "operator fix")             # HEAD moves
    assert run_until(c, lambda: any(h["evidence"].get("action") == "reverify"
                                    for h in transitions(e)), limit=60), hist(e)
    resume = next(h for h in transitions(e) if h["evidence"].get("action") == "reverify")
    assert resume["from_state"] == S.BLOCKED and resume["to_state"] == S.VERIFYING
    assert resume["evidence"].get("head_moved")
    for _ in range(3):
        c.tick()
    assert len(log) == 3                                    # no extra primary session


def test_cancel_cancels_the_case_and_its_sessions(tmp_path):
    """Review A #5: Coordinator.cancel takes the live case with it."""
    e = _game_with(tmp_path, _Hanging(primary_script([]), lambda ctx: {"final": "x"}))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: bool(e["client"].lead_starts()), limit=60), hist(e)
    cid = task(e)["data"]["recovery_case"]
    sid = e["client"].lead_starts()[0]["session_id"]
    c.cancel("game", TID, "not needed", actor="victor")
    k = c.lead.case(cid)
    assert k["state"] == "CANCELLED" and sid in e["client"].aborts
    assert c.store.session(sid)["status"] == "ended"
    assert state(e) == S.CANCELLED
    assert not [lk for lk in c.store.locks() if lk["task_id"] == TID]


def test_disabling_the_lead_manager_cancels_a_live_case(tmp_path):
    e = _game_with(tmp_path, _Hanging(primary_script([]), lambda ctx: {"final": "x"}))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: bool(e["client"].lead_starts()), limit=60), hist(e)
    cid = task(e)["data"]["recovery_case"]
    e["P"].profile.lead_manager["enabled"] = False
    c.tick()
    from foundry_coordinator.lead import LeadManager
    k = LeadManager(c).case(cid)
    assert k["state"] == "CANCELLED" and "disabled" in k["outcome"]
    assert "recovery_case" not in task(e)["data"] and state(e) == S.BLOCKED
    assert e["client"].lead_starts()[0]["session_id"] in e["client"].aborts


def test_false_blocker_with_recovered_evidence_reverifies_before_a_case(tmp_path, monkeypatch):
    """Review A #9: evidence recovered by the deterministic recheck re-runs
    verification (BLOCKED -> VERIFYING) before any case or paid lead call."""
    from foundry_coordinator.engine import Coordinator
    real = Coordinator._lead_recheck
    seen = []

    def recheck(self, P, t):
        out = real(self, P, t)
        if not seen:
            seen.append(1)
            out["evidence_recovered"] = 2
        return out
    monkeypatch.setattr(Coordinator, "_lead_recheck", recheck)
    log = []
    e = make_game(tmp_path, primary_script(log), lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: bool(cases(e)), limit=60), hist(e)
    tr = transitions(e)
    rv = [i for i, h in enumerate(tr) if h["to_state"] == S.VERIFYING and
          str(h["evidence"].get("decision_id") or "").startswith("lead_recheck:")]
    assert len(rv) == 1 and tr[rv[0]]["from_state"] == S.BLOCKED
    assert tr[rv[0]]["evidence"]["evidence_recovered"] == 2
    first_case = next(i for i, h in enumerate(tr) if h["evidence"].get("recovery_case"))
    assert rv[0] < first_case                       # re-verified before any case
    assert len(e["client"].lead_prompts()) <= 1

"""Rules streamline (2026-10-27, reports/RULES_STREAMLINE_PLAN.md): the coordinator side.

  1. push ownership: the coordinator pushes master after closeout verification; no
     session pushes, in any phase (verify_only reads as push; a track never pushes)
  2. one stop protocol: BLOCKED + blocker_kind product | tooling | user_action; the old
     NEEDS_HUMAN reads as user_action; a timeout never applies a human act
  3. prompts: the kickoff points to SESSION_CORE.md (never "applies in full"); track-aware
     BUILD / CLOSE; a repair reruns only the affected checks; no dead text; a continuation
     is a short pointer
  4. permissions: .plan/tmp/** editable, docs/memory never; Studio publish / upload /
     experience / import denied; the lead gets the implementer's skill map;
     KILO_MCP_PROFILES.md leaves kilo.jsonc `instructions`
  5. 2D: foundry-media and the foundry-images skill follow the 2D decision
  6. scope: docs/memory/** is on the default allow-list; a denial is told with its reason
  7. lead: one authority text; a diagnosis that still holds may repeat with a new action

Scripted sessions, fake Foundry and temp git repos only.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

from foundry_automation import role_rules as RR
from foundry_automation import service_map as SM
from foundry_automation.schema import DEFAULT_SCOPE_RULES
from foundry_coordinator import decision_policy as DP
from foundry_coordinator import kilo_config as KC
from foundry_coordinator import scope as SC
from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import (TaskSpec, blocker_kind, report_instructions,
                                               report_pointer, report_status)
from foundry_coordinator.adapters.project import SCRATCH_GLOB
from foundry_coordinator.engine import TRACK_BRIEF
from foundry_coordinator.lead import report_contract, validate_report
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
from tests.coordinator.helpers import coordinator, git, make_repo, run_until, write_profile
from tests.coordinator.test_au_review_evidence import (PLAN_REL, T, _commit, _write,  # noqa: F401
                                                       repo)
from tests.coordinator.test_fast_lane import GREEN, drive, env  # noqa: F401
from tests.coordinator.test_lead_e2e import TID, Clock, lead_toml, make_game
from tests.coordinator.test_revamp_gate import _session_closeout

LC = "\n[lifecycle]\nenabled = true\n"
T1 = {"id": "T1", "title": "t", "spec": "Write src/a.txt containing OK.", "scope": ["src/a.txt"]}


def report(status="DONE", **kw):
    return "```coordinator-report\n" + json.dumps({"status": status, **kw}) + "\n```"


def _state(c, tid="T1"):
    return c.store.task("game", tid)["state"]


def _decisions(c, tid, kind=None):
    return [d for d in c.obligations.all_decisions("game", tid)
            if kind is None or d["kind"] == kind]


def _events(c, kind):
    return [json.loads(r["detail"] or "{}") for r in c.store.q(
        "SELECT detail FROM events WHERE kind=?", (kind,))]


def _game(tmp_path, script, extra=LC, clock=None):
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root, extra=extra)
    c = coordinator(tmp_path, [prof], {"game": script}, clock=clock)
    c.approve("game", "T1", [], "victor")
    return c, root


# ======================================================================= 1. push ownership

def test_a_session_closeout_is_pushed_by_the_coordinator_even_under_verify_only(env):
    """Before: verify_only made the CLOSE session push and failed coordinator.published
    when it obeyed AGENT_RULES ("sessions never push"). Now the coordinator pushes."""
    e = env([GREEN])
    P = _session_closeout(e)                    # a real CLOSE session that does not push
    P.profile.publish["mode"] = "verify_only"
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    assert t["data"].get("closeout_session")                  # the session closeout ran
    remote = git(e.repo, "ls-remote", "origin", "main").split()[0]
    assert remote == git(e.repo, "rev-parse", "HEAD")
    rows = {r["check_name"]: r["result"] for r in e.c.store.q(
        "SELECT check_name, result FROM verifications WHERE set_id=?", (t["closeout_id"],))}
    assert rows["coordinator.published"] == "PASS"


def test_no_session_may_push_in_any_phase(repo):
    a = repo["a"]
    spec = a.describe(T)
    for phase in ("implement", "closeout"):
        rules = a.session_permissions(spec, phase)
        for cmd in ("git push", "git push origin master", "git push -u origin master"):
            assert RR.decide(rules, "bash", cmd)[0] == "deny", (phase, cmd)
    # the hard denies every role carries, deployed role rules or not
    for role in RR.RULE_ROLES:
        lst = RR.session_rules(role, None, "C:/w")
        assert RR.decide(lst, "bash", "git push origin master")[0] == "deny", role
    floor = RR.floor_rules("implementer", "C:/w", ["src/a.txt"])
    assert RR.decide(floor, "bash", "git push")[0] == "deny"


def test_the_close_prompt_has_no_push_duty_and_a_track_close_skips_the_studio_rows(repo):
    a, head = repo["a"], repo["head"]
    spec = a.describe(T)
    prompt = a.closeout_prompt(spec, {"base": head})
    assert "Never push" in prompt and "git push origin" not in prompt
    assert "ls-remote" not in prompt
    assert f"done_check.py {T} until" in prompt and "--skip-rows" not in prompt
    a.p.__dict__["track"] = "ui"
    try:
        track = a.closeout_prompt(a.describe(T), {"base": head})
    finally:
        a.p.__dict__.pop("track", None)
    assert f"done_check.py {T} --skip-rows GATE,BOOT,EVIDENCE until" in track
    assert "a track never pushes" in track and "studio.lock" in track


# ======================================================================= 2. stop protocol

def test_blocker_kind_mapping_and_the_needs_human_compatibility():
    assert blocker_kind({"status": "BLOCKED", "blocker_kind": "product"}) == "product"
    assert blocker_kind({"status": "BLOCKED"}) == "product"                 # the default
    for k in ("tooling", "harness", "environment", "infra", "coordinator"):
        assert blocker_kind({"status": "BLOCKED", "blocker_kind": k}) == "tooling", k
    for k in ("user_action", "user-action", "human", "owner"):
        assert blocker_kind({"status": "BLOCKED", "blocker_kind": k}) == "user_action", k
    # the legacy status word: BLOCKED + user_action, whatever kind it names
    assert blocker_kind({"status": "NEEDS_HUMAN", "blocker_kind": "product"}) == "user_action"
    assert report_status({"status": "needs_human"}) == "BLOCKED"
    text = report_instructions()
    assert "NEEDS_HUMAN" not in text and "ACTION NEEDED" not in text
    assert "product | tooling | user_action" in text and '"next"' in text
    assert "carry on with the steps that don't need it" in text


def test_a_tooling_blocker_with_options_is_never_a_decision(tmp_path):
    c, _ = _game(tmp_path, lambda ctx: {"final": report(
        "BLOCKED", blocker="the gate runner keeps exiting 2", blocker_kind="tooling",
        options=["retry", "skip the gate"], recommended="retry")})
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=12)
    assert not _decisions(c, "T1", "implementer_blocked")
    t = c.store.task("game", "T1")
    assert "decision_wait" not in (t["data"] or {})


def test_a_tooling_blocker_goes_to_the_lead(tmp_path):
    e = make_game(tmp_path, lambda ctx: {"final": report(
        "BLOCKED", blocker="which?", options=["a", "b"], blocker_kind="tooling")},
        lead=lambda ctx: {"final": "hmm"}, extra=LC + lead_toml(), clock=Clock())
    e["c"].approve("game", TID, [], "victor")
    assert run_until(e["c"], lambda: e["c"].store.task("game", TID)["state"] == S.BLOCKED,
                     limit=12)
    for _ in range(4):
        e["c"].tick()
    assert e["c"].lead.cases("game", TID)
    assert not [d for d in e["c"].obligations.all_decisions("game", TID)
                if d["kind"] == "implementer_blocked"]


def test_a_user_action_blocker_waits_for_the_owner_without_a_lead_or_a_timeout(tmp_path):
    e = make_game(tmp_path, lambda ctx: {"final": report(
        "BLOCKED", blocker="log in to the Roblox creator dashboard and accept the new terms",
        blocker_kind="user_action")},
        lead=lambda ctx: {"final": "hmm"}, extra=LC + lead_toml(), clock=Clock())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: c.store.task("game", TID)["state"] == S.BLOCKED, limit=12)
    for _ in range(4):
        c.tick()
    assert not c.lead.cases("game", TID)                         # a model cannot do it
    t = c.store.task("game", TID)
    assert t["state"] == S.BLOCKED and "user_action" in t["blocker"]
    notes = [n for n in _events(c, "operator_notice") if n.get("blocker_kind") == "user_action"]
    assert notes and "accept the new terms" in notes[0]["text"]


def test_a_legacy_needs_human_report_with_options_is_a_decision_no_timeout_answers(tmp_path):
    clock = Clock()
    c, _ = _game(tmp_path, lambda ctx: {"final": report(
        "NEEDS_HUMAN", blocker="the Studio plugin needs a person", options=[
            "the owner reinstalls the plugin", "skip the sprint"])}, clock=clock)
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=12)
    [d] = _decisions(c, "T1", "implementer_blocked")
    assert d["evidence"]["blocker_kind"] == "user_action"
    assert d["recommended"] is None and not d.get("deadline_ts")
    clock.advance(120 * 60)
    for _ in range(3):
        c.tick()
    assert c.obligations.decision(d["decision_id"])["status"] == "open"
    assert "no timeout" in c.store.task("game", "T1")["blocker"]


def test_a_product_recommendation_that_needs_a_human_act_parks_after_the_timeout(tmp_path):
    """audit D (2026-10-28): only a user_action blocker waits without a timeout; a
    human act nobody did is never applied, the sprint parks instead."""
    c, _ = _game(tmp_path, lambda ctx: {"final": report(
        "BLOCKED", blocker="Rojo plugin state", blocker_kind="product",
        options=["Victor clicks Connect in Studio", "drop the live check"],
        recommended="Victor clicks Connect in Studio")})
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=12)
    [d] = _decisions(c, "T1", "implementer_blocked")
    assert json.loads(d["recommended"]) == DP.PARK and d.get("deadline_ts")
    assert d["recommendation"] == "Victor clicks Connect in Studio"      # still shown


def test_human_act_options_and_the_sweep_guard(tmp_path):
    for yes in ("Victor clicks Connect", "the owner provides the icons", "operator logs in",
                "do it manually", "You must restart Studio"):
        assert DP.needs_human_act(yes), yes
    for no in ("grid layout", DP.PARK, DP.CONTINUE_ONCE, "approve: add src/b.txt",
               "use the server-side counter", "show the user a toast"):
        assert not DP.needs_human_act(no), no
    # a decision opened before the change may still carry a timed human act: the sweep
    # never applies it; audit D: it parks instead of waiting forever
    clock = Clock()
    c, _ = _game(tmp_path, lambda ctx: {"final": report("DONE")}, clock=clock)
    did = c.obligations.open_decision("game", "T1", "implementer_blocked", "q?",
                                      options=["Victor clicks Connect", "park"],
                                      recommendation="park")
    c.store.x("UPDATE decisions SET recommended=?, deadline_ts=?, timeout_minutes=30 "
              "WHERE decision_id=?", (json.dumps("Victor clicks Connect"), clock() - 1, did))
    DP.sweep(c, force=True)
    d = c.obligations.decision(did)
    assert d["status"] == "answered" and d["answer"] == DP.PARK
    assert _events(c, "decision_timeout_skipped")


# ======================================================================= 3. prompts

def test_the_kickoff_points_to_session_core_and_never_says_in_full(repo):
    a = repo["a"]
    spec = a.describe(T)
    fallback = a.implement_prompt(spec, {"attempt": 1, "run_id": "r1"})
    assert "SESSION_CORE" not in fallback                 # the AU repo has no core yet
    _write(Path(a.p.workspace), "docs/rules/SESSION_CORE.md", "# core\n")
    prompt = a.implement_prompt(spec, {"attempt": 1, "run_id": "r1"})
    assert "docs/rules/SESSION_CORE.md is the session core; read it first" in prompt
    assert "AGENT_RULES.md is reference: read only the sections" in prompt
    for p in (prompt, fallback, a.closeout_prompt(spec, {"base": "x"})):
        assert "in full" not in p and ".clinerules" not in p
        assert "NEEDS_HUMAN" not in p and "ACTION NEEDED" not in p
        assert "UI-GT1" not in p and "UI-02 session" not in p
        assert "ExecutionPolicy" not in p
        assert f"Your workspace is `{a.p.workspace}`" in p


def test_prework_and_vision_helper_text_only_when_their_lane_runs(repo):
    a = repo["a"]
    prompt = a.implement_prompt(a.describe(T), {"attempt": 1, "run_id": "r1"})
    assert "PREWORK" not in prompt                # helpers are on demand: no prework lane
    assert "vision helper" not in a.recovery_policy(T)
    a.p.raw["lanes"] = {"enabled": True, "prework": {"enabled": True}}
    a.p.raw["automation"] = {"helpers": "proactive"}
    assert "PREWORK" in a.implement_prompt(a.describe(T), {"attempt": 1, "run_id": "r1"})


def test_a_repair_reruns_only_the_affected_checks(repo):
    a = repo["a"]
    spec = a.describe(T)
    ctx = {"attempt": 2, "run_id": "r2", "assignment": {
        "affected_checks_to_rerun": ["suite:UX_01", "lune:Kit"]}}
    prompt = a.repair_prompt(spec, ["suite UX_01 failed: Kit.layout"], ctx)
    assert "REPAIR session" in prompt
    assert "rerun ONLY the checks the fix affects: suite:UX_01, lune:Kit" in prompt
    assert "never rerun passed work" in prompt
    assert "-Files <the files you changed>" in prompt
    assert "including its Studio steps" not in prompt          # no whole-gate instruction
    assert "suite UX_01 failed: Kit.layout" in prompt
    plain = a.repair_prompt(spec, ["x"], {"attempt": 2})
    assert "the checks the findings below name" in plain


def test_track_prompts_have_no_studio_steps_no_push_and_no_main_checkout(repo):
    a = repo["a"]
    a.p.__dict__["track"] = "ui"
    try:
        spec = a.describe(T)
        build = a.implement_prompt(spec, {"attempt": 1, "run_id": "r1"})
        fix = a.repair_prompt(spec, ["Lune Kit failed"], {"attempt": 2})
    finally:
        a.p.__dict__.pop("track", None)
    for p in (build, fix):
        assert p.index("TRACK SPRINT") < p.index("Project rules")      # the notice on top
        assert f"done_check.py {T} --skip-rows GATE,BOOT,EVIDENCE" in p
        assert "tools/gate.ps1 -Sprint" not in p and "ButtonActions" not in p
        assert "Studio MCP tools" not in p
        assert "C:\\Roblox\\AnimalUprising" not in p and "never push" in p
    assert AnimalUprisingAdapter.TRACK_AWARE is True       # the engine adds no second brief
    assert "--skip-rows GATE,BOOT,EVIDENCE" in TRACK_BRIEF and "Never push" in TRACK_BRIEF


def test_a_continuation_is_a_short_pointer(repo):
    a = repo["a"]
    spec = a.describe(T)
    text = a.continue_prompt(spec, "helper proposals arrived")
    assert len(text) < 500 and len(report_instructions()) > 4 * len(report_pointer())
    assert "MEMORY_NOTE: file=" not in text and "coordinator-report" in text
    assert "next" in text and "user_action" in text


def test_the_helper_prompt_names_helper_feedback():
    from foundry_coordinator.helpers import HelperManager
    hm = HelperManager.__new__(HelperManager)
    text = hm.integration_prompt([{"unit_id": "u1", "kind": "code_proposal",
                                   "responsibility": "x", "persona": "p", "result": "r"}])
    assert '"helper_feedback": [{"unit": "<id>"' in text and "integrated | rejected" in text


# ======================================================================= 4. permissions

def test_scratch_is_editable_and_project_memory_never(repo):
    a = repo["a"]
    spec = a.describe(T)
    spec.scope.append("docs/memory/DECISIONS.md")              # e.g. after a scope grant
    scope = a.edit_scope(spec, "implement")
    assert SCRATCH_GLOB in scope
    assert not [x for x in scope if x.startswith("docs/memory")]
    rules = a.session_permissions(spec, "implement")
    assert RR.decide(rules, "edit", ".plan/tmp/scratch/a.lua")[0] == "allow"
    assert RR.decide(rules, "write", ".plan/tmp/notes.md")[0] == "allow"
    assert RR.decide(rules, "edit", "docs/memory/DECISIONS.md")[0] == "deny"
    assert RR.decide(rules, "edit", "src/client/Kit.lua")[0] == "allow"
    assert RR.decide(rules, "edit", "src/server/Other.lua")[0] == "deny"


def test_studio_publish_upload_experience_and_import_are_denied():
    for role in ("implementer", "lead"):
        lst = RR.agent_block_rules(role, None, "C:/w")
        for tool in ("robloxstudio-mcp_upload_asset", "robloxstudio-mcp_manage_experience",
                     "robloxstudio-mcp_import_rbxm", "robloxstudio-mcp_publish_place",
                     "Roblox_Studio_publish_game"):
            assert RR.decide(lst, tool, "*")[0] == "deny", (role, tool)
        assert RR.decide(lst, "robloxstudio-mcp_get_place_info", "*")[0] == "allow", role
        assert RR.decide(lst, "bash", "git push")[0] == "deny"
    errs, warns = RR.validate(RR.all_defaults())
    assert errs == [] and warns == []


SRC = textwrap.dedent('''\
    {
      "instructions": ["docs/rules/CLINERULES_CORE.md",
                       "C:\\\\Users\\\\x\\\\Foundry-Router\\\\config\\\\workstation\\\\KILO_MCP_PROFILES.md"],
      "mcp": {"Media_Gen": {"enabled": true}},
      "agent": {
        "code": {"permission": {"*": "allow", "skill": {"au-luau-development": "allow",
                                                         "au-roblox-*": "allow",
                                                         "foundry-images": "allow"}}},
        "lead": {"permission": {"*": "allow"}}
      }
    }
    ''')


def test_kilo_config_drops_retired_instructions_and_gives_the_lead_the_skill_map(tmp_path):
    rules = RR.all_defaults()
    p = KC.plan(SRC, rules, "C:/w")
    assert p["instructions_removed"] and "KILO_MCP_PROFILES" in p["instructions_removed"][0]
    from foundry_automation import jsonc
    back = jsonc.parse(p["text"])
    assert back["instructions"] == ["docs/rules/CLINERULES_CORE.md"]
    lead = back["agent"]["lead"]["permission"]["skill"]
    assert lead["au-luau-development"] == "allow" and lead["au-roblox-*"] == "allow"
    assert back["agent"]["code"]["permission"]["skill"]["foundry-images"] == "allow"
    # a track's file: same instruction cleanup; without `images` the skill is denied
    tr = KC.track_text(SRC, [], rules, "C:/w/.tracks/ui")
    tback = jsonc.parse(tr["text"])
    assert tback["instructions"] == ["docs/rules/CLINERULES_CORE.md"]
    code = tback["agent"]["code"]["permission"]["skill"]
    assert code["foundry-images"] == "deny" and code["au-luau-development"] == "allow"
    assert tback["agent"]["lead"]["permission"]["skill"]["au-roblox-*"] == "allow"


def test_kilo_instructions_load_session_core_once_the_workspace_has_it(tmp_path):
    from foundry_automation import jsonc
    rules = RR.all_defaults()
    assert jsonc.parse(KC.plan(SRC, rules, str(tmp_path))["text"])["instructions"] == [
        "docs/rules/CLINERULES_CORE.md"]                  # no core yet: the old one stays
    _write(tmp_path, "docs/rules/SESSION_CORE.md", "# core" + chr(10))
    p = KC.plan(SRC, rules, str(tmp_path))
    assert jsonc.parse(p["text"])["instructions"] == ["docs/rules/SESSION_CORE.md"]
    assert p["instructions_changed"]
    again = KC.plan(p["text"], rules, str(tmp_path))
    assert not again["instructions_changed"]              # idempotent
    tr = KC.track_text(SRC, [], rules, str(tmp_path))
    assert jsonc.parse(tr["text"])["instructions"] == ["docs/rules/SESSION_CORE.md"]


# ======================================================================= 5. 2D images

def test_foundry_media_rules_and_a_code_sprint_gets_no_image_tools(tmp_path):
    assert SM.foundry_media_denied(True) == []
    off = SM.foundry_media_denied(False)
    names = {r["permission"] for r in off}
    assert "foundry-media_*" in names
    assert {f"foundry-media_{t}" for t in ("generate_image", "edit_image", "image_job_status",
                                           "list_image_jobs", "image_settings")} <= names
    assert any(r["permission"] == "skill" and r["pattern"] == "foundry-images" for r in off)
    c, _ = _game(tmp_path, lambda ctx: {"writes": {"src/a.txt": "OK\n"}, "commit": "T1: ok",
                                        "final": report("DONE")}, extra="")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=40)
    [s] = [s for s in c.projects["game"].client.sessions.values()
           if s["metadata"].get("role") == "implementer"]
    assert RR.decide(s["permissions"], "foundry-media_generate_image", "*")[0] == "deny"
    assert RR.decide(s["permissions"], "skill", "foundry-images")[0] == "deny"


def test_the_2d_brief_follows_the_answer():
    from types import SimpleNamespace
    from foundry_coordinator import services as SV
    P = SimpleNamespace(id="game", profile=SimpleNamespace(raw={"automation": {
        "media_gen_images": "allow"}}))
    brief = SV.images_brief(None, P, {"task_id": "I1"}, ["images"])
    assert "foundry-images" in brief and "Media_Gen's image tools only when" in brief
    P = SimpleNamespace(id="game", profile=SimpleNamespace(raw={"automation": {
        "media_gen_images": "deny"}}))                     # (settings are cached per table)
    assert "do not generate images" in SV.images_brief(None, P, {"task_id": "I1"}, ["images"])
    assert SV.images_brief(None, P, {"task_id": "T1"}, ["studio"]) == ""


# ======================================================================= 6. scope

def test_docs_memory_is_on_the_default_allow_list():
    from types import SimpleNamespace
    assert any(r["path"] == "docs/memory/**" for r in DEFAULT_SCOPE_RULES)
    P = SimpleNamespace(profile=SimpleNamespace(raw={}))
    assert SC.rule_for(P, "GF-GATE1", "docs/memory/DECISIONS.md")["name"] == \
        "coordinator_project_memory"
    assert SC.rule_for(P, "GF-GATE1", "docs/memory/sprints/GF-GATE1.md")
    assert SC.rule_for(P, "GF-GATE1", "docs/other.md") is None


def _scope_game(tmp_path, first_final):
    prompts = []

    def script(ctx):
        prompts.append(ctx["prompt"])
        if len(prompts) == 1:
            return {"final": first_final}
        return {"writes": {"src/a.txt": "OK\n"}, "commit": "T1: ok", "final": report("DONE")}
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"src/a.txt": "start\n"})
    c = coordinator(tmp_path, [write_profile(tmp_path, "game", root)], {"game": script})
    c.approve("game", "T1", [], "victor")
    return c, root, prompts


def test_a_memory_scope_request_is_granted_at_once_without_asking_the_owner(tmp_path):
    block = "```SCOPE_REQUEST\n" + json.dumps({"path": "docs/memory/DECISIONS.md",
                                              "reason": "done_check SCOPE"}) + "\n```"
    c, root, prompts = _scope_game(tmp_path, block + report("BLOCKED"))
    assert run_until(c, lambda: _state(c) == S.COMPLETE, 60)
    assert not _decisions(c, "T1", "scope_request")
    [g] = c.store.task("game", "T1")["data"]["scope_grants"]
    assert g["rule"] == "coordinator_project_memory"
    assert "never edit or revert those files" in prompts[1] and "MEMORY_NOTE" in prompts[1]


def test_a_never_grantable_path_is_denied_by_rule_and_the_session_is_told_why(tmp_path):
    block = "```SCOPE_REQUEST\n" + json.dumps({"path": "kilo.jsonc", "reason": "x"}) + "\n```"
    c, root, prompts = _scope_game(tmp_path, block + report("BLOCKED"))
    assert run_until(c, lambda: len(prompts) >= 2, 20)
    assert not _decisions(c, "T1", "scope_request")
    assert "NOT granted: kilo.jsonc: denied by rule" in prompts[1]
    assert run_until(c, lambda: _state(c) == S.COMPLETE, 60)


# ======================================================================= 7. lead

def test_one_lead_authority_text():
    write = report_contract(True, True)
    assert "you stay read-only" not in write and "You fix workspace problems yourself" in write
    assert "you stay read-only" in report_contract(True, False)
    assert "materially different hypothesis" not in write


def _lead_report(hyp, assignment):
    return {"classification": "product", "hypothesis": hyp, "evidence_refs": ["src/a.txt:1"],
            "action": {"kind": "assign_game_repair", "assignment": assignment,
                       "files": ["src/a.txt"], "tests": [], "manifest": {"acceptance_tests": []}}}


def test_a_diagnosis_that_still_holds_may_repeat_with_a_new_action():
    from foundry_coordinator.lead import action_fp
    hyp = "The HUD binds the old remote; the fix was incomplete."
    first = _lead_report(hyp, "Rebind the remote in src/a.txt and rerun the HUD story.")
    clean, errs = validate_report(first, scope=["src/a.txt"])
    assert clean is not None, errs
    prior = [action_fp(clean["action"])]
    again = _lead_report(hyp, "Same diagnosis, still valid: retry with the server ack wait.")
    clean2, errs2 = validate_report(again, scope=["src/a.txt"], prior_hypotheses=[hyp],
                                    prior_actions=prior)
    assert clean2 is not None, errs2
    same, errs3 = validate_report(first, scope=["src/a.txt"], prior_hypotheses=[hyp],
                                  prior_actions=prior)
    assert same is None and any("both repeat" in e for e in errs3)


def test_the_formats_file_matches_the_parsers():
    """C:/Roblox/_scratch/COORDINATOR_FORMATS.md quotes these exact shapes; the parsers
    must keep reading them."""
    from foundry_coordinator.adapters.base import parse_report
    from foundry_coordinator.memory.notes import parse_notes, validate
    rep = parse_report("x\n```coordinator-report\n" + json.dumps({
        "status": "NOT DONE", "summary": "s", "next": "step 4"}) + "\n```")
    assert rep["next"] == "step 4"
    [n] = parse_notes('MEMORY_NOTE: file=DECISIONS.md heading="Studio: gate order" '
                      'text="Stories run before the gate."')
    assert validate(n) == "" and n["heading"] == "Studio: gate order"
    assert SC.parse_requests("```SCOPE_REQUEST\n" + json.dumps(
        {"path": "tests/GateRunner.lua", "reason": "boot"}) + "\n```") == [
        {"path": "tests/GateRunner.lua", "reason": "boot"}]


def test_done_check_gets_the_skip_rows_flag_and_falls_back_without_it(repo, monkeypatch):
    import subprocess
    a, head = repo["a"], repo["head"]
    spec = a.describe(T)
    calls = []
    real = subprocess.run

    def fake(cmd, **kw):
        calls.append(list(cmd))
        if "--skip-rows" in cmd:
            return subprocess.CompletedProcess(cmd, 2, "", "done_check.py: error: "
                                               "unrecognized arguments: --skip-rows")
        return subprocess.CompletedProcess(cmd, 0, "PASS COMMITS ok\nFAIL GATE no gate\n", "")
    monkeypatch.setattr(subprocess, "run", fake)
    try:
        rows = a._done_check(spec, "verify", head, skip=("GATE", "BOOT", "EVIDENCE"))
    finally:
        monkeypatch.setattr(subprocess, "run", real)
    assert calls[0][-2:] == ["--skip-rows", "GATE,BOOT,EVIDENCE"]
    assert "--skip-rows" not in calls[1]                     # the old checkout's fallback
    assert {r.check for r in rows} == {"au.done_check.COMMITS"}   # GATE post-filtered
    calls.clear()
    monkeypatch.setattr(subprocess, "run", fake)
    a._done_check(spec, "verify", head)
    assert "--skip-rows" not in calls[0]                     # the main checkout: every row
    assert sys and os                                        # (imports kept for helpers)

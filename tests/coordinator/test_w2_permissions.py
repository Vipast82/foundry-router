"""W2 (2026-10-22), coordinator side: the sprint's scope is the session's edit
scope; subagent rules come from the role rules; the lead's write mode keeps
every deny; the lead cannot approve a test change it made itself; a role-rules
deploy regenerates kilo.jsonc and asks the client to reload Kilo; the overlay
keeps local TOML edits (three-way); restart-scoped keys wait for the client's
safe-point restart."""

from __future__ import annotations

import copy
import json
import os
import time
import tomllib

from foundry_automation import document as D
from foundry_automation import jsonc
from foundry_automation import role_rules as RR
from foundry_coordinator import client_requests as CR
from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.adapters.project import subagent_rules
from foundry_coordinator.config_pull import ConfigPuller, startup_overlay
from foundry_coordinator.lead import (is_read_only, kilo_decision, lead_permissions,
                                      lead_self_changed_tests, lead_write_permissions,
                                      validate_report)

from .helpers import coordinator, git, make_repo, write_profile
from .test_config_pull import KILO, ConfigFoundry

SPEC = TaskSpec(project_id="game", task_id="T1", title="hud", spec="do it",
                scope=["src/hud.lua", "src/ui/*"], data={})


def _setup(tmp_path, extra=""):
    ws = tmp_path / "ws"
    ws.mkdir()
    prof = write_profile(tmp_path, "game", ws, extra=extra)
    f = ConfigFoundry()
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: None}, foundry={"game": f})
    return c, f, c.projects["game"]


def _doc(P, **limits):
    raw = tomllib.loads(open(P.profile.path, encoding="utf-8").read())
    doc, _, _ = D.import_profile(raw, "game")
    doc["settings"]["limits"].update(limits)
    return doc


def _deploy(c, f, doc, rev):
    f.deploy(doc, rev)
    cp = ConfigPuller(c)
    assert cp.sync("game", force=True) == "applied"
    return cp


# ------------------------------------------------------------------ W2.2 scope injection

def test_session_edit_scope_is_the_sprints_files(tmp_path):
    c, f, P = _setup(tmp_path)
    # no role rules deployed: the floor still scopes edits and keeps the hard denies
    rules = P.adapter.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "edit", "src/hud.lua") == "allow"
    assert kilo_decision(rules, "edit", "src/ui/a.lua") == "allow"
    assert kilo_decision(rules, "write", "docs/progress/T1_PLAN.md") == "allow"   # exempt
    assert kilo_decision(rules, "edit", "tests/GateRunner.lua") == "allow"        # allow-list
    assert kilo_decision(rules, "edit", "src/other.lua") == "deny"
    assert kilo_decision(rules, "edit", "kilo.jsonc") == "deny"
    assert kilo_decision(rules, "edit", ".kilo/x") == "deny"
    assert kilo_decision(rules, "read", "src/other.lua") is None       # untouched: Kilo's own
    assert RR.decide(rules, "bash", "git push --force")[0] == "deny"
    assert RR.decide(rules, "bash", "cat .env")[0] == "deny"
    assert RR.decide(rules, "robloxstudio-mcp_manage_monetization", "*")[0] == "deny"
    # with deployed role rules: same scope, the role's own rules around it
    _deploy(c, f, _doc(P), 1)
    rules = P.adapter.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "edit", "src/hud.lua") == "allow"
    assert kilo_decision(rules, "edit", "src/other.lua") == "deny"
    assert kilo_decision(rules, "edit", "kilo.jsonc") == "deny"
    assert kilo_decision(rules, "read", "src/other.lua") == "allow"
    assert kilo_decision(rules, "bash", "git status") == "allow"
    # a GUI edit scope ADDS to the sprint's files; a GUI deny still wins
    doc = _doc(P)
    doc["role_rules"]["implementer"]["edit_paths"] = {"allow": ["art/**"], "deny": ["src/ui/*"]}
    _deploy(c, f, doc, 2)
    rules = P.adapter.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "edit", "art/a.png") == "allow"
    assert kilo_decision(rules, "edit", "src/hud.lua") == "allow"
    assert kilo_decision(rules, "edit", "src/ui/a.lua") == "deny"
    # a sprint with no files may edit nothing
    empty = TaskSpec(project_id="game", task_id="T2", title="", spec="", scope=[],
                     allow_no_change=True)
    rules = P.adapter.session_permissions(empty, "implement")
    assert kilo_decision(rules, "edit", "src/hud.lua") == "deny"
    assert kilo_decision(rules, "edit", "art/a.png") == "allow"         # the GUI's own entry
    # helpers edit nothing; a repair session has no game scope
    assert kilo_decision(P.adapter.helper_permissions(SPEC), "edit", "src/hud.lua") == "deny"
    assert P.adapter.edit_scope(SPEC, "repair") is None
    assert P.adapter.edit_scope(None, "implement") is None


def test_read_only_folders_never_become_writable_in_a_session(tmp_path):
    c, f, P = _setup(tmp_path)
    doc = _doc(P)
    doc["role_rules"]["implementer"]["read_paths"]["allow"] = ["C:/Elsewhere/**"]
    _deploy(c, f, doc, 1)
    rules = P.adapter.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "external_directory", "C:/Elsewhere") == "allow"
    assert kilo_decision(rules, "read", "C:/Elsewhere/a.lua") == "allow"
    assert kilo_decision(rules, "edit", "C:/Elsewhere/a.lua") == "deny"
    assert kilo_decision(rules, "write", "C:/Elsewhere/a.lua") == "deny"


# ------------------------------------------------------------------ W2.4 subagents

def test_subagent_rules_come_from_the_roles_task_agents(tmp_path):
    c, f, P = _setup(tmp_path, extra="[automation]\nsubagents_allowed = ['explore', 'au-lint']\n")
    # no role rules: [automation] subagents_allowed
    rules = P.adapter.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "task", "au-lint") == "allow"
    assert kilo_decision(rules, "task", "au-planner") == "deny"
    doc = _doc(P)
    doc["role_rules"]["implementer"]["task_agents"] = ["explore", "au-docs"]
    doc["role_rules"]["lead"]["permissions"]["task"] = "allow"           # the GUI widens the lead
    _deploy(c, f, doc, 1)
    assert [r["pattern"] for r in subagent_rules(P.profile, "implementer")] == \
        ["*", "explore", "au-docs"]
    rules = P.adapter.session_permissions(SPEC, "implement")
    assert kilo_decision(rules, "task", "au-docs") == "allow"
    assert kilo_decision(rules, "task", "au-lint") == "deny"             # the GUI's list wins
    assert kilo_decision(rules, "task", "explore") == "allow"
    # the subagent rule sits before the role rules, so the GUI's `task` wins
    lead = P.adapter.session_permissions(SPEC, "lead")
    assert kilo_decision(lead, "task", "au-planner") == "allow"
    i_task = [i for i, r in enumerate(lead) if r["permission"] == "task"]
    assert lead[i_task[0]] == {"permission": "task", "pattern": "*", "action": "deny"}
    # the read-only lead still spawns nothing
    assert kilo_decision(lead_permissions(P, SPEC), "task", "explore") == "deny"


# ------------------------------------------------------------------ W2.3 lead write mode

def test_lead_write_mode_keeps_every_deny(tmp_path):
    c, f, P = _setup(tmp_path)
    doc = _doc(P)
    doc["role_rules"]["lead"]["bash"]["deny"] = ["git push", "git push *", "rm -rf *"]
    doc["role_rules"]["lead"]["edit_paths"]["deny"] = ["docs/locked/*"]
    _deploy(c, f, doc, 1)
    rules = lead_write_permissions(P, SPEC)
    assert not is_read_only(rules)
    assert rules[0] == {"permission": "*", "pattern": "*", "action": "allow"}
    d = lambda perm, v: kilo_decision(rules, perm, v)                    # noqa: E731
    for cmd in ("git push", "git push origin master", "git push origin +master",
                "git push origin HEAD:master", "git push origin :old", "git push --force",
                "git reset --hard HEAD~1", "git rebase master", "git commit --amend -m x",
                "git commit -n -m x", "git commit -m x -n", "git update-ref refs/heads/x y",
                "git filter-branch --all", "git -c core.hooksPath=x commit -m y",
                "git config core.hooksPath x", "git commit -m x --no-verify", "git clean -fdx",
                "git tag v1", "cat .env", "type .env.production", "printenv",
                "echo $env:FOUNDRY_COORDINATOR_KEY", "cat C:/Roblox/_secrets/key",
                "rm -rf src"):                                          # the GUI's own deny
        assert d("bash", cmd) == "deny", cmd
    for cmd in ("git status", "git add src/hud.lua", "git commit -m \"T1: fix\"",
                "git diff HEAD~1", "python -m pytest tests -q"):
        assert d("bash", cmd) == "allow", cmd
    # edits: the sprint's scope (plus exempt and allow-listed paths), never config files
    assert d("edit", "src/hud.lua") == "allow"
    assert d("edit", "docs/progress/T1_PLAN.md") == "allow"
    assert d("edit", "tests/GateRunner.lua") == "allow"
    assert d("edit", "src/other.lua") == "deny"
    assert d("edit", "docs/locked/a.md") == "deny"
    assert d("edit", "kilo.jsonc") == "deny"
    assert d("edit", "tools/hooks/pre-commit") == "deny"
    for perm in ("read", "edit", "write", "grep"):
        assert d(perm, ".env") == "deny" and d(perm, "config/.env.local") == "deny"
        assert d(perm, "secrets/x.json") == "deny"
        assert d(perm, "workstation-credentials.xml") == "deny"
    assert d("question", "*") == "deny"
    assert d("task", "explore") == "allow" and d("task", "au-planner") == "deny"
    assert d("agent_manager", "*") == "deny"
    for tool in ("Roblox_Studio_manage_monetization", "robloxstudio-mcp_upload_image",
                 "robloxstudio-mcp_insert_asset", "robloxstudio-mcp_set_script_source"):
        assert d(tool, "*") == "deny", tool
    assert d("robloxstudio-mcp_get_file_tree", "*") == "allow"
    assert d("robloxstudio-mcp_set_property", "*") == "allow"
    # the hard denies are the tail
    assert [(r["permission"], r["pattern"]) for r in rules[-len(RR.HARD_DENIES):]] == \
        [(h["permission"], h["pattern"]) for h in RR.HARD_DENIES]


def test_lead_write_mode_without_deployed_rules_keeps_the_floor(tmp_path):
    c, f, P = _setup(tmp_path)
    rules = lead_write_permissions(P, SPEC)
    assert kilo_decision(rules, "edit", "src/hud.lua") == "allow"
    assert kilo_decision(rules, "edit", "src/other.lua") == "deny"
    assert kilo_decision(rules, "edit", "kilo.jsonc") == "deny"
    assert kilo_decision(rules, "bash", "git push origin master") == "deny"
    assert kilo_decision(rules, "bash", "git rebase -i HEAD~2") == "deny"
    assert kilo_decision(rules, "bash", "git commit -m \"T1: x\"") == "allow"
    # the read-only lead keeps the hard denies too and stays read-only
    ro = lead_permissions(P, SPEC)
    assert is_read_only(ro)
    assert kilo_decision(ro, "bash", "cat .env") == "deny"
    assert kilo_decision(ro, "Roblox_Studio_manage_monetization", "*") == "deny"


def test_lead_cannot_approve_a_test_change_it_made_itself(tmp_path):
    root, _ = make_repo(tmp_path, "game", [{"id": "T1", "title": "t", "spec": "s",
                                            "scope": ["src/a.py"]}],
                        {"tests/test_a.py": "def test_a():\n    assert 1 == 1\n",
                         "tests/data.txt": "x\n", "src/a.py": "OK\n"})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: None})
    P = c.projects["game"]
    before = P.ws.head()
    assert lead_self_changed_tests(P, before) == {}
    # the lead (write mode) weakens a test and commits it, and edits a fixture
    (root / "tests" / "test_a.py").write_text("def test_a():\n    return\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "T1: weaken")
    (root / "tests" / "data.txt").write_text("y\n")                      # dirty, uncommitted
    (root / "src" / "a.py").write_text("OK OK\n")                       # not a test
    changed = lead_self_changed_tests(P, before)
    assert set(changed) == {"tests/test_a.py", "tests/data.txt"}
    assert any("assertion" in w for w in changed["tests/test_a.py"])
    rep = {"classification": "test_harness", "hypothesis": "The test asserts a removed widget.",
           "evidence_refs": ["tests/test_a.py:2"],
           "action": {"kind": "operate", "then": "resume", "operations": [
               {"op": "approve_test_change", "path": "tests/test_a.py",
                "reason": "the widget it asserts was removed"}]}}
    clean, errs = validate_report(copy.deepcopy(rep), full_authority=True)
    assert clean is not None and errs == []                              # the old path still works
    clean, errs = validate_report(copy.deepcopy(rep), full_authority=True,
                                  self_changed_tests=changed)
    assert clean is None and any("changed tests/test_a.py itself" in e for e in errs)
    other = copy.deepcopy(rep)
    other["action"]["operations"][0]["path"] = "tests/test_b.py"
    clean, errs = validate_report(other, full_authority=True, self_changed_tests=changed)
    assert clean is not None and errs == []


# ------------------------------------------------------------------ W2.4 kilo.jsonc + client requests

def test_role_rules_deploy_regenerates_kilo_jsonc_and_asks_for_a_reload(tmp_path):
    c, f, P = _setup(tmp_path)
    path = os.path.join(P.profile.workspace, "kilo.jsonc")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(KILO)
    cp = _deploy(c, f, _doc(P), 1)
    kinds = [e["kind"] for e in c.store.events("game", 30)]
    assert "kilo_config_applied" in kinds and "client_request" in kinds
    data = jsonc.parse(open(path, encoding="utf-8").read())
    code = data["agent"]["code"]["permission"]
    assert code["task"] == {"*": "deny", "explore": "allow"}
    assert code["*manage_monetization"] == "deny"
    assert code["edit"]["kilo.jsonc"] == "deny"
    assert "// AU kilo config" in open(path, encoding="utf-8").read()
    reqs = CR.pending(c.state_dir)
    assert [r["kind"] for r in reqs] == ["reload_kilo"]
    assert reqs[0]["project"] == "game" and reqs[0]["safe_point"] is True
    assert reqs[0]["detail"]["changed"] == ["code", "au-review"]
    assert os.path.isfile(reqs[0]["path"])
    # a settings-only revision leaves kilo.jsonc and the request alone
    text = open(path, encoding="utf-8").read()
    f.deploy(_doc(P, max_attempts=5), 2)
    assert cp.sync("game", force=True) == "applied"
    assert open(path, encoding="utf-8").read() == text
    assert CR.pending(c.state_dir)[0]["count"] == 1
    # a role-rules change regenerates again and refreshes the same request
    doc = _doc(P)
    doc["role_rules"]["implementer"]["task_agents"] = []
    f.deploy(doc, 3)
    assert cp.sync("game", force=True) == "applied"
    assert jsonc.parse(open(path, encoding="utf-8").read())["agent"]["code"]["permission"][
        "task"] == "deny"
    reqs = CR.pending(c.state_dir)
    assert len(reqs) == 1 and reqs[0]["count"] == 2 and reqs[0]["detail"]["rev"] == 3
    # the client consumes it
    assert CR.done(c.state_dir, reqs[0], "kilo restarted")
    assert CR.pending(c.state_dir) == []
    assert os.listdir(os.path.join(c.state_dir, "client-requests", "done"))


def test_missing_kilo_jsonc_is_an_event_not_a_rejection(tmp_path):
    c, f, P = _setup(tmp_path)
    _deploy(c, f, _doc(P), 1)
    kinds = [e["kind"] for e in c.store.events("game", 30)]
    assert "config_applied" in kinds and "kilo_config_skipped" in kinds
    assert CR.pending(c.state_dir) == []
    assert P.profile.raw.get("role_rules")                               # the rules still apply


def test_kilo_regeneration_can_be_switched_off(tmp_path):
    c, f, P = _setup(tmp_path, extra="[config_pull]\nkilo_config_apply = false\n")
    path = os.path.join(P.profile.workspace, "kilo.jsonc")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(KILO)
    _deploy(c, f, _doc(P), 1)
    assert open(path, encoding="utf-8").read() == KILO
    assert CR.pending(c.state_dir) == []


def test_client_requests_dedupe_per_kind_and_project(tmp_path):
    sd = str(tmp_path / "state")
    a = CR.request(sd, "reload_kilo", "game", "first", clock=lambda: 10.0)
    b = CR.request(sd, "reload_kilo", "game", "second", clock=lambda: 20.0)
    CR.request(sd, "restart_coordinator", "game", "restart", clock=lambda: 30.0)
    assert a["id"] == b["id"] and b["count"] == 2 and b["first_ts"] == 10.0 and b["ts"] == 20.0
    assert [r["kind"] for r in CR.pending(sd)] == ["reload_kilo", "restart_coordinator"]
    assert [r["reason"] for r in CR.pending(sd, "reload_kilo")] == ["second"]
    import pytest
    with pytest.raises(ValueError):
        CR.request(sd, "format_disk", "game", "no")
    assert CR.done(sd, "nowhere.json") == ""


# ------------------------------------------------------------------ W2.4 three-way overlay

def test_local_toml_edits_survive_a_pull_and_conflicts_go_to_the_router(tmp_path):
    c, f, P = _setup(tmp_path)
    cp = _deploy(c, f, _doc(P, max_attempts=4), 1)
    assert P.profile.limits["max_attempts"] == 4
    # the operator edits project.toml by hand (a key the router did not change)
    text = open(P.profile.path, encoding="utf-8").read()
    text = text.replace("max_review_calls = 4", "max_review_calls = 4\nmax_sessions_per_task = 5")
    open(P.profile.path, "w", encoding="utf-8").write(text)
    f.deploy(_doc(P, max_attempts=4, max_closeout_attempts=3), 2)
    # (the router doc was imported from the edited TOML: make it the router's older view)
    f.body["doc"]["settings"]["limits"].pop("max_sessions_per_task", None)
    f.body["hash"] = D.doc_hash(f.body["doc"])
    assert cp.sync("game", force=True) == "applied"
    assert P.profile.limits["max_closeout_attempts"] == 3
    assert P.profile.limits["max_sessions_per_task"] == 5                 # kept: local edit
    drift = [e for e in c.store.events("game", 30) if e["kind"] == "config_local_drift"]
    assert drift and drift[0]["detail"]["kept_local"] == ["limits.max_sessions_per_task"]
    assert drift[0]["detail"]["conflicts"] == []
    # a router change to a key the operator left alone since the last pull just applies
    f.deploy(_doc(P, max_attempts=4, max_closeout_attempts=3, max_sessions_per_task=6), 3)
    assert cp.sync("game", force=True) == "applied"
    assert P.profile.limits["max_sessions_per_task"] == 6
    drift = [e for e in c.store.events("game", 30) if e["kind"] == "config_local_drift"]
    assert len(drift) == 1
    # both sides change the same key since the last pull: the router wins, logged
    text = open(P.profile.path, encoding="utf-8").read()
    open(P.profile.path, "w", encoding="utf-8").write(
        text.replace("max_sessions_per_task = 5", "max_sessions_per_task = 9"))
    f.deploy(_doc(P, max_attempts=4, max_closeout_attempts=3, max_sessions_per_task=7), 4)
    assert cp.sync("game", force=True) == "applied"
    assert P.profile.limits["max_sessions_per_task"] == 7
    drift = [e for e in c.store.events("game", 30) if e["kind"] == "config_local_drift"]
    assert drift[0]["detail"]["conflicts"] == ["limits.max_sessions_per_task"]
    # a restarted coordinator (cache applied whole) keeps the same local edit rule
    cp2 = ConfigPuller(c)
    assert cp2.sync("game", force=True) == "applied"
    assert P.profile.limits["max_sessions_per_task"] == 7


def test_overlay3_rules():
    raw = {"id": "g", "limits": {"a": 1, "b": 2}, "foundry": {"url": "http://local", "key_env": "K"}}
    base = {"settings": {"limits": {"a": 1, "b": 2}}}
    lbase = D.local_view(raw)
    doc = {"settings": {"limits": {"a": 1, "b": 3}, "foundry": {"url": "http://evil"}}}
    local = {"id": "g", "limits": {"a": 9, "b": 2}, "foundry": {"url": "http://local", "key_env": "K"}}
    merged, rep = D.overlay3(local, doc, base, lbase)
    assert merged["limits"] == {"a": 9, "b": 3}                           # a: local edit; b: router
    assert merged["foundry"] == {"url": "http://local", "key_env": "K"}   # local keys stay
    assert rep == {"kept_local": ["limits.a"], "conflicts": [], "router": ["limits.b"]}
    both = {"settings": {"limits": {"a": 5, "b": 2}}}
    merged, rep = D.overlay3(local, both, base, lbase)
    assert merged["limits"]["a"] == 5 and rep["conflicts"] == ["limits.a"]
    # a TOML value the router already overrode is not a local edit: the router wins
    merged, rep = D.overlay3(raw, {"settings": {"limits": {"a": 7, "b": 2}}},
                             {"settings": {"limits": {"a": 7, "b": 2}}}, lbase)
    assert merged["limits"]["a"] == 7 and rep["kept_local"] == []
    # no base: the router wins where it has a value, local extras stay
    merged, rep = D.overlay3(local, both, None, None)
    assert merged["limits"] == {"a": 5, "b": 2} and rep["conflicts"] == []
    merged, rep = D.overlay3(local, {"settings": {"limits": {"a": 5}}}, None, None)
    assert merged["limits"] == {"a": 5, "b": 2}
    # a key the router removed since the base goes away locally too
    gone = {"settings": {"limits": {"a": 1}}}
    merged, rep = D.overlay3(raw, gone, base, lbase)
    assert merged["limits"] == {"a": 1} and rep["router"] == ["limits.b"]
    assert D.overlay(raw, doc) == D.overlay3(raw, doc)[0]
    # the TOML's own role rules are a local edit too; without any, the router's apply whole
    rr = {"settings": {}, "role_rules": RR.all_defaults()}
    merged, rep = D.overlay3(raw, rr, None, None)
    assert merged["role_rules"] == rr["role_rules"] and rep["kept_local"] == []
    merged, rep = D.overlay3(raw, rr, rr, D.local_view(raw))
    assert merged["role_rules"] == rr["role_rules"]
    assert rep["kept_local"] == [] and rep["conflicts"] == []           # router-owned, no drift


# ------------------------------------------------------------------ W2.4 restart-scoped keys

def test_restart_scoped_keys_wait_for_the_clients_restart(tmp_path):
    c, f, P = _setup(tmp_path)
    doc = _doc(P, max_attempts=6)
    doc["settings"]["client"]["provider"] = "other"                      # apply: restart
    f.deploy(doc, 1)
    cp = ConfigPuller(c)
    assert cp.sync("game", force=True) == "applied"
    assert P.profile.limits["max_attempts"] == 6                          # live keys applied
    assert P.profile.client.get("provider", "foundry") == "foundry"       # held
    assert f.reports[-1]["status"] == "applied" and \
        f.reports[-1]["restart_needed"] == ["client.provider"]
    reqs = CR.pending(c.state_dir, "restart_coordinator")
    assert len(reqs) == 1 and reqs[0]["detail"]["paths"] == ["client.provider"]
    cache = json.load(open(os.path.join(c.state_dir, "config", "game.json"), encoding="utf-8"))
    assert cache["restart_pending"] == ["client.provider"]
    ev = [e for e in c.store.events("game", 30) if e["kind"] == "config_applied"][0]
    assert ev["detail"]["restart_needed"] == ["client.provider"]
    # the restarted coordinator applies the cached revision whole
    raw = tomllib.loads(open(P.profile.path, encoding="utf-8").read())
    merged, rec = startup_overlay(c.state_dir, raw)
    assert rec["rev"] == 1 and merged["client"]["provider"] == "other"
    assert merged["foundry"]["url"] == "http://foundry.invalid"
    cp2 = ConfigPuller(c)
    assert cp2.sync("game", force=True) == "applied"
    assert P.profile.client["provider"] == "other"
    assert cp2.applied("game")["restart_pending"] == []
    assert startup_overlay(c.state_dir, {"id": "nothing"}) == ({"id": "nothing"}, None)

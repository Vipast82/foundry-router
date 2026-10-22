"""W2 (2026-10-22): permissions that match the GUI. Hard denies no allow can
remove (Studio never-tools, history rewrites, shell secret reads), the Studio
read/write split with migration, sprint-scoped edit rules, read-only folders
that never become writable, config-file denies for every role but the
coordinator."""

from foundry_automation import role_rules as RR

WS = "C:/Roblox/AnimalUprising"


def _lead_star(rules=None):
    """What the lead's write mode starts from: `*` allow, then the role rules."""
    return ([{"permission": "*", "pattern": "*", "action": "allow"}]
            + RR.session_rules("lead", rules, WS))


def test_studio_never_denies_beat_every_allow():
    """W2.1: monetization, uploads, asset insertion and live script mutation stay
    denied for a role with studio allow, for `extra` allows and for the lead's
    `*` allow."""
    wide = {"permissions": {"studio_read": "allow", "studio_write": "allow"},
            "extra": [{"permission": "Roblox_Studio_manage_monetization", "pattern": "*",
                       "action": "allow"},
                      {"permission": "*", "pattern": "*", "action": "allow"}]}
    errs, warns = RR.validate({"implementer": wide})
    assert errs == [] and any(w["code"] == "hard_deny_overrides" for w in warns)
    never = ("Roblox_Studio_manage_monetization", "robloxstudio-mcp_upload_image",
             "robloxstudio_mcp_insert_asset", "robloxstudio-mcp_set_script_source",
             "robloxstudio-mcp_edit_script_lines", "robloxstudio-mcp_multi_edit",
             "robloxstudio-mcp_find_and_replace_in_scripts")
    for role in RR.RULE_ROLES:
        for lst in (RR.agent_block_rules(role, wide, WS), RR.session_rules(role, wide, WS)):
            for tool in never:
                assert RR.decide(lst, tool, "*")[0] == "deny", (role, tool)
    for tool in never:
        assert RR.decide(_lead_star(wide), tool, "*")[0] == "deny", tool
    # ordinary Studio tools follow the matrix
    lst = RR.session_rules("implementer", wide, WS)
    assert RR.decide(lst, "robloxstudio-mcp_get_file_tree", "*")[0] == "allow"
    assert RR.decide(lst, "robloxstudio-mcp_set_property", "*")[0] == "allow"
    # the hard denies are the tail of every session rule list
    tail = lst[-len(RR.HARD_DENIES):]
    assert {r["permission"] for r in tail} >= set(RR.STUDIO_NEVER)


def test_studio_read_and_write_are_separate_categories():
    assert "studio_mcp" not in RR.CATEGORIES
    assert {"studio_read", "studio_write"} <= set(RR.CATEGORIES)
    ro = {"permissions": {"studio_read": "allow", "studio_write": "deny"}}
    lst = RR.session_rules("reviewer", ro, WS)
    assert RR.decide(lst, "robloxstudio-mcp_get_instance_properties", "*")[0] == "allow"
    assert RR.decide(lst, "Roblox_Studio_run_code", "*")[0] == "deny"
    assert RR.decide(lst, "robloxstudio-mcp_set_property", "*")[0] == "deny"
    assert RR.decide(lst, "robloxstudio-mcp_mass_create_objects", "*")[0] == "deny"
    assert RR.decide(lst, "robloxstudio-mcp_delete_object", "*")[0] == "deny"
    assert RR.decide(lst, "robloxstudio-mcp_execute_luau", "*")[0] == "deny"
    blk = RR.agent_block("reviewer", ro, WS)
    assert blk["robloxstudio-mcp_*"] == "allow" and blk["robloxstudio-mcp_set_*"] == "deny"


def test_legacy_studio_mcp_documents_stay_valid():
    """Migration: an older document's `studio_mcp` applies to both new
    categories; the validator warns instead of refusing."""
    old = {"permissions": {"studio_mcp": "deny"}}
    errs, warns = RR.validate({"implementer": old})
    assert errs == [] and [w["code"] for w in warns] == ["legacy_key"]
    n = RR.normalize("implementer", old)
    assert "studio_mcp" not in n["permissions"]
    assert n["permissions"]["studio_read"] == "deny" and n["permissions"]["studio_write"] == "deny"
    # an explicit new value wins over the legacy one
    both = {"permissions": {"studio_mcp": "deny", "studio_write": "allow"}}
    n = RR.normalize("implementer", both)
    assert n["permissions"]["studio_read"] == "deny" and n["permissions"]["studio_write"] == "allow"
    bad = {"permissions": {"studio_mcp": "maybe"}}
    assert RR.validate({"implementer": bad})[0]
    assert RR.decide(RR.session_rules("implementer", old, WS), "Roblox_Studio_x", "*")[0] == "deny"


def test_empty_edit_paths_means_the_sprint_scope_only():
    """W2.2: with a sprint scope injected, an empty allow list is that scope;
    `edit_paths.allow` adds to it; `edit_paths.deny` still applies; no scope
    entry at all denies every edit."""
    scope = ["src/client/UI/Hud.lua", "docs/progress/UI-02b_*"]
    lst = RR.session_rules("implementer", None, WS, scope=scope)
    d = lambda perm, p: RR.decide(lst, perm, p)[0]                      # noqa: E731
    assert d("edit", "src/client/UI/Hud.lua") == "allow"
    assert d("write", "docs/progress/UI-02b_PLAN.md") == "allow"
    assert d("edit", "src/client/UI/Other.lua") == "deny"
    assert d("edit", "AGENT_RULES.md") == "deny"
    assert d("edit", "../Other/x.lua") == "deny"
    assert d("read", "src/client/UI/Other.lua") == "allow"              # reads unchanged
    more = {"edit_paths": {"allow": ["art/**"], "deny": ["src/client/UI/Hud.lua"]}}
    lst = RR.session_rules("implementer", more, WS, scope=scope)
    assert RR.decide(lst, "edit", "art/a.png")[0] == "allow"
    assert RR.decide(lst, "edit", "docs/progress/UI-02b_PLAN.md")[0] == "allow"
    assert RR.decide(lst, "edit", "src/client/UI/Hud.lua")[0] == "deny"   # deny wins
    none = RR.session_rules("implementer", None, WS, scope=[])
    assert RR.decide(none, "edit", "src/a.lua")[0] == "deny"
    assert RR.decide(none, "write", "x.txt")[0] == "deny"
    # without a sprint (GUI tester, agent block) the old meaning stays
    assert RR.test("implementer", None, "edit", "src/a.lua", WS)["decision"] == "allow"
    # the floor (no deployed role rules) scopes edits the same way
    floor = RR.floor_rules("implementer", WS, scope)
    assert RR.decide(floor, "edit", "src/client/UI/Hud.lua")[0] == "allow"
    assert RR.decide(floor, "edit", "src/server/x.lua")[0] == "deny"
    assert RR.decide(floor, "bash", "git push --force")[0] == "deny"
    assert RR.decide(RR.floor_rules("implementer", WS, None), "edit", "src/x.lua")[0] is None


def test_read_paths_never_grant_write():
    """W2.2: a folder opened for reading only is denied to edit/write even when
    external_directory opens it and the edit scope is the whole workspace."""
    rules = {"read_paths": {"allow": ["C:\\Roblox\\**", "C:\\Users\\x\\Foundry-Router\\**"],
                            "deny": []}}
    lst = RR.agent_block_rules("implementer", rules, WS)
    assert RR.decide(lst, "external_directory", "C:/Roblox")[0] == "allow"
    assert RR.decide(lst, "read", "C:/Users/x/Foundry-Router/a.py")[0] == "allow"
    for perm in ("edit", "write", "patch"):
        assert RR.decide(lst, perm, "C:/Users/x/Foundry-Router/a.py")[0] == "deny", perm
        assert RR.decide(lst, perm, "../../Users/x/Foundry-Router/a.py")[0] == "deny", perm
        assert RR.decide(lst, perm, "C:/Roblox/Other/a.lua")[0] == "deny", perm
        assert RR.decide(lst, perm, "../Other/a.lua")[0] == "deny", perm
    assert RR.decide(lst, "edit", "src/a.lua")[0] == "allow"               # inside: fine
    t = lambda op, p: RR.test("implementer", rules, op, p, WS)["decision"]  # noqa: E731
    assert t("read", "C:/Roblox/Other/a.lua") == "allow"
    assert t("edit", "C:/Roblox/Other/a.lua") == "deny"
    assert t("write", "C:/Users/x/Foundry-Router/a.py") == "deny"
    # an edit entry for the same folder keeps it writable (the GUI said so)
    rw = {"read_paths": {"allow": ["C:\\Roblox\\**"], "deny": []},
          "edit_paths": {"allow": ["C:\\Roblox\\**"], "deny": []}}
    assert RR.test("implementer", rw, "edit", "C:/Roblox/Other/a.lua", WS)["decision"] == "allow"


def test_config_files_are_denied_for_every_role_but_the_coordinator():
    wide = {"permissions": {"edit": "allow", "write": "allow"},
            "edit_paths": {"allow": ["**", "kilo.jsonc"], "deny": []}}
    errs, warns = RR.validate({"implementer": wide})
    assert errs == [] and any(w["code"] == "hard_deny_overrides" for w in warns)
    files = ("kilo.jsonc", ".kilo/tally/r1.txt", ".git/hooks/pre-commit",
             "tools/hooks/pre-commit", ".githooks/x", "C:/Roblox/AnimalUprising/kilo.jsonc",
             "C:/Roblox/AnimalUprising/.kilo/x")
    for role in RR.RULE_ROLES:
        lst = RR.agent_block_rules(role, wide, WS)
        for f in files:
            want = "allow" if role == "coordinator" else "deny"
            assert RR.decide(lst, "edit", f)[0] == want, (role, f)
            assert RR.decide(lst, "write", f)[0] == want, (role, f)
    lst = RR.session_rules("implementer", wide, WS, scope=["kilo.jsonc", "src/**"])
    assert RR.decide(lst, "edit", "kilo.jsonc")[0] == "deny"            # even in scope
    assert RR.decide(lst, "edit", "src/a.lua")[0] == "allow"
    assert RR.decide(lst, "read", "kilo.jsonc")[0] == "allow"           # reading is fine


def test_history_rewrites_and_shell_secret_reads_are_hard_denied():
    wide = {"permissions": {c: "allow" for c in RR.CATEGORIES},
            "bash": {"allow": ["*"], "deny": []},
            "extra": [{"permission": "*", "pattern": "*", "action": "allow"}]}
    denied = ("git push origin +master", "git push origin :old", "git push --mirror x",
              "git push origin --prune", "git reset --hard HEAD~1", "git rebase -i HEAD~3",
              "git commit --amend -m x", "git commit -n -m x", "git commit -am x -n",
              "git update-ref refs/heads/master abc", "git filter-branch --all",
              "git -c core.hooksPath=/dev/null commit -m x", "git config core.hooksPath x",
              "git push --no-verify", "cat .env", "type .env.local", "cat config/.env",
              "Get-Content C:\\x\\.env", "cat ~/.foundry-coordinator/workstation-credentials.xml",
              "printenv FOUNDRY_COORDINATOR_KEY", "env", "set", "export",
              "echo $env:KILO_SERVER_PASSWORD", "Get-ChildItem Env:", "cat C:/Roblox/_secrets/k",
              "type media-credential.xml")
    for role in RR.RULE_ROLES:
        lst = RR.agent_block_rules(role, wide, WS)
        for cmd in denied:
            assert RR.decide(lst, "bash", cmd)[0] == "deny", (role, cmd)
    lst = RR.agent_block_rules("implementer", wide, WS)
    for cmd in ("git status", "git commit -m \"UI-02b: hud\"", "git log --oneline -5",
                "git push origin master", "python -m pytest tests -q", "git reset HEAD~1 --soft"):
        assert RR.decide(lst, "bash", cmd)[0] == "allow", cmd
    for perm in ("read", "edit", "write", "grep"):
        assert RR.decide(lst, perm, "config/workstation-credentials.xml")[0] == "deny"


def test_default_lead_and_implementer_shape():
    lead = RR.default_rules("lead")
    assert lead["permissions"]["task"] == "deny" and lead["task_agents"] == ["explore"]
    assert lead["bash"]["deny"] == ["git push", "git push *"]
    lst = RR.session_rules("lead", None, WS)
    assert RR.decide(lst, "task", "explore")[0] == "allow"
    assert RR.decide(lst, "task", "au-planner")[0] == "deny"
    assert RR.decide(lst, "bash", "git push origin master")[0] == "deny"
    assert RR.decide(lst, "question", "*")[0] == "deny"
    impl = RR.session_rules("implementer", None, WS)
    assert RR.decide(impl, "question", "*")[0] == "deny"                  # W1: no human path
    errs, warns = RR.validate(RR.all_defaults())
    assert errs == [] and warns == []

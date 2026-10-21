"""Automation area read-side helpers (automation_view.py): usage normalizing
(unknown stays unknown), dry run (skips with reasons), path tester (hard
denies, deny wins, Windows globs), rule warnings and document diff."""

from foundry_router import automation_view as av


def _doc(**over):
    d = {
        "roles": [{"id": "implementer", "label": "Implementer", "persona": "P-Impl"},
                  {"id": "reviewer", "label": "Reviewer", "persona": "P-Rev"}],
        "personas": {
            "P-Impl": {"routes": [{"backend": "meridian", "model": "claude-sonnet-5-5", "enabled": True},
                                  {"backend": "cliproxyapi", "model": "gpt-6.1-sol", "enabled": True},
                                  {"backend": "strata", "model": "qwen", "enabled": True}]},
            "P-Rev": {"routes": [{"backend": "meridian", "model": "claude-opus-5-5", "enabled": True}]}},
        "backends": {"meridian": {"enabled": True, "cutoff_pct": 95},
                     "cliproxyapi": {"enabled": True, "cutoff_pct": 95},
                     "strata": {"enabled": True, "cutoff_pct": None}},
        "role_rules": av.default_role_rules(),
        "hard_denies": [dict(h) for h in av.HARD_DENIES],
    }
    d.update(over)
    return d


STATUS = [
    {"name": "meridian", "type": "anthropic-compatible", "flavor": "meridian", "subscription": True,
     "healthy": True, "enabled": True, "models": ["claude-sonnet-5-5", "claude-opus-5-5"]},
    {"name": "cliproxyapi", "type": "openai-compatible", "flavor": "cliproxyapi", "subscription": True,
     "healthy": True, "enabled": True, "models": ["gpt-6.1-sol"]},
    {"name": "strata", "type": "openai-compatible", "flavor": "llamacpp", "subscription": False,
     "healthy": True, "enabled": True, "models": ["qwen"]},
    {"name": "old-ollama", "type": "ollama", "flavor": "ollama", "healthy": None, "enabled": False,
     "models": []},
]
MERIDIAN = [{"backend": "meridian", "buckets": [
    {"type": "five_hour", "label": "5-hour", "used": 0.97, "resets_hhmm": "Thu 02:30 UTC"},
    {"type": "seven_day", "label": "weekly", "used": 0.20},
    {"type": "seven_day_fable", "label": "Fable weekly", "fable_scoped": True, "used": 0.99}]}]
CLIPROXY = [{"backend": "cliproxyapi", "ok": True, "accounts": [
    {"provider": "codex", "disabled": False, "unavailable": False, "age_s": 30, "windows": [
        {"name": "weekly", "kind": "weekly", "used": 96.0, "resets_in_s": 3600},
        {"name": "5-hour", "kind": "session", "used": 10.0}]},
    {"provider": "codex", "disabled": True, "windows": [{"name": "weekly", "kind": "weekly", "used": 1.0}]}]}]


def usage(**kw):
    return av.normalize_usage(STATUS, kw.get("mer", MERIDIAN), kw.get("cli", CLIPROXY),
                              {"meridian": 95, "cliproxyapi": 95})


def test_normalize_usage_windows_cutoffs_and_unknown():
    u = usage()
    assert u["meridian"]["windows"]["5h"]["used_pct"] == 97.0
    assert u["meridian"]["windows"]["weekly"]["used_pct"] == 20.0
    assert u["meridian"]["family"] == "claude" and u["meridian"]["cutoff_pct"] == 95
    # the disabled account's 1% never hides the usable account's 96%
    assert u["cliproxyapi"]["windows"]["weekly"]["used_pct"] == 96.0
    assert u["cliproxyapi"]["windows"]["5h"]["used_pct"] == 10.0
    assert u["strata"]["subscription"] is False and u["strata"]["windows"] == {}
    assert u["old-ollama"]["enabled"] is False
    # no readings at all: unknown (None), never 0%
    u2 = usage(cli=[{"backend": "cliproxyapi", "ok": False, "error": "HTTP 403", "accounts": []}])
    w = u2["cliproxyapi"]["windows"]
    assert w["5h"]["used_pct"] is None and w["weekly"]["used_pct"] is None
    assert u2["cliproxyapi"]["unknown"] is True and "403" in u2["cliproxyapi"]["note"]


def test_stale_reading_is_flagged():
    old = [{"backend": "cliproxyapi", "ok": True, "accounts": [
        {"provider": "codex", "age_s": av.STALE_AFTER_S + 5,
         "windows": [{"name": "weekly", "kind": "weekly", "used": 5.0}]}]}]
    assert usage(cli=old)["cliproxyapi"]["windows"]["weekly"]["stale"] is True


def test_dry_run_skips_over_cutoff_and_explains():
    r = av.dry_run(_doc(), "implementer", usage())
    assert [h["outcome"] for h in r["hops"]] == ["skipped", "skipped", "chosen"]
    assert "5-hour 97% ≥ cutoff 95%" in r["hops"][0]["reason"]
    assert "weekly 96%" in r["hops"][1]["reason"]
    assert r["chosen"]["backend"] == "strata"
    assert "after skipping 2" in r["summary"]


def test_dry_run_first_route_wins_and_later_hops_not_reached():
    u = usage(mer=[{"backend": "meridian", "buckets": [{"type": "five_hour", "used": 0.1},
                                                       {"type": "seven_day", "used": 0.1}]}])
    r = av.dry_run(_doc(), "implementer", u)
    assert [h["outcome"] for h in r["hops"]] == ["chosen", "not_reached", "not_reached"]


def test_dry_run_off_route_off_backend_unknown_and_fail_closed():
    d = _doc()
    d["personas"]["P-Impl"]["routes"][0]["enabled"] = False
    d["backends"]["strata"]["enabled"] = False
    u = usage(cli=[{"backend": "cliproxyapi", "ok": False, "accounts": []}])
    r = av.dry_run(d, "implementer", u)
    assert r["hops"][0]["reason"] == "route turned off"
    assert r["hops"][1]["outcome"] == "chosen" and "unknown" in r["hops"][1]["reason"]
    d["backends"]["cliproxyapi"]["fail_closed"] = True
    r = av.dry_run(d, "implementer", u)
    assert "fails closed" in r["hops"][1]["reason"]
    assert r["hops"][2]["reason"] == "backend turned off"
    assert r["chosen"] is None and "pause" in r["summary"]


def test_dry_run_draft_cutoff_beats_live_cutoff_and_unknown_role():
    d = _doc()
    d["backends"]["meridian"]["cutoff_pct"] = 99
    assert av.dry_run(d, "implementer", usage())["chosen"]["backend"] == "meridian"
    assert av.dry_run(d, "nobody", usage())["error"]


def test_glob_match_windows_and_relative():
    assert av.glob_match("C:\\Roblox\\**", "c:/roblox/AnimalUprising/src/x.lua")
    assert av.glob_match("C:/Roblox/AnimalUprising/**", "C:\\Roblox\\AnimalUprising\\a.lua")
    assert not av.glob_match("C:\\Roblox\\AnimalUprising\\**", "C:\\Roblox\\Other\\a.lua")
    assert av.glob_match("**/.env", "C:\\x\\y\\.env")
    assert av.glob_match("src/**", "C:\\Repo\\src\\a\\b.lua")         # workspace-relative
    assert not av.glob_match("src/*.lua", "C:\\Repo\\src\\a\\b.lua")  # * stays in one folder
    assert av.glob_match("src/?.lua", "src/a.lua") and not av.glob_match("src/?.lua", "src/ab.lua")


def _scoped():
    d = _doc()
    d["role_rules"]["implementer"]["edit_paths"]["allow"] = [r"C:\Roblox\AnimalUprising\**"]
    return d


def test_path_test_hard_denies_are_locked():
    d = _scoped()
    sec = av.path_test(d, "implementer", "read", r"C:\Roblox\_secrets\key.txt")
    assert sec["decision"] == "deny" and sec["locked"] is True
    env = av.path_test(d, "implementer", "edit", "C:/Roblox/AnimalUprising/.env")
    assert env["decision"] == "deny" and env["locked"] is True
    fp = av.path_test(d, "implementer", "bash", "git push --force origin main")
    assert fp["decision"] == "deny" and fp["locked"] is True


def test_path_test_scope_deny_wins_and_outside_is_denied():
    d = _scoped()
    ok = av.path_test(d, "implementer", "edit", r"C:\Roblox\AnimalUprising\src\x.lua")
    assert ok["decision"] == "allow"
    d["role_rules"]["implementer"]["edit_paths"]["deny"] = ["C:/Roblox/AnimalUprising/build/**"]
    assert av.path_test(d, "implementer", "edit",
                        "C:/Roblox/AnimalUprising/build/out.lua")["decision"] == "deny"
    assert av.path_test(d, "implementer", "edit", "D:/elsewhere/a.lua")["decision"] == "deny"
    assert av.path_test(d, "reviewer", "edit", "C:/Roblox/AnimalUprising/a.lua")["decision"] == "deny"
    d["role_rules"]["implementer"]["permissions"]["edit"] = "ask"
    assert av.path_test(d, "implementer", "edit",
                        "C:/Roblox/AnimalUprising/src/x.lua")["decision"] == "ask"


def test_path_test_shell_and_git_follow_their_categories():
    d = _doc()
    assert av.path_test(d, "implementer", "bash", "git commit -m x")["decision"] == "allow"
    assert av.path_test(d, "implementer", "bash", "git tag v1")["decision"] == "deny"
    assert av.path_test(d, "reviewer", "bash", "npm test")["decision"] == "deny"


def test_rule_warnings_bash_with_restricted_edit_scope():
    assert not [w for w in av.rule_warnings(_doc()) if "shell" in w["message"]]
    w = av.rule_warnings(_scoped())
    assert any(x["role"] == "implementer" and "shell" in x["message"] for x in w)
    d = _doc()
    d["role_rules"]["reviewer"]["permissions"]["edit"] = "maybe"
    assert any(x["severity"] == "bad" and x["role"] == "reviewer" for x in av.rule_warnings(d))


def test_default_rules_match_the_c1_shape():
    rr = av.default_role_rules()
    assert set(rr) >= {r for r, _ in av.ROLES}
    for role, r in rr.items():
        assert set(r["permissions"]) == set(av.CATEGORIES), role
        assert set(r) >= {"agent", "default", "permissions", "read_paths", "edit_paths", "bash",
                          "task_agents", "deny_tools", "extra"}
    assert rr["implementer"]["permissions"]["edit"] == "allow"
    assert rr["reviewer"]["permissions"]["edit"] == "deny"
    assert all({"permission", "pattern", "action", "why"} <= set(h) for h in av.HARD_DENIES)


def test_diff_docs_reorder_is_one_change_and_roles_by_id():
    a = _doc()
    b = _doc()
    rs = b["personas"]["P-Impl"]["routes"]
    rs[0], rs[1] = rs[1], rs[0]
    b["roles"][1]["persona"] = "P-Impl"
    b["backends"]["new"] = {"enabled": True}
    ch = {c["path"]: c for c in av.diff_docs(a, b)}
    assert set(ch) == {"personas.P-Impl.routes", "roles.reviewer.persona", "backends.new"}
    assert ch["backends.new"]["op"] == "add"
    assert av.diff_docs(a, _doc()) == []


def test_seed_document_maps_personas_to_backend_routes():
    personas = [{"virtual_name": "Foundry-Kilo-Review", "backend": "",
                 "pinned_models": '["claude-sonnet-5-5", "gpt-6.1-sol", "qwen"]'},
                {"virtual_name": "Pinned", "backend": "cliproxyapi", "pinned_models": '["gpt-6.1-sol"]'},
                {"virtual_name": "Broken", "pinned_models": "not json"}]
    d = av.seed_document(personas, STATUS, {"reviewer": "Foundry-Kilo-Review", "lead": "Lead-P"},
                         {"meridian": 90})
    assert d["stub"] is True
    assert [r["backend"] for r in d["personas"]["Foundry-Kilo-Review"]["routes"]] == \
        ["meridian", "cliproxyapi", "strata"]
    assert d["personas"]["Broken"]["routes"] == []
    assert d["backends"]["meridian"]["cutoff_pct"] == 90
    assert d["backends"]["cliproxyapi"]["cutoff_pct"] == 95       # default
    assert d["backends"]["strata"]["cutoff_pct"] is None           # local: no cutoff
    assert [r["id"] for r in d["roles"]] == [r for r, _ in av.ROLES]
    # role personas live where C1 keeps them
    assert d["settings"]["roles"]["reviewer"] == "Foundry-Kilo-Review"
    assert d["settings"]["lead_manager"]["persona"] == "Lead-P"
    assert av.persona_of(d, "reviewer") == "Foundry-Kilo-Review"
    assert av.persona_of(d, "coordinator") == ""


def test_dry_run_resolves_the_persona_from_settings():
    d = _doc()
    for r in d["roles"]:
        r.pop("persona")
    d["settings"] = {"roles": {"implementer": "P-Impl"}}
    assert av.dry_run(d, "implementer", usage())["persona"] == "P-Impl"


def test_scope_star_crosses_folders_and_relative_entries_use_the_workspace():
    d = _doc()
    d["role_rules"]["implementer"]["edit_paths"]["allow"] = ["C:/Roblox/AnimalUprising/*", "tools/*"]
    ok = av.path_test(d, "implementer", "edit", "C:/Roblox/AnimalUprising/src/deep/x.lua")
    assert ok["decision"] == "allow"
    if av._RR is None:
        # the stub approximation; C1's exact tester decides this case itself
        assert av.path_test(d, "implementer", "edit", "C:/Work/tools/a.py",
                            workspace="C:/Work")["decision"] == "allow"
    assert av.path_test(d, "implementer", "edit", "C:/Work/src/a.py",
                        workspace="C:/Work")["decision"] == "deny"

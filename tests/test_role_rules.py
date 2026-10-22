"""Role rules (C1/C3): defaults per role, validation, the two generated Kilo
forms (session permission[] and kilo.jsonc agent blocks), hard denies that no
setting removes, and the comment-preserving JSONC editor."""

import pytest

from foundry_automation import jsonc
from foundry_automation import role_rules as RR

WS = "C:/Roblox/AnimalUprising"


def _d(role, rules, perm, value, ws=WS):
    return RR.decide(RR.agent_block_rules(role, rules, ws), perm, value)[0]


def test_every_role_has_valid_defaults():
    errs, warns = RR.validate(RR.all_defaults())
    assert errs == [] and warns == []
    assert set(RR.all_defaults()) == {"implementer", "lead", "reviewer", "deep_reviewer",
                                      "visual_reviewer", "planner", "researcher", "helper",
                                      "coordinator"}


def test_reviewer_is_read_only_and_implementer_writes():
    for role in ("reviewer", "deep_reviewer", "visual_reviewer", "planner", "helper"):
        assert _d(role, None, "edit", "src/a.lua") == "deny"
        assert _d(role, None, "bash", "ls") == "deny"
        assert _d(role, None, "read", "src/a.lua") == "allow"
        assert _d(role, None, "Roblox_Studio_run_code", "*") == "deny"
        assert _d(role, None, "webfetch", "https://x") == "deny"
    assert _d("researcher", None, "webfetch", "https://x") == "allow"
    assert _d("implementer", None, "edit", "src/a.lua") == "allow"
    assert _d("implementer", None, "bash", "git status") == "allow"
    assert _d("implementer", None, "bash", "git tag v1") == "deny"
    # B6: no subagents except explore
    assert _d("implementer", None, "task", "explore") == "allow"
    assert _d("implementer", None, "task", "au-review") == "deny"


def test_hard_denies_cannot_be_removed():
    rules = {"permissions": {c: "allow" for c in RR.CATEGORIES},
             "read_paths": {"allow": [".env"], "deny": []},
             "bash": {"allow": ["git push --force*", "*"], "deny": []},
             "extra": [{"permission": "bash", "pattern": "git push*--force*", "action": "allow"},
                       {"permission": "*", "pattern": "*", "action": "allow"}]}
    errs, warns = RR.validate({"lead": rules})
    assert errs == []
    assert {w["code"] for w in warns} == {"hard_deny_overrides"}
    for role in RR.RULE_ROLES:
        lst = RR.agent_block_rules(role, rules, WS)
        assert RR.decide(lst, "bash", "git push --force origin master")[0] == "deny"
        assert RR.decide(lst, "bash", "git push origin --tags")[0] == "deny"
        assert RR.decide(lst, "bash", "git tag -d v1")[0] == "deny"
        assert RR.decide(lst, "bash", "git commit -m x --no-verify")[0] == "deny"
        assert RR.decide(lst, "read", ".env")[0] == "deny"
        assert RR.decide(lst, "read", "C:/Roblox/_secrets/key.txt")[0] == "deny"
        assert RR.decide(lst, "edit", "config/.env.local")[0] == "deny"
        sess = RR.session_rules(role, rules, WS)
        tail = sess[-len(RR.HARD_DENIES):]
        assert [(r["permission"], r["pattern"]) for r in tail] == \
            [(h["permission"], h["pattern"]) for h in RR.HARD_DENIES]
        assert all(r["action"] == "deny" for r in tail)
    # the folded kilo.jsonc block keeps them last within each permission
    blk = RR.agent_block("implementer", rules, WS)
    assert list(blk["read"].items())[-1][1] == "deny"
    assert blk["bash"]["git*--no-verify*"] == "deny"


def test_absolute_windows_scope_for_the_implementer():
    rules = {"edit_paths": {"allow": ["C:\\Roblox\\**"], "deny": ["C:\\Roblox\\_secrets\\**"]}}
    sess = RR.session_rules("implementer", rules, WS)
    assert {"permission": "external_directory", "pattern": "C:/Roblox/*",
            "action": "allow"} in sess
    assert {"permission": "edit", "pattern": "C:/Roblox/**", "action": "allow"} in sess
    assert {"permission": "edit", "pattern": "../**", "action": "allow"} in sess
    t = lambda op, p: RR.test("implementer", rules, op, p, WS)["decision"]  # noqa: E731
    assert t("edit", "C:\\Roblox\\Shared\\x.lua") == "allow"
    assert t("edit", "src/client/x.lua") == "allow"          # the workspace is inside the scope
    assert t("edit", "C:\\Users\\x\\y.lua") == "deny"
    assert t("edit", "C:/Roblox/_secrets/k") == "deny"
    # a narrower scope keeps workspace edits to what it names
    narrow = {"edit_paths": {"allow": ["src/**"], "deny": []}}
    assert RR.test("implementer", narrow, "edit", "src/a.lua", WS)["decision"] == "allow"
    assert RR.test("implementer", narrow, "edit", "tools/a.ps1", WS)["decision"] == "deny"


def test_tester_checks_the_path_kilo_sees():
    # reported by the GUI session: an absolute path inside the workspace is
    # matched relative to the worktree, like Kilo does
    rules = {"edit_paths": {"allow": ["tools/*"], "deny": []}}
    out = RR.test("implementer", rules, "edit", "C:/Work/tools/a.py", "C:/Work")
    assert out["decision"] == "allow" and out["checked"] == "tools/a.py"
    assert RR.test("implementer", rules, "edit", "C:/Work/src/a.py", "C:/Work")[
        "decision"] == "deny"


def test_scopes_never_leak_upwards():
    # Kilo's * also matches "../", so a broad scope must not cover parent folders
    t = lambda rules, p: RR.test("implementer", rules, "edit", p, WS)["decision"]  # noqa: E731
    wide = {"edit_paths": {"allow": ["**"], "deny": []}}
    assert t(wide, "src/a.lua") == "allow"
    assert t(wide, "../Other/a.lua") == "deny"
    roblox = {"edit_paths": {"allow": ["C:\\Roblox\\**"], "deny": []}}
    assert t(roblox, "C:\\Roblox\\Shared\\x.lua") == "allow"
    assert t(roblox, "C:\\Users\\x\\y.lua") == "deny"
    assert RR.decide(RR.agent_block_rules("implementer", roblox, WS), "edit",
                     "../../Users/x/y.lua")[0] == "deny"
    # a broader second entry re-allows what it covers, a narrower one doesn't widen
    both = {"edit_paths": {"allow": ["C:\\Roblox\\Shared\\**", "C:\\**"], "deny": []}}
    assert t(both, "C:\\Tools\\a.txt") == "allow"
    assert t(both, "D:\\a.txt") == "deny"


def test_validation_errors():
    errs, warns = RR.validate({
        "boss": {},
        "implementer": {"permissions": {"edit": "maybe", "fly": "allow", "bash": "ask"},
                        "edit_paths": {"allow": ["../outside/**"]},
                        "extra": [{"permission": "bash"}], "colour": "red"}})
    codes = {(e["path"], e["code"]) for e in errs}
    assert ("role_rules.boss", "unknown_role") in codes
    assert ("role_rules.implementer.permissions.edit", "invalid") in codes
    assert ("role_rules.implementer.permissions.fly", "unknown_key") in codes
    assert ("role_rules.implementer.edit_paths.allow", "path_escape") in codes
    assert ("role_rules.implementer.extra[0]", "invalid") in codes
    assert ("role_rules.implementer.colour", "unknown_key") in codes
    assert any(w["code"] == "ask_unattended" for w in warns)


def test_fold_keeps_last_match_order():
    obj = RR.fold([{"permission": "bash", "pattern": "*", "action": "allow"},
                   {"permission": "bash", "pattern": "git *", "action": "deny"},
                   {"permission": "bash", "pattern": "*", "action": "deny"},
                   {"permission": "edit", "pattern": "*", "action": "allow"}])
    assert list(obj["bash"].items()) == [("git *", "deny"), ("*", "deny")]
    assert obj["edit"] == "allow"
    blk = RR.agent_block("reviewer")
    assert list(blk)[0] == "*" and blk["*"] == "deny"


def test_seed_from_profile_uses_legacy_settings():
    raw = {"client": {"agent": "code", "permissions": {"external_directory": {
        "C:/Roblox": "allow", "C:/Roblox/*": "allow"}}},
        "helpers": {"agent": "au-helper", "deny_tools": ["searxng_*"]},
        "lead_manager": {"enabled": True, "write_access": False}}
    rr = RR.seed_from_profile(raw)
    assert rr["reviewer"]["read_paths"]["allow"] == ["C:/Roblox/**"]
    assert "searxng_*" in rr["helper"]["deny_tools"]
    assert rr["lead"]["permissions"]["edit"] == "deny"          # write_access off
    raw["lead_manager"]["write_access"] = True
    assert RR.seed_from_profile(raw)["lead"]["permissions"]["edit"] == "allow"


# --------------------------------------------------------------------------- jsonc

SAMPLE = """{
  // the model
  "model": "foundry/x", /* inline */
  "agent": {
    "code": {
      "model": "foundry/Foundry-Kilo-Local",
      "permission": {"edit": "allow",}, // trailing comma
    },
    "au-review": {
      "model": "foundry/Foundry-Kilo-Review" // no permission yet
    },
  },
}
"""


def test_jsonc_parses_and_replaces_without_losing_comments():
    d = jsonc.parse(SAMPLE)
    assert d["agent"]["code"]["permission"] == {"edit": "allow"}
    out = jsonc.replace_value(SAMPLE, ("agent", "code", "permission"), {"*": "deny"})
    assert "// the model" in out and "/* inline */" in out and "// trailing comma" in out
    assert jsonc.parse(out)["agent"]["code"]["permission"] == {"*": "deny"}
    # a missing member is added after a line comment without breaking the object
    out2 = jsonc.replace_value(out, ("agent", "au-review", "permission"), {"edit": "deny"})
    p = jsonc.parse(out2)
    assert p["agent"]["au-review"] == {"model": "foundry/Foundry-Kilo-Review",
                                       "permission": {"edit": "deny"}}
    assert "// no permission yet" in out2
    with pytest.raises(jsonc.JsoncError):
        jsonc.parse('{"a": 1 "b": 2}')

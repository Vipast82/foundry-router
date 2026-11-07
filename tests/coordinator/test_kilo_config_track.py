"""Per-track kilo.jsonc (Team Lead 5): the MCP allow-list is the track's needs, agent blocks come
from the role rules, `snapshot` is false, comments survive, the result is validated and backed
up, and a bad source changes nothing."""
import os

import pytest

from foundry_automation import jsonc
from foundry_coordinator import kilo_config as K
from foundry_coordinator import services

SOURCE = """{
  // hand-written project file
  "model": "foundry/Foundry-Kilo-Local",
  "agent": {
    "code": {"model": "x", "permission": {"*": "allow"}},
    "au-review": {"permission": {"*": "deny"}}
  },
  "mcp": {
    // Studio
    "Roblox_Studio": {"type": "local", "command": ["cmd.exe", "/c", "mcp.bat"], "enabled": true},
    "robloxstudio-mcp": {"type": "local", "command": ["npx.cmd"], "enabled": true},
    "roblox-docs": {"type": "remote", "url": "http://h:1/sse", "enabled": true},
    "git": {"enabled": true},
    "blender-mcp": {"enabled": false},
    "kokoro-tts": {"enabled": true},
    "foundry-media": {"type": "local", "command": ["x.ps1"], "enabled": true}
  }
}"""
RULES = {"implementer": {"permissions": {"studio_read": "allow", "studio_write": "allow"}},
         "reviewer": {}}


class Track:
    def __init__(self, tmp, needs, name="blender-track"):
        self.worktree = str(tmp / "wt" / name)
        self.needs = needs
        self.name = name


@pytest.fixture()
def src(tmp_path):
    p = tmp_path / "kilo.jsonc"
    p.write_text(SOURCE, encoding="utf-8")
    return str(p)


def load(path):
    with open(path, encoding="utf-8") as f:
        return jsonc.parse(f.read())


def test_allow_list_is_the_tracks_needs(tmp_path, src):
    path = services.track_kilo_config(Track(tmp_path, ["blender"]), source=src, role_rules=RULES)
    assert path == os.path.join(str(tmp_path / "wt" / "blender-track"), "kilo.jsonc")
    mcp = load(path)["mcp"]
    assert mcp["blender-mcp"]["enabled"] is True                       # the track's need, on
    assert mcp["Roblox_Studio"]["enabled"] is False                    # a Blender track: no Studio
    assert mcp["robloxstudio-mcp"]["enabled"] is False
    assert mcp["kokoro-tts"]["enabled"] is False and mcp["foundry-media"]["enabled"] is False
    assert mcp["roblox-docs"]["enabled"] is True and mcp["git"]["enabled"] is True   # not controlled
    assert load(path)["snapshot"] is False
    assert load(path)["model"] == "foundry/Foundry-Kilo-Local"


def test_a_ui_track_gets_studio_and_never_blender(tmp_path, src):
    t = Track(tmp_path, ["studio"], "ui")
    mcp = load(services.track_kilo_config(t, source=src, role_rules=RULES))["mcp"]
    assert mcp["Roblox_Studio"]["enabled"] and mcp["robloxstudio-mcp"]["enabled"]
    assert mcp["blender-mcp"]["enabled"] is False


def test_a_server_only_the_global_config_knows_is_added_as_a_stub(tmp_path, src):
    mcp = load(services.track_kilo_config(Track(tmp_path, ["audio"], "a"), source=src))["mcp"]
    assert mcp["Media_Gen"] == {"enabled": True}          # the global config defines it
    # the servers Media_Gen replaced stay off even for an audio track
    assert mcp["kokoro-tts"]["enabled"] is False
    assert "acestep-music" not in mcp                    # not in the source: nothing to turn off


def test_agent_blocks_follow_role_rules_and_the_needs(tmp_path, src):
    bl = load(services.track_kilo_config(Track(tmp_path, ["blender"], "b"), source=src,
                                         role_rules=RULES))
    code = bl["agent"]["code"]["permission"]
    assert code["Roblox_Studio_*"] == "deny" and code["robloxstudio-mcp_*"] == "deny"
    assert code["kokoro-tts_*"] == "deny" and code["comfy-mcp_*"] == "deny"
    assert "blender-mcp_*" not in code                                  # its own need stays open
    ui = load(services.track_kilo_config(Track(tmp_path, ["studio"], "u"), source=src,
                                         role_rules=RULES))
    assert ui["agent"]["code"]["permission"]["Roblox_Studio_*"] == "allow"
    assert ui["agent"]["code"]["permission"]["blender-mcp_*"] == "deny"
    # an agent the source does not have is skipped, never created
    assert "au-planner" not in ui["agent"] and "au-review" in ui["agent"]


def test_without_role_rules_the_agent_blocks_are_untouched(tmp_path, src):
    out = load(services.track_kilo_config(Track(tmp_path, ["studio"], "r"), source=src))
    assert out["agent"]["code"]["permission"] == {"*": "allow"}


def test_comments_survive_and_a_second_call_is_a_no_op(tmp_path, src):
    t = Track(tmp_path, ["studio"], "c")
    path = services.track_kilo_config(t, source=src, role_rules=RULES)
    text = open(path, encoding="utf-8").read()
    assert "// hand-written project file" in text and "// Studio" in text
    mtime = os.path.getmtime(path)
    assert services.track_kilo_config(t, source=src, role_rules=RULES) == path
    assert os.path.getmtime(path) == mtime and not [f for f in os.listdir(os.path.dirname(path))
                                                     if ".bak-" in f]


def test_changed_needs_rewrite_with_a_backup(tmp_path, src):
    t = Track(tmp_path, ["studio"], "w")
    path = services.track_kilo_config(t, source=src)
    t.needs = ["blender"]
    K.track_kilo_config(t, source=src, clock=lambda: 1_700_000_000)
    names = os.listdir(os.path.dirname(path))
    assert any(n.startswith("kilo.jsonc.bak-") for n in names)
    assert load(path)["mcp"]["blender-mcp"]["enabled"] is True


def test_profile_supplies_the_source_and_rules(tmp_path, src):
    class Profile:
        workspace = str(tmp_path)
        raw = {"role_rules": RULES, "config_pull": {"kilo_config": src}}
    out = load(services.track_kilo_config(Track(tmp_path, ["studio"], "p"), profile=Profile))
    assert out["agent"]["code"]["permission"]["blender-mcp_*"] == "deny"


def test_the_official_studio_server_is_on_only_when_a_sprint_plan_names_it(tmp_path, src):
    def profile(on_demand):
        class Profile:
            workspace = str(tmp_path)
            raw = {"role_rules": RULES, "config_pull": {"kilo_config": src},
                   "automation": {"studio_official_mcp_on_demand": on_demand}}
        return Profile

    def track(name, plans):
        t = Track(tmp_path, ["studio", "blender"], name)
        t.sprints = list(plans)
        d = os.path.join(t.worktree, "docs", "progress")
        os.makedirs(d, exist_ok=True)
        for sid, body in plans.items():
            with open(os.path.join(d, f"{sid}_PLAN.md"), "w", encoding="utf-8") as f:
                f.write(body)
        return t

    plain = {"VR-1": "Rebuild the cap in Blender; ASSET_CHECK via robloxstudio-mcp.",
             "VR-2": "Fix the mask."}
    mcp = load(services.track_kilo_config(track("vr", plain), profile=profile(True)))["mcp"]
    assert mcp["Roblox_Studio"]["enabled"] is False                    # no plan needs it: off
    assert mcp["robloxstudio-mcp"]["enabled"] is True and mcp["blender-mcp"]["enabled"] is True
    named = {**plain, "VR-3": "Make the base mesh with generate_mesh."}
    mcp = load(services.track_kilo_config(track("gen", named), profile=profile(True)))["mcp"]
    assert mcp["Roblox_Studio"]["enabled"] is True                     # a plan uses its tool
    t = track("miss", plain)
    t.sprints.append("VR-9")                                           # no plan file: fail open
    assert load(services.track_kilo_config(t, profile=profile(True)))["mcp"]["Roblox_Studio"]["enabled"]
    mcp = load(services.track_kilo_config(track("off", plain), profile=profile(False)))["mcp"]
    assert mcp["Roblox_Studio"]["enabled"] is True                     # setting off: always on


def test_a_bad_source_or_track_changes_nothing(tmp_path):
    t = Track(tmp_path, ["studio"], "bad")
    with pytest.raises(K.KiloConfigError, match="cannot read"):
        services.track_kilo_config(t, source=str(tmp_path / "missing.jsonc"))
    broken = tmp_path / "broken.jsonc"
    broken.write_text('{"mcp": {', encoding="utf-8")
    with pytest.raises(K.KiloConfigError, match="broken.jsonc"):
        services.track_kilo_config(t, source=str(broken))
    with pytest.raises(K.KiloConfigError, match="no source"):
        services.track_kilo_config(t)
    with pytest.raises(K.KiloConfigError, match="no worktree"):
        services.track_kilo_config({"needs": []}, source=str(broken))
    assert not os.path.exists(os.path.join(t.worktree, "kilo.jsonc"))


def test_dict_tracks_work_and_unknown_needs_are_dropped(tmp_path, src):
    path = services.track_kilo_config({"worktree": str(tmp_path / "d"), "needs": ["bogus", "gpu"]},
                                      source=src)
    mcp = load(path)["mcp"]
    assert all(mcp[n]["enabled"] is False for n in ("Roblox_Studio", "blender-mcp", "kokoro-tts"))


def test_existing_apply_still_works_and_leaves_mcp_alone(tmp_path, src):
    class Profile:
        id = "p"
        workspace = str(tmp_path)
        raw = {"role_rules": RULES}
    r = K.regenerate(Profile, role_rules=RULES, path=src)
    assert r["applied"] is False
    assert load(src)["mcp"]["blender-mcp"]["enabled"] is False

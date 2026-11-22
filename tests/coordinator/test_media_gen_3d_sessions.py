"""v0.181.249: coordinator sessions and Media_Gen 5.21.1. A Media_Gen tool no family names
(38 tools, the deny list knew 20; the private-library ones among them) is off by a
wildcard; a sprint that needs `blender` gets generate_3d_model while [automation]
media_gen_3d is 'allow', with a brief on where the GLB goes and what is recorded; an
OpenCode project gets Media_Gen's remote block so it can reach it at all."""

from __future__ import annotations

import fnmatch
import json

from foundry_automation import schema as SCH
from foundry_automation import service_map as SM
from foundry_coordinator import agent_config as AC
from foundry_coordinator import services as SV
from foundry_coordinator import states as S
from tests.coordinator.helpers import (by_task, coordinator, make_repo, run_until, write_ok,
                                       write_profile)

LIVE_TOOLS = ("generate_music generate_bgm generate_sfx generate_image edit_image remove_background "
              "upscale_image generate_video interpolate_video add_audio_to_video stitch_videos "
              "create_music_video generate_speech generate_dialogue register_voice list_voices "
              "generate_song_personal separate_stems analyze_song generate_parody_song "
              "media_check_job media_list_jobs media_cancel_job make_graphic add_captions edit_video "
              "reformat_video check_text create_explainer_video create_tutorial_video "
              "create_parody_music_video convert_voice create_comic create_presenter_video "
              "create_short_film create_private_image create_private_video generate_3d_model").split()


def allowed(rules: list, name: str) -> bool:
    """Kilo / OpenCode: the last matching rule wins; no match = the config default (allow)."""
    verdict = "allow"
    for r in rules:
        if fnmatch.fnmatchcase(name, r["permission"]):
            verdict = r["action"]
    return verdict == "allow"


def open_tools(rules) -> set:
    return {t for t in LIVE_TOOLS if allowed(rules, f"Media_Gen_{t}")}


def test_a_tool_no_family_names_is_off_and_the_private_ones_never_open():
    assert len(LIVE_TOOLS) == 38
    assert open_tools(SM.media_gen_rules(False, False)) == set()
    audio = open_tools(SM.media_gen_rules(True, False))
    assert audio == (set(SM.MEDIA_GEN_AUDIO) | set(SM.MEDIA_GEN_JOBS)) - {"generate_song_personal"}
    every = open_tools(SM.media_gen_rules(True, True, True))
    assert "generate_3d_model" in every and "make_graphic" not in every
    assert not every & {"create_private_image", "create_private_video", "generate_song_personal",
                        "generate_video", "create_short_film"}
    three = open_tools(SM.media_gen_rules(False, False, True))
    assert three == {"generate_3d_model"} | set(SM.MEDIA_GEN_JOBS)
    # the wildcard goes first so the named allows after it win
    rules = SM.media_gen_rules(False, False, True)
    assert rules[0] == {"permission": "Media_Gen_*", "pattern": "*", "action": "deny"}
    assert rules[-1]["action"] == "allow"


def test_the_3d_setting_is_in_the_schema_and_defaults_to_allow():
    assert SCH.AUTOMATION_DEFAULTS["media_gen_3d"] == "allow"
    f = SCH.find_field("automation.media_gen_3d")
    assert f is not None and f.default == "allow"
    assert SV.models3d_allowed(_project({}), ["blender"]) is True
    assert SV.models3d_allowed(_project({}), ["audio"]) is False
    assert SV.models3d_allowed(_project({"media_gen_3d": "deny"}), ["blender"]) is False


def _project(auto: dict):
    from foundry_coordinator import automation as A

    class Prof:
        raw = {"automation": auto}
        automation = A.table("p", auto)
    return type("P", (), {"profile": Prof(), "id": "p"})()


def test_the_brief_names_the_library_the_folder_the_sidecar_and_the_fbx(monkeypatch):
    monkeypatch.setattr(SM, "merged_mcp", lambda **kw: {
        "Media_Gen": {"type": "remote", "url": "http://10.0.0.5:8190/mcp"}})
    assert SV.media_gen_upload_url() == "http://10.0.0.5:8190/upload"
    b = SV.models3d_brief(_project({}), ["blender"])
    for part in ('`library: "game"`', "http://10.0.0.5:8190/upload",
                 "change_picture", "ONE media_check_job per completed step",
                 "assets/source/<category>/<ID>/gen/<ID>_ai.glb", ".media_gen.json",
                 "glb_detail", "export_fbx.py", "unapproved", "ai_mesh_eval.py",
                 "not installed on the media server"):
        assert part in b, part
    assert SV.models3d_brief(_project({"media_gen_3d": "deny"}), ["blender"]) == ""
    assert SV.models3d_brief(_project({}), ["audio"]) == ""
    monkeypatch.setattr(SM, "merged_mcp", lambda **kw: {})
    assert SV.media_gen_upload_url() == "http://192.168.0.206:8190/upload"


TASKS = [
    {"id": "M1", "title": "mesh sprint", "spec": "Write docs/m.txt.", "scope": ["docs/m.txt"],
     "tags": ["needs:blender"]},
    {"id": "T1", "title": "code sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
]


def _env(tmp_path, extra=""):
    root, _ = make_repo(tmp_path, "game", TASKS, extra_files={"kilo.jsonc": '{"mcp": {}}\n'})
    prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({
        "M1": write_ok("docs/m.txt"), "T1": write_ok("src/a.txt")})})
    return c, c.projects["game"]


def _session(P, tid):
    return [s for s in P.client.sessions.values()
            if s["metadata"].get("task") == tid and s["metadata"].get("role") == "implementer"][-1]


def test_a_blender_sprint_can_call_generate_3d_model_and_a_code_sprint_cannot(tmp_path):
    c, P = _env(tmp_path)
    for tid in ("M1", "T1"):
        c.approve("game", tid, [], "victor")
        assert run_until(c, lambda: c.store.task("game", tid)["state"] == S.COMPLETE, limit=40)
    m1 = _session(P, "M1")
    assert open_tools(m1["permissions"]) == {"generate_3d_model"} | set(SM.MEDIA_GEN_JOBS)
    t1 = _session(P, "T1")
    assert open_tools(t1["permissions"]) == set()
    prompts = " ".join(p for s in P.client.sessions.values()
                       if s["metadata"].get("task") == "M1" for p in s["prompts"])
    assert "3D models from a picture" in prompts
    c2, P2 = _env(tmp_path / "b", extra='\n[automation]\nmedia_gen_3d = "deny"\n')
    c2.approve("game", "M1", [], "victor")
    assert run_until(c2, lambda: c2.store.task("game", "M1")["state"] == S.COMPLETE, limit=40)
    assert open_tools(_session(P2, "M1")["permissions"]) == set()


def test_an_opencode_config_gets_media_gens_remote_block_without_headers(tmp_path):
    g = tmp_path / "kilo.jsonc"
    g.write_text(json.dumps({"mcp": {
        "Media_Gen": {"type": "remote", "url": "http://192.168.0.206:8190/mcp", "enabled": False,
                      "timeout": 300000, "headers": {"Authorization": "Bearer x"}},
        "blender-mcp": {"type": "local", "command": ["uvx", "blender-mcp"]},
        "searxng": {"type": "remote", "url": "http://h/mcp"}}}), encoding="utf-8")
    got = AC.remote_mcp_blocks(str(g))
    assert got == {"Media_Gen": {"type": "remote", "url": "http://192.168.0.206:8190/mcp",
                                 "enabled": False, "timeout": 300000}}
    assert AC.remote_mcp_blocks(str(tmp_path / "missing.jsonc")) == {}

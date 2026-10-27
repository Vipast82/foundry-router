"""2026-10-27: a session gets only the Media_Gen tools its sprint needs, and a sprint
that needs 2D image assets waits for the operator first (they come from ChatGPT for
now, Victor: "if there is anything that needs 2D assets, let me know first")."""

from __future__ import annotations

from foundry_automation import service_map as SM
from foundry_coordinator import services as SV
from foundry_coordinator import states as S
from tests.coordinator.helpers import (by_task, coordinator, make_repo, run_until, write_ok,
                                       write_profile)

TASKS = [
    {"id": "T1", "title": "code sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "A1", "title": "sfx sprint", "spec": "Write docs/a.txt.", "scope": ["docs/a.txt"],
     "tags": ["needs:audio"]},
    {"id": "I1", "title": "icon sprint", "spec": "Write art/i.txt.", "scope": ["art/i.txt"],
     "tags": ["needs:images"]},
]


def env(tmp_path, extra=""):
    root, _ = make_repo(tmp_path, "game", TASKS, extra_files={"kilo.jsonc": '{"mcp": {}}\n'})
    prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({
        "T1": write_ok("src/a.txt"), "A1": write_ok("docs/a.txt"), "I1": write_ok("art/i.txt")})})
    return c, c.projects["game"]


def denied(c, tid):
    perms = []
    for s in c.projects["game"].client.sessions.values():
        if s["metadata"].get("task") == tid and s["metadata"].get("role") == "implementer":
            perms = s["permissions"]
    return {r["permission"] for r in perms if r["action"] == "deny"
            and str(r["permission"]).startswith("Media_Gen_")}


def test_the_denied_set_per_need():
    every = set(SM.media_gen_denied(False, False))
    assert len(every) == 20 and "Media_Gen_media_check_job" in every
    audio = set(SM.media_gen_denied(True, False))
    assert "Media_Gen_generate_sfx" not in audio and "Media_Gen_generate_dialogue" not in audio
    assert "Media_Gen_media_check_job" not in audio                # its jobs can be checked
    assert {"Media_Gen_generate_image", "Media_Gen_generate_video",
            "Media_Gen_generate_song_personal"} <= audio           # never personal-only tools


def test_a_code_sprint_gets_no_media_gen_and_an_audio_sprint_gets_the_audio_tools(tmp_path):
    c, P = env(tmp_path)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, limit=40)
    assert len(denied(c, "T1")) == 20
    c.approve("game", "A1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "A1")["state"] == S.COMPLETE, limit=40)
    d = denied(c, "A1")
    assert "Media_Gen_generate_sfx" not in d and "Media_Gen_generate_music" not in d
    assert "Media_Gen_generate_image" in d


def test_an_images_sprint_waits_for_the_operator_then_follows_the_answer(tmp_path):
    c, P = env(tmp_path)
    c.approve("game", "I1", [], "victor")
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "I1")
    assert t["state"] in (S.QUEUED, S.READY) and not P.client.sessions   # nothing started
    did = SV.images_decision(c, "game", "I1")["decision_id"]
    d = c.obligations.decision(did)
    assert d["status"] == "open" and d["kind"] == "approval" and SV.IMG_CHATGPT in d["options"]
    assert any(e["kind"] == "operator_notice" for e in c.store.events("game", 50))
    c.decision_answer("game", "I1", did, SV.IMG_CHATGPT, actor="victor", channel="control")
    assert run_until(c, lambda: c.store.task("game", "I1")["state"] == S.COMPLETE, limit=40)
    assert SV.images_choice(c, "game", "I1") == "chatgpt"
    assert "Media_Gen_generate_image" in denied(c, "I1")         # ChatGPT: no image tools


def test_media_gen_answer_gives_the_image_tools_and_allow_skips_the_question(tmp_path):
    c, P = env(tmp_path)
    c.approve("game", "I1", [], "victor")
    assert run_until(c, lambda: SV.images_decision(c, "game", "I1") is not None, limit=6)
    did = SV.images_decision(c, "game", "I1")["decision_id"]
    c.decision_answer("game", "I1", did, SV.IMG_MEDIA_GEN, actor="victor", channel="control")
    assert run_until(c, lambda: c.store.task("game", "I1")["state"] == S.COMPLETE, limit=40)
    assert "Media_Gen_generate_image" not in denied(c, "I1")
    c2, P2 = env(tmp_path / "b", extra='\n[automation]\nmedia_gen_images = "allow"\n')
    c2.approve("game", "I1", [], "victor")
    assert run_until(c2, lambda: c2.store.task("game", "I1")["state"] == S.COMPLETE, limit=40)
    assert SV.images_decision(c2, "game", "I1") is None

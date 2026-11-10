"""FOUNDRY_SKILLS_DELIVERY_PLAN (gap 7): Foundry-shipped Kilo skills are installed into the
game's untracked `.kilo/skills/`, kept current, never written into tracked files, a hand copy
is backed up before it is replaced, and kilo.jsonc allows the listed skills."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from foundry_automation import jsonc
from foundry_coordinator import config_pull, kilo_config as K, skills_install as SI
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def ws(tmp_path):
    w = tmp_path / "game"
    w.mkdir()
    _git("init", "-q", cwd=w)
    (w / "README.md").write_text("game\n", encoding="utf-8")
    _git("add", "README.md", cwd=w)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=w)
    return w


@pytest.fixture()
def src(tmp_path, monkeypatch):
    """A fake Foundry skill source with one skill, `demo-skill`."""
    base = tmp_path / "src"
    (base / "demo-skill").mkdir(parents=True)
    (base / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\n---\nv1\n",
                                                  encoding="utf-8")
    monkeypatch.setattr(SI, "sources", lambda pid: [str(base)])
    return base


def prof(ws, install, pid="animaluprising"):
    return SimpleNamespace(id=pid, workspace=str(ws), raw={"skills": {"install": install}})


class Store:
    def __init__(self):
        self.events, self.m = [], {}

    def event(self, pid, tid, kind, detail=None):
        self.events.append((kind, detail))

    def meta(self, k):
        return self.m.get(k)

    def set_meta(self, k, v):
        self.m[k] = v


def test_off_by_default_and_the_real_skills_are_found():
    p = SimpleNamespace(id="animaluprising", workspace="", raw={})
    assert SI.settings(p)["install"] == [] and SI.allow_rules(p) == []
    assert SI.find_source("animaluprising", "au-blender-asset")
    assert SI.find_source("animaluprising", "foundry-images")       # contrib/media-adapter
    assert SI.find_source("animaluprising", "no-such-skill") is None
    text = open(os.path.join(SI.find_source("animaluprising", "au-blender-asset"), "SKILL.md"),
                encoding="utf-8").read()
    assert text.startswith("---\nname: au-blender-asset\n")
    for word in ("+Y", "screen-left", "ref_align.py", "side_check.py", "export_fbx.py",
                 "--bg", "Exit 75"):
        assert word in text


def test_install_update_same_and_remove(ws, src, tmp_path):
    st = Store()
    out = SI.sync(prof(ws, ["demo-skill", "ghost"]), str(tmp_path / "state"), st)
    dst = ws / ".kilo" / "skills" / "demo-skill"
    assert out["installed"] == ["demo-skill"] and out["missing"] == ["ghost"]
    assert (dst / "SKILL.md").read_text(encoding="utf-8").endswith("v1\n")
    marker = json.loads((dst / SI.MARKER).read_text(encoding="utf-8"))
    assert marker["name"] == "demo-skill" and marker["hash"] == SI.tree_hash(str(src / "demo-skill"))
    assert SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"))["same"] == ["demo-skill"]
    (src / "demo-skill" / "SKILL.md").write_text("---\nname: demo-skill\n---\nv2\n",
                                                 encoding="utf-8")
    out = SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"))
    assert out["updated"] == ["demo-skill"] and out["replaced_hand_copy"] == []
    assert (dst / "SKILL.md").read_text(encoding="utf-8").endswith("v2\n")
    (ws / ".kilo" / "skills" / "hand-made").mkdir()
    (ws / ".kilo" / "skills" / "hand-made" / "SKILL.md").write_text("mine", encoding="utf-8")
    out = SI.sync(prof(ws, []), str(tmp_path / "state"))
    assert out["removed"] == ["demo-skill"] and not dst.exists()
    assert (ws / ".kilo" / "skills" / "hand-made" / "SKILL.md").exists()  # never Foundry's
    assert any(k == "skills_installed" for k, _ in st.events)
    r = subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True)
    assert ".kilo" in r.stdout and r.stdout.startswith("??")         # untracked, nothing staged


def test_a_hand_copy_is_backed_up_then_replaced(ws, src, tmp_path):
    dst = ws / ".kilo" / "skills" / "demo-skill"
    dst.mkdir(parents=True)
    (dst / "SKILL.md").write_text("an old hand copy\n", encoding="utf-8")
    out = SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"))
    assert out["updated"] == ["demo-skill"]
    bk = out["replaced_hand_copy"][0]["backup"]
    assert open(os.path.join(bk, "SKILL.md"), encoding="utf-8").read() == "an old hand copy\n"
    assert str(tmp_path / "state") in bk                             # outside the game checkout
    same = ws / ".kilo" / "skills" / "demo-skill"
    (same / SI.MARKER).unlink()                                      # identical, unmarked
    out = SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"))
    assert out["same"] == ["demo-skill"] and (same / SI.MARKER).exists()


def test_a_tracked_target_is_refused(ws, src, tmp_path):
    (ws / ".kilo" / "skills" / "x").mkdir(parents=True)
    (ws / ".kilo" / "skills" / "x" / "SKILL.md").write_text("tracked", encoding="utf-8")
    _git("add", ".kilo", cwd=ws)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "skills", cwd=ws)
    st = Store()
    out = SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"), st)
    assert "tracked in git" in out["refused"] and not out["installed"]
    assert not (ws / ".kilo" / "skills" / "demo-skill").exists()
    assert st.events[0][0] == "skills_install_refused"
    # v0.181.115: the same refusal is logged once, not on every sync
    SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"), st)
    SI.sync(prof(ws, ["demo-skill"]), str(tmp_path / "state"), st)
    assert [k for k, _ in st.events].count("skills_install_refused") == 1


def test_due_runs_at_once_on_a_list_change_then_waits(ws):
    st, t = Store(), [1000.0]
    p = prof(ws, ["a"])
    assert SI.due(p, st, lambda: t[0]) and not SI.due(p, st, lambda: t[0])
    t[0] += 301
    assert SI.due(p, st, lambda: t[0])
    assert SI.due(prof(ws, ["a", "b"]), st, lambda: t[0])           # the list changed


SOURCE = """{
  "agent": {
    "code": {"permission": {"*": "allow", "skill": {"*": "deny", "au-fast-lane": "allow"}}},
    "au-review": {"permission": {"*": "deny"}}
  },
  "mcp": {"foundry-media": {"type": "local", "command": ["x.ps1"], "enabled": true}}
}"""
RULES = {"implementer": {"permissions": {"studio_read": "allow", "studio_write": "allow"}},
         "reviewer": {}}


def test_kilo_jsonc_allows_the_installed_skills(tmp_path):
    p = tmp_path / "kilo.jsonc"
    p.write_text(SOURCE, encoding="utf-8")
    profile = SimpleNamespace(id="g", workspace=str(tmp_path), raw={
        "skills": {"install": ["au-blender-asset", "foundry-images"]}, "role_rules": RULES,
        "config_pull": {"kilo_config": str(p)}})
    out = K.regenerate(profile, apply=True, path=str(p))
    assert out["applied"]
    skill = jsonc.parse(p.read_text(encoding="utf-8"))["agent"]["code"]["permission"]["skill"]
    assert skill["au-blender-asset"] == "allow" and skill["au-fast-lane"] == "allow"
    assert skill["*"] == "deny"
    track = SimpleNamespace(worktree=str(tmp_path / "wt"), needs=["blender"], name="t")
    path = K.track_kilo_config(track, profile=profile, source=str(p))
    tskill = jsonc.parse(open(path, encoding="utf-8").read())["agent"]["code"]["permission"]["skill"]
    assert tskill["au-blender-asset"] == "allow"
    assert tskill["foundry-images"] == "deny"           # no `images` need: the deny still wins


def test_a_skills_change_regenerates_kilo_and_the_as_prompt_names_the_skill():
    assert config_pull.role_rules_changed({"settings": {"skills": {"install": []}}},
                                          {"settings": {"skills": {"install": ["au-blender-asset"]}}})
    on = SimpleNamespace(p=SimpleNamespace(raw={"skills": {"install": ["au-blender-asset"]}}))
    off = SimpleNamespace(p=SimpleNamespace(raw={}))
    assert "au-blender-asset" in AnimalUprisingAdapter._asset_skill_notice(on, "AS-K1")
    assert AnimalUprisingAdapter._asset_skill_notice(on, "GF-RT3") == ""
    assert AnimalUprisingAdapter._asset_skill_notice(off, "AS-K1") == ""


def test_an_installed_skill_refreshes_kilo_and_live_tracks(monkeypatch, tmp_path):
    """2026-11-02: 28 skills listed in the profile file were installed but kilo.jsonc still
    denied them ("*": deny), because only a router-side settings change regenerated it. A skill
    installed or removed now rewrites the main kilo.jsonc, asks for a Kilo reload and rewrites
    every live track's kilo.jsonc."""
    from foundry_coordinator import client_requests, kilo_config
    calls = {"regen": 0, "reload": [], "needs": [], "events": []}

    def fake_regen(profile, rules, apply=False, store=None, clock=None, path=""):
        calls["regen"] += 1
        return {"applied": True, "changed": ["code"], "backup": "b"}
    monkeypatch.setattr(kilo_config, "regenerate", fake_regen)
    monkeypatch.setattr(client_requests, "request",
                        lambda state, kind, pid, why, **kw: calls["reload"].append((kind, why)))
    rows = {"audio-local": {"name": "audio-local", "needs": ["studio", "audio"]},
            "plain": {"name": "plain", "needs": []}}
    store = SimpleNamespace(
        q=lambda sql, args: [{"name": n} for n in rows],
        track=lambda pid, n: rows[n],
        event=lambda pid, tid, kind, detail=None: calls["events"].append(kind))
    tracks = SimpleNamespace(set_needs=lambda P, name, needs, actor="": calls["needs"].append((name, needs)))
    engine = SimpleNamespace(store=store, tracks=tracks, state_dir=str(tmp_path), clock=lambda: 0.0)
    P = SimpleNamespace(id="g", profile=SimpleNamespace(id="g", workspace=str(tmp_path), raw={}))
    res = SI.refresh_kilo(engine, P, {"installed": ["roblox-luau"], "removed": []})
    assert calls["regen"] == 1 and calls["reload"][0][0] == "reload_kilo"
    assert ("audio-local", ["studio", "audio"]) in calls["needs"] and ("plain", ["none"]) in calls["needs"]
    assert res["tracks"] == ["audio-local", "plain"] and "skills_kilo_refreshed" in calls["events"]

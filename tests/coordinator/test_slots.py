"""Studio slots (2026-10-31, Victor: "build dispatch, real sprint"): a track bound to a
containerised Studio slot runs its scripted gate there and reaches the slot's Studio MCP,
never the workstation's Studio."""
import io
import json
import tarfile
from types import SimpleNamespace

from foundry_coordinator import slots as SL

SLOT_CFG = {"container": "studio-slot-poc", "repo": "/home/studio/work/AnimalUprising"}


def _profile(slots=None):
    return SimpleNamespace(raw={"slots": slots if slots is not None else {"slot-1": SLOT_CFG}})


def test_configured_slots_take_defaults_and_skip_disabled_or_containerless():
    out = SL.configured(_profile({"slot-1": SLOT_CFG, "off": {**SLOT_CFG, "enabled": False},
                                  "bad": {"repo": "/x"}}))
    assert list(out) == ["slot-1"]
    assert out["slot-1"]["foundry"] == "/opt/foundry" and out["slot-1"]["repo"] == SLOT_CFG["repo"]


def test_the_pipeline_is_wrapped_and_its_repo_points_into_the_slot():
    slot = {**SL.DEFAULTS, **SLOT_CFG, "id": "slot-1"}
    args = ["pipeline", "--repo", "C:/tracks/t1", "--sprint", "AS-J05", "--suites", "AS_J05"]
    cmd = SL.wrap_pipeline(slot, args, "C:/tracks/t1", "AS-J05", "abc123")
    assert cmd[1:3] == ["-m", "foundry_coordinator.slots"] and "run" in cmd
    i = cmd.index("--")
    assert cmd[i + 1:] == args
    assert cmd[cmd.index("--worktree") + 1] == "C:/tracks/t1" and cmd[cmd.index("--head") + 1] == "abc123"
    inner = SL.slot_pipeline_args(args, slot["repo"])
    assert inner[inner.index("--repo") + 1] == slot["repo"] and inner[0] == "pipeline"


def test_the_track_kilo_config_points_enabled_studio_mcp_at_the_slot(tmp_path):
    kc = tmp_path / "kilo.jsonc"
    kc.write_text(json.dumps({"mcp": {
        "Roblox_Studio": {"type": "local", "enabled": True, "command": ["cmd.exe", "/c", "x"]},
        "robloxstudio-mcp": {"type": "local", "enabled": False, "command": ["npx", "y"]},
        "blender-mcp": {"type": "local", "enabled": True, "command": ["uvx", "blender-mcp"]}}}))
    changed = SL.patch_kilo_config(str(kc), {**SL.DEFAULTS, **SLOT_CFG, "id": "slot-1"})
    data = json.loads(kc.read_text())
    assert changed == ["Roblox_Studio"]
    assert data["mcp"]["Roblox_Studio"]["command"] == [
        "docker", "exec", "-i", "studio-slot-poc", "/usr/local/bin/studio-mcp-official"]
    assert data["mcp"]["robloxstudio-mcp"]["command"] == ["npx", "y"]      # off: untouched
    assert data["mcp"]["blender-mcp"]["command"] == ["uvx", "blender-mcp"]  # not a Studio server
    assert SL.patch_kilo_config(str(kc), {**SL.DEFAULTS, **SLOT_CFG, "id": "slot-1"}) == []


def _tar(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_copy_back_takes_only_this_tasks_outputs(tmp_path, monkeypatch):
    blob = _tar({"docs/progress/AS-J05_studio_lines.txt": b"PASS",
                 ".plan/tmp/gate/AS-J05/calls_pipeline.json": b"{}",
                 "docs/evidence/AS-J05/props.png": b"png",
                 "docs/progress/OTHER_studio_lines.txt": b"no",
                 "src/server/Evil.lua": b"no",
                 "docs/evidence/AS-J05/../../../escape.txt": b"no"})
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=blob, stderr=b"")
    monkeypatch.setattr(SL.subprocess, "run", fake_run)
    out = SL.copy_back("studio-slot-poc", "/home/studio/work/AnimalUprising", str(tmp_path), "AS-J05")
    assert sorted(out) == [".plan/tmp/gate/AS-J05/calls_pipeline.json",
                           "docs/evidence/AS-J05/props.png",
                           "docs/progress/AS-J05_studio_lines.txt"]
    assert not (tmp_path / "src").exists() and not (tmp_path.parent / "escape.txt").exists()
    assert "AS-J05" in seen["cmd"][-1]
    assert SL.copy_back("c", "/r", str(tmp_path), "bad id; rm -rf /") == []


def _calls_for_track(slot_id):
    from foundry_coordinator.studio_calls import StudioCalls
    store = SimpleNamespace(track=lambda pid, name: {"data": {"slot": slot_id} if slot_id else {}})
    main = SimpleNamespace(profile=_profile())
    tracks = SimpleNamespace(is_track=lambda P: True, track_name=lambda P: "t1",
                             main_of=lambda P: main)
    c = SimpleNamespace(store=store, tracks=tracks,
                        _workspace_locks=lambda P: ["workspace:c:\\tracks\\t1"],
                        _session_locks=lambda P: ["studio:victor-workstation", "writer"])
    sc = StudioCalls.__new__(StudioCalls)
    sc.c, sc.store = c, store
    return sc, SimpleNamespace(id="animaluprising", profile=SimpleNamespace(workspace="C:/tracks/t1"))


def test_a_slot_track_takes_the_slot_lock_never_the_workstation_studio():
    sc, P = _calls_for_track("slot-1")
    locks = sc._run_locks(P)
    assert "slot:slot-1" in locks and not any(x.startswith("studio:") for x in locks)
    sc2, P2 = _calls_for_track("")
    assert sc2._slot(P2) is None


def test_a_lead_abort_that_fails_here_is_queued_and_retried_by_the_running_coordinator(tmp_path):
    """2026-10-31 AS-J05: a CLI `unblock` cancelled a lead case in a process with no Kilo
    credentials; the abort failed silently and the lead session kept the workstation's
    studio.lock. Now it is queued and the coordinator retries it."""
    from foundry_coordinator.lead import LeadManager
    from foundry_coordinator.store import Store
    store = Store(str(tmp_path / "c.sqlite"))
    calls = []

    class Client:
        def __init__(self, ok):
            self.ok = ok

        def abort(self, sid, directory):
            calls.append((sid, self.ok))
            if self.ok is None:
                raise RuntimeError("HTTP 401")
            return self.ok

    P_cli = SimpleNamespace(id="game", client=Client(None), profile=SimpleNamespace(workspace="w"))
    lm = LeadManager.__new__(LeadManager)
    lm.store, lm.clock = store, (lambda: 100.0)
    lm.c = SimpleNamespace(projects={"game": SimpleNamespace(id="game", client=Client(True),
                                                            profile=SimpleNamespace(workspace="w"))})
    lm._abort(P_cli, "ses_x", "C:/tracks/t1")
    assert json.loads(store.meta(LeadManager.ABORT_PENDING_KEY))[0]["sid"] == "ses_x"
    assert lm.retry_aborts() == ["ses_x"]
    assert json.loads(store.meta(LeadManager.ABORT_PENDING_KEY)) == []
    assert calls == [("ses_x", None), ("ses_x", True)]

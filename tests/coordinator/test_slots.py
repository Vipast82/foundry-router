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


def test_a_lead_on_a_slot_track_is_told_to_leave_the_workstation_studio_lock_alone():
    from foundry_coordinator.lead import LeadManager
    lm = LeadManager.__new__(LeadManager)
    rows = {"t1": {"data": {"slot": "slot-1"}}, "t2": {"data": {}}}
    tasks = {"AS-J05": "t1", "X": "t2", "M": ""}
    lm.store = SimpleNamespace(task_track=lambda pid, tid: tasks[tid], track=lambda pid, n: rows[n])
    P = SimpleNamespace(id="game")
    txt = lm._slot_text(P, {"task_id": "AS-J05"})
    assert "slot-1" in txt and "studio.lock" in txt
    assert lm._slot_text(P, {"task_id": "X"}) == "" and lm._slot_text(P, {"task_id": "M"}) == ""


def test_autoheal_on_a_slot_track_repairs_the_slot_never_the_workstation(monkeypatch):
    """2026-10-31 AS-J05: a slot track's stale Rojo sync reopened the WORKSTATION's TEST Studio
    twice (the main sprint's Studio closed; the slot stayed stale)."""
    from foundry_coordinator import autoheal, client_api
    slot = {**SL.DEFAULTS, **SLOT_CFG, "id": "slot-1"}
    monkeypatch.setattr(SL, "slot_for", lambda c, P: slot)
    seen = {}
    monkeypatch.setattr(SL, "resync", lambda s, restart_rojo=True: (seen.setdefault("slot", s["id"]), True)[1:] + ("Studio is back",))
    monkeypatch.setattr(client_api, "studio_reopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("workstation reopen")))
    monkeypatch.setattr(client_api, "restart_component", lambda *a, **k: (_ for _ in ()).throw(AssertionError("workstation rojo")))
    events = []
    c = SimpleNamespace(store=SimpleNamespace(event=lambda *a, **k: events.append(a)))
    P = SimpleNamespace(id="game", profile=SimpleNamespace(workspace="w"))
    out = autoheal.repair(c, P, {"task_id": "AS-J05", "data": {}}, "rojo_stale", "retry")
    assert seen["slot"] == "slot-1" and out[0]["repair"] == "slot_resync" and out[0]["ok"]
    assert autoheal.request_studio_reopen(c, P, {"task_id": "AS-J05"})[0] == ""


# ---- remote slots (Proxmox LXC over ssh), 2026-10-31 ------------------------------------------

REMOTE = {"id": "slot-2", "container": "studio-slot", "host": "studio@10.0.0.5",
          "ssh": ["ssh", "-o", "BatchMode=yes"], "repo": "/home/studio/work/AnimalUprising",
          "foundry": "/opt/foundry", "path_prefix": "/home/studio/.rokit/bin",
          "official_mcp": "/usr/local/bin/studio-mcp-official",
          "community_mcp": "/usr/local/bin/robloxstudio-mcp",
          "au_push_url": "ssh://studio@10.0.0.5/srv/studio-slot/AnimalUprising.git",
          "foundry_push_url": "ssh://studio@10.0.0.5/srv/studio-slot/foundry.git",
          "foundry_host_dir": "/srv/studio-slot/foundry"}


def test_a_local_slot_runs_docker_here_and_a_remote_one_over_ssh_with_one_quoted_command():
    import shlex
    assert SL.exec_argv({"container": "c"}, ["bash", "-lc", "echo 'a b'"]) == \
        ["docker", "exec", "c", "bash", "-lc", "echo 'a b'"]
    argv = SL.exec_argv(REMOTE, ["bash", "-lc", "cd /r && echo 'x y' $HOME"])
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "studio@10.0.0.5"] and len(argv) == 5
    # the remote shell splits the one string back into exactly the argv we meant
    assert shlex.split(argv[4]) == ["docker", "exec", "studio-slot", "bash", "-lc",
                                    "cd /r && echo 'x y' $HOME"]
    assert SL.ssh_cmd({"ssh": "ssh -p 2222 -o BatchMode=yes"}) == ["ssh", "-p", "2222", "-o", "BatchMode=yes"]


def test_a_remote_slots_kilo_studio_mcp_is_stdio_over_ssh():
    import shlex
    blocks = SL.kilo_mcp_overrides(REMOTE)
    cmd = blocks["robloxstudio-mcp"]["command"]
    assert cmd[:4] == ["ssh", "-o", "BatchMode=yes", "studio@10.0.0.5"]
    assert shlex.split(cmd[4]) == ["docker", "exec", "-i", "studio-slot", "/usr/local/bin/robloxstudio-mcp"]


def test_the_wrapped_pipeline_carries_the_remote_settings_and_run_parses_them_back():
    argv = SL.wrap_pipeline(REMOTE, ["pipeline", "--repo", "C:/t", "--sprint", "AS-J05"],
                            "C:/t", "AS-J05", "abc123")
    i = argv.index("run")
    seen = {}
    real = SL.run
    try:
        SL.run = lambda a: seen.setdefault("a", a) and 0
        SL.main(argv[i:])
    finally:
        SL.run = real
    a = seen["a"]
    assert (a.host, a.au_push_url, a.foundry_host_dir) == (REMOTE["host"], REMOTE["au_push_url"],
                                                           REMOTE["foundry_host_dir"])
    assert a.ssh == "ssh -o BatchMode=yes" and a.pipeline[0] == "pipeline"
    # a local slot's wrapper has no remote flags at all
    assert "--host" not in SL.wrap_pipeline({**REMOTE, "host": ""}, [], "C:/t", "T", "h")


def test_a_remote_run_pushes_the_track_commit_and_the_deployed_foundry_before_checkout(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = b"SLOT-CHECKOUT abc OK\n" if "slot-checkout" in " ".join(cmd) else b"0123456789ab\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    class P:
        def __init__(self, cmd, **kw):
            calls.append(cmd)

        def wait(self):
            return 0
    monkeypatch.setattr(SL.subprocess, "run", fake_run)
    monkeypatch.setattr(SL.subprocess, "Popen", P)
    monkeypatch.setattr(SL, "copy_back", lambda *a, **k: [])
    argv = SL.wrap_pipeline(REMOTE, ["pipeline", "--repo", "C:/t"], "C:/t", "AS-J05", "abc123")
    assert SL.main(argv[argv.index("run"):]) == 0
    flat = [" ".join(c) for c in calls]
    au = next(i for i, c in enumerate(flat) if "AnimalUprising.git" in c)
    fd = next(i for i, c in enumerate(flat) if "foundry.git" in c and "push" in c)
    co = next(i for i, c in enumerate(flat) if "slot-checkout" in c)
    assert au < co and fd < co
    assert "abc123:refs/heads/slot/AS-J05" in flat[au] and "HEAD:refs/heads/deploy" in flat[fd]
    assert any("checkout -q --force --detach FETCH_HEAD" in c and c.startswith("ssh") for c in flat)


def test_a_remote_run_stops_before_checkout_when_the_push_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout=b"permission denied", stderr=b"")
    monkeypatch.setattr(SL.subprocess, "run", fake_run)
    argv = SL.wrap_pipeline(REMOTE, ["pipeline"], "C:/t", "AS-J05", "abc123")
    assert SL.main(argv[argv.index("run"):]) == 3
    assert not any("slot-checkout" in " ".join(c) for c in calls)


def test_a_remote_slot_derives_its_push_targets_and_ssh_accepts_a_new_host_once():
    class P:
        raw = {"slots": {"slot-2": {"container": "studio-slot", "host": "studio@10.0.0.6"},
                         "slot-3": {"container": "x", "host": "studio@10.0.0.7",
                                    "au_push_url": "ssh://studio@10.0.0.7/other.git"},
                         "off": {"container": "y", "enabled": False}}}
    got = SL.configured(P)
    assert set(got) == {"slot-2", "slot-3"}
    s2 = got["slot-2"]
    assert s2["au_push_url"] == "ssh://studio@10.0.0.6/srv/studio-slot/AnimalUprising.git"
    assert s2["foundry_push_url"] == "ssh://studio@10.0.0.6/srv/studio-slot/foundry.git"
    assert s2["foundry_host_dir"] == "/srv/studio-slot/foundry"
    assert got["slot-3"]["au_push_url"] == "ssh://studio@10.0.0.7/other.git"
    argv = SL.exec_argv(s2, ["true"])
    assert "StrictHostKeyChecking=accept-new" in argv and "BatchMode=yes" in argv
    assert argv[argv.index("studio@10.0.0.6") + 1].startswith("docker exec studio-slot")


# ----------------------------------------------------------------------------- W2: main on a slot

def _main_calls(main_slot="slot-1"):
    """StudioCalls for the MAIN project with [studio] main_slot (LINUX_WORKSTATION_PLAN W2)."""
    from foundry_coordinator.studio_calls import StudioCalls
    prof = SimpleNamespace(raw={"slots": {"slot-1": SLOT_CFG}, "studio": {"main_slot": main_slot}},
                           workspace="C:/Roblox/AnimalUprising")
    P = SimpleNamespace(id="animaluprising", profile=prof)
    store = SimpleNamespace(track=lambda pid, name: None)
    tracks = SimpleNamespace(is_track=lambda X: X is not P, track_name=lambda X: "t1",
                             main_of=lambda X: P)
    c = SimpleNamespace(store=store, tracks=tracks,
                        _workspace_locks=lambda X: ["workspace:c:\roblox\animaluprising"],
                        _session_locks=lambda X: ["studio:victor-workstation", "writer"])
    sc = StudioCalls.__new__(StudioCalls)
    sc.c, sc.store = c, store
    return sc, P, c


def test_main_on_a_slot_takes_the_slot_like_a_slot_track():
    sc, P, c = _main_calls()
    assert SL.slot_for(c, P)["id"] == "slot-1"
    locks = sc._run_locks(P)
    assert "slot:slot-1" in locks and not any(x.startswith("studio:") for x in locks)
    # off (empty) = this PC's Studio, as before
    sc0, P0, c0 = _main_calls("")
    assert SL.slot_for(c0, P0) is None
    # a main slot that is not configured (or disabled) is none at all
    sc9, P9, c9 = _main_calls("slot-9")
    assert SL.slot_for(c9, P9) is None


def test_an_unbound_track_never_takes_the_main_slot():
    sc, P, c = _main_calls()
    track = SimpleNamespace(id="animaluprising", profile=P.profile)   # shares main's settings
    assert SL.slot_for(c, track) is None


def test_the_fast_lane_steps_aside_while_main_studio_is_a_slots():
    from foundry_coordinator import fast_lane as FL
    prof = SimpleNamespace(raw={"studio": {"main_slot": "slot-1"}}, fast_lane_task=lambda t: True)
    assert FL.is_task(SimpleNamespace(profile=prof), "UI-1") is False
    prof.raw = {}
    assert FL.is_task(SimpleNamespace(profile=prof), "UI-1") is True
    tp = SimpleNamespace(raw={"studio": {"main_slot": "slot-1"}}, fast_lane_task=lambda t: True)
    tp.__dict__["track"] = "t1"                     # a track keeps its own fast-lane setting
    assert FL.is_task(SimpleNamespace(profile=tp), "UI-1") is True


def test_main_kilo_studio_mcp_points_at_the_slot_and_comes_back(tmp_path):
    kc = tmp_path / "kilo.jsonc"
    orig = {"Roblox_Studio": {"type": "local", "enabled": True,
                              "command": ["cmd.exe", "/d", "/c", "mcp.bat"]},
            "robloxstudio-mcp": {"type": "local", "enabled": True,
                                 "command": ["npx", "-y", "@chrrxs/robloxstudio-mcp@3.4.2"]},
            "context7": {"enabled": False}}
    kc.write_text(json.dumps({"mcp": orig}, indent=1), encoding="utf-8")
    prof = SimpleNamespace(id="animaluprising", raw={
        "slots": {"slot-1": {"container": "studio-slot", "host": "studio@192.168.0.115"}},
        "studio": {"main_slot": "slot-1"}})
    out = SL.sync_main_kilo(prof, str(tmp_path / "state"), path=str(kc))
    assert out["action"] == "slot" and sorted(out["changed"]) == ["Roblox_Studio", "robloxstudio-mcp"]
    now = json.loads(kc.read_text(encoding="utf-8"))["mcp"]
    assert "studio@192.168.0.115" in now["Roblox_Studio"]["command"]
    assert now["context7"] == {"enabled": False}
    # again: nothing to change, the saved originals are not overwritten by the slot's blocks
    assert SL.sync_main_kilo(prof, str(tmp_path / "state"), path=str(kc))["changed"] == []
    # cleared: the workstation's own blocks come back, the saved copy is gone
    prof.raw["studio"]["main_slot"] = ""
    out = SL.sync_main_kilo(prof, str(tmp_path / "state"), path=str(kc))
    assert out["action"] == "restore"
    assert json.loads(kc.read_text(encoding="utf-8"))["mcp"] == orig
    assert SL.sync_main_kilo(prof, str(tmp_path / "state"), path=str(kc))["action"] == "none"

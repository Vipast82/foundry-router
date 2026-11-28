"""The slot installers (contrib/studio-slot/proxmox/setup-host.sh, install/slot.ps1) and the slot
image's scripts: repair without a code, the project's own game mirror, several slots on one host,
the Windows key re-sync. Text and syntax checks (the installers run on a VM), plus unit tests of
the Python pieces."""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SLOT = ROOT / "contrib" / "studio-slot"
SETUP = SLOT / "proxmox" / "setup-host.sh"


def text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _gnu_bash() -> str:
    exe = shutil.which("bash") or ""
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return exe if r.returncode == 0 and b"GNU bash" in (r.stdout or b"") else ""


@pytest.mark.skipif(not _gnu_bash(), reason="no GNU bash here")
@pytest.mark.parametrize("script", ["contrib/studio-slot/proxmox/setup-host.sh",
                                    "contrib/studio-slot/entrypoint.sh",
                                    "contrib/studio-slot/slot-checkout.sh",
                                    "contrib/studio-slot/lane-agent.sh"])
def test_the_slot_scripts_parse(script):
    r = subprocess.run([_gnu_bash(), "-n", str(ROOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", ["contrib/studio-slot/proxmox/setup-host.sh",
                                    "contrib/studio-slot/entrypoint.sh",
                                    "contrib/studio-slot/slot-checkout.sh", "install/workstation.sh",
                                    "install/junction/junction-entrypoint.sh", "install/pins.env"])
def test_shell_files_keep_lf_line_endings(script):
    assert b"\r\n" not in (ROOT / script).read_bytes()


def test_no_installer_assumes_a_private_router_address():
    for p in ("contrib/studio-slot/proxmox/setup-host.sh", "install/workstation.sh",
              "install/workstation.ps1", "install/slot.ps1", "install/junction/docker-compose.yml",
              "install/junction/junction-entrypoint.sh", "install/create-workstation-lxc.sh",
              "contrib/studio-slot/proxmox/create-slot-lxc.sh"):
        t = text(ROOT / p)
        assert "192.168.0.112" not in t, p
    # asked for (no default) when not given
    assert 'read -r -p "Foundry router address (e.g.' in text(SETUP)
    assert '[ -n "$ROUTER" ] || die "no router address' in text(SETUP)


def test_setup_host_repairs_an_enrolled_slot_without_a_code():
    t = text(SETUP)
    assert "--repair) MODE=repair" in t and "--reenrol|--reenroll) MODE=enrol" in t
    assert '"/v1/foundry/secrets/enrollment"' in t
    # the router address and fingerprint come from the slot's own slot.env
    assert 'ROUTER="$(envget "$ENR_ROOT" FOUNDRY_URL)"' in t
    # the key is only (re)written on an enrolment, and a re-enrolment restarts a running slot
    assert 'if [ "$REPAIR" = 0 ]; then\n    py write-key' in t
    assert 'if [ "$KEYS_CHANGED" = 1 ] && [ "$WAS_RUNNING" = 1 ]; then' in t
    assert 'docker restart "$CONTAINER"' in t
    # the Foundry checkout is not moved under a running slot
    assert 'elif [ "$WAS_RUNNING" = 1 ]; then' in t
    # the answer is in a private temp folder, removed whatever happens
    assert "trap 'rm -rf \"$WORK\"' EXIT" in t and "chmod 700 \"$WORK\"" in t


def test_setup_host_uses_the_projects_mirror_and_a_folder_per_extra_slot():
    t = text(SETUP)
    assert 'game_name() { case "${1,,}" in \'\'|animaluprising) echo AnimalUprising' in t
    assert '"$GAME.git"' in t and "AnimalUprising.git" not in t.replace("echo AnimalUprising", "")
    assert '"$BASE"/*/foundry) ROOT="${HOST_DIR%/foundry}"' in t
    for k in ("SLOT_ROOT", "SLOT_GAME", "SLOT_AGENT_PORT", "SLOT_KILO_PORT", "SLOT_OPENCODE_PORT",
              "SLOT_LANES_ROOT"):
        assert f'echo "{k}=' in t, k
    # only a slot in its own folder is its own compose project: the first keeps its volume
    assert 'if [ "$SHARED" = 1 ]; then echo "COMPOSE_PROJECT_NAME=studio-' in t
    assert 'UNIT=foundry-slot-keys; [ "$SHARED" = 0 ] || UNIT="foundry-slot-keys-$SLOT"' in t
    assert "--root $ROOT" in t


def test_the_compose_files_take_the_slots_folder_and_game_with_todays_defaults():
    dc = text(SLOT / "proxmox" / "docker-compose.yml")
    assert '"${SLOT_ROOT:-/srv/studio-slot}/${SLOT_GAME:-AnimalUprising}.git:/work/${SLOT_GAME:-AnimalUprising}:ro"' in dc
    assert '"${SLOT_ROOT:-/srv/studio-slot}/foundry:/opt/foundry:ro"' in dc
    assert 'AU_SOURCE: "/work/${SLOT_GAME:-AnimalUprising}"' in dc
    # the AU LIVE place stays on the never-list until the router's setting replaces it
    assert 'ROJO_NEVER_PLACE_IDS: "${ROJO_NEVER_PLACE_IDS:-138024886323256}"' in dc
    lanes = text(SLOT / "proxmox" / "docker-compose.lanes.yml")
    assert '"/srv/foundry-lanes:/srv/foundry-lanes"' in lanes      # a slot's own folder is inside
    assert 'SLOT_LANES_ROOT: "${SLOT_LANES_ROOT:-/srv/foundry-lanes}"' in lanes
    assert "${SLOT_ROOT:-/srv/studio-slot}/${SLOT_GAME:-AnimalUprising}.git" in lanes


@pytest.mark.skipif(not _gnu_bash(), reason="no GNU bash here")
@pytest.mark.parametrize("project,name", [("", "AnimalUprising"), ("animaluprising", "AnimalUprising"),
                                          ("AnimalUprising", "AnimalUprising"),
                                          ("othergame", "othergame")])
def test_the_slot_image_scripts_name_the_game_like_the_coordinator(project, name):
    from foundry_coordinator.slots import game_repo_name
    from foundry_router.machines import game_repo_name as router_name
    assert game_repo_name(project) == router_name(project) == name
    for script in ("entrypoint.sh", "slot-checkout.sh"):
        t = text(SLOT / script)
        start = t.index('_p="${FOUNDRY_PROJECT:-}"')
        snippet = t[start:t.index("\n", t.index("case ", start))] + '\necho "$GAME_NAME"\n'
        r = subprocess.run([_gnu_bash(), "-c", snippet], capture_output=True, text=True,
                           env={"FOUNDRY_PROJECT": project, "PATH": "/usr/bin:/bin"})
        assert r.stdout.strip() == name, (script, r.stderr)


def test_configured_slots_take_the_routers_mirror_and_keep_aus_defaults():
    """The router writes a slot's mirror (and another game's checkout) when it is added; a slot
    configured by hand without them keeps AnimalUprising's, as before."""
    from foundry_coordinator import slots
    from foundry_router.machines import derived_slot
    au = types.SimpleNamespace(id="animaluprising", raw={"slots": {
        "s1": {"container": "studio-slot", "host": "studio@10.0.0.5"},
        "w1": {"os": "windows", "host": "me@10.0.0.6"}}})
    c = slots.configured(au)
    assert c["s1"]["au_push_url"] == "ssh://studio@10.0.0.5/srv/studio-slot/AnimalUprising.git"
    assert c["s1"]["repo"] == "/home/studio/work/AnimalUprising"
    assert c["w1"]["au_push_url"] == "me@10.0.0.6:C:/Foundry/studio-slot/AnimalUprising.git"
    og = types.SimpleNamespace(id="othergame", raw={"slots": {
        "s1": derived_slot("studio@10.0.0.5", {"container": "studio-slot"}, "othergame"),
        "w1": derived_slot("me@10.0.0.6", {"os": "windows"}, "othergame", "windows")}})
    c = slots.configured(og)
    assert c["s1"]["au_push_url"] == "ssh://studio@10.0.0.5/srv/studio-slot/othergame.git"
    assert c["s1"]["repo"] == "/home/studio/work/othergame"
    assert c["w1"]["au_push_url"] == "me@10.0.0.6:C:/Foundry/studio-slot/othergame.git"
    # AU's router settings stay what they were (no repo written)
    assert "repo" not in derived_slot("studio@h", {}, "animaluprising")


def test_slot_keys_takes_a_root_per_slot(tmp_path):
    t = text(SLOT / "proxmox" / "slot-keys.py")
    assert 'ap.add_argument("--root", default=ROOT)' in t
    assert 'a.key_file = a.key_file or f"{ROOT}/slot-key/slot-key.txt"' in t
    # it runs against a folder with no slot.env and says so
    import runpy
    mod = runpy.run_path(str(SLOT / "proxmox" / "slot-keys.py"), run_name="slot_keys")
    assert mod["main"](["--root", str(tmp_path)]) == 2


def test_the_windows_key_sync_keeps_hand_added_keys():
    import runpy
    mod = runpy.run_path(str(ROOT / "install" / "slot-keys-win.py"), run_name="slot_keys_win")
    old = ("ssh-ed25519 " + "A" * 60 + " mine\n"
           "ssh-ed25519 " + "B" * 60 + " old-ws foundry-managed\n")
    new = mod["merge"](old, ["ssh-ed25519 " + "C" * 60 + " ws"])
    assert ("A" * 60) in new and ("B" * 60) not in new
    assert new.endswith("ws foundry-managed\n")
    assert mod["merge"](new, ["ssh-ed25519 " + "C" * 60 + " ws"]) == new


def test_the_windows_key_sync_writes_in_place(tmp_path):
    import runpy
    mod = runpy.run_path(str(ROOT / "install" / "slot-keys-win.py"), run_name="slot_keys_win")
    ak = tmp_path / "administrators_authorized_keys"
    ak.write_text("ssh-ed25519 " + "A" * 60 + " mine\n", encoding="utf-8")
    assert mod["write_keys"](["ssh-ed25519 " + "C" * 60 + " ws"], str(ak)) is True
    assert mod["write_keys"](["ssh-ed25519 " + "C" * 60 + " ws"], str(ak)) is False
    assert mod["main"](["--root", str(tmp_path)]) == 2          # no slot.json: nothing done


def _slot_ps1() -> str:
    return text(ROOT / "install" / "slot.ps1")


def test_slot_ps1_repairs_keeps_passwords_and_resyncs_keys():
    t = _slot_ps1()
    assert "[switch]$Repair" in t and "[switch]$Reenrol" in t and "[string]$Fingerprint" in t
    assert '"/v1/foundry/secrets/enrollment"' in t
    # passwords: kept from the lane secrets file, else from the cryptographic generator
    assert "[Security.Cryptography.RandomNumberGenerator]::Create()" in t
    assert not re.search(r"\|\s*ForEach-Object\s*\{\s*Get-Random", t)     # the old passwords
    assert 'if (-not $vals[$pw]) { $vals[$pw] = New-Password }' in t
    # the scheduled task that keeps administrators_authorized_keys in step
    assert 'Register-ScheduledTask -TaskName "Foundry slot keys"' in t
    assert "install\\slot-keys-win.py" in t and "-RepetitionInterval (New-TimeSpan -Minutes 5)" in t
    # client.toml is merged, not overwritten; a re-enrolment restarts a running client
    assert "workstation settings --settings $wantFile" in t
    assert "[IO.File]::WriteAllText((Join-Path $cfgDir \"client.toml\")" not in t
    assert "if ($running -and $keysChanged)" in t and "workstation restart" in t
    # the game mirror is the project's; DRAFT stays until the Linux pilot (Db5)
    assert '$mirror = Join-Path $slotDir "$game.git"' in t
    assert "DRAFT" in t and "Db5" in t


def test_every_native_command_in_the_windows_installers_is_checked():
    """git / pip / npm / icacls / winget lines are followed by Test-Exit (or read $LASTEXITCODE)."""
    for name in ("slot.ps1", "workstation.ps1"):
        lines = text(ROOT / "install" / name).splitlines()
        for i, ln in enumerate(lines):
            s = ln.strip()
            if re.match(r"^(&\s*(git|npm|winget|icacls|py|\$py)\b|\$\w+\s*=\s*&\s*\$py\b|"
                        r"if \(.*\) \{ & (git|py)\b)", s) and "describe --tags" not in s:
                window = " ".join(x.strip() for x in lines[i:i + 3])
                assert "Test-Exit" in window or "$LASTEXITCODE" in window, f"{name}:{i + 1}: {s}"


def test_the_ps1_never_prints_the_slot_key():
    t = _slot_ps1()
    for ln in t.splitlines():
        if "Write-Host" in ln or ln.strip().startswith(("Ok ", "Warn ")):
            assert "$en.key" not in ln and "tmpKey" not in ln and "Get-Content -Raw -Encoding UTF8 $keyFile" not in ln, ln
    assert "Remove-Item -Recurse -Force $work" in t and "Lock-Folder $work" in t

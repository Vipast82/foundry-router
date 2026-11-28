"""A Windows slot (Distributed Foundry b-5, docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md B10): no
Docker, native Studio, lanes under C:/Foundry; its OpenSSH shell is Git for Windows' bash
(install/slot.ps1), so the coordinator's POSIX commands and git over ssh work as on Linux."""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from foundry_coordinator import lane_fetch as F
from foundry_coordinator import lane_tunnel as T
from foundry_coordinator import slots as SL
from foundry_coordinator.adapters.laneclient import lane_root

ROOT = pathlib.Path(__file__).resolve().parents[2]
WIN = {"host": "victor@192.168.0.130", "os": "windows", "studio": "native", "lanes": True,
       "ssh": ["ssh", "-o", "BatchMode=yes"]}


def prof(slots):
    import types
    return types.SimpleNamespace(raw={"slots": slots})


def test_a_windows_slot_needs_no_container_and_gets_its_layout():
    s = SL.configured(prof({"slot-w": dict(WIN)}))["slot-w"]
    assert s["au_push_url"] == "victor@192.168.0.130:C:/Foundry/studio-slot/AnimalUprising.git"
    assert s["foundry_push_url"] == "victor@192.168.0.130:C:/Foundry/studio-slot/foundry.git"
    assert s["foundry_host_dir"] == "C:/Foundry/studio-slot/foundry"
    # a Linux slot without a container is still left out, as before
    assert SL.configured(prof({"slot-x": {"host": "studio@h"}})) == {}


def test_commands_run_on_the_windows_host_itself():
    argv = SL.exec_argv({**WIN, "id": "slot-w"}, ["df", "-P", "-k", "C:/Foundry"])
    assert "docker" not in argv and argv[-2] == "victor@192.168.0.130"
    assert argv[-1] == "df -P -k C:/Foundry"
    linux = SL.exec_argv({"host": "studio@h", "container": "studio-slot",
                          "ssh": ["ssh"]}, ["df"])
    assert linux[-1] == "docker exec studio-slot df"                  # unchanged


def test_windows_lane_paths():
    assert lane_root(WIN) == "C:/Foundry/lanes"
    assert lane_root({**WIN, "lanes_root": "D:\\Lanes\\"}) == "D:/Lanes"
    assert lane_root({"lanes": True}) == "/srv/foundry-lanes"
    assert F.store_url(WIN, "pilot") == "victor@192.168.0.130:C:/Foundry/lanes/pilot.git"
    assert F.worktree_path(WIN, "pilot", "l1") == "C:/Foundry/lanes/pilot/l1"


def test_the_lane_secrets_are_read_on_the_windows_host():
    argv = T.secrets_argv(WIN)
    assert argv[-2] == "victor@192.168.0.130"
    assert argv[-1] == "cat C:/Foundry/secrets/lane/lane.json"


def test_the_settings_know_os_and_studio():
    from foundry_automation.schema import SLOT_FIELDS, SettingsError, slots_table
    fields = {k: v for k, _t, v, *_ in SLOT_FIELDS}
    assert fields["os"] == SL.DEFAULTS["os"] == "linux"
    assert fields["studio"] == SL.DEFAULTS["studio"] == "container"
    slots_table("p", {"slot-w": {"os": "windows", "studio": "native", "host": "u@h"}})
    with pytest.raises(SettingsError, match="linux or windows"):
        slots_table("p", {"slot-w": {"os": "macos", "container": "c"}})
    with pytest.raises(SettingsError, match="container or native"):
        slots_table("p", {"slot-w": {"studio": "vm", "container": "c"}})
    with pytest.raises(SettingsError, match="container is required"):
        slots_table("p", {"slot-l": {"host": "u@h"}})                   # Linux needs one


def test_the_windows_installer_makes_bash_the_ssh_shell():
    text = (ROOT / "install" / "slot.ps1").read_text(encoding="utf-8")
    assert "DefaultShell" in text and "Git\\bin\\bash.exe" in text
    assert "studio-slot\\AnimalUprising.git" in text and 'secrets_file = $(Q (Join-Path $laneDir' \
        in text
    assert 'listen = "127.0.0.1"' in text                              # never the LAN
    for ln in text.splitlines():
        if "Write-Host" in ln or ln.strip().startswith("Ok ") or ln.strip().startswith("Warn "):
            assert "$en.key" not in ln and "$vals" not in ln and "$k " not in ln, ln


@pytest.mark.skipif(not shutil.which("pwsh") and not shutil.which("powershell"),
                    reason="no PowerShell here")
def test_the_windows_installer_parses():
    exe = shutil.which("pwsh") or shutil.which("powershell")
    path = str(ROOT / "install" / "slot.ps1").replace("'", "''")
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                        "$e=$null; [void][System.Management.Automation.Language.Parser]::"
                        f"ParseFile('{path}', [ref]$null, [ref]$e); if ($e) {{ $e | % {{ $_.Message }}; exit 1 }}"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr

"""The junction installers (Distributed Foundry phase e): the container (install/junction/), the
Linux installer it reuses (install/workstation.sh --tools-only) and the Windows one
(install/workstation.ps1). Text checks and syntax checks: the builds run on a VM."""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
J = ROOT / "install" / "junction"


def text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def arg(df: str, name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)", df, re.M)
    return m.group(1) if m else ""


def test_every_machine_pins_the_same_coding_clients():
    """Kilo, OpenCode and PowerShell at one version on slots and junctions (drift guard)."""
    df = text(ROOT / "contrib" / "studio-slot" / "Dockerfile")
    ws = text(ROOT / "install" / "workstation.sh")
    for name in ("KILO_VERSION", "OPENCODE_VERSION", "PWSH_VERSION", "PWSH_SHA256"):
        assert re.search(rf"^{name}={re.escape(arg(df, name))}\b", ws, re.M), name


def test_the_image_installs_the_tools_without_a_router():
    df = text(J / "Dockerfile")
    assert "workstation.sh --tools-only" in df and "kilo --version && opencode --version" in df
    ws = text(ROOT / "install" / "workstation.sh")
    assert "--tools-only) TOOLS_ONLY=1" in ws
    assert 'if [ "$TOOLS_ONLY" = 0 ]; then          # --tools-only' in ws
    # it stops before enrolment
    assert ws.index('if [ "$TOOLS_ONLY" = 1 ]; then') < ws.index('bold "2/6 enrolling')


def test_the_junction_keeps_its_ports_and_settings_on_this_host():
    dc = text(J / "docker-compose.yml")
    ports = re.findall(r'^\s*- "([^"]+:\d+)"\s*$', dc, re.M)
    assert ports and all(p.startswith("127.0.0.1:") or p.startswith("${") for p in ports), ports
    assert "junction-data:/srv/foundry" in dc and "/var/run/docker.sock" in dc
    assert 'profiles: ["studio"]' in dc                 # its Studio is opt-in
    ep = text(J / "junction-entrypoint.sh")
    assert "--secret-store file" in ep and "--no-start" in ep and "persist_home" in ep
    for cmd in ("install)", "run)", "doctor)", "shell)"):
        assert cmd in ep


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
@pytest.mark.parametrize("script", ["install/workstation.sh", "install/junction/junction-entrypoint.sh"])
def test_the_scripts_parse(script):
    r = subprocess.run([_gnu_bash(), "-n", str(ROOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which("pwsh") and not shutil.which("powershell"),
                    reason="no PowerShell here")
@pytest.mark.parametrize("script", ["install/workstation.ps1", "install/slot.ps1"])
def test_the_windows_installers_parse(script):
    exe = shutil.which("pwsh") or shutil.which("powershell")
    path = str(ROOT / script).replace("'", "''")
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                        "$e=$null; [void][System.Management.Automation.Language.Parser]::"
                        f"ParseFile('{path}', [ref]$null, [ref]$e); if ($e) {{ $e | % {{ $_.Message }}; exit 1 }}"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_windows_junction_never_prints_its_keys():
    t = text(ROOT / "install" / "workstation.ps1")
    assert "Remove-Item -Force $enrollFile" in t and "icacls $enrollFile" in t
    for ln in t.splitlines():
        if "Write-Host" in ln or ln.strip().startswith(("Ok ", "Warn ")):
            assert ".keys" not in ln and "$raw" not in ln, ln

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


def pins() -> dict:
    out = {}
    for ln in text(ROOT / "install" / "pins.env").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", ln)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_one_pins_file_and_every_copy_agrees():
    """install/pins.env is the one list; the Dockerfile's ARG defaults, workstation.sh's lines,
    the PowerShell installers' $Pins tables and CI's Node version are copies of it."""
    p = pins()
    assert {"NODE_VERSION", "KILO_VERSION", "KILO_INTEGRITY", "OPENCODE_VERSION",
            "OPENCODE_INTEGRITY", "PWSH_VERSION", "PWSH_SHA256", "ROKIT_VERSION", "ROKIT_SHA256",
            "BLENDER_VERSION", "BLENDER_SHA256", "PYTHON_WINDOWS_VERSION"} <= set(p)
    assert all(re.fullmatch(r"[0-9a-f]{64}", p[k]) for k in ("PWSH_SHA256", "ROKIT_SHA256",
                                                               "BLENDER_SHA256"))
    df = text(ROOT / "contrib" / "studio-slot" / "Dockerfile")
    for name in ("NODE_VERSION", "KILO_VERSION", "KILO_INTEGRITY", "OPENCODE_VERSION",
                 "OPENCODE_INTEGRITY", "PWSH_VERSION", "PWSH_SHA256", "ROKIT_VERSION",
                 "ROKIT_SHA256", "BLENDER_VERSION", "BLENDER_SHA256"):
        assert arg(df, name) == p[name], f"Dockerfile ARG {name}"
    ws = text(ROOT / "install" / "workstation.sh")
    for name in ("NODE_VERSION", "KILO_VERSION", "KILO_INTEGRITY", "OPENCODE_VERSION",
                 "OPENCODE_INTEGRITY", "PWSH_VERSION", "PWSH_SHA256", "ROKIT_VERSION",
                 "ROKIT_SHA256", "BLENDER_VERSION", "BLENDER_SHA256"):
        assert re.search(rf"^{name}={re.escape(p[name])}(\s|$)", ws, re.M), f"workstation.sh {name}"
    assert '. "$PINS"' in ws                                  # and it reads the file beside it
    for ps in ("workstation.ps1", "slot.ps1"):
        t = text(ROOT / "install" / ps)
        table = re.findall(r'^\s+([A-Z0-9_]+) = "([^"]*)"$', t, re.M)
        assert table, ps
        for k, v in table:
            assert p.get(k) == v, f"{ps} {k}"
        assert 'Read-Pins (Join-Path $PSScriptRoot "pins.env") $Pins' in t
        assert 'Read-Pins (Join-Path $app "install\\pins.env") $Pins' in t
    ci = text(ROOT / ".github" / "workflows" / "platform.yml")
    assert f'node-version: "{p["NODE_VERSION"]}"' in ci
    assert "pins=install/pins.env" in ci
    jd = text(J / "Dockerfile")
    assert "COPY install/workstation.sh install/pins.env /opt/foundry-install/" in jd


def test_npm_installs_check_the_registry_integrity_everywhere():
    df = text(ROOT / "contrib" / "studio-slot" / "Dockerfile")
    assert 'dist.integrity)" = "${KILO_INTEGRITY}"' in df
    ws = text(ROOT / "install" / "workstation.sh")
    assert 'npm view "$1@$2" dist.integrity' in ws
    assert 'npm_pinned "@kilocode/cli" "$KILO_VERSION" "$KILO_INTEGRITY"' in ws
    assert 'npm_pinned "@opencode/cli" "$OPENCODE_VERSION" "$OPENCODE_INTEGRITY"' in ws
    assert 'npm install -g --silent "@kilocode' not in ws
    for ps in ("workstation.ps1", "slot.ps1"):
        t = text(ROOT / "install" / ps)
        assert '& npm view "$($p[0])@$($p[1])" dist.integrity' in t, ps
        assert "does not have the pinned integrity" in t, ps


def test_winget_pins_a_version_where_it_takes_one():
    for ps in ("workstation.ps1", "slot.ps1"):
        t = text(ROOT / "install" / ps)
        assert 'if ($w[1]) { $wargs += @("--version", $w[1]) }' in t
        assert '@("OpenJS.NodeJS.LTS", $Pins.NODE_VERSION)' in t
        assert '@("Python.Python.3.12", $Pins.PYTHON_WINDOWS_VERSION)' in t
    assert '@("Microsoft.PowerShell", $Pins.PWSH_VERSION)' in text(ROOT / "install" / "workstation.ps1")


def test_workstation_sh_repairs_without_a_code_and_restarts_after_new_keys():
    ws = text(ROOT / "install" / "workstation.sh")
    assert "--repair) MODE=repair" in ws and "--reenrol|--reenroll) MODE=enrol" in ws
    assert "workstation fetch --check" in ws and 'workstation fetch --out "$ENROLL"' in ws
    assert 'if [ "$WAS_ACTIVE" = 1 ] && [ "$KEYS_CHANGED" = 1 ]; then' in ws
    assert "systemctl --user restart foundry-client.service" in ws
    # a running coordinator's checkout moves the client's own way, never by a forced checkout
    i_update = ws.index("workstation update --tag")
    i_force = ws.index('checkout -q --force "v$VERSION"')
    assert ws.index("elif client_active; then") < i_update < i_force
    # the version self-match keeps its download private
    assert "NEWDIR=$(mktemp -d /tmp/workstation.XXXXXX)" in ws


def test_workstation_ps1_matches_the_router_and_checks_what_it_runs():
    t = text(ROOT / "install" / "workstation.ps1")
    for flag in ("[string]$Fingerprint", "[string]$Import", "[switch]$AtBoot", "[switch]$Repair",
                 "[switch]$Matched"):
        assert flag in t, flag
    assert "/install/workstation.ps1" in t                                  # the self-match
    assert "(Get-FileHash $new).Hash -ne (Get-FileHash $PSCommandPath).Hash" in t
    assert "apply-kilo-config --no-reload" in t and "install-autostart --at-boot" in t
    assert "workstation import $Import --root $Root --passphrase-stdin" in t
    assert "rokit install --no-trust-check" in t
    assert "workstation restart" in t and "workstation update --tag" in t
    # the docstring no longer claims a rokit download
    head = t[:t.index("[CmdletBinding()]")]
    assert "rokit is NOT downloaded here" in head


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
    for cmd in ("install)", "run)", "doctor)", "shell)", "studio-enrol)"):
        assert cmd in ep


def test_the_junction_install_ends_with_the_role_rules_and_doctor():
    ep = text(J / "junction-entrypoint.sh")
    block = ep[ep.index("    install)"):ep.index("    studio-enrol)")]
    assert block.index("workstation.sh") < block.index("kilo-config") < block.index("doctor --platform")


def test_studio_main_enrols_with_a_code_and_assumes_no_router():
    dc = text(J / "docker-compose.yml")
    assert 'FOUNDRY_URL: "${FOUNDRY_URL:-}"' in dc
    assert 'FOUNDRY_URL_FILE: "/srv/foundry/studio-main/router.txt"' in dc
    assert 'FOUNDRY_SLOT_KEY_FILE: "/srv/foundry/studio-main/slot-key.txt"' in dc
    assert "studio-main-key" not in dc                   # no hand-copied key any more
    ep = text(J / "junction-entrypoint.sh")
    assert '"/v1/foundry/enroll"' in ep and 'data.get("slot") != "studio-main"' in ep
    assert 'private("slot-key.txt", data["key"])' in ep and "os.chown(p, 1000, 1000)" in ep
    # the slot image reads the router address from that file when FOUNDRY_URL is empty
    assert 'os.environ.get(name + "_FILE")' in text(ROOT / "contrib" / "studio-slot" / "slot-secrets.py")


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
    # the answer lives in a private folder (this user and SYSTEM) that is removed at the end
    assert '$enrollFile = Join-Path $work "enroll.json"' in t
    assert "icacls $work /inheritance:r" in t and "Remove-Item -Recurse -Force $work" in t
    for ln in t.splitlines():
        if "Write-Host" in ln or ln.strip().startswith(("Ok ", "Warn ")):
            assert not re.search(r"\.keys\b(?!_)", ln) and "$raw" not in ln, ln

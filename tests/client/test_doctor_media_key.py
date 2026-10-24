"""Doctor: where the media adapter's FOUNDRY_KEY comes from. A launcher script that loads the key
itself (start_media_adapter.ps1 reads it from a DPAPI file) must not show as a blocking red."""
import shutil
import sys

import pytest

from foundry_client import doctor, secrets

SECRET = "sk-test-do-not-show"


@pytest.fixture()
def no_key(monkeypatch):
    monkeypatch.delenv("FOUNDRY_KEY", raising=False)
    monkeypatch.setattr(doctor, "_user_env_has_key", lambda: False)
    monkeypatch.setattr(secrets, "status", lambda names: {n: False for n in names})


def _cfg(kilo_secrets=()):
    return {"components": {"kilo": {"secrets": list(kilo_secrets)}}}


def _entry(argv, env=None):
    e = {"type": "local", "command": list(argv)}
    if env:
        e["environment"] = env
    return {"foundry-media": e}


def _launcher(tmp_path, exit_code, loads=True, check=True):
    body = ["import os, sys"]
    if loads:
        body.append(f'os.environ["FOUNDRY_KEY"] = "{SECRET}"')
    if check:
        body += ["if '--check' in sys.argv:",
                 "    print('key: ' + os.environ.get('FOUNDRY_KEY', ''))",
                 f"    sys.exit({exit_code})"]
    p = tmp_path / "start_media_adapter.py"
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return [sys.executable, str(p)]


def test_key_in_entry_environment_is_ok(tmp_path, no_key):
    d = doctor._adapter_check(_entry(_launcher(tmp_path, 1, loads=False, check=False),
                                     {"FOUNDRY_KEY": "k"}), _cfg())
    assert d["status"] == "ok"


def test_key_from_kilo_secret_is_ok(tmp_path, no_key, monkeypatch):
    monkeypatch.setattr(secrets, "status", lambda names: {n: True for n in names})
    d = doctor._adapter_check(_entry(_launcher(tmp_path, 1, loads=False, check=False)),
                              _cfg(["FOUNDRY_KEY"]))
    assert d["status"] == "ok"


def test_self_loading_launcher_whose_check_passes_is_ok(tmp_path, no_key):
    d = doctor._adapter_check(_entry(_launcher(tmp_path, 0)), _cfg())
    assert d["status"] == "ok"
    assert "loads FOUNDRY_KEY itself" in d["detail"] and "--check passed" in d["detail"]
    assert SECRET not in d["detail"]


def test_self_loading_launcher_whose_check_fails_is_red(tmp_path, no_key):
    d = doctor._adapter_check(_entry(_launcher(tmp_path, 3)), _cfg())
    assert d["status"] == "fail"
    assert "--check failed (exit 3)" in d["detail"] and SECRET not in d["detail"]


def test_self_loading_launcher_without_a_check_mode_warns(tmp_path, no_key):
    d = doctor._adapter_check(_entry(_launcher(tmp_path, 0, check=False)), _cfg())
    assert d["status"] == "warn" and "no check mode" in d["detail"]


def test_nothing_provides_the_key_is_red(tmp_path, no_key):
    d = doctor._adapter_check(_entry(_launcher(tmp_path, 0, loads=False, check=False)), _cfg())
    assert d["status"] == "fail" and "FOUNDRY_KEY is not available" in d["detail"]


def test_launcher_check_that_times_out_warns(tmp_path, no_key, monkeypatch):
    monkeypatch.setattr(doctor, "LAUNCHER_CHECK_TIMEOUT", 1)
    p = tmp_path / "start_media_adapter.py"
    p.write_text("import time, sys\nif '--check' in sys.argv:\n    time.sleep(10)\n",
                 encoding="utf-8")
    d = doctor._adapter_check(_entry([sys.executable, str(p)]), _cfg())
    assert d["status"] == "warn" and "did not finish" in d["detail"]


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="PowerShell launcher")
def test_powershell_launcher_gets_dash_check(tmp_path, no_key):
    p = tmp_path / "start_media_adapter.ps1"
    p.write_text("param([switch]$Check)\n"
                 f"$env:FOUNDRY_KEY = '{SECRET}'\n"
                 "if ($Check) { Write-Output \"key: $env:FOUNDRY_KEY\"; exit 0 }\n"
                 "exit 9\n", encoding="utf-8")
    argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(p)]
    d = doctor._adapter_check(_entry(argv), _cfg())
    assert d["status"] == "ok" and "-Check passed" in d["detail"] and SECRET not in d["detail"]

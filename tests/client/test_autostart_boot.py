"""install-autostart --at-boot (self-driving plan part 4): a Task Scheduler task "at
startup, run whether the user is logged on or not", created with /RP * so Windows asks
for the password itself. Every schtasks call is mocked: nothing is registered here."""
import subprocess
import sys

import pytest

from foundry_client import __main__ as cli
from foundry_client import autostart, config, doctor


class FakeSchtasks:
    def __init__(self, create_rc=0):
        self.calls = []
        self.kwargs = []
        self.installed = False
        self.create_rc = create_rc
        self.xml = ""

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kwargs.append(kw)
        verb = argv[1]
        rc = 0
        if verb == "/Create":
            self.xml = open(argv[argv.index("/XML") + 1], encoding="utf-16").read()
            rc = self.create_rc
            self.installed = rc == 0
        elif verb == "/Query":
            rc = 0 if self.installed else 1
        elif verb == "/Delete":
            rc = 0 if self.installed else 1
            self.installed = False
        return subprocess.CompletedProcess(argv, rc, b"", b"")


@pytest.fixture()
def win(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("USERNAME", "victor")
    monkeypatch.setenv("USERDOMAIN", "VICTOR-WS")
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    fake = FakeSchtasks()
    monkeypatch.setattr(autostart, "_RUN", fake)
    return fake


def test_boot_task_xml_runs_at_startup_without_logon_and_without_a_password(win, tmp_path):
    r = autostart.enable_boot(xml_dir=tmp_path / "x")
    assert r == {"ok": True, "task": "FoundryClient-Boot", "user": "VICTOR-WS\\victor",
                 "returncode": 0}
    create = win.calls[0]
    assert create[:4] == ["schtasks", "/Create", "/TN", "FoundryClient-Boot"]
    # the password is asked by schtasks itself; nothing captured, nothing passed
    assert create[create.index("/RU") + 1] == "VICTOR-WS\\victor"
    assert create[create.index("/RP") + 1] == "*" and "/F" in create
    assert "capture_output" not in win.kwargs[0] and "input" not in win.kwargs[0]
    x = win.xml
    assert "<BootTrigger>" in x and "<LogonType>Password</LogonType>" in x
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in x      # never killed after 72 h
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in x
    assert "-m foundry_client run --headless" in x
    exe, args, root = autostart.boot_command()
    assert f"<WorkingDirectory>{root}</WorkingDirectory>" in x
    assert not list((tmp_path / "x").iterdir())                       # the XML is removed


def test_boot_replaces_the_logon_entry_and_mode_reports_it(win):
    autostart.enable()
    assert autostart.mode() == "logon"
    autostart.enable_boot()
    assert autostart.mode() == "boot" and not autostart.is_enabled()
    assert doctor.autostart_check()["status"] == "ok"


def test_failed_create_keeps_the_logon_entry(win):
    win.create_rc = 1
    autostart.enable()
    r = autostart.enable_boot()
    assert not r["ok"] and autostart.is_enabled() and autostart.mode() == "logon"
    assert doctor.autostart_check()["status"] == "warn"


def test_remove_autostart_removes_both(win, capsys, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: config.DEFAULTS)
    autostart.enable_boot()
    autostart.enable()
    assert autostart.mode() == "boot+logon"
    assert cli.main(["remove-autostart"]) == 0
    assert "boot task" in capsys.readouterr().out
    assert autostart.mode() == "off"
    assert ["schtasks", "/Delete", "/TN", "FoundryClient-Boot", "/F"] in win.calls
    assert doctor.autostart_check()["status"] == "warn"


def test_cli_at_boot_and_at_logon(win, capsys, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: config.DEFAULTS)
    assert cli.main(["install-autostart", "--at-boot", "--user", "WS\\svc"]) == 0
    out = capsys.readouterr().out
    assert "asks for the password of WS\\svc" in out and "at boot" in out
    create = next(c for c in win.calls if c[1] == "/Create")
    assert create[create.index("/RU") + 1] == "WS\\svc"
    assert cli.main(["install-autostart", "--at-logon"]) == 0
    assert autostart.is_enabled()
    assert cli.main(["install-autostart"]) == 0                       # default: at sign-in
    win.create_rc = 1
    assert cli.main(["install-autostart", "--at-boot"]) == 1
    assert "Run as administrator" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["install-autostart", "--at-boot", "--at-logon"])


def test_at_boot_refused_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(autostart, "_RUN", lambda argv, **kw: calls.append(argv))
    with pytest.raises(RuntimeError):
        autostart.enable_boot()
    assert autostart.boot_enabled() is False and calls == []


def test_status_prints_the_autostart_mode(win, capsys, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: config.DEFAULTS)
    autostart.enable_boot()
    cli.main(["status"])                       # the client is not running in tests
    assert "Autostart" in capsys.readouterr().out.splitlines()[0]

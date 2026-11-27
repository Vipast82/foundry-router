"""foundry_platform (docs/DISTRIBUTED_FOUNDRY.md §7.1, phase a): the one OS layer.

These run on both CI jobs (.github/workflows/platform.yml: ubuntu + windows)."""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

import foundry_platform as P

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_constants_match_the_copies_they_replace():
    # every module had `0x08000000 if os.name == "nt" else 0`
    assert P.NO_WINDOW == (0x08000000 if os.name == "nt" else 0)
    assert P.CREATE_NO_WINDOW == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


@pytest.mark.parametrize("plat,kind", [("win32", "windows"), ("linux", "linux"),
                                       ("darwin", "macos"), ("freebsd14", "freebsd14")])
def test_predicates_read_sys_platform_at_call_time(monkeypatch, plat, kind):
    monkeypatch.setattr(sys, "platform", plat)
    assert P.os_kind() == kind
    assert P.is_windows() is (plat == "win32")
    assert P.is_linux() is (plat == "linux")


def test_a_modules_own_sys_reference_wins():
    """foundry_client/autostart.py passes its own `sys`; its tests swap that for a stand-in."""
    fake_win = type("S", (), {"platform": "win32"})()
    fake_lin = type("S", (), {"platform": "linux"})()
    assert P.is_windows(fake_win) is True and P.is_windows(fake_lin) is False
    assert P.is_linux(fake_lin) is True and P.is_linux(fake_win) is False
    assert P.is_windows() is (sys.platform == "win32")


def test_windows_answers(monkeypatch):
    # shutil.which itself takes its Windows path (_winapi) when sys.platform says win32, which
    # cannot run on Linux: the PowerShell lookup is stubbed here (pwsh found on PATH)
    monkeypatch.setattr(P.shutil, "which", lambda n: "/x/pwsh" if n == "pwsh" else None)
    monkeypatch.setattr(sys, "platform", "win32")
    assert P.no_window_flags() == P.CREATE_NO_WINDOW
    assert P.no_window_kw() == {"creationflags": P.CREATE_NO_WINDOW}
    assert P.detached_kw() == {"creationflags": 0x00000008 | 0x00000200 | P.CREATE_NO_WINDOW}
    assert P.script_ext() == ".cmd"
    assert P.service_kind() == "scheduled-task"
    assert P.default_root() == "C:\\Foundry"
    argv = P.job_shell("echo hi")
    assert argv[0] == "/x/pwsh"
    assert argv[-2:] == ["-Command", "echo hi"] and "-NoProfile" in argv


def test_posix_answers(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert P.no_window_flags() == 0
    assert P.no_window_kw() == {}
    assert P.detached_kw() == {"start_new_session": True}
    assert P.script_ext() == ".sh"
    assert P.service_kind() == "systemd-user"
    assert P.default_root() == "/srv/foundry"
    assert P.job_shell("echo hi") == ["bash", "-lc", "echo hi"]
    assert P.job_shell("echo hi", login=False) == ["bash", "-c", "echo hi"]


def test_job_shell_really_runs_here():
    argv = P.job_shell("echo foundry-ok")
    if not __import__("shutil").which(argv[0]):
        pytest.skip(f"{argv[0]} is not installed here")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60, **P.no_window_kw())
    assert r.returncode == 0 and "foundry-ok" in r.stdout


def test_text_is_utf8_both_ways(tmp_path):
    p = str(tmp_path / "t.txt")
    text = "héllo → 世界\r\nline 2\n"
    P.write_text(p, text)
    assert P.read_text(p) == text
    assert open(p, "rb").read() == text.encode("utf-8")      # newlines kept as given
    assert not [n for n in os.listdir(tmp_path) if ".tmp" in n]


def test_file_lock_is_exclusive_across_processes(tmp_path):
    lock = str(tmp_path / "sub" / "x.lock")
    child = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(ROOT)!r})
        import foundry_platform as P
        try:
            with P.file_lock({lock!r}, timeout=0):
                print("GOT")
        except P.LockTimeout:
            print("BUSY")
    """)

    def other() -> str:
        return subprocess.run([sys.executable, "-c", child], capture_output=True, text=True,
                              timeout=60).stdout.strip()

    with P.file_lock(lock, timeout=5):
        assert other() == "BUSY"
    assert other() == "GOT"                                     # released after the block


def test_file_lock_times_out(tmp_path):
    lock = str(tmp_path / "y.lock")
    child = textwrap.dedent(f"""
        import sys, time; sys.path.insert(0, {str(ROOT)!r})
        import foundry_platform as P
        with P.file_lock({lock!r}):
            print("HELD", flush=True)
            time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "HELD"
        with pytest.raises(P.LockTimeout):
            with P.file_lock(lock, timeout=0.3):
                pass
    finally:
        p.kill()
        p.wait(timeout=30)
    with P.file_lock(lock, timeout=10):                         # dies with its process
        pass


def test_facts_name_no_secrets():
    f = P.facts()
    assert f["os"] == P.os_kind() and f["python"] and f["service"] in ("scheduled-task",
                                                                        "systemd-user")
    assert set(f) == {"os", "release", "machine", "python", "encoding", "preferred_encoding",
                      "service", "root", "job_shell"}


# ---------------------------------------------------------------------------- the guard

# OS questions in the coordinator and the client go through foundry_platform. A value that is
# only RECORDED (not branched on) is allowed by file:line-free name here.
ALLOWED = {("foundry_client/move.py", "sys.platform")}


def _os_checks(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            name = f"{node.value.id}.{node.attr}"
            if name in ("os.name", "sys.platform"):
                out.append((node.lineno, name))
        if isinstance(node, ast.Constant) and node.value == 0x08000000:
            out.append((node.lineno, "0x08000000"))
    return out


def test_no_os_checks_outside_the_platform_layer():
    bad = []
    for pkg in ("foundry_coordinator", "foundry_client"):
        for p in sorted((ROOT / pkg).rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            for line, name in _os_checks(p):
                if (rel, name) not in ALLOWED:
                    bad.append(f"{rel}:{line} {name}")
    assert not bad, ("use foundry_platform (is_windows(), NO_WINDOW, ...) instead of:\n"
                     + "\n".join(bad))

"""`foundry-client doctor --platform` (docs/DISTRIBUTED_FOUNDRY.md §9, phase a): this machine's
OS and tools in plain words, without a running client."""

from __future__ import annotations

import json

from foundry_client import doctor as D


def _which(have):
    import os

    def which(n):
        # job_shell may hand back a full path (pwsh on Windows): match by bare name
        base = os.path.splitext(os.path.basename(str(n)))[0].lower()
        return f"/bin/{base}" if base in have else None
    return which


def _by_id(d):
    return {c["id"]: c for c in d["checks"]}


ALL = {"bash", "pwsh", "git", "ssh", "node", "kilo", "opencode", "rokit", "lune"}


def test_everything_present_is_ok():
    d = D.platform_checks(which=_which(ALL), version=lambda exe: "v1")
    c = _by_id(d)
    assert d["ok"] is True
    assert c["git"]["status"] == "ok" and "v1" in c["git"]["detail"]
    assert c["os"]["status"] == "info" and c["locks"]["status"] == "ok"


def test_a_needed_tool_missing_fails_in_plain_words():
    d = D.platform_checks(which=_which(ALL - {"git"}), version=lambda exe: "v1")
    c = _by_id(d)
    assert d["ok"] is False
    assert c["git"]["status"] == "fail"
    assert c["git"]["detail"] == ("git was not found on the PATH; it is needed for every "
                                  "checkout, worktree and hand-back")


def test_an_optional_client_missing_only_warns():
    d = D.platform_checks(which=_which(ALL - {"opencode"}), version=lambda exe: "v1")
    c = _by_id(d)
    assert d["ok"] is True
    assert c["opencode"]["status"] == "warn" and "OpenCode sessions" in c["opencode"]["detail"]


def test_no_job_shell_fails(monkeypatch):
    monkeypatch.setattr(D._plat, "job_shell", lambda s, login=True: ["nosuchshell", "-c", s])
    d = D.platform_checks(which=_which(ALL), version=lambda exe: "v1")
    assert _by_id(d)["shell"]["status"] == "fail" and d["ok"] is False


def test_the_cli_entry_point(monkeypatch, tmp_path, capsys):
    """The command itself, not only what it computes (memory note test-the-entry-point)."""
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    from foundry_client import __main__ as M

    def no_config():
        raise AssertionError("doctor --platform must not need the client's config")
    # a fresh slot has no client config or secret store yet
    from foundry_client import config as CFG
    monkeypatch.setattr(CFG, "load", no_config)
    rc = M.main(["doctor", "--platform", "--json"])
    d = json.loads(capsys.readouterr().out)
    assert rc == (0 if d["ok"] else 1)
    assert {"os", "shell", "git", "locks"} <= set(_by_id(d))
    rc = M.main(["doctor", "--platform"])
    out = capsys.readouterr().out
    assert ("PLATFORM OK" in out) or ("PLATFORM NOT READY" in out)

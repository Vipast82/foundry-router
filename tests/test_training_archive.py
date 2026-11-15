"""The training archive (v0.181.147): one more copy of the fine-tuning data in a
folder the router can write to (a second disk, a mounted network share)."""

import json
import sqlite3
import time
from types import SimpleNamespace

from foundry_router import training_archive as ta
from foundry_router.capture import Capture
from foundry_router.db import Database


def _svc(tmp_path, archive="", **cfg_extra):
    cfg = SimpleNamespace(store_path=str(tmp_path / "data" / "training" / "capture.sqlite"), enabled=True,
                          projects=[], exclude_projects=[], include_unbound=False, retention_days=30,
                          backends=[], exclude_backends=[], personas=[], exclude_personas=[],
                          max_body_bytes=8_000_000, archive_dir=archive, archive_capture=True,
                          archive_datasets=True, archive_hours=24)
    for k, v in cfg_extra.items():
        setattr(cfg, k, v)
    db = Database(tmp_path / "main.sqlite")
    cap = Capture(lambda: cfg, tmp_path / "data")
    return SimpleNamespace(config_store=SimpleNamespace(config=SimpleNamespace(training_capture=cfg)),
                           db=db, capture=cap), cfg


def _fill(svc, tmp_path, n=5):
    body = json.dumps({"request": {"messages": [{"role": "user", "content": "x" * 900}]}})
    for i in range(n):
        svc.capture.store.insert({"record_uid": f"u{i}", "ts": time.time(), "kind": "backend_call",
                                  "project_id": "p", "body": body, "bytes": len(body), "truncated": 0,
                                  "redaction": "{}", "purpose": "agent_turn"})
    ds = tmp_path / "data" / "training" / "datasets" / "p" / "all-sessions"
    ds.mkdir(parents=True)
    (ds / "train.jsonl").write_text('{"messages": []}\n')
    (ds / "manifest.json").write_text("{}")
    media = tmp_path / "data" / "training" / "capture-media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "abc.png").write_bytes(b"png")


def test_off_without_a_folder(tmp_path):
    svc, _ = _svc(tmp_path)
    s = ta.status(svc)
    assert s["dir"] == "" and "exists" not in s and ta.due(svc) is False
    out = ta.run(svc)
    assert out["ok"] is False and out["errors"] == ["no archive destination is set"]


def test_a_copy_of_everything_lands_in_the_folder(tmp_path):
    arc = tmp_path / "share"
    arc.mkdir()
    svc, _ = _svc(tmp_path, str(arc))
    _fill(svc, tmp_path)
    assert ta.due(svc) is True                                        # never copied yet
    out = ta.run(svc)
    assert out["ok"] and out["errors"] == []
    assert out["datasets"]["files"] == 2 and out["capture"]["bytes"] > 0 and out["capture"]["media_files"] == 1
    assert (arc / "datasets" / "p" / "all-sessions" / "train.jsonl").read_text() == '{"messages": []}\n'
    assert (arc / "capture" / "capture-media" / "abc.png").read_bytes() == b"png"
    assert not (arc / "capture" / "capture.sqlite.part").exists()     # renamed when complete
    copy = sqlite3.connect(arc / "capture" / "capture.sqlite")
    assert copy.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0] == 5
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    info = json.loads((arc / "ARCHIVE.json").read_text())
    assert info["capture"]["bytes"] == out["capture"]["bytes"] and info["finished"] >= info["started"]
    s = ta.status(svc)
    assert s["exists"] and s["writable"] and s["free_bytes"] > 0 and s["last"]["ok"] is True
    assert ta.due(svc) is False                                       # copied a moment ago
    assert ta.due(svc, now=time.time() + 25 * 3600) is True           # a day later
    # the second pass copies only what changed, and the copy is replaced whole
    svc.capture.store.insert({"record_uid": "new", "ts": time.time(), "kind": "received",
                              "project_id": "p", "body": "{}", "bytes": 2, "truncated": 0,
                              "redaction": "{}", "purpose": "agent_turn"})
    out = ta.run(svc)
    assert out["ok"] and out["datasets"]["files"] == 0 and out["capture"]["media_files"] == 0
    copy = sqlite3.connect(arc / "capture" / "capture.sqlite")
    assert copy.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0] == 6
    copy.close()


def test_each_kind_has_its_switch_and_zero_hours_means_on_demand(tmp_path):
    arc = tmp_path / "share"
    arc.mkdir()
    svc, cfg = _svc(tmp_path, str(arc), archive_capture=False, archive_hours=0)
    _fill(svc, tmp_path)
    assert ta.due(svc) is False
    out = ta.run(svc)
    assert out["ok"] and "capture" not in out and out["datasets"]["files"] == 2
    assert not (arc / "capture").exists()
    cfg.archive_capture, cfg.archive_datasets = True, False
    out = ta.run(svc)
    assert out["ok"] and "datasets" not in out and (arc / "capture" / "capture.sqlite").exists()


def test_a_missing_or_full_folder_is_reported_and_nothing_is_lost(tmp_path, monkeypatch):
    svc, cfg = _svc(tmp_path, str(tmp_path / "not-mounted"))
    _fill(svc, tmp_path)
    out = ta.run(svc)
    assert out["ok"] is False and "does not exist or is not mounted" in out["errors"][0]
    assert ta.status(svc)["exists"] is False
    arc = tmp_path / "share"
    arc.mkdir()
    cfg.archive_dir = str(arc)
    assert ta.run(svc)["ok"]
    before = (arc / "capture" / "capture.sqlite").read_bytes()
    monkeypatch.setattr(ta.Folder, "free", lambda self: 10)           # the share is full
    out = ta.run(svc)
    assert out["ok"] is False and "GB free on the archive" in out["errors"][0]
    assert (arc / "capture" / "capture.sqlite").read_bytes() == before   # the previous copy is kept
    assert ta.status(svc)["last"]["ok"] is False
    ev = svc.db.query("SELECT message FROM event_log WHERE source='training' ORDER BY id DESC LIMIT 1")
    assert ev and "training archive" in ev[0]["message"]


def test_settings_route_page_and_loop():
    import inspect
    from pathlib import Path

    from foundry_router import main
    from foundry_router.config import TrainingCaptureConfig
    from foundry_router.ui import routes
    c = TrainingCaptureConfig()
    assert (c.archive_dir, c.archive_capture, c.archive_datasets, c.archive_hours) == ("", True, True, 24)
    src = inspect.getsource(routes)
    assert '@router.post("/admin/api/training/archive")' in src
    assert "the archive folder must be a full path" in src and '"archive": await asyncio.to_thread(training_archive.status, svc)' in src
    assert "if training_archive.due(self):" in inspect.getsource(main)
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    for fid in ("arc-dir", "arc-cap", "arc-ds", "arc-hours", "arc-save", "arc-run"):
        assert f'id="{fid}"' in html, fid
    assert 'api("training/archive", {method: "POST", body: {}})' in html


def test_the_container_has_an_archive_mount():
    from pathlib import Path
    compose = (Path(ta.__file__).parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- ${FOUNDRY_ARCHIVE_DIR_HOST:-./archive}:/archive" in compose


# -- an SMB share the router connects to itself (v0.181.150) ------------------------------------

class FakeSmb:
    """smbclient's calls, on a local folder: \\\\server\\share\\... -> <root>/share/..."""

    def __init__(self, root, deny=False):
        self.root, self.deny, self.sessions = root, deny, []

    def _local(self, p):
        parts = [x for x in p.strip("\\").split("\\") if x]
        return self.root.joinpath(*parts[1:])

    def register_session(self, server, username=None, password=None, **kw):
        if self.deny:
            raise PermissionError("STATUS_LOGON_FAILURE")
        self.sessions.append((server, username, password))

    def makedirs(self, p, exist_ok=False):
        self._local(p).mkdir(parents=True, exist_ok=exist_ok)

    def listdir(self, p):
        return [x.name for x in self._local(p).iterdir()]

    def open_file(self, p, mode="rb"):
        return open(self._local(p), mode)

    def replace(self, a, b):
        import os as _os
        _os.replace(self._local(a), self._local(b))

    def remove(self, p):
        self._local(p).unlink()

    def stat(self, p):
        return self._local(p).stat()

    def stat_volume(self, p):
        return SimpleNamespace(caller_available_size=50 * 1024 ** 3)


def _smb_svc(tmp_path, monkeypatch, deny=False, **extra):
    svc, cfg = _svc(tmp_path, archive_type="smb", archive_smb_server="192.168.0.206",
                    archive_smb_share="finetuning", archive_smb_path="foundry/archive",
                    archive_smb_user="foundry", archive_smb_password="pw", **extra)
    fake = FakeSmb(tmp_path / "nas", deny)
    (tmp_path / "nas" / "finetuning").mkdir(parents=True)
    if not hasattr(ta.Smb, "_fake"):                   # wrap once; later calls swap the fake
        real = ta.Smb.__init__
        monkeypatch.setattr(ta.Smb, "__init__",
                            lambda self, *a, **k: real(self, *a, client=ta.Smb._fake, **k))
    monkeypatch.setattr(ta.Smb, "_fake", fake, raising=False)
    return svc, cfg, fake


def test_an_smb_share_gets_the_same_copy_with_nothing_mounted(tmp_path, monkeypatch):
    svc, cfg, fake = _smb_svc(tmp_path, monkeypatch)
    _fill(svc, tmp_path)
    d = ta.destination(svc)
    assert d.kind == "smb" and d.describe() == "\\\\192.168.0.206\\finetuning\\foundry\\archive"
    t = ta.test(svc)
    assert t["ok"] and t["free_bytes"] == 50 * 1024 ** 3 and fake.sessions[-1] == ("192.168.0.206", "foundry", "pw")
    base = tmp_path / "nas" / "finetuning" / "foundry" / "archive"
    assert list(base.iterdir()) == []                                 # the test file was removed
    out = ta.run(svc)
    assert out["ok"], out["errors"]
    assert out["datasets"]["files"] == 2 and out["capture"]["media_files"] == 1
    copy = sqlite3.connect(base / "capture" / "capture.sqlite")
    assert copy.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0] == 5
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    assert (base / "datasets" / "p" / "all-sessions" / "train.jsonl").exists()
    assert json.loads((base / "ARCHIVE.json").read_text())["where"].endswith("archive")
    assert not list(base.rglob("*.part"))                             # every file was renamed into place
    assert not (tmp_path / "data" / "training" / "archive-tmp" / "capture.sqlite").exists()   # temp copy removed
    again = ta.run(svc)
    assert again["ok"] and again["datasets"]["files"] == 0            # unchanged files are not sent again
    s = ta.status(svc)
    assert s["type"] == "smb" and s["on"] and s["smb"] == {
        "server": "192.168.0.206", "share": "finetuning", "path": "foundry/archive", "user": "foundry",
        "has_password": True}
    assert "pw" not in json.dumps(s)                                  # the password is never returned


def test_a_refused_sign_in_or_a_full_router_disk_is_reported(tmp_path, monkeypatch):
    svc, cfg, fake = _smb_svc(tmp_path, monkeypatch, deny=True)
    t = ta.test(svc)
    assert t["ok"] is False and "STATUS_LOGON_FAILURE" in t["error"]
    out = ta.run(svc)
    assert out["ok"] is False and "PermissionError" in out["errors"][0]
    (tmp_path / "b").mkdir()
    svc, cfg, fake = _smb_svc(tmp_path / "b", monkeypatch)
    _fill(svc, tmp_path / "b")
    import shutil as _sh
    monkeypatch.setattr(ta.shutil, "disk_usage", lambda p: _sh._ntuple_diskusage(100, 99, 1))
    out = ta.run(svc)
    assert out["ok"] is False and "has to be copied on the router before it is uploaded" in out["errors"][0]
    cfg.archive_smb_server = ""
    assert ta.destination(svc) is None and ta.due(svc) is False


def test_smb_settings_route_and_page():
    import inspect
    from pathlib import Path

    from foundry_router.config import TrainingCaptureConfig
    from foundry_router.ui import routes
    c = TrainingCaptureConfig()
    assert c.archive_type == "folder" and c.archive_smb_password == ""
    src = inspect.getsource(routes)
    assert 'cfg["training_capture"]["archive_smb_password"] = _mask(' in src      # never shown in the config view
    assert "if isinstance(pw, str) and pw and not _is_masked(pw):" in src         # empty keeps the saved one
    assert 'body.get("test")' in src and "training_archive.test, svc" in src
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    for fid in ("arc-type", "arc-s-server", "arc-s-share", "arc-s-path", "arc-s-user", "arc-s-pw", "arc-test"):
        assert f'id="{fid}"' in html, fid
    assert 'type="password" id="arc-s-pw"' in html and "body: {test: true}" in html
    req = (Path(routes.__file__).parent.parent.parent / "requirements.txt").read_text()
    assert "smbprotocol==" in req
    import smbclient                                                 # the library is installed
    assert hasattr(smbclient, "register_session") and hasattr(smbclient, "stat_volume")


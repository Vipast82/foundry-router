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
    assert out["ok"] is False and out["errors"] == ["no archive folder is set"]


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
    monkeypatch.setattr(ta, "_free", lambda p: 10)                    # the share is full
    out = ta.run(svc)
    assert out["ok"] is False and "GB free on the archive folder" in out["errors"][0]
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

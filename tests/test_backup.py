"""Backup & restore (backup.py): one encrypted file, checked restore, applied at the next start."""

import io
import os
import sqlite3

import pytest

from foundry_router import backup as B

PW = "correct horse battery staple"


def _router_dirs(tmp_path, tag="a"):
    data, config = tmp_path / f"data-{tag}", tmp_path / f"config-{tag}"
    data.mkdir()
    config.mkdir()
    db = sqlite3.connect(str(data / "foundry.sqlite"))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES (?)", (tag,))
    db.commit()
    db.close()
    s = sqlite3.connect(str(data / "secrets.sqlite"))
    s.execute("CREATE TABLE meta (k TEXT, v TEXT)")
    s.commit()
    s.close()
    (data / "tls").mkdir()
    (data / "tls" / "cert.pem").write_text(f"cert-{tag}")
    (data / "media").mkdir()
    (data / "media" / "x.png").write_bytes(b"png-" + tag.encode())
    (config / "config.yaml").write_text(f"tag: {tag}\n")
    (config / "provider-keys.env").write_text(f"MERIDIAN_API_KEY=test-{tag}\n")
    return data, config


def _make(data, config, **kw):
    buf = io.BytesIO()
    m = B.create(buf, PW, data, config, **kw)
    return buf.getvalue(), m


def test_backup_is_encrypted_and_roundtrips(tmp_path):
    data, config = _router_dirs(tmp_path, "a")
    raw, m = _make(data, config)
    names = {f["path"] for f in m["files"]}
    assert names == {"data/foundry.sqlite", "data/secrets.sqlite", "data/tls/cert.pem",
                     "config/config.yaml", "config/provider-keys.env"}
    # nothing readable in the file
    assert b"test-a" not in raw and b"tag: a" not in raw and b"cert-a" not in raw
    assert B.info(io.BytesIO(raw))["router_version"]
    tdata, tconfig = _router_dirs(tmp_path, "b")
    s = B.stage(io.BytesIO(raw), PW, tdata)
    assert s["files"] == 5 and s["has_secret_store"] and s["has_provider_keys"]
    assert B.staged(tdata)["host"]
    # not applied until the start
    assert (tconfig / "config.yaml").read_text() == "tag: b\n"
    B.apply_pending(tdata, tconfig)
    assert (tconfig / "config.yaml").read_text() == "tag: a\n"
    assert (tconfig / "provider-keys.env").read_text() == "MERIDIAN_API_KEY=test-a\n"
    assert (tdata / "tls" / "cert.pem").read_text() == "cert-a"
    v = sqlite3.connect(str(tdata / "foundry.sqlite")).execute("SELECT v FROM t").fetchone()[0]
    assert v == "a"
    # media was not in the backup: the target's own stays
    assert (tdata / "media" / "x.png").read_bytes() == b"png-b"
    # the replaced files are kept once
    prev = tdata / "restore" / "previous"
    assert (prev / "config" / "config.yaml").read_text() == "tag: b\n"
    assert (prev / "data" / "foundry.sqlite").exists()
    assert B.staged(tdata) is None and B.apply_pending(tdata, tconfig) is None


def test_include_files_carries_media(tmp_path):
    data, config = _router_dirs(tmp_path, "a")
    raw, m = _make(data, config, include_files=True)
    assert "data/media/x.png" in {f["path"] for f in m["files"]}
    tdata, tconfig = _router_dirs(tmp_path, "b")
    B.stage(io.BytesIO(raw), PW, tdata)
    B.apply_pending(tdata, tconfig)
    assert (tdata / "media" / "x.png").read_bytes() == b"png-a"


def test_wrong_passphrase_short_passphrase_and_damage(tmp_path):
    data, config = _router_dirs(tmp_path, "a")
    with pytest.raises(B.BackupError) as e:
        B.create(io.BytesIO(), "short", data, config)
    assert e.value.code == "weak_passphrase"
    raw, _ = _make(data, config)
    tdata, _ = _router_dirs(tmp_path, "b")
    with pytest.raises(B.BackupError) as e:
        B.stage(io.BytesIO(raw), PW + "x", tdata)
    assert e.value.code == "wrong_passphrase"
    flipped = bytearray(raw)
    flipped[len(flipped) // 2] ^= 1
    with pytest.raises(B.BackupError) as e:
        B.stage(io.BytesIO(bytes(flipped)), PW, tdata)
    assert e.value.code == "damaged"
    with pytest.raises(B.BackupError) as e:
        B.stage(io.BytesIO(raw[:-20]), PW, tdata)
    assert e.value.code == "damaged"
    with pytest.raises(B.BackupError):
        B.stage(io.BytesIO(raw + b"junk"), PW, tdata)
    with pytest.raises(B.BackupError) as e:
        B.stage(io.BytesIO(b"PK\x03\x04 not a backup"), PW, tdata)
    assert e.value.code == "not_a_backup"
    # nothing was staged by the failures
    assert B.staged(tdata) is None and not (tdata / "restore" / "incoming").exists()


def test_newer_backup_is_refused(tmp_path, monkeypatch):
    data, config = _router_dirs(tmp_path, "a")
    monkeypatch.setattr(B, "_version", lambda: "9.0.0")
    raw, _ = _make(data, config)
    monkeypatch.setattr(B, "_version", lambda: "1.0.0")
    tdata, _ = _router_dirs(tmp_path, "b")
    with pytest.raises(B.BackupError) as e:
        B.stage(io.BytesIO(raw), PW, tdata)
    assert e.value.code == "newer_backup"


def test_unsafe_names_are_refused():
    for bad in ("/etc/passwd", "data/../../x", "config/other.yaml", "data/restore/x", "x.txt"):
        with pytest.raises(B.BackupError):
            B._safe_name(bad)
    assert B._safe_name("data/tls/cert.pem")


def test_admin_api_backup_download_once_then_restore_on_restart(app, client, tmp_path):
    svc = app.state.services
    r = client.post("/admin/api/backup", json={"passphrase": "short"})
    assert r.status_code == 400
    r = client.post("/admin/api/backup", json={"passphrase": PW, "note": "test"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert "passphrase" not in r.text.lower() or PW not in r.text
    d = client.get(out["download"])
    assert d.status_code == 200 and d.content.startswith(B.MAGIC)
    assert client.get(out["download"]).status_code == 404        # once
    # upload + restore into the same router (stages; applied at the next create_app)
    u = client.post("/admin/api/backup/restore/upload", content=d.content,
                    headers={"Content-Type": "application/octet-stream"})
    assert u.status_code == 200, u.text
    assert u.json()["backup"]["note"] == "test"
    bad = client.post("/admin/api/backup/restore", json={"passphrase": PW + "!"})
    assert bad.status_code == 403
    ok = client.post("/admin/api/backup/restore", json={"passphrase": PW})
    assert ok.status_code == 200, ok.text
    assert client.get("/admin/api/backup").json()["staged"]["note"] == "test"
    # a "restart": create_app applies it first
    from foundry_router.main import create_app
    cfg_path = svc.config_store.path
    svc.db.close()                       # the old process is gone (Windows locks open files)
    svc.secret_store.conn.close()
    app2 = create_app(config_path=cfg_path, database_path=svc.db.path)
    assert app2.state.services.restored_from["note"] == "test"
    assert not (os.path.exists(os.path.join(os.path.dirname(svc.db.path), "restore", "pending")))


def test_cancel_restore(app, client):
    r = client.post("/admin/api/backup", json={"passphrase": PW})
    d = client.get(r.json()["download"]).content
    client.post("/admin/api/backup/restore/upload", content=d)
    client.post("/admin/api/backup/restore", json={"passphrase": PW})
    assert client.get("/admin/api/backup").json()["staged"]
    assert client.delete("/admin/api/backup/restore").status_code == 200
    assert client.get("/admin/api/backup").json()["staged"] is None


def test_failed_apply_puts_everything_back(tmp_path, monkeypatch):
    data, config = _router_dirs(tmp_path, "a")
    raw, _ = _make(data, config)
    tdata, tconfig = _router_dirs(tmp_path, "b")
    B.stage(io.BytesIO(raw), PW, tdata)
    real = B.shutil.copyfile
    calls = {"n": 0}

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(B.shutil, "copyfile", flaky)
    with pytest.raises(OSError):
        B.apply_pending(tdata, tconfig)
    monkeypatch.setattr(B.shutil, "copyfile", real)
    assert (tconfig / "config.yaml").read_text() == "tag: b\n"
    assert (tdata / "tls" / "cert.pem").read_text() == "cert-b"
    v = sqlite3.connect(str(tdata / "foundry.sqlite")).execute("SELECT v FROM t").fetchone()[0]
    assert v == "b"
    assert B.staged(tdata) is None and B.apply_pending(tdata, tconfig) is None

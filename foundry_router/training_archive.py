"""Training archive: one more copy of the fine-tuning data, somewhere else.

Two kinds of destination (`training_capture.archive_type`), both set on the
admin page (Logs -> Training -> Capture -> Archive):

  folder  (v0.181.147)  a path the router can write to: a second disk, or a share
                        that is mounted on the host and passed into the container.
  smb     (v0.181.150)  a Windows / SMB share the router connects to ITSELF:
                        server, share, sub-folder, user and password. Nothing is
                        mounted and nothing is set up on the host, which an
                        unprivileged container could not do anyway.

What goes there, each kind behind its own switch:

  capture/capture.sqlite     one consistent copy of the capture store, taken with
                             SQLite's online backup while capture keeps writing
                             (`archive_capture`)
  capture/capture-media/     the images captured requests refer to
  datasets/<project>/<id>/   the prepared datasets, as uploaded
                             (`archive_datasets`): what a trainer reads
  ARCHIVE.json               when, how large, what failed

Runs every `archive_hours` (24) from the maintenance loop and on demand
(POST /admin/api/training/archive). The working database never lives on the
share: SQLite on a network filesystem is prone to corruption, so the share holds
copies. A file is written as `<name>.part` and renamed when complete, so a reader
never sees half a file and the previous copy stays until then. For an SMB
destination the capture store is first copied to a temporary file on the router
(it needs that much free space there), then uploaded, then the temporary file is
removed.

A copy that would not fit is not started. Nothing here deletes anything outside
the archive destination and its own temporary file.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

KV = "training_archive.last"
MARGIN = 512 * 1024 * 1024
CHUNK = 4 * 1024 * 1024
_lock = threading.Lock()
_state: dict = {"state": "idle"}


def _cfg(svc):
    return svc.config_store.config.training_capture


# -- destinations ------------------------------------------------------------------------

class Folder:
    kind = "folder"

    def __init__(self, path: str):
        self.root = Path(path)

    def describe(self) -> str:
        return str(self.root)

    def check(self) -> str:
        if not self.root.is_dir():
            return f"{self.root} does not exist or is not mounted"
        if not os.access(self.root, os.W_OK):
            return f"{self.root} is not writable"
        return ""

    def free(self) -> Optional[int]:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return None

    def same(self, rel: str, size: int, mtime: float) -> bool:
        t = self.root / rel
        try:
            s = t.stat()
        except OSError:
            return False
        return s.st_size == size and s.st_mtime >= mtime - 2

    def put(self, local: Path, rel: str) -> int:
        t = self.root / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        part = t.with_name(t.name + ".part")
        shutil.copy2(local, part)
        os.replace(part, t)
        return t.stat().st_size

    def write_text(self, rel: str, text: str) -> None:
        t = self.root / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(text, encoding="utf-8")

    def local_path(self, rel: str) -> Optional[Path]:
        return self.root / rel                       # SQLite can write here directly

    def remove(self, rel: str) -> None:
        try:
            (self.root / rel).unlink()
        except OSError:
            pass


class Smb:
    """An SMB share reached with smbprotocol's `smbclient` (pure Python: no mount,
    no kernel support, no privileges)."""
    kind = "smb"

    def __init__(self, server: str, share: str, sub: str, user: str, password: str,
                 client=None):
        self.server, self.share = server.strip(), share.strip().strip("\\/")
        self.sub = sub.strip().strip("\\/").replace("/", "\\")
        self.user, self.password = user.strip(), password
        self._c = client                              # injected by tests
        self._ready = False

    def describe(self) -> str:
        return f"\\\\{self.server}\\{self.share}" + (f"\\{self.sub}" if self.sub else "")

    def _client(self):
        if self._c is None:
            import smbclient                          # smbprotocol
            self._c = smbclient
        if not self._ready:
            self._c.register_session(self.server, username=self.user or None,
                                     password=self.password or None)
            self._ready = True
        return self._c

    def _p(self, rel: str = "") -> str:
        rel = rel.strip("/").replace("/", "\\")
        return self.describe() + (f"\\{rel}" if rel else "")

    def check(self) -> str:
        if not self.server or not self.share:
            return "the SMB server and share name are both needed"
        try:
            c = self._client()
            c.makedirs(self._p(), exist_ok=True)
            c.listdir(self._p())
        except Exception as e:                                    # noqa: BLE001
            return f"{self.describe()}: {type(e).__name__}: {str(e)[:200]}"
        return ""

    def free(self) -> Optional[int]:
        try:
            v = self._client().stat_volume(self._p())
            return int(v.caller_available_size)
        except Exception:                                         # noqa: BLE001
            return None

    def same(self, rel: str, size: int, mtime: float) -> bool:
        try:
            s = self._client().stat(self._p(rel))
        except Exception:                                         # noqa: BLE001
            return False
        return s.st_size == size and s.st_mtime >= mtime - 2

    def put(self, local: Path, rel: str) -> int:
        c = self._client()
        target = self._p(rel)
        parent = target.rsplit("\\", 1)[0]
        c.makedirs(parent, exist_ok=True)
        part = target + ".part"
        n = 0
        with open(local, "rb") as src, c.open_file(part, mode="wb") as dst:
            while True:
                b = src.read(CHUNK)
                if not b:
                    break
                dst.write(b)
                n += len(b)
        c.replace(part, target)
        return n

    def write_text(self, rel: str, text: str) -> None:
        c = self._client()
        target = self._p(rel)
        c.makedirs(target.rsplit("\\", 1)[0], exist_ok=True)
        with c.open_file(target, mode="wb") as f:
            f.write(text.encode("utf-8"))

    def local_path(self, rel: str) -> Optional[Path]:
        return None                                   # not a path SQLite can write to

    def remove(self, rel: str) -> None:
        try:
            self._client().remove(self._p(rel))
        except Exception:                                         # noqa: BLE001
            pass


def destination(svc):
    """The configured destination, or None when the archive is off."""
    cfg = _cfg(svc)
    if (getattr(cfg, "archive_type", "folder") or "folder") == "smb":
        if not (getattr(cfg, "archive_smb_server", "") or "").strip():
            return None
        return Smb(cfg.archive_smb_server, getattr(cfg, "archive_smb_share", "") or "",
                   getattr(cfg, "archive_smb_path", "") or "",
                   getattr(cfg, "archive_smb_user", "") or "",
                   getattr(cfg, "archive_smb_password", "") or "")
    d = (getattr(cfg, "archive_dir", "") or "").strip()
    return Folder(d) if d else None


def folder(svc) -> Optional[Path]:
    """The archive folder (folder destinations only; kept for callers of v0.181.147)."""
    d = destination(svc)
    return d.root if isinstance(d, Folder) else None


def last(svc) -> dict:
    try:
        raw = svc.db.kv_get(KV)
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:                                             # noqa: BLE001
        return {}


def status(svc) -> dict:
    cfg = _cfg(svc)
    d = destination(svc)
    out = {"type": getattr(cfg, "archive_type", "folder") or "folder",
           "dir": (getattr(cfg, "archive_dir", "") or "").strip(),
           "smb": {"server": getattr(cfg, "archive_smb_server", "") or "",
                   "share": getattr(cfg, "archive_smb_share", "") or "",
                   "path": getattr(cfg, "archive_smb_path", "") or "",
                   "user": getattr(cfg, "archive_smb_user", "") or "",
                   "has_password": bool(getattr(cfg, "archive_smb_password", "") or "")},
           "on": d is not None, "where": d.describe() if d is not None else "",
           "capture": bool(getattr(cfg, "archive_capture", True)),
           "datasets": bool(getattr(cfg, "archive_datasets", True)),
           "prepared": bool(getattr(cfg, "archive_prepared", True)),
           "hours": int(getattr(cfg, "archive_hours", 24) or 0),
           "run": dict(_state), "last": last(svc)}
    if isinstance(d, Folder):                # cheap, local: shown on every page load
        out["exists"] = d.root.is_dir()
        out["writable"] = bool(d.root.is_dir() and os.access(d.root, os.W_OK))
        out["free_bytes"] = d.free() if d.root.is_dir() else None
    return out


def due(svc, now: Optional[float] = None) -> bool:
    hours = int(getattr(_cfg(svc), "archive_hours", 24) or 0)
    if destination(svc) is None or hours <= 0 or _state.get("state") == "running":
        return False
    return (now or time.time()) - float(last(svc).get("finished") or 0) >= hours * 3600


def test(svc) -> dict:
    """Write a small file to the destination, read its size back, remove it."""
    d = destination(svc)
    if d is None:
        return {"ok": False, "error": "no archive destination is set"}
    err = d.check()
    if err:
        return {"ok": False, "error": err, "where": d.describe()}
    name = f"foundry-archive-test-{int(time.time())}.txt"
    tmp = Path(getattr(svc.capture, "data_dir", ".")) / "training" / name
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("Foundry archive write test\n", encoding="utf-8")
        n = d.put(tmp, name)
        ok = d.same(name, n, 0)
        d.remove(name)
        return {"ok": bool(ok), "where": d.describe(), "free_bytes": d.free(),
                "error": "" if ok else "the test file did not read back"}
    except Exception as e:                                        # noqa: BLE001
        return {"ok": False, "where": d.describe(), "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _copy_tree(src: Path, d, rel_root: str) -> tuple[int, int]:
    """New and changed files only (by size and time). (files copied, bytes)."""
    n = b = 0
    if not src.is_dir():
        return 0, 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = rel_root + "/" + p.relative_to(src).as_posix()
        s = p.stat()
        if d.same(rel, s.st_size, s.st_mtime):
            continue
        try:
            d.put(p, rel)
        except Exception as e:                                    # noqa: BLE001
            raise OSError(f"{p.name}: {type(e).__name__}: {e}") from e
        n += 1
        b += s.st_size
    return n, b


def _backup_to(store, target: Path, progress) -> int:
    """A consistent copy of the capture store in a local file. `nolock=1`: a
    mounted network folder often has no working file locks, and this process is
    the only writer of the copy."""
    target.parent.mkdir(parents=True, exist_ok=True)
    for p in (target, Path(str(target) + "-journal")):
        try:
            p.unlink()
        except OSError:
            pass
    src = sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(target.resolve().as_uri() + "?nolock=1", uri=True)
    try:
        dst.execute("PRAGMA journal_mode=OFF")
        dst.execute("PRAGMA synchronous=OFF")
        src.backup(dst, pages=4096, sleep=0.01,
                   progress=lambda _s, remaining, total: progress(total - remaining, total))
        dst.commit()
    finally:
        dst.close()
        src.close()
    return target.stat().st_size


def _archive_capture(svc, d, store, out: dict) -> None:
    size, free = store.size_bytes(), d.free()
    rel = "capture/capture.sqlite"
    if free is not None and free < size + MARGIN:
        out["errors"].append(
            f"capture store: {size / 1e9:.1f} GB to copy, {free / 1e9:.1f} GB free on the "
            f"archive (the previous copy is kept until a new one is complete)")
        return

    def progress(done, total):
        _state["pages"], _state["of"] = done, total
    direct = d.local_path(rel)
    if direct is not None:
        part = direct.with_name(direct.name + ".part")
        n = _backup_to(store, part, progress)
        os.replace(part, direct)
    else:
        tmp = store.path.parent / "archive-tmp" / "capture.sqlite"
        local_free = shutil.disk_usage(store.path.parent).free
        if local_free < size + MARGIN:
            out["errors"].append(
                f"capture store: {size / 1e9:.1f} GB has to be copied on the router before it "
                f"is uploaded, and the router has {local_free / 1e9:.1f} GB free")
            return
        try:
            _backup_to(store, tmp, progress)
            _state["phase"] = "uploading the capture store"
            n = d.put(tmp, rel)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    out["capture"] = {"bytes": n}
    files, _ = _copy_tree(store.path.parent / "capture-media", d, "capture/capture-media")
    out["capture"]["media_files"] = files


def _archive_prepared(svc, d, out: dict) -> None:
    """The prepared store (v0.181.162): what the router keeps for good, including
    the records other machines sent. Each file is copied consistently, as the
    capture store is."""
    from . import training_prep
    root = training_prep._dirs(svc)["store"]
    names = ["normalized.sqlite", "blobs.sqlite", "raw/raw.sqlite", "curated.sqlite"]
    files, total = 0, 0
    for rel in names:
        src = root / rel.replace("/", os.sep)
        if not src.is_file():
            continue

        class _Store:                       # what _backup_to needs
            path = src
        target = f"prepared/{rel.replace('/', '_')}"
        direct = d.local_path(target)
        if direct is not None:
            part = direct.with_name(direct.name + ".part")
            n = _backup_to(_Store, part, lambda a, b: None)
            os.replace(part, direct)
        else:
            tmp = Path(svc.capture.data_dir) / "training" / "archive-tmp" / rel.replace("/", "_")
            try:
                _backup_to(_Store, tmp, lambda a, b: None)
                n = d.put(tmp, target)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        files += 1
        total += n
    if files:
        out["prepared"] = {"files": files, "bytes": total}


def run(svc) -> dict:
    """One archive pass. Never raises: what failed is in the result."""
    if not _lock.acquire(blocking=False):
        return dict(_state)
    out: dict = {"started": time.time(), "errors": []}
    try:
        _state.clear()
        _state.update({"state": "running", "phase": "checking", "started": out["started"]})
        d = destination(svc)
        err = "no archive destination is set" if d is None else d.check()
        if err:
            out["errors"].append(err)
        else:
            out["where"] = d.describe()
            cfg = _cfg(svc)
            cap = getattr(svc, "capture", None)
            if getattr(cfg, "archive_datasets", True) and cap is not None:
                _state["phase"] = "datasets"
                try:
                    n, b = _copy_tree(cap.data_dir / "training" / "datasets", d, "datasets")
                    out["datasets"] = {"files": n, "bytes": b}
                except OSError as e:
                    out["errors"].append(f"datasets: {e}")
            if getattr(cfg, "archive_capture", True) and cap is not None and (
                    cap._store is not None or cfg.enabled):
                _state["phase"] = "capture store"
                try:
                    _archive_capture(svc, d, cap.store, out)
                except Exception as e:                            # noqa: BLE001
                    out["errors"].append(f"capture store: {type(e).__name__}: {str(e)[:200]}")
            if getattr(cfg, "archive_prepared", True):
                _state["phase"] = "prepared store"
                try:
                    _archive_prepared(svc, d, out)
                except Exception as e:                            # noqa: BLE001
                    out["errors"].append(f"prepared store: {type(e).__name__}: {str(e)[:200]}")
            out["free_bytes"] = d.free()
            try:
                d.write_text("ARCHIVE.json", json.dumps({**out, "finished": time.time()}, indent=1))
            except Exception as e:                                # noqa: BLE001
                out["errors"].append(f"ARCHIVE.json: {type(e).__name__}: {e}")
    except Exception as e:                                        # noqa: BLE001
        log.exception("training archive failed")
        out["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        out["finished"] = time.time()
        out["ok"] = not out["errors"]
        try:
            svc.db.kv_set(KV, json.dumps(out))
            if out["errors"]:
                svc.db.log_event("warning", "training", "training archive: " + "; ".join(
                    out["errors"])[:300])
        except Exception:                                         # noqa: BLE001
            pass
        _state.clear()
        _state.update({"state": "idle"})
        _lock.release()
    return out


def start(svc) -> dict:
    if _state.get("state") == "running":
        return dict(_state)
    threading.Thread(target=run, args=(svc,), name="foundry-training-archive", daemon=True).start()
    time.sleep(0.05)
    return dict(_state)

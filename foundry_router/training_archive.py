"""Training archive (v0.181.147): one more copy of the fine-tuning data, somewhere else.

`training_capture.archive_dir` is a folder the router can write to: a second
disk, or a network share (NFS, SMB) that is MOUNTED on the host and passed into
the container. To Foundry it is a path; it mounts nothing itself. Empty = off.

What goes there, each kind behind its own switch:

  <dir>/capture/capture.sqlite     one consistent copy of the capture store,
                                   taken with SQLite's online backup while
                                   capture keeps writing (`archive_capture`)
  <dir>/capture/capture-media/     the images captured requests refer to
  <dir>/datasets/<project>/<id>/   the prepared datasets, as uploaded
                                   (`archive_datasets`): what a trainer reads
  <dir>/ARCHIVE.json               when, how large, what failed

Runs every `archive_hours` (24) from the maintenance loop and on demand
(POST /admin/api/training/archive). The working database never lives on the
share: SQLite on a network filesystem is prone to corruption, so the share holds
copies. The copy is written to `capture.sqlite.part` and renamed when complete,
so a reader never sees half a file; the previous copy stays until then.

A copy that would not fit (free space under the store's size plus a margin) is
not started. Nothing here deletes anything outside the archive folder.
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
_lock = threading.Lock()
_state: dict = {"state": "idle"}


def _cfg(svc):
    return svc.config_store.config.training_capture


def folder(svc) -> Optional[Path]:
    d = (getattr(_cfg(svc), "archive_dir", "") or "").strip()
    return Path(d) if d else None


def _free(p: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


def last(svc) -> dict:
    try:
        raw = svc.db.kv_get(KV)
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:                                             # noqa: BLE001
        return {}


def status(svc) -> dict:
    cfg = _cfg(svc)
    d = folder(svc)
    out = {"dir": str(d) if d else "", "capture": bool(getattr(cfg, "archive_capture", True)),
           "datasets": bool(getattr(cfg, "archive_datasets", True)),
           "hours": int(getattr(cfg, "archive_hours", 24) or 0),
           "run": dict(_state), "last": last(svc)}
    if d is not None:
        out["exists"] = d.is_dir()
        out["writable"] = bool(d.is_dir() and os.access(d, os.W_OK))
        out["free_bytes"] = _free(d) if d.is_dir() else None
    return out


def due(svc, now: Optional[float] = None) -> bool:
    d = folder(svc)
    hours = int(getattr(_cfg(svc), "archive_hours", 24) or 0)
    if d is None or hours <= 0 or _state.get("state") == "running":
        return False
    return (now or time.time()) - float(last(svc).get("finished") or 0) >= hours * 3600


def _copy_tree(src: Path, dst: Path) -> tuple[int, int]:
    """New and changed files only (by size and time). (files copied, bytes)."""
    n = b = 0
    if not src.is_dir():
        return 0, 0
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        t = dst / p.relative_to(src)
        try:
            s = p.stat()
            if t.exists():
                ts = t.stat()
                if ts.st_size == s.st_size and ts.st_mtime >= s.st_mtime - 2:
                    continue
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, t)
            n += 1
            b += s.st_size
        except OSError as e:
            raise OSError(f"{p.name}: {e}") from e
    return n, b


def _backup_capture(store, target: Path, progress) -> int:
    """A consistent copy of the capture store. `nolock=1` on the destination: a
    network share often has no working file locks, and this process is the only
    writer of the copy."""
    part = target.with_name(target.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    for p in (part, Path(str(part) + "-journal")):
        try:
            p.unlink()
        except OSError:
            pass
    src = sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(part.resolve().as_uri() + "?nolock=1", uri=True)
    try:
        dst.execute("PRAGMA journal_mode=OFF")
        dst.execute("PRAGMA synchronous=OFF")
        src.backup(dst, pages=4096, sleep=0.01,
                   progress=lambda _s, remaining, total: progress(total - remaining, total))
        dst.commit()
    finally:
        dst.close()
        src.close()
    os.replace(part, target)
    return target.stat().st_size


def run(svc) -> dict:
    """One archive pass. Never raises: what failed is in the result."""
    if not _lock.acquire(blocking=False):
        return dict(_state)
    out: dict = {"started": time.time(), "errors": []}
    try:
        _state.clear()
        _state.update({"state": "running", "phase": "checking", "started": out["started"]})
        d = folder(svc)
        if d is None:
            out["errors"].append("no archive folder is set")
        elif not d.is_dir():
            out["errors"].append(f"{d} does not exist or is not mounted")
        elif not os.access(d, os.W_OK):
            out["errors"].append(f"{d} is not writable")
        else:
            cfg = _cfg(svc)
            cap = getattr(svc, "capture", None)
            if getattr(cfg, "archive_datasets", True) and cap is not None:
                _state["phase"] = "datasets"
                try:
                    n, b = _copy_tree(cap.data_dir / "training" / "datasets", d / "datasets")
                    out["datasets"] = {"files": n, "bytes": b}
                except OSError as e:
                    out["errors"].append(f"datasets: {e}")
            if getattr(cfg, "archive_capture", True) and cap is not None and cap._store is not None \
                    or (getattr(cfg, "archive_capture", True) and cap is not None and cfg.enabled):
                store = cap.store
                size, free = store.size_bytes(), _free(d)
                old = d / "capture" / "capture.sqlite"
                have = old.stat().st_size if old.exists() else 0
                if free is not None and free < size + MARGIN:
                    out["errors"].append(
                        f"capture store: {size / 1e9:.1f} GB to copy, {free / 1e9:.1f} GB free on "
                        f"the archive folder (the previous copy of {have / 1e9:.1f} GB is kept "
                        f"until the new one is complete)")
                else:
                    _state["phase"] = "capture store"

                    def progress(done, total):
                        _state["pages"], _state["of"] = done, total
                    try:
                        out["capture"] = {"bytes": _backup_capture(store, old, progress)}
                        n, b = _copy_tree(store.path.parent / "capture-media",
                                          d / "capture" / "capture-media")
                        out["capture"]["media_files"] = n
                    except (OSError, sqlite3.Error) as e:
                        out["errors"].append(f"capture store: {type(e).__name__}: {e}")
            out["free_bytes"] = _free(d)
            try:
                with open(d / "ARCHIVE.json", "w", encoding="utf-8") as f:
                    json.dump({**out, "finished": time.time()}, f, indent=1)
            except OSError as e:
                out["errors"].append(f"ARCHIVE.json: {e}")
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

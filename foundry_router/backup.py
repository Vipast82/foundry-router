"""Backup and restore of a whole router (Logs → Data → Backup & restore, the setup wizard,
the router installer's --restore).

A backup is ONE encrypted file (`.fbak`) holding:
  - every SQLite database in the data folder, as a consistent online copy (the main DB:
    personas, clients' usage, machines, automation settings and history; the secret store,
    whose values stay encrypted under its own passphrase);
  - the HTTPS certificate (`tls/`), so pinned slots keep trusting the router;
  - config.yaml and provider-keys.env;
  - optionally every other file in the data folder (media, training captures).

Encryption: the archive (a tar stream) is cut into 1 MiB chunks, each AES-256-GCM
encrypted under a key derived with scrypt from a backup passphrase that the person types
when making the backup. Each chunk's associated data binds the file header, the chunk
number and a last-chunk flag, so a reordered, cut or edited file fails to decrypt. The
passphrase is never stored.

Restoring stages the files under <data>/restore/pending and checks each one's sha256.
The router applies a staged restore at its next start, before it opens anything: the
current files move to <data>/restore/previous/ (one generation kept) and the backup's
files take their place. A restored secret store is locked until its own passphrase is
entered (Access → Secrets).

CLI (the router installer runs these in the container; the passphrase is read from
stdin, never from an argument):
    python -m foundry_router.backup info FILE
    python -m foundry_router.backup create FILE [--files]
    python -m foundry_router.backup restore FILE
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import secrets as _rand
import shutil
import socket
import sqlite3
import struct
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Optional

log = logging.getLogger(__name__)

MAGIC = b"FNDRBAK1\n"
FORMAT = 1
CHUNK = 1 << 20
MIN_PASSPHRASE = 12
KDF = {"alg": "scrypt", "n": 2 ** 15, "r": 8, "p": 1}
SKIP_DIRS = {"restore", "backups"}
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
CONFIG_FILES = ("config.yaml", "provider-keys.env")
MAX_HEADER = 64 * 1024
CHECK = b"foundry-router-backup-v1"
CHECK_NONCE = bytes([0xFF] * 8)


class BackupError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _aead(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:                                     # pragma: no cover
        raise BackupError("the router image lacks the 'cryptography' package",
                          "unavailable", 503) from e
    return AESGCM(key)


def _derive(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=int(kdf["n"]),
                          r=int(kdf["r"]), p=int(kdf["p"]), dklen=32,
                          maxmem=128 * 1024 * 1024)


def _version() -> str:
    from . import __version__
    return __version__


def _vtuple(v: str) -> tuple:
    out = []
    for part in str(v or "0").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


# -- encrypted stream -------------------------------------------------------------------

class _EncryptingWriter(io.RawIOBase):
    """File-like sink: buffers plaintext, writes [len][ciphertext] chunks to `out`."""

    def __init__(self, out: BinaryIO, passphrase: str, meta: dict):
        super().__init__()
        if len(passphrase or "") < MIN_PASSPHRASE:
            raise BackupError(f"the backup passphrase needs at least {MIN_PASSPHRASE} "
                              "characters", "weak_passphrase")
        salt = _rand.token_bytes(16)
        self._prefix = _rand.token_bytes(4)
        self._aes = _aead(_derive(passphrase, salt, KDF))
        # tells a wrong passphrase apart from a damaged file
        check = self._aes.encrypt(self._prefix + CHECK_NONCE, CHECK, b"check")
        header = {"format": FORMAT, "kdf": KDF, "salt": salt.hex(), "check": check.hex(),
                  "nonce_prefix": self._prefix.hex(), "chunk": CHUNK, **meta}
        hb = json.dumps(header, sort_keys=True).encode("utf-8")
        out.write(MAGIC + struct.pack(">I", len(hb)) + hb)
        self._hd = hashlib.sha256(MAGIC + hb).digest()
        self._out = out
        self._buf = bytearray()
        self._n = 0
        self._done = False

    def writable(self):
        return True

    def _emit(self, data: bytes, last: bool) -> None:
        nonce = self._prefix + struct.pack(">Q", self._n)
        ad = self._hd + struct.pack(">QB", self._n, 1 if last else 0)
        ct = self._aes.encrypt(nonce, bytes(data), ad)
        self._out.write(struct.pack(">I", len(ct)) + ct)
        self._n += 1

    def write(self, b) -> int:
        self._buf += b
        while len(self._buf) > CHUNK:
            self._emit(self._buf[:CHUNK], False)
            del self._buf[:CHUNK]
        return len(b)

    def finish(self) -> None:
        if not self._done:
            self._emit(self._buf, True)
            self._buf = bytearray()
            self._done = True


def read_header(f: BinaryIO) -> tuple[dict, bytes]:
    head = f.read(len(MAGIC))
    if head != MAGIC:
        raise BackupError("this is not a Foundry router backup (.fbak)", "not_a_backup")
    raw = f.read(4)
    if len(raw) != 4:
        raise BackupError("the backup file is cut short", "damaged")
    (n,) = struct.unpack(">I", raw)
    if n > MAX_HEADER:
        raise BackupError("the backup header is damaged", "damaged")
    hb = f.read(n)
    try:
        header = json.loads(hb)
    except ValueError as e:
        raise BackupError("the backup header is damaged", "damaged") from e
    if int(header.get("format", 0)) != FORMAT:
        raise BackupError(f"backup format {header.get('format')} is not supported by this "
                          "router version", "unsupported")
    return header, hashlib.sha256(MAGIC + hb).digest()


class _DecryptingReader(io.RawIOBase):
    def __init__(self, f: BinaryIO, passphrase: str):
        super().__init__()
        self.header, self._hd = read_header(f)
        kdf = self.header.get("kdf") or {}
        if kdf.get("alg") != "scrypt" or int(kdf.get("n", 0)) > 2 ** 20:
            raise BackupError("the backup uses an unknown key derivation", "unsupported")
        self._aes = _aead(_derive(passphrase or "", bytes.fromhex(self.header["salt"]), kdf))
        self._prefix = bytes.fromhex(self.header["nonce_prefix"])
        from cryptography.exceptions import InvalidTag
        try:
            ok = self._aes.decrypt(self._prefix + CHECK_NONCE,
                                   bytes.fromhex(self.header.get("check") or ""), b"check") == CHECK
        except (InvalidTag, ValueError):
            ok = False
        if not ok:
            raise BackupError("wrong passphrase for this backup", "wrong_passphrase", 403)
        self._max = int(self.header.get("chunk", CHUNK)) + 16
        self._f = f
        self._n = 0
        self._buf = b""
        self._eof = False

    def readable(self):
        return True

    def _next(self) -> None:
        raw = self._f.read(4)
        if len(raw) != 4:
            raise BackupError("the backup file is cut short", "damaged")
        (n,) = struct.unpack(">I", raw)
        if n > self._max:
            raise BackupError("the backup file is damaged", "damaged")
        ct = self._f.read(n)
        if len(ct) != n:
            raise BackupError("the backup file is cut short", "damaged")
        nonce = self._prefix + struct.pack(">Q", self._n)
        from cryptography.exceptions import InvalidTag
        for last in (0, 1):
            try:
                self._buf += self._aes.decrypt(nonce, ct, self._hd + struct.pack(">QB", self._n, last))
                break
            except InvalidTag:
                continue
        else:
            raise BackupError("the backup file is damaged", "damaged")
        self._n += 1
        if last:
            self._eof = True
            if self._f.read(1):
                raise BackupError("the backup file has data after its end", "damaged")

    def readinto(self, b) -> int:
        while not self._buf and not self._eof:
            self._next()
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


# -- what goes in ------------------------------------------------------------------------

def _is_sqlite(p: Path) -> bool:
    return p.suffix in (".sqlite", ".sqlite3", ".db") and p.is_file()


def _collect(data: Path, include_files: bool) -> list[tuple[str, Path]]:
    """(archive name, source path) for every file to back up; side files are left out."""
    out: list[tuple[str, Path]] = []
    for p in sorted(data.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(data)
        if rel.parts[0] in SKIP_DIRS or p.name.endswith(SIDE_SUFFIXES) or p.name.endswith(".tmp"):
            continue
        core = (len(rel.parts) == 1 and _is_sqlite(p)) or rel.parts[0] == "tls"
        if core or include_files:
            out.append(("data/" + rel.as_posix(), p))
    return out


def _sqlite_copy(src: Path, dst: Path) -> None:
    """A consistent copy of a live SQLite DB (the online backup API; WAL readers allowed)."""
    s = sqlite3.connect(Path(src).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


class _Hashing(io.RawIOBase):
    def __init__(self, f: BinaryIO):
        super().__init__()
        self.f = f
        self.h = hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, b) -> int:
        data = self.f.read(len(b))
        self.h.update(data)
        b[:len(data)] = data
        return len(data)


def create(out: BinaryIO, passphrase: str, data: Path, config: Path,
           include_files: bool = False, note: str = "", main_db: str = "foundry.sqlite") -> dict:
    """Write an encrypted backup of `data` (the data folder) and `config` (config.yaml's
    folder) to `out`. Returns the manifest (names, sizes, hashes; no contents)."""
    data, config = Path(data), Path(config)
    meta = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "router_version": _version(), "host": socket.gethostname(),
            "include_files": bool(include_files), "note": str(note or "")[:200],
            "main_db": main_db}
    enc = _EncryptingWriter(out, passphrase, meta)
    files: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="fbak-") as tmp:
        with tarfile.open(fileobj=enc, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            def add(name: str, path: Path):
                ti = tarfile.TarInfo(name)
                ti.size = path.stat().st_size
                ti.mtime = int(path.stat().st_mtime)
                ti.mode = 0o600
                with open(path, "rb") as f:
                    hf = _Hashing(f)
                    tar.addfile(ti, io.BufferedReader(hf))
                files.append({"path": name, "size": ti.size, "sha256": hf.h.hexdigest()})
            for name, src in _collect(data, include_files):
                if _is_sqlite(src):
                    snap = Path(tmp) / "snap.sqlite"
                    _sqlite_copy(src, snap)
                    add(name, snap)
                    snap.unlink()
                else:
                    add(name, src)
            for fn in CONFIG_FILES:
                p = config / fn
                if p.is_file():
                    add("config/" + fn, p)
            manifest = {**meta, "format": FORMAT, "files": files}
            mb = json.dumps(manifest, indent=1).encode("utf-8")
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(mb)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(mb))
    enc.finish()
    return manifest


def info(f: BinaryIO) -> dict:
    """The plain header (date, router version, host, note); needs no passphrase."""
    header, _ = read_header(f)
    return {k: header.get(k) for k in ("created", "router_version", "host", "include_files", "note")}


# -- restore -----------------------------------------------------------------------------

def _safe_name(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise BackupError(f"unsafe path in the backup: {name!r}", "damaged")
    if p.parts[0] == "config":
        if len(p.parts) != 2 or p.parts[1] not in CONFIG_FILES:
            raise BackupError(f"unexpected file in the backup: {name!r}", "damaged")
    elif p.parts[0] == "data":
        if len(p.parts) < 2 or p.parts[1] in SKIP_DIRS:
            raise BackupError(f"unexpected file in the backup: {name!r}", "damaged")
    elif name != "manifest.json":
        raise BackupError(f"unexpected file in the backup: {name!r}", "damaged")
    return p


def pending_dir(data: Path) -> Path:
    return Path(data) / "restore" / "pending"


def stage(f: BinaryIO, passphrase: str, data: Path) -> dict:
    """Decrypt and check a backup into <data>/restore/pending. Applied at the next start."""
    data = Path(data)
    rd = _DecryptingReader(f, passphrase)
    bv = rd.header.get("router_version") or "0"
    if _vtuple(bv) > _vtuple(_version()):
        raise BackupError(f"this backup is from router v{bv}, newer than this router "
                          f"(v{_version()}): upgrade the router first", "newer_backup", 409)
    root = Path(data) / "restore"
    work = root / "incoming"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    got: dict[str, str] = {}
    manifest: Optional[dict] = None
    try:
        with tarfile.open(fileobj=io.BufferedReader(rd, CHUNK), mode="r|") as tar:
            for m in tar:
                p = _safe_name(m.name)
                if not m.isfile():
                    raise BackupError(f"unexpected entry in the backup: {m.name!r}", "damaged")
                src = tar.extractfile(m)
                if m.name == "manifest.json":
                    manifest = json.loads(src.read(MAX_HEADER * 64))
                    continue
                dst = work.joinpath(*p.parts)
                dst.parent.mkdir(parents=True, exist_ok=True)
                h = hashlib.sha256()
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    while True:
                        chunk = src.read(CHUNK)
                        if not chunk:
                            break
                        h.update(chunk)
                        out.write(chunk)
                got[m.name] = h.hexdigest()
            # read to the last chunk, so a file cut after the tar's end is caught too
            while rd.read(CHUNK):
                pass
        if not manifest:
            raise BackupError("the backup has no manifest", "damaged")
        want = {x["path"]: x["sha256"] for x in manifest.get("files") or []}
        if want != got:
            raise BackupError("the backup's files do not match its manifest", "damaged")
        main = "data/" + str(manifest.get("main_db") or "foundry.sqlite")
        if main not in got:
            raise BackupError(f"the backup has no main database ({main})", "damaged")
        for name in got:
            if name.endswith(".sqlite"):
                c = sqlite3.connect(str(work.joinpath(*PurePosixPath(name).parts)))
                try:
                    ok = c.execute("PRAGMA quick_check").fetchone()[0]
                finally:
                    c.close()
                if ok != "ok":
                    raise BackupError(f"{name} in the backup is damaged ({ok})", "damaged")
        (work / "READY").write_text(json.dumps(manifest), encoding="utf-8")
        pend = pending_dir(data)
        shutil.rmtree(pend, ignore_errors=True)
        os.replace(work, pend)
    except tarfile.TarError as e:
        shutil.rmtree(work, ignore_errors=True)
        raise BackupError(f"the backup file is damaged ({e})", "damaged") from e
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return summary(manifest)


def summary(manifest: dict) -> dict:
    files = manifest.get("files") or []
    return {"created": manifest.get("created"), "router_version": manifest.get("router_version"),
            "host": manifest.get("host"), "note": manifest.get("note"),
            "files": len(files), "bytes": sum(int(x.get("size") or 0) for x in files),
            "has_secret_store": any(x["path"] == "data/secrets.sqlite" for x in files),
            "has_provider_keys": any(x["path"] == "config/provider-keys.env" for x in files)}


def staged(data: Path) -> Optional[dict]:
    p = pending_dir(data) / "READY"
    if not p.is_file():
        return None
    try:
        return summary(json.loads(p.read_text(encoding="utf-8")))
    except ValueError:
        return None


def apply_pending(data: Path, config: Path) -> Optional[dict]:
    """Put a staged restore in place. Call before the config or any DB is opened.
    The replaced files go to <data>/restore/previous/ (the generation before is removed).
    All or nothing: if any step fails, the current files are put back, the staged restore
    is marked FAILED (not retried at every start) and the error is raised."""
    data, config = Path(data), Path(config)
    pend = pending_dir(data)
    ready = pend / "READY"
    if not ready.is_file():
        return None
    manifest = json.loads(ready.read_text(encoding="utf-8"))
    prev = data / "restore" / "previous"
    shutil.rmtree(prev, ignore_errors=True)
    (prev / "data").mkdir(parents=True)
    (prev / "config").mkdir(parents=True)
    # the current state to move aside: every top-level DB (+ side files), tls/, the file
    # trees the backup restores, and the config files
    trees = {PurePosixPath(x["path"]).parts[1] for x in manifest.get("files") or []
             if x["path"].startswith("data/") and len(PurePosixPath(x["path"]).parts) > 2}
    trees.add("tls")
    aside: list[tuple[Path, Path]] = []
    placed: list[Path] = []
    try:
        for p in sorted(data.iterdir()):
            if p.name in SKIP_DIRS:
                continue
            top_db = p.is_file() and (p.suffix in (".sqlite", ".sqlite3", ".db")
                                      or any(p.name.endswith(x) for x in SIDE_SUFFIXES))
            if top_db or (p.is_dir() and p.name in trees):
                dst = prev / "data" / p.name
                os.replace(p, dst)
                aside.append((p, dst))
        for fn in CONFIG_FILES:
            if (config / fn).exists():
                dst = prev / "config" / fn
                shutil.move(str(config / fn), str(dst))
                aside.append((config / fn, dst))
        src_data = pend / "data"
        for p in sorted(src_data.rglob("*")) if src_data.exists() else []:
            if p.is_file():
                dst = data / p.relative_to(src_data)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dst)
                placed.append(dst)
        for fn in CONFIG_FILES:
            src = pend / "config" / fn
            if src.is_file():
                config.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, config / fn)
                placed.append(config / fn)
                try:
                    os.chmod(config / fn, 0o600)
                except OSError:
                    pass
    except BaseException as e:
        for p in placed:
            p.unlink(missing_ok=True)
        for orig, moved in reversed(aside):
            try:
                if orig.exists() and orig.is_dir():
                    shutil.rmtree(orig, ignore_errors=True)
                shutil.move(str(moved), str(orig))
            except OSError:
                log.exception("could not put %s back from %s", orig, moved)
        try:
            os.replace(ready, pend / "FAILED")
            (pend / "FAILED.txt").write_text(f"{type(e).__name__}: {e}\n", encoding="utf-8")
        except OSError:
            pass
        raise
    shutil.rmtree(pend, ignore_errors=True)
    log.warning("restored the router from a backup made %s on %s (router v%s); the replaced "
                "files are in %s", manifest.get("created"), manifest.get("host"),
                manifest.get("router_version"), prev)
    return summary(manifest)


# -- admin API ---------------------------------------------------------------------------

from fastapi import APIRouter, Request                                    # noqa: E402
from fastapi.responses import FileResponse, JSONResponse                   # noqa: E402
from starlette.background import BackgroundTask                            # noqa: E402

router = APIRouter()
A = "/admin/api/backup"
_downloads: dict[str, tuple[Path, float]] = {}
_dl_lock = threading.Lock()
DOWNLOAD_TTL = 600


def _svc(request: Request):
    return request.app.state.services


def _paths(svc) -> tuple[Path, Path]:
    return Path(svc.db.path).parent, Path(svc.config_store.path).parent


def _err(e: BackupError) -> JSONResponse:
    return JSONResponse({"error": {"message": str(e), "code": e.code}}, status_code=e.status,
                        headers={"Cache-Control": "no-store"})


def _sweep() -> None:
    now = time.time()
    with _dl_lock:
        for k, (p, exp) in list(_downloads.items()):
            if exp < now:
                _downloads.pop(k, None)
                p.unlink(missing_ok=True)


@router.get(A)
async def backup_status(request: Request):
    svc = _svc(request)
    data, _ = _paths(svc)
    return {"staged": staged(data), "min_passphrase": MIN_PASSPHRASE,
            "restored": getattr(svc, "restored_from", None), "version": _version()}


@router.post(A)
async def make_backup(request: Request):
    """{passphrase, include_files?, note?} -> {download: url}. The file is made now and can
    be downloaded once within 10 minutes."""
    import asyncio
    from .secrets_api import _need_transport
    from .secret_store import SecretStoreError
    svc = _svc(request)
    try:
        _need_transport(request)
    except SecretStoreError as e:
        return JSONResponse({"error": {"message": str(e), "code": e.code}}, status_code=e.status)
    try:
        body = await request.json()
    except Exception:                                            # noqa: BLE001
        body = {}
    data, config = _paths(svc)
    out_dir = data / "backups"
    out_dir.mkdir(exist_ok=True)
    _sweep()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    host = "".join(ch for ch in socket.gethostname() if ch.isalnum() or ch in "-_")[:40] or "router"
    path = out_dir / f"foundry-router-{host}-{stamp}.fbak"

    def run():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            return create(out, str(body.get("passphrase") or ""), data, config,
                          include_files=bool(body.get("include_files")), note=body.get("note") or "",
                          main_db=Path(svc.db.path).name)
    try:
        manifest = await asyncio.to_thread(run)
    except BackupError as e:
        path.unlink(missing_ok=True)
        return _err(e)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    token = _rand.token_urlsafe(18)
    with _dl_lock:
        _downloads[token] = (path, time.time() + DOWNLOAD_TTL)
    svc.db.log_event("info", "backup", f"backup made ({len(manifest['files'])} files, "
                     f"{path.stat().st_size} bytes)")
    return {"ok": True, "download": f"{A}/download/{token}", "name": path.name,
            "bytes": path.stat().st_size, **summary(manifest)}


@router.get(A + "/download/{token}")
async def download_backup(request: Request, token: str):
    _sweep()
    with _dl_lock:
        item = _downloads.pop(token, None)
    if not item:
        return JSONResponse({"error": "this download link was used or has expired: make the "
                                      "backup again"}, status_code=404)
    path = item[0]
    return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                        headers={"Cache-Control": "no-store"},
                        background=BackgroundTask(lambda: path.unlink(missing_ok=True)))


def _upload_path(svc) -> Path:
    data, _ = _paths(svc)
    return data / "restore" / "upload.fbak"


@router.post(A + "/restore/upload")
async def upload_backup(request: Request):
    """The raw .fbak as the request body. Answers its plain header (no passphrase yet)."""
    svc = _svc(request)
    dst = _upload_path(svc)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        async for chunk in request.stream():
            out.write(chunk)
    try:
        with open(tmp, "rb") as f:
            head = info(f)
    except BackupError as e:
        tmp.unlink(missing_ok=True)
        return _err(e)
    os.replace(tmp, dst)
    return {"ok": True, "backup": head, "bytes": dst.stat().st_size, "version": _version()}


@router.post(A + "/restore")
async def restore_backup(request: Request):
    """{passphrase} -> decrypts and checks the uploaded backup and stages it. The router
    applies it when it restarts (the UI restarts it next)."""
    import asyncio
    from .secrets_api import _need_transport
    from .secret_store import SecretStoreError
    svc = _svc(request)
    try:
        _need_transport(request)
    except SecretStoreError as e:
        return JSONResponse({"error": {"message": str(e), "code": e.code}}, status_code=e.status)
    try:
        body = await request.json()
    except Exception:                                            # noqa: BLE001
        body = {}
    src = _upload_path(svc)
    if not src.is_file():
        return JSONResponse({"error": "upload the backup file first"}, status_code=400)
    data, _ = _paths(svc)

    def run():
        with open(src, "rb") as f:
            return stage(f, str(body.get("passphrase") or ""), data)
    try:
        s = await asyncio.to_thread(run)
    except BackupError as e:
        return _err(e)
    src.unlink(missing_ok=True)
    svc.db.log_event("warning", "backup", f"a restore from the backup made {s['created']} on "
                     f"{s['host']} is staged: applied at the next router start")
    return {"ok": True, "staged": s}


@router.delete(A + "/restore")
async def cancel_restore(request: Request):
    svc = _svc(request)
    data, _ = _paths(svc)
    shutil.rmtree(pending_dir(data), ignore_errors=True)
    _upload_path(svc).unlink(missing_ok=True)
    return {"ok": True}


# -- CLI ---------------------------------------------------------------------------------

def _read_passphrase(prompt: str) -> str:
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\r\n")


def main(argv: Optional[list] = None) -> int:
    import argparse
    from .config import config_path, data_dir
    ap = argparse.ArgumentParser(prog="python -m foundry_router.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").add_argument("file")
    c = sub.add_parser("create")
    c.add_argument("file")
    c.add_argument("--files", action="store_true", help="also media and training files")
    r = sub.add_parser("restore", help="stage a restore; applied when the router starts")
    r.add_argument("file")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "info":
            with open(a.file, "rb") as f:
                print(json.dumps(info(f), indent=1))
            return 0
        if a.cmd == "create":
            pw = _read_passphrase("backup passphrase: ")
            fd = os.open(a.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                m = create(out, pw, data_dir(), config_path().parent, include_files=a.files)
            print(f"backup written: {a.file} ({len(m['files'])} files)")
            return 0
        pw = _read_passphrase("backup passphrase: ")
        with open(a.file, "rb") as f:
            s = stage(f, pw, data_dir())
        print(f"restore staged: the backup made {s['created']} on {s['host']} "
              f"(router v{s['router_version']}, {s['files']} files). "
              "It is applied when the router starts.")
        return 0
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

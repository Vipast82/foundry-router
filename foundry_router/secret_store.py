"""Central secret store: per-game (project) and shared secrets for Studio slots and
other Foundry machines (Access → Secrets, secrets_api.py).

Values are write-only. They can be set, rotated, cleared and pulled by a client key
with the `secrets` scope, but never shown back in the UI or the admin API.

At rest
  A separate SQLite file (<data>/secrets.sqlite, directory 0700, file 0600). It is not
  the main DB, so DB dumps, diagnostics and export bundles never contain it. Each value
  is AES-256-GCM encrypted under the master key, with associated data
  `group|name|key_id`, so a ciphertext copied to another row does not decrypt.

Master key (Victor 2026-10-31: a passphrase)
  Derived from a passphrase with scrypt. The passphrase and the key are never written
  anywhere. Only the salt, the KDF parameters and a verifier (a known value encrypted
  under the key) are stored. After a router restart the store is LOCKED until someone
  unlocks it on the Secrets page. While it is locked, pulls answer 503 and the slots
  keep the files they pulled last.

Audit
  `secret_pulls` records every pull (time, client, address, user agent, project, slot,
  names served, outcome) and never a value. It is pruned after
  `secrets.pull_log_days`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as _rand
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

SHARED = "shared"
GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_VALUE = 16384
MIN_PASSPHRASE = 12
# scrypt: 32 MiB, ~0.1 s per try on the router host
KDF = {"alg": "scrypt", "n": 2 ** 15, "r": 8, "p": 1}
VERIFIER = b"foundry-secret-store-v1"
UNLOCK_BACKOFF_AFTER = 5          # failed unlocks before each further try waits
UNLOCK_BACKOFF_S = 30


class SecretStoreError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _aead():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:                                     # pragma: no cover
        raise SecretStoreError("the router image lacks the 'cryptography' package "
                               "(rebuild it from requirements.txt)", "unavailable", 503) from e
    return AESGCM


def _derive(passphrase: str, salt: bytes, kdf: dict) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=int(kdf["n"]),
                          r=int(kdf["r"]), p=int(kdf["p"]), dklen=32, maxmem=128 * 1024 * 1024)


def check_group(g: str) -> str:
    g = str(g or "").strip()
    if not GROUP_RE.match(g):
        raise SecretStoreError("group: 'shared' or a project id (lower-case letters, digits, "
                               "_ and -)", "invalid_group")
    return g


def check_name(n: str) -> str:
    n = str(n or "").strip()
    if not NAME_RE.match(n):
        raise SecretStoreError("name: UPPER_SNAKE_CASE (letters, digits, _), starting with "
                               "a letter", "invalid_name")
    return n


def check_file(f: str) -> str:
    f = str(f or "").strip()
    if not FILE_RE.match(f) or ".." in f:
        raise SecretStoreError("file: a plain file name (letters, digits, . _ -), no folders",
                               "invalid_file")
    return f


def default_file(name: str) -> str:
    return name.lower() + ".txt"


def _now() -> float:
    return time.time()


class SecretStore:
    def __init__(self, path: Path, clock=_now):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._key: Optional[bytes] = None
        self._key_id = ""
        self._failed = 0
        self._last_fail = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets (
                grp TEXT NOT NULL, name TEXT NOT NULL, file TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                ciphertext BLOB, nonce BLOB, key_id TEXT,
                created_at REAL NOT NULL, set_at REAL, set_by TEXT,
                last_pulled_at REAL, last_pulled_by TEXT, last_pulled_addr TEXT,
                PRIMARY KEY (grp, name));
            CREATE TABLE IF NOT EXISTS secret_pulls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                client TEXT NOT NULL, addr TEXT, agent TEXT, transport TEXT,
                project TEXT, slot TEXT, names TEXT, outcome TEXT NOT NULL, detail TEXT);
            CREATE INDEX IF NOT EXISTS secret_pulls_ts ON secret_pulls (ts);
            CREATE INDEX IF NOT EXISTS secret_pulls_client ON secret_pulls (client);
        """)
        if new and os.name != "nt":
            try:
                os.chmod(self.path.parent, 0o700)
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # ------------------------------------------------------------------ meta

    def _meta(self, k: str) -> Optional[str]:
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def _set_meta(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE "
                          "SET v=excluded.v", (k, v))

    def state(self) -> str:
        """uninitialized (no passphrase set yet) | locked | unlocked."""
        with self._lock:
            if not self._meta("verifier"):
                return "uninitialized"
            return "unlocked" if self._key is not None else "locked"

    def status(self) -> dict:
        with self._lock:
            st = self.state()
            return {"state": st, "initialized_at": float(self._meta("initialized_at") or 0) or None,
                    "passphrase_changed_at": float(self._meta("passphrase_changed_at") or 0)
                    or None,
                    "failed_unlocks": self._failed, "min_passphrase": MIN_PASSPHRASE}

    # ------------------------------------------------------------------ passphrase

    def _check_passphrase(self, p) -> str:
        p = str(p or "")
        if len(p) < MIN_PASSPHRASE:
            raise SecretStoreError(f"the passphrase needs at least {MIN_PASSPHRASE} characters",
                                   "weak_passphrase")
        return p

    def _make_verifier(self, key: bytes, key_id: str) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        return nonce, _aead()(key).encrypt(nonce, VERIFIER, f"verify|{key_id}".encode())

    def initialize(self, passphrase: str, actor: str = "") -> dict:
        p = self._check_passphrase(passphrase)
        with self._lock:
            if self._meta("verifier"):
                raise SecretStoreError("the store already has a passphrase (unlock it, or use "
                                       "Change passphrase)", "already_initialized", 409)
            salt = os.urandom(16)
            key = _derive(p, salt, KDF)
            key_id = _rand.token_hex(4)
            nonce, ver = self._make_verifier(key, key_id)
            self.conn.execute("BEGIN")
            try:
                self._set_meta("kdf", json.dumps(KDF))
                self._set_meta("salt", salt.hex())
                self._set_meta("key_id", key_id)
                self._set_meta("verifier", (nonce + ver).hex())
                self._set_meta("initialized_at", str(self.clock()))
                self._set_meta("initialized_by", str(actor or "")[:80])
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            self._key, self._key_id, self._failed = key, key_id, 0
        return self.status()

    def _try_key(self, passphrase: str) -> tuple[bytes, str]:
        kdf = json.loads(self._meta("kdf") or "{}") or KDF
        salt = bytes.fromhex(self._meta("salt") or "")
        key_id = self._meta("key_id") or ""
        blob = bytes.fromhex(self._meta("verifier") or "")
        key = _derive(str(passphrase or ""), salt, kdf)
        try:
            ok = _aead()(key).decrypt(blob[:12], blob[12:], f"verify|{key_id}".encode()) == VERIFIER
        except Exception:                                        # noqa: BLE001 (InvalidTag)
            ok = False
        if not ok:
            raise SecretStoreError("wrong passphrase", "wrong_passphrase", 403)
        return key, key_id

    def _backoff(self) -> None:
        if self._failed >= UNLOCK_BACKOFF_AFTER:
            wait = UNLOCK_BACKOFF_S - (self.clock() - self._last_fail)
            if wait > 0:
                raise SecretStoreError(f"too many wrong passphrases: try again in {int(wait) + 1} s",
                                       "unlock_backoff", 429)

    def unlock(self, passphrase: str) -> dict:
        with self._lock:
            if not self._meta("verifier"):
                raise SecretStoreError("set a passphrase first", "uninitialized", 409)
            if self._key is not None:
                return self.status()
            self._backoff()
            try:
                self._key, self._key_id = self._try_key(passphrase)
            except SecretStoreError:
                self._failed += 1
                self._last_fail = self.clock()
                raise
            self._failed = 0
        return self.status()

    def lock(self) -> dict:
        with self._lock:
            self._key = None
        return self.status()

    def change_passphrase(self, old: str, new: str, actor: str = "") -> dict:
        """Re-encrypt every value under a key from the new passphrase (one transaction)."""
        n = self._check_passphrase(new)
        with self._lock:
            if not self._meta("verifier"):
                raise SecretStoreError("set a passphrase first", "uninitialized", 409)
            self._backoff()
            try:
                old_key, old_id = self._try_key(old)
            except SecretStoreError:
                self._failed += 1
                self._last_fail = self.clock()
                raise
            salt = os.urandom(16)
            new_key = _derive(n, salt, KDF)
            new_id = _rand.token_hex(4)
            A = _aead()
            rows = self.conn.execute("SELECT grp, name, ciphertext, nonce, key_id FROM secrets "
                                     "WHERE ciphertext IS NOT NULL").fetchall()
            self.conn.execute("BEGIN")
            try:
                for r in rows:
                    plain = A(old_key).decrypt(r["nonce"], r["ciphertext"],
                                               self._aad(r["grp"], r["name"], r["key_id"]))
                    nonce = os.urandom(12)
                    ct = A(new_key).encrypt(nonce, plain, self._aad(r["grp"], r["name"], new_id))
                    self.conn.execute("UPDATE secrets SET ciphertext=?, nonce=?, key_id=? "
                                      "WHERE grp=? AND name=?", (ct, nonce, new_id, r["grp"],
                                                                 r["name"]))
                vn, ver = self._make_verifier(new_key, new_id)
                self._set_meta("kdf", json.dumps(KDF))
                self._set_meta("salt", salt.hex())
                self._set_meta("key_id", new_id)
                self._set_meta("verifier", (vn + ver).hex())
                self._set_meta("passphrase_changed_at", str(self.clock()))
                self._set_meta("passphrase_changed_by", str(actor or "")[:80])
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            del old_id
            self._key, self._key_id, self._failed = new_key, new_id, 0
        return self.status()

    # ------------------------------------------------------------------ values

    @staticmethod
    def _aad(group: str, name: str, key_id: str) -> bytes:
        return f"{group}|{name}|{key_id}".encode()

    def _need_key(self) -> bytes:
        if self._key is None:
            st = self.state()
            raise SecretStoreError(
                "the secret store is locked: unlock it on Access → Secrets" if st == "locked"
                else "the secret store has no passphrase yet: set one on Access → Secrets",
                st, 503)
        return self._key

    def _row(self, group: str, name: str):
        return self.conn.execute("SELECT * FROM secrets WHERE grp=? AND name=?",
                                 (group, name)).fetchone()

    def define(self, group: str, name: str, file: str = "", description: str = "") -> dict:
        """Add a secret's entry (no value yet), or update its file/description."""
        g, n = check_group(group), check_name(name)
        f = check_file(file) if file else default_file(n)
        with self._lock:
            if self._row(g, n):
                self.conn.execute("UPDATE secrets SET file=?, description=? WHERE grp=? AND "
                                  "name=?", (f, str(description or "")[:300], g, n))
            else:
                self._file_free(g, n, f)
                self.conn.execute("INSERT INTO secrets (grp, name, file, description, "
                                  "created_at) VALUES (?, ?, ?, ?, ?)",
                                  (g, n, f, str(description or "")[:300], self.clock()))
        return self.meta(g, n)

    def _file_free(self, g: str, n: str, f: str) -> None:
        clash = self.conn.execute("SELECT name FROM secrets WHERE grp=? AND file=? AND name<>?",
                                  (g, f, n)).fetchone()
        if clash:
            raise SecretStoreError(f"{clash['name']} in {g} already uses file {f}",
                                   "file_taken", 409)

    def set_value(self, group: str, name: str, value: str, actor: str = "") -> dict:
        g, n = check_group(group), check_name(name)
        if not isinstance(value, str):
            raise SecretStoreError("value must be text", "invalid_value")
        v = value.rstrip("\r\n")
        if not v:
            raise SecretStoreError("empty value (use Clear to remove a value)", "invalid_value")
        if len(v.encode("utf-8")) > MAX_VALUE:
            raise SecretStoreError(f"value is over {MAX_VALUE} bytes", "invalid_value")
        with self._lock:
            key = self._need_key()
            if not self._row(g, n):
                self.define(g, n)
            nonce = os.urandom(12)
            ct = _aead()(key).encrypt(nonce, v.encode("utf-8"), self._aad(g, n, self._key_id))
            self.conn.execute("UPDATE secrets SET ciphertext=?, nonce=?, key_id=?, set_at=?, "
                              "set_by=? WHERE grp=? AND name=?",
                              (ct, nonce, self._key_id, self.clock(), str(actor or "")[:80], g, n))
        return self.meta(g, n)

    def clear(self, group: str, name: str, actor: str = "") -> dict:
        g, n = check_group(group), check_name(name)
        with self._lock:
            if not self._row(g, n):
                raise SecretStoreError(f"no secret {g}/{n}", "not_found", 404)
            self.conn.execute("UPDATE secrets SET ciphertext=NULL, nonce=NULL, key_id=NULL, "
                              "set_at=?, set_by=? WHERE grp=? AND name=?",
                              (self.clock(), str(actor or "")[:80], g, n))
        return self.meta(g, n)

    def delete(self, group: str, name: str) -> None:
        g, n = check_group(group), check_name(name)
        with self._lock:
            if not self._row(g, n):
                raise SecretStoreError(f"no secret {g}/{n}", "not_found", 404)
            self.conn.execute("DELETE FROM secrets WHERE grp=? AND name=?", (g, n))

    @staticmethod
    def _view(r) -> dict:
        return {"group": r["grp"], "name": r["name"], "file": r["file"],
                "description": r["description"], "set": r["ciphertext"] is not None,
                "created_at": r["created_at"], "set_at": r["set_at"], "set_by": r["set_by"],
                "last_pulled_at": r["last_pulled_at"], "last_pulled_by": r["last_pulled_by"],
                "last_pulled_addr": r["last_pulled_addr"]}

    def meta(self, group: str, name: str) -> dict:
        with self._lock:
            r = self._row(group, name)
        if not r:
            raise SecretStoreError(f"no secret {group}/{name}", "not_found", 404)
        return self._view(r)

    def list(self) -> list[dict]:
        """Metadata only. There is no method that returns a value to the admin UI."""
        with self._lock:
            return [self._view(r) for r in self.conn.execute(
                "SELECT * FROM secrets ORDER BY grp <> 'shared', grp, name")]

    def groups(self) -> list[str]:
        with self._lock:
            return [r["grp"] for r in self.conn.execute(
                "SELECT DISTINCT grp FROM secrets ORDER BY 1")]

    def bundle(self, groups: list[str], names: Optional[list[str]] = None) -> list[dict]:
        """The set values of these groups (later groups win on a name clash), for a pull.
        [{group, name, file, value, set_at}]. Raises 503 while locked."""
        with self._lock:
            key = self._need_key()
            A = _aead()(key)
            out: dict[str, dict] = {}
            for g in groups:
                for r in self.conn.execute("SELECT * FROM secrets WHERE grp=? AND ciphertext IS "
                                           "NOT NULL ORDER BY name", (g,)):
                    if names and r["name"] not in names:
                        continue
                    v = A.decrypt(r["nonce"], r["ciphertext"],
                                  self._aad(r["grp"], r["name"], r["key_id"])).decode("utf-8")
                    out[r["name"]] = {"group": r["grp"], "name": r["name"], "file": r["file"],
                                      "value": v, "set_at": r["set_at"]}
            return [out[k] for k in sorted(out)]

    def known_values(self) -> set[str]:
        """Every stored value while unlocked, so diagnostic text can scrub them
        (configops.secret_values). Empty while locked."""
        if self._key is None:
            return set()
        try:
            groups = self.groups()
            return {s["value"] for s in self.bundle(groups) if len(s["value"]) >= 6}
        except SecretStoreError:
            return set()

    # ------------------------------------------------------------------ audit

    def record_pull(self, client: str, addr: str, agent: str, transport: str, project: str,
                    slot: str, served: list[dict], outcome: str, detail: str = "") -> None:
        now = self.clock()
        names = [f"{s['group']}/{s['name']}" for s in served]
        with self._lock:
            self.conn.execute(
                "INSERT INTO secret_pulls (ts, client, addr, agent, transport, project, slot, "
                "names, outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, client[:80], addr[:80], agent[:160], transport[:10], project[:40],
                 slot[:40], json.dumps(names), outcome[:20], detail[:300]))
            if outcome in ("ok", "unchanged"):
                for s in served:
                    self.conn.execute("UPDATE secrets SET last_pulled_at=?, last_pulled_by=?, "
                                      "last_pulled_addr=? WHERE grp=? AND name=?",
                                      (now, client[:80], addr[:80], s["group"], s["name"]))

    def has_pulled(self, client: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM secret_pulls WHERE client=? AND outcome IN "
                                     "('ok', 'unchanged') LIMIT 1", (client,)).fetchone() is not None

    def pulls(self, limit: int = 100, client: str = "", project: str = "") -> list[dict]:
        q, args = "SELECT * FROM secret_pulls WHERE 1=1", []
        if client:
            q += " AND client=?"
            args.append(client)
        if project:
            q += " AND project=?"
            args.append(project)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        out = []
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        for r in rows:
            d = dict(r)
            d["names"] = json.loads(d.get("names") or "[]")
            out.append(d)
        return out

    def prune(self, days: int) -> int:
        if not days or days <= 0:
            return 0
        cut = self.clock() - days * 86400
        with self._lock:
            return self.conn.execute("DELETE FROM secret_pulls WHERE ts < ?", (cut,)).rowcount

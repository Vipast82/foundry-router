"""Web-managed client keys and client activity.

Keys made in the web UI are random (`fnd_` + 43 URL-safe characters), shown
once, and stored only as `sha256:<hex>` (plus a short hint for display), so
config.yaml, config history and export bundles never contain them. Keys
written into config.yaml by hand (`key:`, usually a ${VAR}) keep working.

Activity is kept in memory (reset on restart) and never holds a key:
  seen       per client: last time, address, user agent, requests since start
  rejected   keys that matched no client, by fingerprint (first 10 hex of the
             key's SHA-256): count, last path, address, user agent — so the
             operator can tell which program is sending which wrong key.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

PREFIX = "fnd_"
MAX_REJECTED = 50


def generate_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def hint(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def matches(key: str, client) -> bool:
    """Constant-time match against a client's plain or hashed key."""
    if getattr(client, "key", None):
        if hmac.compare_digest(key.encode(), str(client.key).encode()):
            return True
    h = getattr(client, "key_hash", None)
    if h:
        return hmac.compare_digest(hash_key(key).encode(), str(h).encode())
    return False


def key_kind(client) -> str:
    """managed (made in the UI) | config (key: in config.yaml) | none."""
    if getattr(client, "key_hash", None):
        return "managed"
    if getattr(client, "key", None):
        return "config"
    return "none"


class Activity:
    def __init__(self):
        self._lock = threading.Lock()
        self.seen: dict[str, dict] = {}
        self.rejected: dict[str, dict] = {}

    def client_seen(self, name: str, addr: str, agent: str) -> None:
        with self._lock:
            s = self.seen.setdefault(name, {"count": 0})
            s.update(last_ts=time.time(), addr=addr, agent=agent[:160])
            s["count"] += 1

    def key_rejected(self, key: str, path: str, addr: str, agent: str,
                     outcome: str) -> None:
        fp = fingerprint(key)
        with self._lock:
            r = self.rejected.setdefault(fp, {"fingerprint": fp, "count": 0,
                                              "first_ts": time.time(),
                                              "looks_like": _looks_like(key)})
            r.update(last_ts=time.time(), path=path[:120], addr=addr, agent=agent[:160],
                     outcome=outcome)
            r["count"] += 1
            if len(self.rejected) > MAX_REJECTED:
                oldest = min(self.rejected.values(), key=lambda x: x["last_ts"])
                self.rejected.pop(oldest["fingerprint"], None)

    def forget_rejected(self, fp: Optional[str] = None) -> None:
        with self._lock:
            if fp:
                self.rejected.pop(fp, None)
            else:
                self.rejected.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {"seen": {k: dict(v) for k, v in self.seen.items()},
                    "rejected": sorted((dict(v) for v in self.rejected.values()),
                                       key=lambda x: -x["last_ts"])}


def _looks_like(key: str) -> str:
    """What kind of key a client sent, without revealing it."""
    if key.startswith(PREFIX):
        return "a Foundry key (deleted, rotated or mistyped)"
    if key.startswith("sk-ant-"):
        return "an Anthropic API key"
    if key.startswith("sk-"):
        return "an OpenAI-style key"
    if len(key) < 12:
        return f"a short placeholder ({len(key)} characters)"
    return f"an unrecognised key ({len(key)} characters)"


activity = Activity()

#!/usr/bin/env python3
"""Pull this slot's secrets and settings from the Foundry router (Setup → Secrets).

    slot-secrets            pull now (container start, and slot-checkout before every gate run)
    slot-secrets --status   what is in place (names and dates only)

Reads (none of these is a secret):
    FOUNDRY_URL               http://192.168.0.112:11435, or https://...:11436 with the HTTPS
                              listener (then FOUNDRY_TLS_FINGERPRINT is required)
    FOUNDRY_TLS_FINGERPRINT   the router certificate's SHA-256 (Setup → Secrets), pinned
    FOUNDRY_PROJECT           the game, e.g. animaluprising
    FOUNDRY_SLOT              this slot's id, e.g. slot-1
    FOUNDRY_SLOT_KEY_FILE     the slot's own client key (secrets scope), a mounted file
                              (default /run/slot-key/key)
    FOUNDRY_SECRETS_DIR       where values go (default /run/secrets: a tmpfs, memory only)

Writes each secret to <FOUNDRY_SECRETS_DIR>/<file> (mode 600, written to a temp file and then
renamed). The slot's non-secret settings (place, universe, Rojo's never-sync places) go to
~/.config/foundry-slot/slot.env for entrypoint.sh and studio-reopen.

Values never go on a command line, in the environment, in a log or on stdout. The key goes
only in the Authorization header.
  * router unreachable or the store locked: the files from the last pull stay;
  * 401/403 (key disabled, deleted or out of scope): the secret files are DELETED, so a
    revoked machine loses its access at its next pull;
  * a secret no longer served (cleared, deleted, or no longer allowed): its file is removed.
Exit 0 = in place (pulled, unchanged, or kept after a transient failure),
     2 = not configured, 3 = revoked (files removed), 4 = refused to write to disk.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse

MANIFEST = ".slot-secrets.json"
UA = "foundry-slot-secrets/1"


def log(msg: str) -> None:
    print(f"slot-secrets: {msg}", file=sys.stderr, flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def on_tmpfs(path: str) -> bool:
    """True when `path` is on a tmpfs (memory only). Unknown (no /proc/mounts) = False."""
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            mounts = [ln.split() for ln in f if ln.strip()]
    except OSError:
        return False
    real = os.path.realpath(path)
    best, fs = "", ""
    for m in mounts:
        if len(m) >= 3 and (real == m[1] or real.startswith(m[1].rstrip("/") + "/")) \
                and len(m[1]) > len(best):
            best, fs = m[1], m[2]
    return fs == "tmpfs"


def read_manifest(d: str) -> dict:
    try:
        with open(os.path.join(d, MANIFEST), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def write_private(path: str, data: bytes) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remove_files(d: str, files) -> list:
    gone = []
    for f in files:
        if not f or "/" in f or "\\" in f or f.startswith("."):
            continue
        try:
            os.remove(os.path.join(d, f))
            gone.append(f)
        except FileNotFoundError:
            pass
        except OSError as e:
            log(f"could not remove {f}: {e.strerror}")
    return gone


class PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a self-signed router: no CA check, the certificate's SHA-256 must match."""

    def __init__(self, host, port, fingerprint: str, timeout: float):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, timeout=timeout, context=ctx)
        self.want = fingerprint.replace(":", "").strip().lower()

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b""
        if hashlib.sha256(der).hexdigest() != self.want:
            self.sock.close()
            raise ssl.SSLError("the router's certificate does not match FOUNDRY_TLS_FINGERPRINT")


def fetch(url: str, key: str, project: str, slot: str, etag: str, fingerprint: str,
          timeout: float = 20.0) -> tuple[int, dict, bytes]:
    u = urllib.parse.urlsplit(url.rstrip("/"))
    q = urllib.parse.urlencode({"project": project, "slot": slot})
    path = (u.path or "") + "/v1/foundry/secrets/bundle?" + q
    if u.scheme == "https":
        if not fingerprint:
            raise ValueError("an https FOUNDRY_URL needs FOUNDRY_TLS_FINGERPRINT (Setup → Secrets)")
        conn = PinnedHTTPS(u.hostname, u.port or 443, fingerprint, timeout)
    elif u.scheme == "http":
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    else:
        raise ValueError("FOUNDRY_URL must start with http:// or https://")
    headers = {"Authorization": "Bearer " + key, "User-Agent": UA, "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


def slot_env(config: dict) -> str:
    """slot.env lines from the slot's router settings (only what is set)."""
    out = []
    place, uni = int(config.get("place_id") or 0), int(config.get("universe_id") or 0)
    if place:
        link = f"roblox-studio:1+launchmode:edit+task:EditPlace+placeId:{place}"
        out.append(f"STUDIO_OPEN='{link}{'+universeId:' + str(uni) if uni else ''}'")
    never = [str(int(x)) for x in (config.get("rojo_never_place_ids") or [])
             if str(x).lstrip("-").isdigit()]
    if never:
        out.append(f"ROJO_NEVER_PLACE_IDS='{' '.join(never)}'")
    return "".join(line + "\n" for line in out)


def pull(allow_disk: bool = False) -> int:
    url, project, slot = env("FOUNDRY_URL"), env("FOUNDRY_PROJECT"), env("FOUNDRY_SLOT")
    key_file = env("FOUNDRY_SLOT_KEY_FILE", "/run/slot-key/key")
    d = env("FOUNDRY_SECRETS_DIR", "/run/secrets")
    if not url:
        log("FOUNDRY_URL is not set: not pulling (secrets come from the router, Setup → Secrets)")
        return 2
    try:
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        log(f"no slot key at {key_file}: make one (Access → Clients & Keys, preset slot) and "
            "mount it there; not pulling")
        return 2
    if not key:
        log(f"{key_file} is empty; not pulling")
        return 2
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        log(f"cannot use {d}: {e.strerror}")
        return 4
    if not (allow_disk or env("FOUNDRY_SECRETS_ALLOW_DISK") == "1" or on_tmpfs(d)):
        log(f"{d} is not a tmpfs: refusing to write secrets to disk (mount a tmpfs there)")
        return 4
    man = read_manifest(d)
    have = [f for f in (man.get("files") or []) if os.path.exists(os.path.join(d, f))]
    etag = str(man.get("etag") or "") if have and len(have) == len(man.get("files") or []) else ""
    where = f"{project or 'shared'}{'/' + slot if slot else ''}"
    try:
        status, headers, body = fetch(url, key, project, slot, etag, env("FOUNDRY_TLS_FINGERPRINT"))
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError, socket.timeout) as e:
        log(f"router not reachable ({type(e).__name__}: {e}); keeping the {len(have)} file(s) "
            "from the last pull")
        return 0
    del key
    if status == 304:
        log(f"{where}: unchanged ({len(have)} file(s))")
        return 0
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        data = {}
    if status in (401, 403):
        why = ((data.get("error") or {}).get("message") if isinstance(data.get("error"), dict)
               else data.get("error")) or f"HTTP {status}"
        gone = remove_files(d, man.get("files") or [])
        try:
            os.remove(os.path.join(d, MANIFEST))
        except OSError:
            pass
        log(f"refused ({why}): removed {len(gone)} secret file(s)")
        return 3
    if status != 200:
        why = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) \
            else f"HTTP {status}"
        log(f"{where}: {why}; keeping the {len(have)} file(s) from the last pull")
        return 0
    files, seen = [], []
    for s in data.get("secrets") or []:
        f = str(s.get("file") or "")
        if not f or "/" in f or "\\" in f or f.startswith(".") or not isinstance(s.get("value"), str):
            continue
        write_private(os.path.join(d, f), s["value"].encode("utf-8"))
        files.append(f)
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["set_at"])) if s.get("set_at") else "?"
        seen.append(f"{s.get('name')} (set {when})")
    data.clear()
    stale = remove_files(d, [f for f in (man.get("files") or []) if f not in files])
    write_private(os.path.join(d, MANIFEST), json.dumps(
        {"etag": headers.get("etag", ""), "files": files, "pulled_at": time.time(),
         "project": project, "slot": slot}).encode())
    cfg_dir = os.path.join(os.path.expanduser("~"), ".config", "foundry-slot")
    try:
        os.makedirs(cfg_dir, exist_ok=True)
        cfg = json.loads(body.decode("utf-8")).get("config") or {}
        write_private(os.path.join(cfg_dir, "slot.json"), json.dumps(cfg, indent=1).encode())
        write_private(os.path.join(cfg_dir, "slot.env"), slot_env(cfg).encode())
    except (OSError, ValueError):
        pass
    del body
    log(f"{where}: {len(files)} secret(s) in {d}: {', '.join(seen) or 'none'}"
        + (f"; removed {', '.join(stale)}" if stale else ""))
    return 0


def status() -> int:
    d = env("FOUNDRY_SECRETS_DIR", "/run/secrets")
    man = read_manifest(d)
    if not man:
        print("no pull yet")
        return 0
    when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(man.get("pulled_at") or 0))
    print(f"pulled {when} for {man.get('project') or 'shared'}/{man.get('slot') or '-'}: "
          f"{', '.join(man.get('files') or []) or 'no files'} (tmpfs: {on_tmpfs(d)})")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--status" in args:
        sys.exit(status())
    sys.exit(pull(allow_disk="--allow-disk" in args))

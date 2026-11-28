"""Keep a Windows Studio slot's ssh trust in step with the Foundry router: the Windows twin of
contrib/studio-slot/proxmox/slot-keys.py (install/slot.ps1 runs it once and registers the
scheduled task "Foundry slot keys" that runs it every 5 minutes as SYSTEM).

With the slot's own key (secrets scope) it
  * reports this host's ssh HOST public keys (POST /v1/foundry/secrets/host-keys), so the
    coordinator checks this slot strictly;
  * fetches the workstation keys to allow (GET /v1/foundry/secrets/authorized-keys) and writes
    them into C:/ProgramData/ssh/administrators_authorized_keys as the lines ending in
    " foundry-managed" (an administrator's keys live there: Windows OpenSSH's rule). Lines
    without that mark stay, so a key added by hand is kept. The file keeps its ACL
    (Administrators and SYSTEM only): it is rewritten in place, never replaced.

Settings: <root>/studio-slot/slot.json (router, fingerprint; written by slot.ps1, no secret)
and the key file <root>/studio-slot/slot-key.txt. Router unreachable: nothing changes (exit 0).
The key never goes on a command line or in a log.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import urllib.parse

MARK = " foundry-managed"
AK = "C:/ProgramData/ssh/administrators_authorized_keys"
PUBKEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|"
                       r"sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]{40,}( [^\r\n]{0,200})?$")


def log(msg: str) -> None:
    print(f"slot-keys: {msg}", file=sys.stderr, flush=True)


def connect(router: str, fp: str):
    u = urllib.parse.urlsplit(router)
    if u.scheme != "https":
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=20)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # pinned below instead (a self-signed router cert)
    c = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=20, context=ctx)
    c.connect()
    got = hashlib.sha256(c.sock.getpeercert(binary_form=True)).hexdigest().upper()
    if not fp or fp.replace(":", "").upper() != got:
        c.close()
        raise OSError("the router's certificate does not match the slot's fingerprint")
    return c


def call(c, method: str, path: str, key: str, body=None):
    h = {"Authorization": "Bearer " + key, "User-Agent": "foundry-slot-keys-win/1",
         "Accept": "application/json"}
    if body is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, {}


def host_keys(ssh_dir: str = "C:/ProgramData/ssh") -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(ssh_dir, "ssh_host_*_key.pub"))):
        try:
            with open(p, encoding="utf-8") as f:
                parts = f.read().split()
        except OSError:
            continue
        if len(parts) >= 2:
            out.append(f"{parts[0]} {parts[1]}")
    return out


def merge(text: str, keys: list) -> str:
    """The file with the foundry-managed lines replaced by `keys` (other lines kept)."""
    keep = [ln for ln in text.splitlines() if ln.strip() and not ln.rstrip().endswith(MARK.strip())]
    return "\n".join(keep + [k + MARK for k in keys]) + "\n"


def write_keys(keys: list, path: str = AK) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = ""
    new = merge(old, keys)
    if new == old:
        return False
    with open(path, "r+" if os.path.exists(path) else "w", encoding="utf-8", newline="\n") as f:
        f.seek(0)
        f.write(new)
        f.truncate()
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default="C:/Foundry")
    ap.add_argument("--authorized-keys", default=AK)
    a = ap.parse_args(argv)
    base = os.path.join(a.root, "studio-slot")
    try:
        with open(os.path.join(base, "slot.json"), encoding="utf-8") as f:
            conf = json.load(f)
    except (OSError, ValueError):
        log(f"no {base}/slot.json: run install/slot.ps1 first")
        return 2
    try:
        with open(os.path.join(base, "slot-key.txt"), encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        log("no slot key: nothing to do")
        return 2
    try:
        c = connect(str(conf.get("router") or ""), str(conf.get("fingerprint") or ""))
        hk = host_keys()
        if hk:
            st, data = call(c, "POST", "/v1/foundry/secrets/host-keys", key, {"keys": hk})
            if st != 200:
                log(f"host keys not taken: HTTP {st}")
        st, data = call(c, "GET", "/v1/foundry/secrets/authorized-keys", key)
        c.close()
    except (OSError, ValueError) as e:
        log(f"router not reachable ({e}); nothing changed")
        return 0
    del key
    if st != 200:
        log(f"authorized keys: HTTP {st}; nothing changed")
        return 0
    keys = [str(k).strip() for k in data.get("keys") or [] if PUBKEY_RE.match(str(k).strip())]
    changed = write_keys(keys, a.authorized_keys)
    log(f"{len(hk)} host key(s) reported; {len(keys)} workstation key(s) allowed"
        + (" (updated)" if changed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

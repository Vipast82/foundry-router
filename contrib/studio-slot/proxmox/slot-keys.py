#!/usr/bin/env python3
"""Keep a Studio slot host's ssh trust in step with the Foundry router (v0.162.0).

Runs as root on the slot host: once from setup-host.sh, then every 5 minutes from the
foundry-slot-keys systemd timer. With the slot's own key (secrets scope) it
  * reports this host's ssh HOST public keys (POST /v1/foundry/secrets/host-keys), so the
    coordinator checks this slot strictly and a reinstall or a reused IP is picked up by itself;
  * reports the host's GPU (v0.162.4: present, used by the container, its name), shown on
    Access → Machines;
  * keeps the container's CPU limit at the slot's setting (v0.162.1: `docker update --cpus`,
    live; slot.env for the next start, where the software renderer's threads follow it);
  * fetches the workstation keys to allow (GET /v1/foundry/secrets/authorized-keys: every main PC
    that registered itself, plus the extra keys on Access → Machines) and writes them into a
    Foundry-managed block of /home/studio/.ssh/authorized_keys. Lines outside the block are left
    alone, so a key you added by hand stays.

Settings: /srv/studio-slot/slot.env (FOUNDRY_URL, FOUNDRY_TLS_FINGERPRINT) and the key file
/srv/studio-slot/slot-key/slot-key.txt; --router / --fingerprint / --key-file override them.
Router unreachable: nothing changes (exit 0). The key never goes on a command line or in a log.
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

ROOT = "/srv/studio-slot"
START, END = "# >>> foundry-managed keys (slot-keys.py) >>>", "# <<< foundry-managed keys <<<"
PUBKEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|"
                       r"sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]{40,}( [^\r\n]{0,200})?$")


def log(msg: str) -> None:
    print(f"slot-keys: {msg}", file=sys.stderr, flush=True)


def env_file(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


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
        raise OSError("the router's certificate does not match FOUNDRY_TLS_FINGERPRINT")
    return c


def call(c, method: str, path: str, key: str, body=None):
    h = {"Authorization": "Bearer " + key, "User-Agent": "foundry-slot-keys/1",
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


def host_keys() -> list:
    out = []
    for p in sorted(glob.glob("/etc/ssh/ssh_host_*_key.pub")):
        try:
            with open(p, encoding="utf-8") as f:
                parts = f.read().split()
        except OSError:
            continue
        if len(parts) >= 2:
            out.append(f"{parts[0]} {parts[1]}")
    return out


def gpu_report(container: str) -> dict:
    """{device, in_use, name}: does this host have a GPU render node, does the slot's container
    have it (docker-compose.gpu.yml), and the GPU's name as Vulkan sees it inside (v0.162.4)."""
    import subprocess
    out = {"device": os.path.exists("/dev/dri/renderD128"), "in_use": False, "name": ""}
    if not out["device"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", container):
        return out
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{json .HostConfig.Devices}}", container],
                           capture_output=True, text=True, timeout=30)
        out["in_use"] = r.returncode == 0 and "/dev/dri" in r.stdout
        if out["in_use"]:
            r = subprocess.run(["docker", "exec", container, "vulkaninfo", "--summary"],
                               capture_output=True, text=True, timeout=30)
            names = re.findall(r"deviceName\s*=\s*(.+)", r.stdout or "")
            out["name"] = next((n.strip() for n in names if "llvmpipe" not in n), "")[:80]
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def merge_block(text: str, keys: list) -> str:
    """authorized_keys with the Foundry block replaced by `keys` (lines outside it kept)."""
    out, inside = [], False
    for line in text.splitlines():
        if line.strip() == START:
            inside = True
            continue
        if line.strip() == END:
            inside = False
            continue
        if not inside:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    block = [START] + keys + [END]
    return "\n".join(out + ([""] if out else []) + block) + "\n"


def write_authorized(keys: list, user: str) -> bool:
    """Write the managed block; True when the file changed."""
    import pwd                                   # the slot host is Linux
    pw = pwd.getpwnam(user)
    d = os.path.join(pw.pw_dir, ".ssh")
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chown(d, pw.pw_uid, pw.pw_gid)
    path = os.path.join(d, "authorized_keys")
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        old = ""
    new = merge_block(old, keys)
    if new == old:
        return False
    tmp = path + ".foundry-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new)
    os.chown(tmp, pw.pw_uid, pw.pw_gid)
    os.replace(tmp, path)
    return True


def effective_cpus(limit: int, nproc: int) -> int:
    """The slot's CPU limit: its setting, or 0 = a third of the host's cores (at least 4);
    never more than the host has (setup-host.sh uses the same rule)."""
    n = int(limit or 0)
    if n <= 0:
        n = max(4, nproc // 3)
    return max(1, min(n, nproc))


def set_env_line(text: str, key: str, value: str) -> str:
    """slot.env with KEY=value set (added when missing)."""
    lines, done = text.splitlines(), False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i], done = f"{key}={value}", True
    if not done:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def apply_cpu_limit(host: dict) -> str:
    """Keep the running container's CPU limit (docker update, live) and slot.env (for the
    next container start: SLOT_CPUS, and the renderer's SLOT_RENDER_THREADS) at the router's
    setting. Returns what changed ('' = nothing)."""
    import subprocess
    want = effective_cpus(int(host.get("cpu_limit") or 0), os.cpu_count() or 4)
    name = str(host.get("container") or "studio-slot")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        return ""
    out = []
    r = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", name],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0 and r.stdout.strip().isdigit() and \
            int(r.stdout.strip()) != want * 1_000_000_000:
        u = subprocess.run(["docker", "update", "--cpus", str(want), name],
                           capture_output=True, text=True, timeout=60)
        out.append(f"container CPU limit {want}" if u.returncode == 0
                   else f"docker update failed: {(u.stderr or u.stdout).strip()[-150:]}")
    p = f"{ROOT}/slot.env"
    try:
        with open(p, encoding="utf-8") as f:
            old = f.read()
    except OSError:
        return "; ".join(out)
    new = set_env_line(set_env_line(old, "SLOT_CPUS", str(want)), "SLOT_RENDER_THREADS", str(want))
    if new != old:
        st = os.stat(p)
        tmp = p + ".foundry-tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, p)
        out.append(f"slot.env SLOT_CPUS/SLOT_RENDER_THREADS={want} (renderer threads at the next "
                   "container start)")
    return "; ".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--router", default="")
    ap.add_argument("--fingerprint", default="")
    ap.add_argument("--key-file", default=f"{ROOT}/slot-key/slot-key.txt")
    ap.add_argument("--user", default="studio")
    a = ap.parse_args(argv)
    conf = env_file(f"{ROOT}/slot.env")
    router = a.router or conf.get("FOUNDRY_URL", "")
    fp = a.fingerprint or conf.get("FOUNDRY_TLS_FINGERPRINT", "")
    if not router:
        log(f"no router address (FOUNDRY_URL in {ROOT}/slot.env): nothing to do")
        return 2
    try:
        with open(a.key_file, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        log(f"no slot key at {a.key_file}: nothing to do")
        return 2
    try:
        c = connect(router, fp)
        hk = host_keys()
        if hk:
            gpu = gpu_report(conf.get("SLOT_CONTAINER") or "studio-slot")
            st, data = call(c, "POST", "/v1/foundry/secrets/host-keys", key,
                            {"keys": hk, "gpu": gpu})
            if st != 200:
                log(f"host keys not taken: HTTP {st} {json.dumps(data)[:200]}")
        st, data = call(c, "GET", "/v1/foundry/secrets/authorized-keys", key)
        c.close()
    except OSError as e:
        log(f"router not reachable ({e}); nothing changed")
        return 0
    del key
    if st != 200:
        log(f"authorized keys: HTTP {st}; nothing changed")
        return 0
    keys = [str(k).strip() for k in data.get("keys") or [] if PUBKEY_RE.match(str(k).strip())]
    changed = write_authorized(keys, a.user)
    log(f"{len(hk)} host key(s) reported; {len(keys)} workstation key(s) allowed"
        + (" (updated)" if changed else ""))
    if isinstance(data.get("host"), dict):            # v0.162.1: the slot's CPU limit, live
        try:
            done = apply_cpu_limit(data["host"])
        except (OSError, ValueError) as e:
            done = f"CPU limit not applied: {e}"
        if done:
            log(done)
    return 0


if __name__ == "__main__":
    sys.exit(main())

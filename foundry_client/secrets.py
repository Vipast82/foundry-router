"""Secrets in the OS keyring (Windows Credential Manager, Linux Secret Service /
KWallet), or in a mode-600 file for a machine without one (a Linux LXC with no desktop:
LINUX_WORKSTATION_PLAN D-L5; client.secret_store = auto | keyring | file). Values are
write-only from the UI: the API reports which are set, never their values. They reach a
component only through its environment at start."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Optional

SERVICE = "foundry-client"
KNOWN = {
    "KILO_SERVER_PASSWORD": "Password of the local Kilo server (port 4096)",
    "FOUNDRY_COORDINATOR_KEY": "Foundry coordinator key (inference + orchestration scopes)",
    "FOUNDRY_KEY": "Foundry key (media scope) for the media adapter Kilo starts; add FOUNDRY_KEY to the Kilo component's secrets so Kilo passes it on",
    "FOUNDRY_CLIENT_KEY": "Router key for this client's relay (operator scope): the router then "
                          "sees the stack and the operator chat can restart Kilo",
}


STORES = ("auto", "keyring", "file")
_mode = {"store": os.environ.get("FOUNDRY_CLIENT_SECRET_STORE", "auto"), "file": ""}
_flock = threading.Lock()


def configure(cfg: dict) -> None:
    """From client.toml: client.secret_store and client.secrets_file."""
    c = (cfg or {}).get("client") or {}
    store = str(c.get("secret_store") or "auto")
    _mode["store"] = store if store in STORES else "auto"
    _mode["file"] = str(c.get("secrets_file") or "")


def secrets_file() -> str:
    if _mode["file"]:
        return os.path.expanduser(_mode["file"])
    from .config import config_dir
    return str(config_dir() / "secrets.json")


class _FileKeyring:
    """keyring's three calls over one JSON file, mode 600 (only this user can read it)."""

    @staticmethod
    def _read() -> dict:
        try:
            with open(secrets_file(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(d: dict) -> None:
        path = secrets_file()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def get_password(self, service: str, name: str) -> Optional[str]:
        return (self._read().get(service) or {}).get(name)

    def set_password(self, service: str, name: str, value: str) -> None:
        with _flock:
            d = self._read()
            d.setdefault(service, {})[name] = value
            self._write(d)

    def delete_password(self, service: str, name: str) -> None:
        with _flock:
            d = self._read()
            if (d.get(service) or {}).pop(name, None) is not None:
                self._write(d)


def _os_keyring():
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None                          # no backend on this machine
        return keyring
    except Exception:                                            # noqa: BLE001
        return None


def store_in_use() -> str:
    """'keyring' or 'file': where values go now."""
    if _mode["store"] == "file":
        return "file"
    if _mode["store"] == "keyring":
        return "keyring"
    return "keyring" if _os_keyring() is not None else "file"


def _kr():
    if store_in_use() == "file":
        return _FileKeyring()
    return _os_keyring()


def get(name: str) -> Optional[str]:
    env = os.environ.get("FOUNDRY_CLIENT_SECRET_" + name)        # tests / CI
    if env:
        return env
    kr = _kr()
    return kr.get_password(SERVICE, name) if kr else None


def set_secret(name: str, value: str) -> None:
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("secret names are letters, digits and _")
    kr = _kr()
    if kr is None:
        raise RuntimeError("no OS keyring is available on this machine")
    if value:
        kr.set_password(SERVICE, name, value)
    else:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:                                        # noqa: BLE001
            pass


def status(names) -> dict:
    return {n: bool(get(n)) for n in sorted(set(names) | set(KNOWN))}


def env_for(names) -> dict:
    """{name: value} for the names that are set (missing ones are left out)."""
    out = {}
    for n in names or []:
        v = get(n)
        if v:
            out[n] = v
    return out


def import_windows_clixml(path: str, names=(), single: str = "") -> list[str]:
    """One-time migration from the old workstation scripts: read SecureStrings from a
    PowerShell Export-Clixml file (DPAPI, current user) and store them in the keyring.
    `names`: keys of a saved hashtable; `single`: the name for a file that holds one
    SecureString. Returns the names imported. Values never reach a console or log."""
    if sys.platform != "win32" or not os.path.isfile(path):
        return []
    jobs = [(n, "$v=$s[$env:FC_NAME]") for n in names] + ([(single, "$v=$s")] if single else [])
    done = []
    for n, pick in jobs:
        ps = ("$ErrorActionPreference='Stop'; $s=Import-Clixml -LiteralPath $env:FC_PATH; "
              + pick + "; if ($v -is [securestring]) { "
              "[Console]::Out.Write([Net.NetworkCredential]::new('', $v).Password) }")
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "FC_PATH": path, "FC_NAME": n})
        val = (r.stdout or "").strip()
        if r.returncode == 0 and val:
            set_secret(n, val)
            done.append(n)
    return done


def migrate_legacy(state_dir: str) -> list[str]:
    """Import the credentials the old PowerShell launchers saved, if present."""
    out = import_windows_clixml(os.path.join(state_dir, "workstation-credentials.xml"),
                                names=("KILO_SERVER_PASSWORD", "FOUNDRY_COORDINATOR_KEY"))
    out += import_windows_clixml(os.path.join(state_dir, "media-credential.xml"),
                                 single="FOUNDRY_KEY")
    return out

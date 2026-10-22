"""Secrets in the OS keyring (Windows Credential Manager, Linux Secret Service /
KWallet). Values are write-only from the UI: the API reports which are set, never
their values. They reach a component only through its environment at start."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

SERVICE = "foundry-client"
KNOWN = {
    "KILO_SERVER_PASSWORD": "Password of the local Kilo server (port 4096)",
    "FOUNDRY_COORDINATOR_KEY": "Foundry coordinator key (inference + orchestration scopes)",
    "FOUNDRY_KEY": "Foundry key used by the media adapter",
}


def _kr():
    try:
        import keyring
        return keyring
    except Exception:                                            # noqa: BLE001
        return None


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

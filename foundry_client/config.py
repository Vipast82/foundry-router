"""Client settings (client.toml). No secrets here: they live in the OS keyring
(secrets.py) and are injected into a component's environment when it starts."""
from __future__ import annotations

import copy
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from . import APP_ID

COMPONENTS = ("kilo", "coordinator", "supervisor", "media", "rojo")

DEFAULTS: dict[str, Any] = {
    "client": {
        "backend_url": "http://127.0.0.1:11435",      # the Foundry router (backend host)
        "state_dir": "~/.foundry-coordinator",         # coordinator state, logs
        "api_port": 47811,                              # local settings page / API (127.0.0.1)
        "open_settings_on_start": False,
        # router relay (relay.py): heartbeat + stack actions for the operator chat.
        # router_key names the keyring entry holding the router key (operator scope)
        "relay_enabled": True,
        "router_key": "FOUNDRY_CLIENT_KEY",
    },
    "workspace": {
        "game": "",                                     # e.g. C:/Roblox/AnimalUprising
        "profiles": "",                                 # coordinator profiles dir (projects/)
        "deploy": "",                                   # coordinator release checkout, if any
    },
    "components": {
        "kilo": {"enabled": True, "path": "", "port": 4096, "args": [],
                 "env": {"KILO_DISABLE_EXTERNAL_SKILLS": "true"},
                 "secrets": ["KILO_SERVER_PASSWORD"], "safe_restart": True},
        "coordinator": {"enabled": True, "poll_seconds": 3, "args": [],
                        "env": {"KILO_DISABLE_EXTERNAL_SKILLS": "true"},
                        "secrets": ["KILO_SERVER_PASSWORD", "FOUNDRY_COORDINATOR_KEY"],
                        "safe_restart": True, "after": ["kilo"]},
        "supervisor": {"enabled": True, "args": [], "env": {}, "secrets": [],
                       "after": ["coordinator"]},
        "media": {"enabled": False, "command": [], "cwd": "", "env": {},
                  "secrets": ["FOUNDRY_KEY"]},
        "rojo": {"enabled": False, "path": "", "project": "", "port": 34872, "args": [],
                 "env": {}, "secrets": []},
    },
}


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "FoundryClient"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_ID


def config_path() -> Path:
    return Path(os.environ.get("FOUNDRY_CLIENT_CONFIG") or (config_dir() / "client.toml"))


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(ValueError):
    pass


def validate(cfg: dict) -> list[str]:
    """Problems that stop the stack from starting (empty list = fine)."""
    errs = []
    c = cfg["client"]
    if not str(c.get("backend_url", "")).startswith(("http://", "https://")):
        errs.append("client.backend_url must be an http(s) URL")
    if not isinstance(c.get("api_port"), int) or not 1024 <= c["api_port"] <= 65535:
        errs.append("client.api_port must be a port number (1024-65535)")
    for name, comp in cfg["components"].items():
        if name not in COMPONENTS:
            errs.append(f"unknown component {name!r}")
            continue
        if not isinstance(comp.get("enabled"), bool):
            errs.append(f"components.{name}.enabled must be true or false")
        for k in ("args", "secrets"):
            if k in comp and not (isinstance(comp[k], list) and all(isinstance(x, str) for x in comp[k])):
                errs.append(f"components.{name}.{k} must be a list of strings")
        if "env" in comp and not (isinstance(comp["env"], dict)
                                  and all(isinstance(v, str) for v in comp["env"].values())):
            errs.append(f"components.{name}.env must map names to strings")
        if any(_secretish(k) for k in (comp.get("env") or {})):
            errs.append(f"components.{name}.env holds a secret-looking name; store it as a "
                        f"secret instead (it is kept in the OS keyring)")
    comps = cfg["components"]
    if comps["kilo"]["enabled"] and not cfg["workspace"]["game"]:
        errs.append("workspace.game is required for the Kilo server (its working folder)")
    if comps["coordinator"]["enabled"] and not cfg["workspace"]["profiles"]:
        errs.append("workspace.profiles is required for the coordinator")
    if comps["media"]["enabled"] and not comps["media"]["command"]:
        errs.append("components.media.command is required when the media adapter is enabled")
    return errs


def _secretish(name: str) -> bool:
    n = name.upper()
    return any(w in n for w in ("PASSWORD", "SECRET", "TOKEN", "_KEY", "APIKEY"))


def load(path: Path | None = None) -> dict:
    p = Path(path or config_path())
    raw: dict = {}
    if p.is_file():
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    return _merge(DEFAULTS, raw)


def save(cfg: dict, path: Path | None = None) -> Path:
    errs = validate(cfg)
    if errs:
        raise ConfigError("; ".join(errs))
    p = Path(path or config_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".toml.tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(cfg, f)
    os.replace(tmp, p)
    return p


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path)) if path else ""

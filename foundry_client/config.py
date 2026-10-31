"""Client settings (client.toml). No secrets here: they live in the OS keyring
(secrets.py) and are injected into a component's environment when it starts."""
from __future__ import annotations

import copy
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from . import APP_ID

COMPONENTS = ("kilo", "coordinator", "supervisor", "rojo", "blender", "acestep-music",
              "stable-audio", "kokoro-tts", "chatterbox-tts")

# optional service components (Team Lead 5): local programs a sprint's needs can ask the
# client to start. Off by default and with no program set; a server that only runs on
# another machine (the audio MCPs on the GPU host) is probed, never managed.
SERVICE_COMPONENTS = ("blender", "acestep-music", "stable-audio", "kokoro-tts", "chatterbox-tts")
# components a service tab / chat may switch on or off (enabled in client.toml)
TOGGLE_COMPONENTS = ("rojo",) + SERVICE_COMPONENTS

# components that earlier versions managed; a settings file that still has one is cleaned on load
RETIRED_COMPONENTS = ("media",)       # Kilo spawns the media adapter itself (stdio MCP)

DEFAULTS: dict[str, Any] = {
    "client": {
        "backend_url": "http://127.0.0.1:11435",      # the Foundry router (backend host)
        "state_dir": "~/.foundry-coordinator",         # coordinator state, logs
        "api_port": 47811,                              # local settings page / API (127.0.0.1)
        "open_settings_on_start": False,
        "unhealthy_restart_seconds": 300,               # a component unhealthy this long is restarted (0 = never)
        "safe_restart_max_wait_minutes": 60,            # a pending safe-point restart warns after this
        "update_check": True,                           # look for a newer release on GitHub (no auto-update)
        "update_repo": "Vipast82/foundry-router",
        # router relay (relay.py): heartbeat + stack actions for the operator chat.
        # router_key names the keyring entry holding the router key (operator scope)
        "relay_enabled": True,
        "router_key": "FOUNDRY_CLIENT_KEY",
    },
    "workspace": {
        "game": "",                                     # e.g. C:/Roblox/AnimalUprising
        "profiles": "",                                 # coordinator profiles dir (projects/)
        "deploy": "",                                   # coordinator release checkout, if any
        "kilo_config": "",                              # kilo.jsonc (empty = <game>/kilo.jsonc)
        "project": "",                                  # coordinator project id (empty = the only one)
    },
    # resource watch (resources.py): free RAM + the development / testing jobs the stack runs
    "resources": {"enabled": True, "interval_seconds": 60, "low_ram_gb": 2, "auto_stop": True,
                  "limits": {"training": {"gb": 6, "minutes": 120},
                             "studio_check": {"gb": 3, "minutes": 240},
                             "gate_ps1": {"gb": 2, "minutes": 90},
                             "lune": {"gb": 2, "minutes": 60},
                             "blender_build": {"gb": 8, "minutes": 120},
                             "rojo_build": {"gb": 2, "minutes": 20},
                             "pytest": {"gb": 4, "minutes": 60},
                             "repo_script": {"gb": 2, "minutes": 120}}},
    # Studio auto-reopen (studio.py): off by default; the TEST place only
    "studio": {"auto_reopen": False, "place_id": 0, "universe_id": 0, "title_match": "",
               "never_place_ids": [], "idle_minutes": 2, "max_wait_minutes": 10,
               "max_per_hour": 2, "rojo_auto_sync": True},
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
        "rojo": {"enabled": False, "path": "", "project": "", "port": 34872, "args": [],
                 "env": {}, "secrets": []},
        # blender: the Blender program (path) with the blender-mcp add-on enabled; port is the
        # add-on's socket (BLENDER_PORT, default 9876). The socket only opens after "Connect"
        # in Blender's side panel, so the client does not health-check it (a started Blender
        # that is not connected shows as running; the MCP probe reports the socket).
        "blender": {"enabled": False, "path": "", "cwd": "", "port": 9876, "args": [],
                    "env": {}, "secrets": []},
        "acestep-music": {"enabled": False, "path": "", "cwd": "", "port": 0, "args": [],
                          "env": {}, "secrets": []},
        "stable-audio": {"enabled": False, "path": "", "cwd": "", "port": 0, "args": [],
                         "env": {}, "secrets": []},
        "kokoro-tts": {"enabled": False, "path": "", "cwd": "", "port": 0, "args": [],
                       "env": {}, "secrets": []},
        "chatterbox-tts": {"enabled": False, "path": "", "cwd": "", "port": 0, "args": [],
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
    for name in SERVICE_COMPONENTS:
        comp = comps.get(name) or {}
        if comp.get("enabled") and not str(comp.get("path") or "").strip():
            errs.append(f"components.{name}.path is required to enable {name}")
        port = comp.get("port", 0)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            errs.append(f"components.{name}.port must be 0 or a port number")
    if comps["kilo"]["enabled"] and not cfg["workspace"]["game"]:
        errs.append("workspace.game is required for the Kilo server (its working folder)")
    if comps["coordinator"]["enabled"] and not cfg["workspace"]["profiles"]:
        errs.append("workspace.profiles is required for the coordinator")
    for k in ("unhealthy_restart_seconds", "safe_restart_max_wait_minutes"):
        v = c.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            errs.append(f"client.{k} must be a whole number, 0 or more")
    for k in ("relay_enabled", "update_check"):
        if not isinstance(c.get(k), bool):
            errs.append(f"client.{k} must be true or false")
    if isinstance(cfg.get("resources"), dict):
        from .resources import validate as resources_validate
        errs += resources_validate(cfg["resources"])
    if isinstance(cfg.get("studio"), dict):
        from .studio import DEFAULTS as STUDIO_DEFAULTS, validate as studio_validate
        errs += studio_validate({**STUDIO_DEFAULTS, **cfg["studio"]})
    return errs


# git's own config-by-environment names (GIT_CONFIG_COUNT / _KEY_n / _VALUE_n) hold
# config keys such as core.abbrev, never a secret
_NOT_SECRET = re.compile(r"^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$")


def _secretish(name: str) -> bool:
    n = name.upper()
    if _NOT_SECRET.match(n):
        return False
    return any(w in n for w in ("PASSWORD", "SECRET", "TOKEN", "_KEY", "APIKEY"))


def migrate(raw: dict) -> bool:
    """Bring an older settings document up to date in place. True when it changed."""
    comps = raw.get("components")
    changed = False
    if isinstance(comps, dict):
        for name in RETIRED_COMPONENTS:
            changed = comps.pop(name, None) is not None or changed
    return changed


def load(path: Path | None = None) -> dict:
    p = Path(path or config_path())
    raw: dict = {}
    if p.is_file():
        with open(p, "rb") as f:
            raw = tomllib.load(f)
        if migrate(raw):                       # drop retired tables quietly, keep one copy
            try:
                shutil.copy2(p, p.with_name(p.name + ".before-migration"))
                tmp = p.with_suffix(".toml.tmp")
                with open(tmp, "wb") as f:
                    tomli_w.dump(raw, f)
                os.replace(tmp, p)
            except OSError:
                pass                           # read-only file: the in-memory copy is clean anyway
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

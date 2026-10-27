"""What a sprint can need and which services provide it (Team Lead piece 5).

Shared by the coordinator (services.py: the registry table, `ready()`, per-track
kilo.jsonc), the Foundry Client (MCP availability in the heartbeat) and the
router (the Services tab and chat tools), so the three agree on one map. Pure
standard library plus the JSONC reader; every network or filesystem probe takes
an injectable function so tests never touch a real machine.

Needs (the words a sprint declares, plans carry them in a `## Needs` block):

    studio   Roblox Studio via Rojo + the Studio MCP + the Kilo server
    blender  the Blender MCP (and Blender's add-on socket)
    audio    music / sound-effect / speech MCP servers
    images   Comfy or the Foundry media adapter
    gpu      the local model backend (shares one card with audio and images)
    backend  the router only (no extra service)
    none     nothing

Kilo assumptions (documented, not discovered):
  * Kilo reads MCP servers from `kilo.jsonc` `mcp{}`, merged over the global
    config (`~/.config/kilo/kilo.jsonc`). The project file often holds only
    `{"enabled": false}` stubs for servers the global file defines, so probing
    resolves a stub against the global entry.
  * `type: "local"` servers are stdio commands Kilo spawns itself (`command` is
    an argv list; Windows wrappers `cmd /c`, `powershell -File` are looked
    through to the real target); `type: "remote"` servers have a `url`.
  * "available" means: the command's binary or script exists / the URL's host
    and port accept a connection. It is NOT an MCP handshake (starting a
    server for a probe could spawn npx downloads or GPU loads).
  * A stub with no definition anywhere has availability None ("unknown"),
    which `ready()` does not treat as missing.
"""

from __future__ import annotations

import os
import shutil
import socket
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from . import jsonc

NEEDS = ("studio", "blender", "audio", "images", "gpu", "backend")
NEEDS_ALL = NEEDS + ("none",)

ALIASES = {
    "roblox": "studio", "roblox_studio": "studio", "rojo": "studio", "ui": "studio",
    "robloxstudio": "studio", "robloxstudio-mcp": "studio",
    "blender-mcp": "blender", "3d": "blender", "models": "blender", "mesh": "blender",
    "music": "audio", "sfx": "audio", "tts": "audio", "sound": "audio", "voice": "audio",
    "acestep-music": "audio", "stable-audio": "audio", "kokoro-tts": "audio",
    "chatterbox-tts": "audio", "media-gen": "audio", "media_gen": "audio",
    "image": "images", "comfy": "images", "comfy-mcp": "images", "media": "images",
    "foundry-media": "images", "textures": "images",
    "llm": "gpu", "local_model": "gpu", "local-model": "gpu", "model": "gpu",
    "router": "backend", "code": "backend", "docs": "backend",
}

# the reference servers every track keeps (no Studio, no GPU, read-only)
BASE_MCP = ("roblox-docs", "searxng")

# network defaults an operator can override in a profile `[services.<name>]` table
ROUTER_URL = "http://127.0.0.1:11435/health"
BLENDER_PORT = 9876          # blender-mcp's default add-on socket (BLENDER_PORT)


@dataclass(frozen=True)
class Service:
    name: str
    kind: str                       # mcp | process | backend
    check: str                      # port | http | mcp
    provider: str                   # client | backend | external
    needed_by: tuple
    label: str = ""
    component: str = ""             # the Foundry Client component that can run it
    port: int = 0
    url: str = ""
    also_port: int = 0              # an extra local port that must be open (Blender's socket)
    note: str = ""


SERVICES: dict[str, Service] = {s.name: s for s in (
    Service("kilo", "process", "port", "client", ("studio",), "Kilo server", "kilo", port=4096),
    Service("rojo", "process", "port", "client", ("studio",), "Rojo serve", "rojo", port=34872),
    Service("robloxstudio-mcp", "mcp", "mcp", "external", ("studio",), "Roblox Studio MCP",
            note="Studio itself and its plugin must be open; Kilo spawns the server"),
    Service("blender-mcp", "mcp", "mcp", "external", ("blender",), "Blender MCP", "blender",
            also_port=BLENDER_PORT,
            note="the Blender add-on's socket must be connected; Kilo spawns the server"),
    # 2026-10-27: one Media-Gen server (TrueNAS media-mcp, streamable HTTP) replaced the
    # acestep-music, stable-audio, kokoro-tts and chatterbox-tts servers
    Service("Media_Gen", "mcp", "mcp", "external", ("audio", "images"),
            "Media-Gen (music, SFX, speech, images, video)",
            note="TrueNAS media-mcp at 192.168.0.206:8190; jobs return a job_id, "
                 "results via media_check_job; library 'game' for AU assets"),
    Service("comfy-mcp", "mcp", "mcp", "external", ("images",), "Comfy MCP"),
    Service("foundry-media", "mcp", "mcp", "external", ("images",), "Foundry media adapter"),
    Service("local-model", "backend", "http", "backend", ("gpu",), "Local model backend",
            url=ROUTER_URL),
    Service("router", "backend", "http", "backend", ("backend",), "Foundry router", url=ROUTER_URL),
)}

# need -> services. A tuple inside the list is an any-of group (images: Comfy or the
# Foundry media adapter is enough).
NEEDS_SERVICES: dict[str, list] = {
    "studio": ["rojo", "robloxstudio-mcp", "kilo"],
    "blender": ["blender-mcp"],
    "audio": ["Media_Gen"],
    "images": [("Media_Gen", "comfy-mcp", "foundry-media")],
    "gpu": ["local-model"],
    "backend": ["router"],
    "none": [],
}

# need -> MCP servers a track with that need gets enabled in its kilo.jsonc
NEEDS_MCP: dict[str, tuple] = {
    "studio": ("Roblox_Studio", "robloxstudio-mcp"),
    "blender": ("blender-mcp",),
    "audio": ("Media_Gen",),
    "images": ("Media_Gen", "comfy-mcp", "foundry-media"),
}
# every server some need controls (an unlisted server is left as the source has it)
# servers Media_Gen replaced (2026-10-27): no need enables them, a track always has them off
LEGACY_MCP = ("acestep-music", "stable-audio", "kokoro-tts", "chatterbox-tts")
CONTROLLED_MCP = tuple(dict.fromkeys([m for v in NEEDS_MCP.values() for m in v]
                                     + list(LEGACY_MCP)))
# tool-name prefixes denied for a role when the track lacks the need
# Media_Gen serves both audio and images: its tools are denied per family, not per server
MEDIA_GEN_AUDIO = ("generate_music", "generate_bgm", "generate_sfx", "generate_speech",
                   "generate_dialogue", "register_voice", "list_voices",
                   "generate_song_personal")
MEDIA_GEN_IMAGES = ("generate_image", "edit_image", "remove_background", "upscale_image")
NEEDS_TOOLS = {"blender": ("blender-mcp_*", "blender_mcp_*"),
               "audio": tuple(f"Media_Gen_{t}" for t in MEDIA_GEN_AUDIO)
               + ("acestep-music_*", "stable-audio_*", "kokoro-tts_*", "chatterbox-tts_*"),
               "images": tuple(f"Media_Gen_{t}" for t in MEDIA_GEN_IMAGES)
               + ("comfy-mcp_*", "foundry-media_*")}


def normalize_needs(x) -> list[str]:
    """Need words from a list, a comma/space separated string, or None -> sorted unique
    valid words. Unknown words are dropped; 'none' alone means nothing."""
    if x is None:
        return []
    if isinstance(x, str):
        import re
        x = [p for p in re.split(r"[\s,;/+]+", x) if p]
    out = set()
    for w in x if isinstance(x, Iterable) else []:
        w = str(w).strip().lower().replace("needs:", "")
        w = ALIASES.get(w, w)
        if w in NEEDS:
            out.add(w)
    return sorted(out)


def services_for(needs) -> list[str]:
    """Flat service names for the needs (any-of groups listed in full)."""
    out: list[str] = []
    for n in normalize_needs(needs):
        for s in NEEDS_SERVICES.get(n, []):
            for name in (s if isinstance(s, tuple) else (s,)):
                if name not in out:
                    out.append(name)
    return out


def mcp_for(needs) -> list[str]:
    out: list[str] = []
    for n in normalize_needs(needs):
        for m in NEEDS_MCP.get(n, ()):
            if m not in out:
                out.append(m)
    return out


def needed_by(name: str) -> list[str]:
    return [n for n, groups in NEEDS_SERVICES.items()
            if any(name in (g if isinstance(g, tuple) else (g,)) for g in groups)]


# --------------------------------------------------------------------------- kilo.jsonc mcp

def global_kilo_paths() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, ".config", "kilo", "kilo.jsonc"),
            os.path.join(home, ".config", "kilo", "kilo.json")]


def _read_mcp(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = jsonc.parse(f.read())
    except (OSError, jsonc.JsoncError):
        return {}
    m = (data or {}).get("mcp") if isinstance(data, dict) else None
    return {k: v for k, v in (m or {}).items() if isinstance(v, dict)}


def merged_mcp(project_text: str = "", project_path: str = "", global_path: Optional[str] = None
               ) -> dict:
    """{name: server dict}: the global config under the project file (a project entry's keys
    win; a stub `{"enabled": false}` keeps the global command/url). `global_path` "" = none,
    None = the default locations."""
    out: dict = {}
    gpaths = [global_path] if global_path else ([] if global_path == "" else global_kilo_paths())
    for gp in gpaths:
        for k, v in _read_mcp(gp).items():
            out.setdefault(k, {}).update(v)
    proj: dict = {}
    if project_text:
        try:
            d = jsonc.parse(project_text)
            proj = {k: v for k, v in ((d or {}).get("mcp") or {}).items() if isinstance(v, dict)}
        except jsonc.JsoncError:
            proj = {}
    elif project_path:
        proj = _read_mcp(project_path)
    for k, v in proj.items():
        out.setdefault(k, {}).update(v)
    return out


_WRAPPERS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "sh"}


def command_target(command: list) -> tuple[str, str]:
    """(the wrapper or program, the real target it runs) of a local MCP argv. The target is
    what must exist: `cmd /c npx.cmd ...` -> npx.cmd, `powershell -File x.ps1` -> x.ps1."""
    argv = [os.path.expandvars(str(a)) for a in command or []]
    if not argv:
        return "", ""
    first = os.path.basename(argv[0]).lower()
    if first not in _WRAPPERS:
        return argv[0], argv[0]
    rest = argv[1:]
    if first.startswith(("powershell", "pwsh")):
        for i, a in enumerate(rest):
            if a.lower() in ("-file", "-f") and i + 1 < len(rest):
                return argv[0], rest[i + 1]
            if a.lower() in ("-command", "-c") and i + 1 < len(rest):
                return argv[0], rest[i + 1].split()[0] if rest[i + 1].split() else ""
    for a in rest:
        if a.startswith("/") and len(a) <= 3 or a.startswith("-"):
            continue
        return argv[0], a
    return argv[0], ""


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _binary_exists(target: str, which: Callable = shutil.which,
                   isfile: Callable = os.path.isfile) -> bool:
    if not target:
        return False
    if os.path.isabs(target) or "/" in target or "\\" in target:
        return bool(isfile(target))
    return bool(which(target))


def probe_server(name: str, srv: dict, port_fn: Callable = port_open,
                 which: Callable = shutil.which, isfile: Callable = os.path.isfile) -> dict:
    """{name, type, enabled, available (True|False|None), detail, target}. Cheap on purpose:
    a binary/script that exists, or a host:port that accepts a connection."""
    stype = str(srv.get("type") or ("remote" if srv.get("url") else "local" if srv.get("command")
                                    else "stub"))
    row = {"name": name, "type": stype, "enabled": srv.get("enabled", True) is not False,
           "available": None, "detail": "", "target": ""}
    if stype == "remote" or srv.get("url"):
        u = urlparse(str(srv.get("url") or ""))
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        row["target"] = f"{host}:{port}"
        if not host:
            row.update(available=False, detail="remote server without a url")
        elif port_fn(host, port):
            row.update(available=True, detail=f"{host}:{port} accepts connections")
        else:
            row.update(available=False, detail=f"{host}:{port} is not reachable")
        return row
    if srv.get("command"):
        wrapper, target = command_target(list(srv.get("command") or []))
        row["target"] = target or wrapper
        if wrapper and not _binary_exists(wrapper, which, isfile):
            row.update(available=False, detail=f"{wrapper} not found")
        elif not target:
            row.update(available=False, detail="no program to run in the command")
        elif _binary_exists(target, which, isfile):
            row.update(available=True, detail=f"{os.path.basename(target)} found")
        else:
            row.update(available=False, detail=f"{target} not found")
        return row
    row["detail"] = "no command or url here (defined in the global Kilo config, or nowhere)"
    return row


def probe_all(mcp: dict, **fns) -> list[dict]:
    return [probe_server(n, s, **fns) for n, s in mcp.items()]


# --------------------------------------------------------------------------- service checks

def check_service(svc: Service, mcp_rows: dict, overrides: Optional[dict] = None,
                  port_fn: Callable = port_open,
                  http_fn: Optional[Callable] = None) -> tuple[Optional[bool], str]:
    """(ok, detail) for one catalog service. ok None = cannot tell (not an error).
    `mcp_rows`: {name: probe_server row}; `overrides`: {name: {port, url, host, enabled}}."""
    ov = dict((overrides or {}).get(svc.name) or {})
    if ov.get("enabled") is False:
        return None, "turned off in the settings"
    if svc.check == "port":
        port = int(ov.get("port") or svc.port)
        host = str(ov.get("host") or "127.0.0.1")
        ok = port_fn(host, port)
        return ok, f"port {port} {'is open' if ok else 'is closed'}"
    if svc.check == "http":
        url = str(ov.get("url") or svc.url)
        if not url:
            return None, "no endpoint configured"
        if http_fn is None:
            http_fn = _http_check
        why = http_fn(url)
        return (not why), (f"{url} answers" if not why else f"{url}: {why}")
    row = (mcp_rows or {}).get(svc.name)
    if row is None:
        return None, "not defined in kilo.jsonc or the global Kilo config"
    ok = row.get("available")
    detail = str(row.get("detail") or "")
    if ok and svc.also_port:
        p = int(ov.get("port") or svc.also_port)
        if not port_fn("127.0.0.1", p):
            return False, f"{detail}; but port {p} is closed ({svc.note or 'its app is not running'})"
    return ok, detail


def _http_check(url: str, timeout: float = 3.0) -> str:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return "" if int(getattr(r, "status", 200)) < 500 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return "" if e.code < 500 else f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return f"{type(e).__name__}: {e}"[:200]


def evaluate(needs, results: dict) -> tuple[bool, list[str]]:
    """(ok, missing) for `needs` given {service: ok|None|False}. None counts as present.
    An any-of group is missing only when every member is False (its first name is listed)."""
    missing: list[str] = []
    for n in normalize_needs(needs):
        for g in NEEDS_SERVICES.get(n, []):
            names = g if isinstance(g, tuple) else (g,)
            if all(results.get(x) is False for x in names):
                label = names[0] if len(names) == 1 else "/".join(names)
                if label not in missing:
                    missing.append(label)
    return (not missing), missing

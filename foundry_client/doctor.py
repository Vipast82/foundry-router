"""Doctor: does this workstation look able to run the stack? Every check is read-only and
answers {id, label, status, detail}; status is ok | warn | fail | info (info = optional and
not set up)."""
from __future__ import annotations

import os
import subprocess
import sys

from . import secrets
from .components import (find_binary, kilo_config_path, kilo_health, port_open, project_ids)
from .config import COMPONENTS, expand
from .probe import NO_WINDOW, get_json, port_owner_pid

ADAPTER_MARKERS = ("foundry_media_mcp", "foundry-media")


def _c(id_: str, label: str, status: str, detail: str) -> dict:
    return {"id": id_, "label": label, "status": status, "detail": detail}


def run(mgr) -> dict:
    cfg, checks = mgr.cfg, []
    comps = cfg["components"]
    st = {c["name"]: c for c in mgr.status()["components"]}

    # binaries
    for name, label, names in (("kilo", "Kilo", ["kilo", "kilo.cmd"]), ("rojo", "Rojo", ["rojo"])):
        spec = mgr.procs[name].spec
        want = comps[name]["enabled"]
        exe = spec.argv[0] if spec.argv and spec.argv[0] else ""
        if exe:
            ver = _version(exe)
            checks.append(_c(f"{name}_bin", f"{label} program", "ok", f"{exe}" + (f" ({ver})" if ver else "")))
        else:
            checks.append(_c(f"{name}_bin", f"{label} program", "fail" if want else "info",
                             spec.problem or f"{name} was not found"))

    # ports
    ports = [("api", "Settings page port", cfg["client"]["api_port"], None)]
    for n in ("kilo", "rojo"):
        if comps[n]["enabled"]:
            ports.append((n, f"{mgr.procs[n].spec.label} port", mgr.procs[n].spec.port, n))
    for key, label, port, comp in ports:
        if key == "api":
            checks.append(_c("port_api", label, "ok", f"{port} (this page is served from it)"))
        elif comp and st[comp]["state"] in ("running", "starting") or not port_open(port):
            checks.append(_c(f"port_{key}", label, "ok", f"{port}" + (" in use by the component"
                             if port_open(port) else " is free")))
        else:
            pid = port_owner_pid(port)
            checks.append(_c(f"port_{key}", label, "warn",
                             f"{port} is in use by another program" + (f" (pid {pid})" if pid else "")))

    # secrets
    needed = sorted({s for n, c in comps.items() if c["enabled"] for s in c.get("secrets") or []})
    have = secrets.status(needed)
    miss = [n for n in needed if not have.get(n)]
    checks.append(_c("secrets", "Secrets", "fail" if miss else "ok",
                     ("missing: " + ", ".join(miss) + " (Secrets tab)") if miss else
                     (", ".join(needed) + " present") if needed else "none needed"))

    # Kilo reachability with the password
    if comps["kilo"]["enabled"]:
        ok, why, *_ = kilo_health(comps["kilo"]["port"])
        checks.append(_c("kilo_api", "Kilo server answers", "ok" if ok else "fail", why))

    # kilo.jsonc
    checks += _kilo_config_checks(cfg)

    # profiles
    if comps["coordinator"]["enabled"]:
        ids = project_ids(cfg["workspace"]["profiles"])
        checks.append(_c("profiles", "Coordinator projects", "ok" if ids else "fail",
                         ", ".join(ids) if ids else "no <project>/project.toml under "
                                                    "workspace.profiles"))

    # Rojo plugin
    if comps["rojo"]["enabled"]:
        r = st["rojo"]
        sid = (r.get("info") or {}).get("sessionId")
        alert = next((a for a in mgr.alerts.values() if a["key"] == "rojo_reconnect"), None)
        if r["state"] != "running":
            checks.append(_c("rojo_plugin", "Rojo plugin session", "warn", f"Rojo is {r['state']}"))
        elif alert:
            checks.append(_c("rojo_plugin", "Rojo plugin session", "warn", alert["text"]))
        else:
            checks.append(_c("rojo_plugin", "Rojo plugin session", "ok",
                             f"session {str(sid)[:8]}; the client cannot see whether Studio is "
                             f"connected (click Connect in the plugin after any Rojo restart)"))

    # router
    if not mgr.link.state["checked_at"]:
        mgr.link.poll()
    link = mgr.link.snapshot()
    if link["reachable"]:
        checks.append(_c("router", "Foundry router", "warn" if link["warming_up"] else "ok",
                         f"{link['url']} v{link['version']}" +
                         (" (still warming up)" if link["warming_up"] else "")))
    else:
        checks.append(_c("router", "Foundry router", "fail", f"{link['url']}: {link['error']}"))

    return {"checks": checks, "ok": not any(c["status"] == "fail" for c in checks)}


def _version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8,
                           creationflags=NO_WINDOW)
        return (r.stdout or r.stderr or "").strip().splitlines()[0][:60] if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def _kilo_config_checks(cfg: dict) -> list:
    from foundry_automation import jsonc
    path = kilo_config_path(cfg)
    if not path:
        return [_c("kilo_config", "kilo.jsonc", "warn", "set the game folder (or workspace.kilo_config)")]
    if not os.path.isfile(path):
        return [_c("kilo_config", "kilo.jsonc", "fail", f"{path} does not exist")]
    try:
        with open(path, encoding="utf-8") as f:
            data = jsonc.parse(f.read())
    except (OSError, ValueError) as e:
        return [_c("kilo_config", "kilo.jsonc", "fail", f"{path} is not valid JSONC: {e}")]
    out = [_c("kilo_config", "kilo.jsonc", "ok", f"{path} parses")]
    out.append(_adapter_check((data or {}).get("mcp") or {}, cfg))
    return out


def _adapter_check(mcp: dict, cfg: dict) -> dict:
    """Kilo starts the Foundry media adapter itself (stdio MCP), so the only thing to verify
    is that its entry in kilo.jsonc is right and can find its key."""
    entry = None
    for name, e in (mcp.items() if isinstance(mcp, dict) else ()):
        blob = " ".join([name, *_argv(e)]).lower()
        if any(m in blob for m in ADAPTER_MARKERS):
            entry = (name, e)
            break
    label = "Media adapter in kilo.jsonc"
    if entry is None:
        return _c("media_adapter", label, "info", "no Foundry media adapter entry (optional; see "
                                                   "docs/MEDIA.md to add one)")
    name, e = entry
    problems = []
    if isinstance(e, dict):
        if e.get("enabled") is False:
            problems.append("the entry is disabled")
        if str(e.get("type") or "local") != "local":
            problems.append(f"type is {e.get('type')!r}, the adapter is a local (stdio) server")
    argv = _argv(e)
    script = next((a for a in argv if a.lower().endswith(".py") and "media" in a.lower()), "")
    launcher = argv[0] if argv else ""
    target = script or launcher
    if not argv:
        problems.append("no command")
    elif target and os.path.isabs(expand(target)) and not os.path.exists(expand(target)):
        problems.append(f"{target} does not exist")
    env = (e.get("environment") or e.get("env") or {}) if isinstance(e, dict) else {}
    if not _key_available(argv, env, cfg):
        problems.append("FOUNDRY_KEY is not available to Kilo: put it in the entry's "
                        "environment, add FOUNDRY_KEY to the Kilo component's secrets and set it "
                        "on the Secrets tab, or set it as a user environment variable")
    if problems:
        return _c("media_adapter", label, "fail", f"{name}: " + "; ".join(problems))
    return _c("media_adapter", label, "ok", f"{name}: {' '.join(argv)[:120]}")


def _argv(e) -> list:
    if not isinstance(e, dict):
        return []
    cmd = e.get("command")
    out = [cmd] if isinstance(cmd, str) else list(cmd or [])
    out += list(e.get("args") or [])
    return [str(x) for x in out]


def _key_available(argv: list, env: dict, cfg: dict) -> bool:
    """Kilo spawns the adapter, which inherits Kilo's environment. The key can come from the
    entry's own environment, from the client (a Kilo secret), from the user's environment, or
    from the adapter's foundry-media.json."""
    if env.get("FOUNDRY_KEY") or os.environ.get("FOUNDRY_KEY"):
        return True
    if "FOUNDRY_KEY" in (cfg["components"]["kilo"].get("secrets") or [])             and secrets.status(["FOUNDRY_KEY"]).get("FOUNDRY_KEY"):
        return True
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                if winreg.QueryValueEx(k, "FOUNDRY_KEY")[0]:
                    return True
        except OSError:
            pass
    for a in argv:
        f = os.path.join(os.path.dirname(expand(a)), "foundry-media.json")
        if os.path.isabs(expand(a)) and os.path.isfile(f):
            try:
                import json
                if json.load(open(f, encoding="utf-8")).get("api_key"):
                    return True
            except (OSError, ValueError):
                pass
    return False

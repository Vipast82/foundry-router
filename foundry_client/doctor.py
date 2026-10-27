"""Doctor: does this workstation look able to run the stack? Every check is read-only and
answers {id, label, status, detail}; status is ok | warn | fail | info (info = optional and
not set up)."""
from __future__ import annotations

import os
import re
import subprocess
import sys

from . import secrets
from .components import (find_binary, kilo_config_path, kilo_health, port_open, project_ids)
from .config import COMPONENTS, expand
from .probe import NO_WINDOW, get_json, port_owner_pid

ADAPTER_MARKERS = ("foundry_media_mcp", "foundry-media")
SCRIPT_EXTS = (".py", ".ps1", ".cmd", ".bat", ".sh")
LAUNCHER_CHECK_TIMEOUT = 30


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

    # Studio auto-reopen (studio.py)
    studio = getattr(mgr, "studio", None)
    sts = studio.settings() if studio is not None else {}
    if sts.get("auto_reopen"):
        from .studio import validate as studio_validate
        bad = studio_validate(sts)
        checks.append(_c("studio_reopen", "Studio auto-reopen", "fail" if bad else "ok",
                         "; ".join(bad) if bad else
                         f"on: place {sts['place_id']} (window title contains "
                         f"'{sts['title_match']}'), at most {sts['max_per_hour']}/hour"))
    elif studio is not None:
        checks.append(_c("studio_reopen", "Studio auto-reopen", "info",
                         "off: a lost Rojo connection waits for Plugins → Rojo → Connect"))

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
    launcher = _launcher_script(argv)
    target = script or launcher or (argv[0] if argv else "")
    if not argv:
        problems.append("no command")
    elif target and os.path.isabs(expand(target)) and not os.path.exists(expand(target)):
        problems.append(f"{target} does not exist")
    env = (e.get("environment") or e.get("env") or {}) if isinstance(e, dict) else {}
    key_note, warn = "", ""
    if argv and not problems and not _key_available(argv, env, cfg):
        verdict, note = _launcher_self_check(argv, env)
        if verdict == "ok":
            key_note = note
        elif verdict == "warn":
            warn = note
        else:
            problems.append(note or (
                "FOUNDRY_KEY is not available to Kilo: put it in the entry's environment, add "
                "FOUNDRY_KEY to the Kilo component's secrets and set it on the Secrets tab, set "
                "it as a user environment variable, or launch the adapter through a script that "
                "loads it"))
    if problems:
        return _c("media_adapter", label, "fail", f"{name}: " + "; ".join(problems))
    if warn:
        return _c("media_adapter", label, "warn", f"{name}: {warn}")
    return _c("media_adapter", label, "ok", f"{name}: {key_note or ' '.join(argv)[:120]}")


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
    if _user_env_has_key():
        return True
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


def _user_env_has_key() -> bool:
    """FOUNDRY_KEY set as a user environment variable (Windows registry; new processes get it
    even when this one started before it was set). Only presence is checked."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return bool(winreg.QueryValueEx(k, "FOUNDRY_KEY")[0])
    except OSError:
        return False


def _launcher_script(argv: list) -> str:
    """The script the entry runs (start_media_adapter.ps1, a .cmd/.sh wrapper, the .py adapter)."""
    return next((a for a in argv if a.lower().endswith(SCRIPT_EXTS)), "")


# The launcher sets FOUNDRY_KEY for the adapter itself ($env:FOUNDRY_KEY = ..., export
# FOUNDRY_KEY=..., set FOUNDRY_KEY=..., os.environ["FOUNDRY_KEY"] = ...).
_SETS_KEY = re.compile(r"FOUNDRY_KEY['\"]?\]?\s*=(?!=)", re.I)
_PS_CHECK = re.compile(r"\[switch\]\s*\$Check\b", re.I)


def _launcher_self_check(argv: list, env: dict) -> tuple:
    """For an entry whose launcher script provides the key itself, run the launcher's own check
    mode (-Check for a PowerShell script with a [switch]$Check, --check otherwise) and trust its
    exit code. Returns (verdict, note): ok | warn | fail, or ("", "") when the launcher neither
    loads the key nor has a check mode. The launcher's output is never shown: it is the one
    place the key could appear."""
    script = _launcher_script(argv)
    path = expand(script) if script else ""
    if not path or not os.path.isfile(path):
        return "", ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
    except OSError:
        return "", ""
    base = os.path.basename(path)
    loads = bool(_SETS_KEY.search(text))
    flag = "-Check" if path.lower().endswith(".ps1") and _PS_CHECK.search(text) else (
        "--check" if "--check" in text else "")
    if not flag:
        if loads:
            return "warn", (f"{base} loads FOUNDRY_KEY itself but has no check mode, so the "
                            "Doctor cannot confirm it works")
        return "", ""
    run_env = {**os.environ, **{str(k): str(v) for k, v in (env or {}).items()}}
    try:
        r = subprocess.run([expand(a) for a in argv] + [flag], env=run_env, capture_output=True,
                           text=True, timeout=LAUNCHER_CHECK_TIMEOUT,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "warn", f"{base} {flag} did not finish in {LAUNCHER_CHECK_TIMEOUT}s"
    except OSError as ex:
        return "fail", f"could not run {base} {flag}: {ex.strerror or ex}"
    if r.returncode == 0:
        how = "loads FOUNDRY_KEY itself" if loads else "finds FOUNDRY_KEY"
        return "ok", f"{base} {how}; its {flag} passed"
    return "fail", (f"FOUNDRY_KEY not confirmed: {base} {flag} failed (exit {r.returncode}); "
                    f"run it by hand to see why")

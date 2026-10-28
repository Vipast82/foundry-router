"""foundry-client: run the workstation stack with a tray icon and settings page.

  foundry-client run [--headless] [--no-start]   manager + local API + tray
  foundry-client status                            component states (from the running client)
  foundry-client restart <component> [--force] [--wait]
  foundry-client install-autostart [--at-logon | --at-boot [--user DOMAIN\\name]]
  foundry-client remove-autostart                  removes the sign-in entry and the boot task
  foundry-client install-service | remove-service   systemd --user unit (Linux, headless)
  foundry-client doctor                            checks of this workstation (running client)
  foundry-client apply-kilo-config [--dry-run]     role rules -> kilo.jsonc, then reload Kilo
  foundry-client import-legacy-secrets             one-time: old PowerShell credential files
  foundry-client coordinator <args...>             the bundled coordinator CLI
  foundry-client supervisor <args...>              the bundled upgrade supervisor
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser


def _api(cfg, path: str, method: str = "GET", body=None, timeout: float = 10):
    from .api import load_token
    url = f"http://127.0.0.1:{cfg['client']['api_port']}{path}"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode()
                                 if body is not None else None,
                                 headers={"X-Client-Token": load_token(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _running(cfg) -> bool:
    try:
        _api(cfg, "/api/status", timeout=2)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _start_relay(mgr, cfg) -> None:
    """The router claim loop lives in relay.py (added separately); it is optional here.
    Contract: relay.start(mgr, cfg) starts its own daemon thread and returns at once; it
    reads mgr.observation() for what to report and calls mgr.restart / mgr.apply_kilo_config
    for what it is asked."""
    if not cfg["client"].get("relay", True):
        return
    try:
        from . import relay
    except ImportError:
        return
    try:
        relay.start(mgr, cfg)
    except Exception as e:                                       # noqa: BLE001
        mgr._event("client", f"router relay did not start: {type(e).__name__}: {e}")


def cmd_run(cfg, headless: bool, no_start: bool) -> int:
    from . import api, config
    from .manager import Manager
    from .tray import run_tray
    import uvicorn

    port = cfg["client"]["api_port"]
    url = f"http://127.0.0.1:{port}/#t={api.load_token()}"
    if _running(cfg):                       # single instance: show the running one
        webbrowser.open(url)
        return 0
    first_run = not config.config_path().is_file()
    if first_run:
        cfg["components"]["kilo"]["enabled"] = False      # nothing starts before setup
        cfg["components"]["coordinator"]["enabled"] = False
        cfg["components"]["supervisor"]["enabled"] = False
        config.config_path().parent.mkdir(parents=True, exist_ok=True)
        import tomli_w
        config.config_path().write_bytes(tomli_w.dumps(cfg).encode())
    mgr = Manager(cfg)
    if not no_start:
        mgr.start_all()
    mgr.start_thread()
    _start_relay(mgr, cfg)
    app = api.create_app(mgr, api.load_token())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    if first_run or cfg["client"].get("open_settings_on_start"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    stop = threading.Event()
    # router relay (client.relay_enabled, default true when a router key is set):
    # the router sees the stack and the operator chat can restart its parts
    from . import relay as _relay
    mgr.relay = _relay.start_if_enabled(mgr, cfg, stop)
    try:
        if headless or not run_tray(mgr, url, stop):
            print(f"Foundry Client running headless; settings: http://127.0.0.1:{port}/ "
                  f"(token in {api.token_path()})", flush=True)
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.should_exit = True
        mgr.shutdown(stop_components=True)
    return 0


def _windowless_streams() -> None:
    """pythonw / a windowless bundle has no stdout/stderr; uvicorn's logging and print()
    would crash on None. Send them to <config dir>/client.log instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .config import config_dir
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    log = open(d / "client.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main(argv=None) -> int:
    _windowless_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "coordinator":
        from foundry_coordinator.cli import main as coord_main
        return coord_main(argv[1:])
    if argv and argv[0] == "supervisor":
        from foundry_coordinator.supervisor import main as sup_main
        return sup_main(argv[1:])
    from . import autostart, config, secrets
    ap = argparse.ArgumentParser(prog="foundry-client", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--no-start", action="store_true", help="start the manager with nothing running")
    sub.add_parser("status")
    x = sub.add_parser("restart")
    x.add_argument("component", choices=config.COMPONENTS)
    x.add_argument("--force", action="store_true", help="don't wait for a safe point")
    x.add_argument("--wait", action="store_true", help="return once it is running again")
    ia = sub.add_parser("install-autostart",
                        help="start the client at sign-in (default) or at boot")
    g = ia.add_mutually_exclusive_group()
    g.add_argument("--at-logon", action="store_true",
                   help="Startup-folder entry: starts when you sign in (default)")
    g.add_argument("--at-boot", action="store_true",
                   help="Windows Task Scheduler task 'At startup, run whether the user is "
                        "logged on or not'; Windows asks for your password itself")
    ia.add_argument("--user", default="",
                    help="account the boot task runs as (default: you, DOMAIN\\name)")
    sub.add_parser("remove-autostart", help="remove the sign-in entry and the boot task")
    sub.add_parser("install-service")
    sub.add_parser("remove-service")
    sub.add_parser("doctor")
    k = sub.add_parser("apply-kilo-config")
    k.add_argument("--dry-run", action="store_true")
    k.add_argument("--project", default="")
    k.add_argument("--no-reload", action="store_true", help="do not reload Kilo afterwards")
    sub.add_parser("import-legacy-secrets")
    a = ap.parse_args(argv or ["run"])
    cfg = config.load()
    if a.cmd in (None, "run"):
        return cmd_run(cfg, getattr(a, "headless", False), getattr(a, "no_start", False))
    if a.cmd == "status":
        am = autostart.mode()
        print(f"{'Autostart':<20} {am:<10} {autostart.MODE_TEXT.get(am, '')}")
        try:
            st = _api(cfg, "/api/status")
        except (urllib.error.URLError, OSError):
            print("the Foundry Client is not running")
            return 1
        for c in st["components"]:
            print(f"{c['label']:<20} {c['state']:<10} {c['detail']}"
                  + (f"  [restart pending: {c['pending_reason'] or 'queued'}]"
                     if c["pending_restart"] else ""))
        r = st.get("router") or {}
        print(f"{'Foundry router':<20} " + (f"{'reachable':<10} v{r.get('version')}"
              if r.get("reachable") else f"{'unreachable':<10} {r.get('error', '')}"))
        for al in st.get("alerts") or []:
            print(f"! {al['text']}")
        u = st.get("update") or {}
        if u.get("available"):
            print(f"update available: {u['latest']} (installed {u['current']}) {u['url']}")
        return 0 if st["overall"] == "ok" else 1
    if a.cmd == "restart":
        q = "?force=true" if a.force else ""
        try:
            st = _api(cfg, f"/api/components/{a.component}/restart{q}", "POST", {}, timeout=120)
        except (urllib.error.URLError, OSError):
            print("the Foundry Client is not running")
            return 1
        print(f"{st['label']}: {st['state']} {st['detail']}"
              + (f" - {st['result']}" if st.get("result") else ""))
        if str(st.get("result", "")).startswith("failed"):
            return 1
        if a.wait:
            c = st
            for _ in range(600):
                time.sleep(2)
                st = _api(cfg, "/api/status")
                c = next(c for c in st["components"] if c["name"] == a.component)
                if c["state"] == "running" and not c["pending_restart"]:
                    return 0
                if c["restart_result"].startswith("failed"):
                    break
            print(f"{c['label']}: {c['state']} {c['detail']}"
                  + (f" (restart pending: {c['pending_reason']})" if c["pending_restart"] else ""))
            return 1
        return 0
    if a.cmd == "doctor":
        try:
            d = _api(cfg, "/api/doctor", timeout=60)
        except (urllib.error.URLError, OSError):
            print("the Foundry Client is not running")
            return 1
        for c in d["checks"]:
            print(f"{c['status'].upper():<5} {c['label']:<30} {c['detail']}")
        return 0 if d["ok"] else 1
    if a.cmd == "apply-kilo-config":
        try:
            r = _api(cfg, "/api/kilo-config/apply", "POST",
                     {"project": a.project, "dry_run": a.dry_run, "reload": not a.no_reload},
                     timeout=180)
        except urllib.error.HTTPError as e:
            r = json.loads(e.read() or b"{}")
        except (urllib.error.URLError, OSError):
            print("the Foundry Client is not running")
            return 1
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1
    if a.cmd == "install-service":
        try:
            print(f"systemd unit: {autostart.install_service()}")
        except RuntimeError as e:
            print(e)
            return 2
        return 0
    if a.cmd == "remove-service":
        autostart.remove_service()
        print("systemd unit removed")
        return 0
    if a.cmd == "install-autostart":
        if a.at_boot:
            exe, args, root = autostart.boot_command()
            print(f"Creating the Task Scheduler task {autostart.BOOT_TASK}: at startup, whether "
                  f"or not anyone is signed in, run\n  {exe} {args}\n  in {root}\n"
                  f"Windows now asks for the password of {a.user or autostart.boot_user()} "
                  f"(typed into Windows' own prompt; Foundry never sees or stores it).",
                  flush=True)
            try:
                r = autostart.enable_boot(a.user)
            except RuntimeError as e:
                print(e)
                return 2
            if not r["ok"]:
                print(f"schtasks failed (exit {r['returncode']}). If it said 'Access is denied', "
                      f"run this again from a terminal opened with 'Run as administrator'.")
                return 1
            print(f"autostart: at boot (task {r['task']}, runs as {r['user']}); the sign-in "
                  f"entry was removed")
            return 0
        print(f"autostart: at sign-in ({autostart.enable()})")
        return 0
    if a.cmd == "remove-autostart":
        autostart.disable()
        boot = autostart.disable_boot()
        print("autostart removed (sign-in entry" + (" and boot task)" if boot else ")"))
        return 0
    if a.cmd == "import-legacy-secrets":
        done = secrets.migrate_legacy(config.expand(cfg["client"]["state_dir"]))
        print("imported: " + (", ".join(done) or "nothing found"))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

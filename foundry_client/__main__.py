"""foundry-client: run the workstation stack with a tray icon and settings page.

  foundry-client run [--headless] [--no-start]   manager + local API + tray
  foundry-client status                            component states (from the running client)
  foundry-client restart <component> [--force] [--wait]
  foundry-client install-autostart | remove-autostart
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
    sub.add_parser("install-autostart")
    sub.add_parser("remove-autostart")
    sub.add_parser("import-legacy-secrets")
    a = ap.parse_args(argv or ["run"])
    cfg = config.load()
    if a.cmd in (None, "run"):
        return cmd_run(cfg, getattr(a, "headless", False), getattr(a, "no_start", False))
    if a.cmd == "status":
        try:
            st = _api(cfg, "/api/status")
        except (urllib.error.URLError, OSError):
            print("the Foundry Client is not running")
            return 1
        for c in st["components"]:
            print(f"{c['label']:<20} {c['state']:<10} {c['detail']}")
        return 0 if st["overall"] == "ok" else 1
    if a.cmd == "restart":
        q = "?force=true" if a.force else ""
        st = _api(cfg, f"/api/components/{a.component}/restart{q}", "POST", {})
        print(f"{st['label']}: {st['state']} {st['detail']}")
        if a.wait:
            for _ in range(600):
                time.sleep(2)
                st = _api(cfg, f"/api/status")
                c = next(c for c in st["components"] if c["name"] == a.component)
                if c["state"] == "running":
                    return 0
            return 1
        return 0
    if a.cmd == "install-autostart":
        print(f"autostart: {autostart.enable()}")
        return 0
    if a.cmd == "remove-autostart":
        autostart.disable()
        print("autostart removed")
        return 0
    if a.cmd == "import-legacy-secrets":
        done = secrets.migrate_legacy(config.expand(cfg["client"]["state_dir"]))
        print("imported: " + (", ".join(done) or "nothing found"))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

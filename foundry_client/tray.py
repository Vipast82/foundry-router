"""Tray icon (pystray): green = all running, amber = starting/waiting, red = a
component failed, grey = nothing enabled. Falls back to headless when the desktop
has no tray (e.g. GNOME without the AppIndicator extension)."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser

from . import APP_NAME, autostart
from .config import expand

COLORS = {"ok": (46, 160, 67), "busy": (219, 154, 4), "error": (207, 34, 46), "idle": (110, 118, 129)}


def icon_image(state: str, size: int = 64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=COLORS.get(state, COLORS["idle"]) + (255,))
    w = size // 8
    x0, y0 = size * 0.34, size * 0.24
    d.rectangle((x0, y0, x0 + w, size * 0.76), fill=(255, 255, 255, 255))           # F stem
    d.rectangle((x0, y0, size * 0.70, y0 + w), fill=(255, 255, 255, 255))           # top bar
    d.rectangle((x0, size * 0.46, size * 0.62, size * 0.46 + w), fill=(255, 255, 255, 255))
    return img


def open_folder(path: str) -> None:
    path = expand(path)
    if sys.platform == "win32":
        os.startfile(path)                                       # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])


def run_tray(mgr, settings_url: str, stop: threading.Event) -> bool:
    """Blocks until Quit. Returns False when no tray is available (caller goes headless)."""
    try:
        import pystray
    except Exception:                                            # noqa: BLE001
        return False

    def overall():
        st = mgr.status()
        if not any(c["want"] for c in st["components"]):
            return "idle", st
        return st["overall"], st

    def title():
        state, st = overall()
        bad = [c["label"] for c in st["components"] if c["want"] and c["state"] in ("error", "unhealthy")]
        if not bad and st.get("alerts"):
            return f"{APP_NAME}: " + st["alerts"][0]["text"][:100]
        return f"{APP_NAME}: " + ("problem with " + ", ".join(bad) if bad else
                                   {"ok": "all running", "busy": "starting…",
                                    "idle": "nothing enabled"}.get(state, state))

    def comp_items():
        def item(c):
            return pystray.MenuItem(f"{c['label']}: {c['state']}", None, enabled=False)
        return [item(c) for c in mgr.status()["components"] if c["enabled"]]

    backend = mgr.cfg["client"]["backend_url"].rstrip("/")
    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: title(), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open settings", lambda: webbrowser.open(settings_url), default=True),
        pystray.MenuItem("Components", pystray.Menu(lambda: comp_items())),
        pystray.MenuItem(lambda _i: "Update available: v" + mgr.update.snapshot()["latest"],
                         lambda: webbrowser.open(mgr.update.snapshot()["url"]),
                         visible=lambda _i: mgr.update.snapshot()["available"]),
        pystray.MenuItem("Restart Kilo server (safe point)", lambda: mgr.restart("kilo")),
        pystray.MenuItem("Restart coordinator (safe point)", lambda: mgr.restart("coordinator")),
        pystray.MenuItem("Restart everything now",
                         lambda: [mgr.restart(n, force=True) for n in mgr.order()
                                  if mgr.procs[n].want]),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Foundry control center", lambda: webbrowser.open(backend + "/ui/control")),
        pystray.MenuItem("Automation", lambda: webbrowser.open(backend + "/ui/automation")),
        pystray.MenuItem("Open logs folder", lambda: open_folder(str(mgr.log_dir))),
        pystray.MenuItem("Start at logon", lambda: (autostart.disable() if autostart.is_enabled()
                                                    else autostart.enable()),
                         checked=lambda _i: autostart.is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit (stops the stack)", lambda icon: icon.stop()),
    )
    icon = pystray.Icon("foundry-client", icon_image("busy"), APP_NAME, menu)

    def refresh():
        last = None
        while not stop.wait(4):
            state, _ = overall()
            if state != last:
                icon.icon = icon_image(state)
                last = state
            icon.title = title()
            try:
                icon.update_menu()
            except Exception:                                    # noqa: BLE001
                pass

    threading.Thread(target=refresh, daemon=True).start()
    try:
        icon.run()
    except Exception:                                            # noqa: BLE001
        return False
    return True

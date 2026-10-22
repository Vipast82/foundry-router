"""Start the client at logon. Windows: a shortcut-free Startup-folder .cmd (no COM
dependency); Linux: an XDG autostart .desktop file."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .components import self_argv

NAME = "FoundryClient"


def _launch_line() -> str:
    argv = self_argv()
    if not getattr(sys, "frozen", False) and sys.platform == "win32":
        # pythonw: no console window at logon
        py = Path(argv[0])
        w = py.with_name("pythonw.exe")
        if w.is_file():
            argv = [str(w), *argv[1:]]
    return " ".join(f'"{a}"' if " " in a else a for a in [*argv, "run"])


def _win_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{NAME}.cmd"


def _linux_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "foundry-client.desktop"


def path() -> Path:
    return _win_path() if sys.platform == "win32" else _linux_path()


def is_enabled() -> bool:
    return path().is_file()


def enable() -> Path:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    root = _root()
    if sys.platform == "win32":
        cd = f'cd /d "{root}"\r\n' if root else ""
        p.write_text(f'@echo off\r\n{cd}start "" {_launch_line()}\r\n', encoding="utf-8",
                     newline="")
    else:
        p.write_text("[Desktop Entry]\nType=Application\nName=Foundry Client\n"
                     f"Exec={_launch_line()}\nX-GNOME-Autostart-enabled=true\n"
                     + (f"Path={root}\n" if root else "")
                     + "Icon=foundry-client\nTerminal=false\n", encoding="utf-8")
    return p


def _root() -> str:
    """Running from source: the checkout's folder, so `-m foundry_client` finds it."""
    if getattr(sys, "frozen", False):
        return ""
    return str(Path(__file__).resolve().parent.parent)


def disable() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass

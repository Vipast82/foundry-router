"""Start the client at logon. Windows: a shortcut-free Startup-folder .cmd (no COM
dependency); Linux: an XDG autostart .desktop file."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .components import self_argv

NAME = "FoundryClient"


def _launch_argv() -> list:
    """What to start at logon. Inside an AppImage sys.executable is a temporary /tmp/.mount_*
    path that is gone after a reboot: the AppImage file itself ($APPIMAGE) is what persists."""
    img = os.environ.get("APPIMAGE") or ""
    if img and os.path.isfile(img):
        return [img]
    return self_argv()


def _launch_line(extra: tuple = ("run",)) -> str:
    argv = _launch_argv()
    if not getattr(sys, "frozen", False) and sys.platform == "win32":
        # pythonw: no console window at logon
        py = Path(argv[0])
        w = py.with_name("pythonw.exe")
        if w.is_file():
            argv = [str(w), *argv[1:]]
    return " ".join(f'"{a}"' if " " in a else a for a in [*argv, *extra])


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
    if getattr(sys, "frozen", False) or os.environ.get("APPIMAGE"):
        return ""
    return str(Path(__file__).resolve().parent.parent)


def disable() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- systemd --user (Linux, headless)

SERVICE = "foundry-client.service"


def service_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / SERVICE


def service_text() -> str:
    root = _root()
    return ("[Unit]\nDescription=Foundry Client (Kilo server, coordinator, Rojo)\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={_launch_line(('run', '--headless'))}\n"
            + (f"WorkingDirectory={root}\n" if root else "")
            + "Restart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")


def install_service(activate: bool = True) -> Path:
    """Write ~/.config/systemd/user/foundry-client.service and enable it. For a start at
    boot without a login, run `loginctl enable-linger $USER` once."""
    if sys.platform == "win32":
        raise RuntimeError("systemd units are for Linux; on Windows use install-autostart")
    p = service_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(service_text(), encoding="utf-8")
    if activate and shutil.which("systemctl"):
        for args in (["daemon-reload"], ["enable", "--now", SERVICE]):
            try:
                subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                break
    return p


def remove_service() -> None:
    if shutil.which("systemctl"):
        try:
            subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        service_path().unlink()
    except FileNotFoundError:
        pass

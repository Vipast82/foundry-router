"""Start the client at logon or at boot.

At logon (default, `install-autostart` / `--at-logon`): Windows: a shortcut-free
Startup-folder .cmd (no COM dependency); Linux: an XDG autostart .desktop file.

At boot (`install-autostart --at-boot`, Windows): a Task Scheduler task "At
startup, run whether the user is logged on or not" (BOOT_TASK) that runs
`pythonw -m foundry_client run --headless` in the checkout folder (the deploy
dir). It is created with `schtasks /Create /XML ... /RU <user> /RP *`, so
Windows itself asks for the account password in the console: this code never
takes, sees or stores a password. Linux: install-service (systemd --user) plus
`loginctl enable-linger`."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .components import self_argv

NAME = "FoundryClient"


def _launch_argv() -> list:
    """What to start at logon. Inside an AppImage sys.executable is a temporary /tmp/.mount_*
    path that is gone after a reboot: the AppImage file itself ($APPIMAGE) is what persists."""
    img = os.environ.get("APPIMAGE") or ""
    if img and os.path.isfile(img):
        return [img]
    return self_argv()


def _windowless_argv() -> list:
    argv = _launch_argv()
    if not getattr(sys, "frozen", False) and sys.platform == "win32":
        # pythonw: no console window at logon
        py = Path(argv[0])
        w = py.with_name("pythonw.exe")
        if w.is_file():
            argv = [str(w), *argv[1:]]
    return argv


def _quote(argv) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def _launch_line(extra: tuple = ("run",)) -> str:
    return _quote([*_windowless_argv(), *extra])


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


# ---------------------------------------------------------------- Windows: at boot (Task Scheduler)

BOOT_TASK = "FoundryClient-Boot"
_RUN = subprocess.run          # tests replace this: no real Task Scheduler call


def boot_user() -> str:
    """DOMAIN\\user of whoever runs install-autostart (the account the task runs as)."""
    user = os.environ.get("USERNAME") or ""
    dom = os.environ.get("USERDOMAIN") or ""
    return f"{dom}\\{user}" if dom and user else user


def _x(s: str) -> str:
    from xml.sax.saxutils import escape
    return escape(str(s), {'"': "&quot;"})


def boot_command() -> tuple[str, str, str]:
    """(program, arguments, working directory) of the boot task."""
    argv = _windowless_argv()
    root = _root() or str(Path(argv[0]).parent)
    return argv[0], _quote([*argv[1:], "run", "--headless"]), root


def boot_task_xml(user: str) -> str:
    """Task Scheduler 1.2 XML: boot trigger (1 min delay for the network), logon
    type Password ("run whether the user is logged on or not"), no 72 h run
    limit, normal priority (the default 7 would slow Kilo and the coordinator,
    which inherit it), restarts on failure, never stopped on battery."""
    exe, args, root = boot_command()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Foundry Client: starts the workstation stack (Kilo, coordinator, Rojo) at boot, before anyone signs in. Created by foundry_client install-autostart --at-boot.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_x(user)}</UserId>
      <LogonType>Password</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure><Interval>PT5M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_x(exe)}</Command>
      <Arguments>{_x(args)}</Arguments>
      <WorkingDirectory>{_x(root)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def boot_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        r = _RUN(["schtasks", "/Query", "/TN", BOOT_TASK], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def enable_boot(user: str = "", xml_dir: Optional[Path] = None) -> dict:
    """Create (or replace) the boot task. schtasks prompts for the password of
    `user` in this console (/RP *): the prompt and the answer stay between the
    person and Windows. The at-logon entry is removed (a second start at sign-in
    would only open the settings page of the running client)."""
    if sys.platform != "win32":
        raise RuntimeError("--at-boot is for Windows; on Linux use install-service and "
                           "`loginctl enable-linger $USER`")
    user = user or boot_user()
    if not user:
        raise RuntimeError("could not tell which account to run as; pass --user DOMAIN\\name")
    import tempfile
    d = Path(xml_dir) if xml_dir else Path(tempfile.mkdtemp(prefix="foundry-boot-"))
    d.mkdir(parents=True, exist_ok=True)
    xml = d / "FoundryClient-Boot.xml"
    xml.write_text(boot_task_xml(user), encoding="utf-16")
    try:
        # no capture: schtasks asks "Please enter the run as password for ..."
        # on this console and reads the answer itself
        r = _RUN(["schtasks", "/Create", "/TN", BOOT_TASK, "/XML", str(xml), "/RU", user,
                  "/RP", "*", "/F"])
    finally:
        try:
            xml.unlink()
        except OSError:
            pass
    ok = r.returncode == 0
    if ok:
        disable()                                   # one start, at boot
    return {"ok": ok, "task": BOOT_TASK, "user": user, "returncode": r.returncode}


def disable_boot() -> bool:
    """Delete the boot task; True when one was there."""
    if sys.platform != "win32" or not boot_enabled():
        return False
    r = _RUN(["schtasks", "/Delete", "/TN", BOOT_TASK, "/F"], capture_output=True, timeout=30)
    return r.returncode == 0


def mode() -> str:
    """Which autostart is installed: boot | logon | boot+logon | service | off."""
    parts = []
    if boot_enabled():
        parts.append("boot")
    if is_enabled():
        parts.append("logon")
    if sys.platform != "win32" and service_path().is_file():
        parts.append("service")
    return "+".join(parts) or "off"


MODE_TEXT = {
    "boot": "at boot (Task Scheduler, runs before anyone signs in)",
    "logon": "at sign-in only: after a reboot nothing runs until someone signs in "
             "(install-autostart --at-boot)",
    "boot+logon": "at boot, and again at sign-in (the second start only opens the settings "
                  "page)",
    "service": "systemd --user service",
    "off": "not installed: a reboot stops the stack (install-autostart --at-boot)",
}


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

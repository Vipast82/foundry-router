"""Restart the Foundry Client in place (Status page "Restart client").

A detached helper waits until the old client's settings port is free, then starts
`<python> -m foundry_client run` in the same working directory. The old client exits
without stopping its components, so the new one adopts them ("started outside the
client") and no Kilo session, coordinator or Blender is interrupted."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

HELPER = r"""
import socket, subprocess, sys, time
port, cwd, exe = int(sys.argv[1]), sys.argv[2], sys.argv[3]
deadline = time.time() + 60
while time.time() < deadline:
    s = socket.socket()
    try:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            break
    finally:
        s.close()
    time.sleep(0.5)
flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0
subprocess.Popen([exe, "-m", "foundry_client", "run"], cwd=cwd, creationflags=flags,
                 close_fds=True)
"""


def spawn_successor(port: int, exe: str = "", cwd: str = "", popen=subprocess.Popen) -> int:
    """Start the detached helper; returns its pid."""
    exe = exe or sys.executable
    cwd = cwd or os.getcwd()
    flags = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0  # DETACHED | NEW_GROUP
    p = popen([exe, "-c", HELPER, str(int(port)), cwd, exe], cwd=cwd, creationflags=flags,
              close_fds=True)
    return int(getattr(p, "pid", 0) or 0)


def exit_soon(mgr, delay: float = 1.0, _exit=os._exit) -> threading.Thread:
    """Leave the components running (the successor adopts them) and exit after `delay`
    seconds, so the HTTP answer reaches the page first."""
    mgr.keep_components_on_exit = True

    def go():
        time.sleep(delay)
        try:
            mgr.stop_evt.set()
        finally:
            _exit(0)
    t = threading.Thread(target=go, name="foundry-client-restart", daemon=True)
    t.start()
    return t

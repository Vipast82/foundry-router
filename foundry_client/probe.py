"""Small blocking helpers shared by the health checks, the doctor and the router link."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def get_json(url: str, headers: Optional[dict] = None, timeout: float = 2.0
             ) -> tuple[int, Any, str]:
    """(http status, parsed body or None, error text). Status 0 = no answer at all."""
    req = urllib.request.Request(url, headers={"User-Agent": "foundry-client", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else None), ""
            except ValueError:
                return r.status, None, ""
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, None, str(getattr(e, "reason", e)) or type(e).__name__


def basic_auth(password: str, username: str = "kilo") -> dict:
    if not password:
        return {}
    return {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}


def port_owner_pid(port: int) -> Optional[int]:
    """The pid listening on 127.0.0.1:<port>, or None. psutil when installed, else netstat/ss."""
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.status == "LISTEN" and c.laddr and c.laddr.port == port and c.pid:
                return int(c.pid)
        return None
    except ImportError:
        pass
    except Exception:                                            # noqa: BLE001 (access denied ...)
        pass
    try:
        if sys.platform == "win32":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                                 timeout=15, creationflags=NO_WINDOW).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                        and parts[1].rsplit(":", 1)[-1] == str(port):
                    return int(parts[4])
        else:
            out = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                if re.search(rf":{port}\s", line):
                    m = re.search(r"pid=(\d+)", line)
                    if m:
                        return int(m.group(1))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                 text=True, timeout=15, creationflags=NO_WINDOW).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

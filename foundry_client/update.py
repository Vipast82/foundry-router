"""Is a newer Foundry Client release out? Looks at the GitHub releases of the configured
repository and reports; it never downloads or installs anything."""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

from . import __version__
from .probe import get_json


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", str(v).split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


class UpdateCheck:
    def __init__(self, repo_fn: Callable[[], str], clock: Callable[[], float] = time.time,
                 get: Callable = get_json, current: str = __version__):
        self.repo_fn, self.clock, self.get, self.current = repo_fn, clock, get, current
        self.lock = threading.Lock()
        self.state = {"current": current, "latest": "", "available": False, "url": "",
                      "name": "", "published": "", "checked_at": 0.0, "error": ""}

    def poll(self) -> dict:
        repo = self.repo_fn().strip("/")
        code, body, err = self.get(f"https://api.github.com/repos/{repo}/releases/latest",
                                   {"Accept": "application/vnd.github+json"}, 10)
        with self.lock:
            s = self.state
            s["checked_at"] = self.clock()
            if code == 200 and isinstance(body, dict) and body.get("tag_name"):
                latest = str(body["tag_name"]).lstrip("vV")
                s.update(latest=latest, url=str(body.get("html_url") or
                                                f"https://github.com/{repo}/releases"),
                         name=str(body.get("name") or ""), published=str(body.get("published_at") or ""),
                         available=parse_version(latest) > parse_version(self.current), error="")
            elif code == 404:       # no release published yet
                s.update(error="", available=False, latest="")
            else:
                s["error"] = err or f"HTTP {code}"
            return dict(s)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

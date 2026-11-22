"""Studio auto-reopen (0.145.0): close the TEST place's Studio window politely and
open the TEST place again, so Rojo's Auto Reconnect connects and syncs without a
click.

Why: the Studio Rojo plugin reconnects only when a place is opened. After Rojo or
Studio restarts, a sprint used to wait for the owner to click Plugins -> Rojo ->
Connect.

Opt-in ([studio] auto_reopen) and narrow by design:
- only the Studio window whose title contains `title_match` is ever touched; any
  other Studio window (LIVE, another place, a local file) is left alone
- the place opened is always `place_id` / `universe_id`, never one in `never_place_ids`
- a polite close (WM_CLOSE), never a kill: if Studio asks anything (save, publish,
  discard) the question is cancelled, Studio stays open and the job fails with the
  dialog's title, so unsaved work is never lost and no crash-recovery prompt follows
- waits for the PC to be idle (`idle_minutes`), at most `max_wait_minutes`, then goes
  ahead with an alert; at most `max_per_hour` reopens

The coordinator calls it (POST /api/studio/reopen) only after its own guards: no
other sprint holds Studio, no gate runner or playtest is live. A job runs in a
background thread; GET /api/studio/jobs/<id> reports it. `dry_run` checks the guards
and says what it would do without touching Studio.

v0.181.229, from the Studio update of 2026-11-17 (0.747 -> 0.748):
- one reopen at a time. ORI-2 asked at 21:47 and FIN-5a1 at 21:52; both jobs sat in the
  idle wait, the first opened the TEST place at 21:58 and the second closed that window
  at 22:02 to open it again. Two reopens in five minutes used up `max_per_hour`, and Studio
  stayed closed until 23:00. A request made while a reopen runs, or within REUSE_S of one
  that succeeded, gets that job back.
- a Studio update is waited for: while the installer runs nothing is closed, opened or
  counted (up to UPDATE_WAIT_S), and the wait for the TEST window does not run out while
  it installs.
- an update starts the count again: when the newest Studio logs name two versions and the
  new one first ran inside the last hour, reopens made before that do not count against
  `max_per_hour` (the launch that triggered the update is one of them). The job says which
  versions it saw.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

STUDIO_EXE = "robloxstudiobeta.exe"
INSTALLER_EXE = "robloxstudioinstaller"       # RobloxStudioInstaller.exe, any suffix
CLOSE_WAIT_S = 60
OPEN_WAIT_S = 240
SETTLE_S = 30
REUSE_S = 300                 # a reopen that succeeded this recently answers the next request
UPDATE_WAIT_S = 900           # how long a job waits for a running Studio update
# %LOCALAPPDATA%/Roblox/logs/0.748.0.7481526_20261118T035745Z_Studio_9151C_last.log
_LOG_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)_(\d{8}T\d{6})Z_Studio_")


def log_versions(log_dir: Optional[str] = None, limit: int = 60) -> list:
    """[(first-line time, version)] of the newest Studio logs, newest first: the version
    and UTC start time are in each log's name. [] when the folder cannot be read."""
    d = Path(log_dir or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "logs"))
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return []
    for name in names:
        m = _LOG_RE.match(name)
        if not m:
            continue
        try:
            ts = calendar.timegm(time.strptime(m.group(2), "%Y%m%dT%H%M%S"))
        except ValueError:
            continue
        out.append((float(ts), m.group(1)))
    out.sort(reverse=True)
    return out[:limit]

DEFAULTS = {
    "auto_reopen": False,
    "place_id": 0,
    "universe_id": 0,
    "title_match": "",
    "never_place_ids": [],
    "idle_minutes": 2,
    "max_wait_minutes": 10,
    "max_per_hour": 2,
    # 2026-10-30 Victor: "make Rojo automatically connect and sync so it doesn't ask"
    "rojo_auto_sync": True,
}

# Rojo plugin settings (rojo-rbx/rojo v7.7.1 plugin/src/App/init.lua): autoReconnect
# connects on place open when that place synced before (priorEndpoints[placeId]) and
# the server's project name matches; confirmationBehavior "Never" accepts every patch.
ROJO_AUTO_SYNC = {"Rojo_autoReconnect": True, "Rojo_confirmationBehavior": "Never"}


def plugin_settings_files(base: Optional[str] = None) -> list[Path]:
    """Studio's per-user plugin settings: %LOCALAPPDATA%/Roblox/<userId>/InstalledPlugins/
    0/settings.json (local-file plugins such as Rojo keep their settings there)."""
    root = Path(base or os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox"))
    try:
        return sorted(p for p in root.glob("*/InstalledPlugins/0/settings.json")
                      if p.parent.parent.parent.name.isdigit())
    except OSError:
        return []


def apply_rojo_auto_sync(never_place_ids=(), base: Optional[str] = None) -> list[str]:
    """Turn on Rojo auto-reconnect and sync-without-asking in every Studio user's plugin
    settings; drop any saved Rojo connection for a never_place_ids place (LIVE) so it is
    never auto-synced. Studio keeps these in memory and writes them back, so call this
    only while Studio is closed. Returns the files changed."""
    never = {str(x) for x in never_place_ids or ()}
    changed = []
    for p in plugin_settings_files(base):
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        new = dict(data)
        new.update(ROJO_AUTO_SYNC)
        prior = new.get("Rojo_priorEndpoints")
        if isinstance(prior, dict) and never & set(map(str, prior)):
            new["Rojo_priorEndpoints"] = {k: v for k, v in prior.items() if str(k) not in never}
        if new != data:
            tmp = p.with_name(p.name + ".foundry-tmp")
            tmp.write_text(json.dumps(new), encoding="utf-8")
            os.replace(tmp, p)
            changed.append(str(p))
    return changed


def validate(st: dict) -> list[str]:
    errs = []
    for k in ("auto_reopen", "rojo_auto_sync"):
        if not isinstance(st.get(k), bool):
            errs.append(f"studio.{k} must be true or false")
    for k in ("place_id", "universe_id", "idle_minutes", "max_wait_minutes", "max_per_hour"):
        v = st.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            errs.append(f"studio.{k} must be a whole number, 0 or more")
    if not isinstance(st.get("title_match"), str):
        errs.append("studio.title_match must be text")
    nv = st.get("never_place_ids")
    if not (isinstance(nv, list) and all(isinstance(x, int) and not isinstance(x, bool)
                                         for x in nv)):
        errs.append("studio.never_place_ids must be a list of place ids")
    if st.get("auto_reopen"):
        if not st.get("place_id") or not st.get("universe_id"):
            errs.append("studio.place_id and studio.universe_id are required for auto_reopen")
        if len(str(st.get("title_match") or "").strip()) < 3:
            errs.append("studio.title_match (e.g. \"[TEST]\") is required for auto_reopen")
        if st.get("place_id") in (nv or []):
            errs.append("studio.place_id is listed in studio.never_place_ids")
    return errs


def launch_uri(st: dict) -> str:
    return (f"roblox-studio:1+launchmode:edit+task:EditPlace+placeId:{int(st['place_id'])}"
            f"+universeId:{int(st['universe_id'])}")


# ------------------------------------------------------------------ Windows backend

class WinBackend:
    """The few Win32 calls the reopen needs (ctypes, no extra dependency)."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Studio auto-reopen runs on Windows only")
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.u32 = ctypes.windll.user32
        self.k32 = ctypes.windll.kernel32

    def _exe(self, pid: int) -> str:
        h = self.k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = self.ct.create_unicode_buffer(1024)
            n = self.wt.DWORD(1024)
            if self.k32.QueryFullProcessImageNameW(h, 0, buf, self.ct.byref(n)):
                return os.path.basename(buf.value).lower()
            return ""
        finally:
            self.k32.CloseHandle(h)

    def windows(self) -> list[dict]:
        """Visible top-level windows of Studio processes: {hwnd, pid, title}."""
        out, exes = [], {}
        proto = self.ct.WINFUNCTYPE(self.wt.BOOL, self.wt.HWND, self.wt.LPARAM)

        def cb(hwnd, _):
            if not self.u32.IsWindowVisible(hwnd):
                return True
            pid = self.wt.DWORD()
            self.u32.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
            p = int(pid.value)
            if p not in exes:
                exes[p] = self._exe(p)
            if exes[p] != STUDIO_EXE:
                return True
            n = self.u32.GetWindowTextLengthW(hwnd)
            buf = self.ct.create_unicode_buffer(n + 1)
            self.u32.GetWindowTextW(hwnd, buf, n + 1)
            out.append({"hwnd": int(hwnd), "pid": p, "title": buf.value})
            return True
        self.u32.EnumWindows(proto(cb), 0)
        return out

    def pid_alive(self, pid: int) -> bool:
        h = self.k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = self.wt.DWORD()
            return bool(self.k32.GetExitCodeProcess(h, self.ct.byref(code))) and code.value == 259
        finally:
            self.k32.CloseHandle(h)

    def close(self, hwnd: int) -> None:
        self.u32.PostMessageW(hwnd, 0x0010, 0, 0)              # WM_CLOSE

    def idle_seconds(self) -> float:
        class LASTINPUTINFO(self.ct.Structure):
            _fields_ = [("cbSize", self.wt.UINT), ("dwTime", self.wt.DWORD)]
        li = LASTINPUTINFO()
        li.cbSize = self.ct.sizeof(LASTINPUTINFO)
        if not self.u32.GetLastInputInfo(self.ct.byref(li)):
            return 0.0
        return max(0.0, (self.k32.GetTickCount() - li.dwTime) / 1000.0)

    def launch(self, uri: str) -> None:
        os.startfile(uri)                                          # the roblox-studio: handler

    def updating(self) -> str:
        """'' or the Studio installer that is running ("RobloxStudioInstaller.exe pid 1234"):
        Studio is being updated. Needs psutil; without it the answer is always ''."""
        try:
            import psutil
        except ImportError:
            return ""
        try:
            for p in psutil.process_iter(["name", "pid"]):
                name = str(p.info.get("name") or "")
                if name.lower().startswith(INSTALLER_EXE):
                    return f"{name} pid {p.info.get('pid')}"
        except Exception:                                         # noqa: BLE001
            return ""
        return ""

    def log_versions(self) -> list:
        return log_versions()


# ------------------------------------------------------------------ the reopen job

class StudioReopen:
    def __init__(self, get_settings: Callable[[], dict], state_dir: str,
                 backend_factory: Callable[[], object] = WinBackend,
                 alert: Callable[[str, str], None] = lambda key, text: None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 plugin_settings_dir: Optional[str] = None):
        self.plugin_settings_dir = plugin_settings_dir
        self.get_settings = get_settings
        self.state_dir = state_dir
        self.backend_factory = backend_factory
        self.alert = alert
        self.clock = clock
        self.sleep = sleep
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    # -- settings / history

    def settings(self) -> dict:
        st = dict(DEFAULTS)
        st.update(self.get_settings() or {})
        return st

    def _history_path(self) -> Path:
        return Path(os.path.expanduser(self.state_dir)) / "studio-reopen.json"

    def _history(self) -> list[float]:
        try:
            v = json.loads(self._history_path().read_text(encoding="utf-8"))
            return [float(x) for x in v if isinstance(x, (int, float))]
        except (OSError, ValueError, TypeError):
            return []

    def _record(self) -> None:
        now = self.clock()
        keep = [t for t in self._history() if now - t < 3600] + [now]
        try:
            p = self._history_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(keep), encoding="utf-8")
        except OSError:
            pass

    # -- jobs

    def start(self, task: str = "", dry_run: bool = False, wait: bool = False) -> dict:
        """Start a reopen job; {"job": id, "state": ...}. Guards that fail before any
        action make the job fail at once with the reason."""
        if not dry_run:
            with self.lock:
                cur = self._reusable()
                if cur is not None:
                    # the request joins the reopen under way (or just made): a second job
                    # would close the window the first one opened
                    cur["steps"].append({"ts": self.clock(), "text": (
                        f"also asked for by {str(task)[:80] or 'another task'}: one reopen "
                        f"serves both")[:300]})
                    cur.setdefault("joined", []).append(str(task)[:80])
                    return json.loads(json.dumps(cur))
        jid = "sr-" + uuid.uuid4().hex[:10]
        job = {"job": jid, "task": str(task)[:80], "dry_run": bool(dry_run), "state": "running",
               "steps": [], "result": "", "started": self.clock(), "ended": None}
        with self.lock:
            self.jobs[jid] = job
            for k in list(self.jobs)[:-50]:
                self.jobs.pop(k, None)
        if wait:
            self._run(job)
        else:
            threading.Thread(target=self._run, args=(job,), daemon=True,
                             name=f"studio-reopen-{jid}").start()
        return self.get(jid)

    def get(self, jid: str) -> Optional[dict]:
        with self.lock:
            j = self.jobs.get(jid)
            return json.loads(json.dumps(j)) if j else None

    def _reusable(self) -> Optional[dict]:
        """The real (not dry-run) job a new request joins: one still running, or one that
        reopened the TEST place within REUSE_S. Called with the lock held."""
        now = self.clock()
        for j in reversed(list(self.jobs.values())):
            if j.get("dry_run"):
                continue
            if j["state"] == "running":
                return j
            if j["state"] == "done" and now - float(j.get("ended") or 0) < REUSE_S:
                return j
        return None

    # -- a Studio update

    def _versions(self, be) -> list:
        fn = getattr(be, "log_versions", None)
        if not callable(fn):
            return []
        try:
            return [(float(ts), str(v)) for ts, v in fn() or []]
        except Exception:                                         # noqa: BLE001
            return []

    def _updated_since(self, vers: list) -> tuple:
        """(time the newest version first ran, "old -> new") when the newest logs name two
        versions and the new one first ran inside the last hour; else (0.0, "")."""
        if not vers:
            return 0.0, ""
        cur = vers[0][1]
        prev = next((v for _, v in vers if v != cur), "")
        if not prev:
            return 0.0, ""
        first = min(ts for ts, v in vers if v == cur)
        if self.clock() - first >= 3600:
            return 0.0, ""
        return first, f"{prev} -> {cur}"

    def _wait_for_update(self, job: dict, be) -> str:
        """'' once no Studio update is running (waited for up to UPDATE_WAIT_S), else why
        the reopen is not made. A dry run only says what it sees."""
        fn = getattr(be, "updating", None)
        what = fn() if callable(fn) else ""
        if not what:
            return ""
        if job["dry_run"]:
            self._step(job, f"Studio is updating ({what}): a real reopen would wait for it")
            return ""
        self._step(job, f"Studio is updating ({what}): waiting for it to finish")
        t0 = self.clock()
        while fn() and self.clock() - t0 < UPDATE_WAIT_S:
            self.sleep(5)
        if fn():
            return (f"Studio is still updating after {UPDATE_WAIT_S // 60} min ({what}): "
                    f"nothing was closed, opened or counted")
        self._step(job, f"the update finished after {int(self.clock() - t0)} s")
        return ""

    def _step(self, job: dict, text: str) -> None:
        with self.lock:
            job["steps"].append({"ts": self.clock(), "text": text[:300]})

    def _end(self, job: dict, state: str, result: str) -> None:
        with self.lock:
            job["state"], job["result"], job["ended"] = state, result[:500], self.clock()

    def _run(self, job: dict) -> None:
        try:
            state, result = self._reopen(job)
        except Exception as e:                                    # noqa: BLE001
            state, result = "failed", f"{type(e).__name__}: {e}"
        self._end(job, state, result)
        if state == "failed" and not job["dry_run"]:
            self.alert("studio_reopen", f"Studio auto-reopen did not finish: {result}")

    def _reopen(self, job: dict) -> tuple[str, str]:
        st = self.settings()
        errs = validate(st)
        if not st.get("auto_reopen") and not job["dry_run"]:
            return "failed", "studio.auto_reopen is off"
        if errs:
            return "failed", "; ".join(errs)
        be = self.backend_factory()
        why = self._wait_for_update(job, be)
        if why:
            return "failed", why
        before = self._versions(be)
        since, change = self._updated_since(before)
        if change:
            self._step(job, f"Studio was updated ({change}) "
                            f"{int((self.clock() - since) // 60)} min ago: reopens before the "
                            f"update do not count against max_per_hour")
        recent = [t for t in self._history() if self.clock() - t < 3600 and t >= since]
        if not job["dry_run"] and len(recent) >= int(st["max_per_hour"]):
            return "failed", (f"rate limit: {len(recent)} reopen(s) in the last hour "
                              f"(max_per_hour {st['max_per_hour']})")
        match = str(st["title_match"]).strip().lower()
        wins = be.windows()
        mine = [w for w in wins if match in w["title"].lower()]
        others = [w for w in wins if w not in mine]
        if len({w["pid"] for w in mine}) > 1:
            return "failed", "more than one Studio window matches title_match: " + \
                ", ".join(w["title"] for w in mine)[:200]
        uri = launch_uri(st)
        self._step(job, f"found {len(mine)} TEST window(s); {len(others)} other Studio "
                        f"window(s) left alone" + (": " + ", ".join(w["title"] for w in others)
                                                    if others else ""))
        if job["dry_run"]:
            what = (f"would close '{mine[0]['title']}' (pid {mine[0]['pid']}) politely, "
                    if mine else "no TEST window open: would only ")
            return "done", what + f"open {uri}"
        # idle guard: do not pull Studio away while someone is working at the PC
        idle_need = int(st["idle_minutes"]) * 60
        deadline = self.clock() + int(st["max_wait_minutes"]) * 60
        while idle_need and be.idle_seconds() < idle_need and self.clock() < deadline:
            self.sleep(5)
        if idle_need and be.idle_seconds() < idle_need:
            self.alert("studio_reopen", "Reopening the TEST place in Studio now so Rojo "
                                        "reconnects (an automation sprint needs it).")
            self._step(job, "the PC was not idle within max_wait_minutes: going ahead")
        if mine:
            w = mine[0]
            self._step(job, f"closing '{w['title']}' (pid {w['pid']})")
            be.close(w["hwnd"])
            t0 = self.clock()
            while be.pid_alive(w["pid"]) and self.clock() - t0 < CLOSE_WAIT_S:
                self.sleep(1)
                dialogs = [x for x in be.windows() if x["pid"] == w["pid"]
                           and x["hwnd"] != w["hwnd"]]
                if dialogs and self.clock() - t0 >= 2:
                    for d in dialogs:
                        be.close(d["hwnd"])                     # cancel the question
                    return "failed", (f"Studio asked '{dialogs[0]['title'] or 'a question'}' "
                                      f"(unsaved changes?): cancelled, Studio left open")
            if be.pid_alive(w["pid"]):
                return "failed", f"Studio (pid {w['pid']}) did not close within {CLOSE_WAIT_S} s"
            self._step(job, "Studio closed")
        if st.get("rojo_auto_sync"):
            # Studio is closed now, so its plugin settings can be written without being
            # overwritten from memory: Rojo reconnects and syncs on open without a prompt
            try:
                ch = apply_rojo_auto_sync(st.get("never_place_ids") or [], self.plugin_settings_dir)
                note = (f"updated {len(ch)} settings file(s)" if ch else "already set")
                if ch and others:
                    note += "; other open Studios may write their own copy back"
                self._step(job, f"Rojo auto-reconnect + sync without asking: {note}")
            except OSError as e:
                self._step(job, f"Rojo settings not updated: {e}")
        self._record()
        be.launch(uri)
        self._step(job, f"opened {uri}")
        t0 = self.clock()
        upd = getattr(be, "updating", None)
        installing = 0.0                 # time the installer ran: not counted as waiting
        noted = False
        while self.clock() - t0 - installing < OPEN_WAIT_S and installing < UPDATE_WAIT_S:
            self.sleep(3)
            if any(match in x["title"].lower() for x in be.windows()):
                self._step(job, "TEST window is open; waiting for Rojo to reconnect")
                self.sleep(SETTLE_S)
                after = self._versions(be)
                old, new = (before[0][1] if before else ""), (after[0][1] if after else "")
                note = (f" (Studio updated {old} -> {new})" if old and new and old != new
                        else f" (Studio {new})" if new else "")
                return "done", "TEST place reopened" + note
            if callable(upd) and upd():
                installing += 3
                if not noted:
                    noted = True
                    self._step(job, "the launch started a Studio update: waiting for it")
        if installing >= UPDATE_WAIT_S:
            return "failed", (f"Studio was still updating {UPDATE_WAIT_S // 60} min after the "
                              f"launch; the TEST window did not appear")
        return "failed", f"the TEST window did not appear within {OPEN_WAIT_S} s"

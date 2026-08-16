#!/usr/bin/env python3
"""Meridian auth companion — a tiny HTTP shim on the Meridian host.

Foundry Router can READ Meridian's quota (POST /v1/usage/quota) but it can't fix
a stale login: the fix is a HOST CLI, and it's interactive —

    meridian profile login <profile> --headless

prints a sign-in URL, waits for you to authenticate in a browser, then asks you
to paste a code back on stdin. Foundry (a remote client) can neither run the CLI
nor answer that prompt. This service closes the gap so you never SSH in to
re-auth:

    GET  /health          -> {"ok": true}
    POST /refresh         -> `meridian refresh-token` (non-interactive) — enough
                             when the token has merely EXPIRED
         body: {"profile": "victor"}  (profile optional)
    POST /login/start     -> spawn `meridian profile login <profile> --headless`,
                             capture the sign-in URL, HOLD the process open
         body: {"profile": "victor"}
         -> {"ok", "session_id", "url", "output"}
    POST /login/complete  -> write the pasted code to that held process's stdin,
                             return its result
         body: {"session_id": "...", "code": "..."}
         -> {"ok", "exit_code", "output"}
    POST /login/cancel    -> kill a held session   body: {"session_id": "..."}

Trust model — identical to the gateway inspect companion: it runs host commands
and touches live Claude credentials, so BIND IT TO LOCALHOST, firewall it to
Foundry Router's host only, and set a bearer token. The pasted code is a short-
lived OAuth authorization code; it transits Foundry's admin backend once and is
not stored. Stdlib only — no pip install.

Run:
    MERIDIAN_AUTH_TOKEN=$(openssl rand -hex 16) \
    python3 meridian_auth_service.py            # 127.0.0.1:8898 by default

Env:
    MERIDIAN_AUTH_BIND     default 127.0.0.1
    MERIDIAN_AUTH_PORT     default 8898
    MERIDIAN_AUTH_TOKEN    optional bearer token (STRONGLY recommended)
    MERIDIAN_BIN           meridian binary (default "meridian")
    MERIDIAN_URL_TIMEOUT   seconds to wait for the sign-in URL (default 60)
    MERIDIAN_LOGIN_TTL     seconds a pending login session is held (default 600)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("MERIDIAN_AUTH_BIND", "127.0.0.1")
PORT = int(os.environ.get("MERIDIAN_AUTH_PORT", "8898"))
TOKEN = os.environ.get("MERIDIAN_AUTH_TOKEN", "")
MERIDIAN = os.environ.get("MERIDIAN_BIN", "meridian")
URL_TIMEOUT = int(os.environ.get("MERIDIAN_URL_TIMEOUT", "60"))
LOGIN_TTL = int(os.environ.get("MERIDIAN_LOGIN_TTL", "600"))

# A profile name is operator-facing but goes on a command line — constrain it
# hard so this can never become a shell-injection foothold (we never use a
# shell, but defense in depth).
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# An OAuth authorization code: base64url-ish plus the '#state' suffix Meridian
# prints. No whitespace, bounded length.
_SAFE_CODE = re.compile(r"^[A-Za-z0-9._~#/+=-]{1,4096}$")
_URL_RE = re.compile(r"https://\S+")

# session_id -> {proc, buf(list[str]), url, done, returncode, started}
SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _reap() -> None:
    """Drop finished or stale sessions so a held process can't leak forever."""
    now = time.time()
    with _LOCK:
        for sid in list(SESSIONS):
            s = SESSIONS[sid]
            if s.get("done") or now - s["started"] > LOGIN_TTL:
                proc = s.get("proc")
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                SESSIONS.pop(sid, None)


def _reader(session: dict) -> None:
    """Drain the login process's stdout: buffer every line, and flag the first
    sign-in URL. Lines are newline-terminated (the URL, the instructions); the
    final 'Paste code:' prompt has no newline, so this thread simply blocks
    there until the code is written and the process exits (then EOF)."""
    proc = session["proc"]
    for line in iter(proc.stdout.readline, ""):
        session["buf"].append(line)
        if session["url"] is None:
            m = _URL_RE.search(line)
            if m:
                session["url"] = m.group(0).rstrip(".,)")
    try:
        proc.stdout.close()
    except Exception:
        pass
    session["returncode"] = proc.wait()
    session["done"] = True


def _start_login(profile: str) -> dict:
    proc = subprocess.Popen(
        [MERIDIAN, "profile", "login", profile, "--headless"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1)
    sid = secrets.token_hex(8)
    session = {"proc": proc, "buf": [], "url": None, "done": False,
               "returncode": None, "started": time.time()}
    with _LOCK:
        SESSIONS[sid] = session
    threading.Thread(target=_reader, args=(session,), daemon=True).start()
    # Wait for the reader to surface the URL (or the process to die early).
    deadline = time.time() + URL_TIMEOUT
    while time.time() < deadline:
        if session["url"] or session["done"]:
            break
        time.sleep(0.1)
    output = "".join(session["buf"])
    if session["url"]:
        return {"ok": True, "session_id": sid, "url": session["url"],
                "output": output}
    # No URL: the command failed, or hasn't printed one in time.
    with _LOCK:
        SESSIONS.pop(sid, None)
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    return {"ok": False, "session_id": None, "url": None,
            "output": output or f"no sign-in URL within {URL_TIMEOUT}s"}


def _complete_login(sid: str, code: str) -> dict:
    with _LOCK:
        session = SESSIONS.get(sid)
    if not session:
        return {"ok": False, "error": "unknown or expired session_id",
                "exit_code": None, "output": ""}
    proc = session["proc"]
    try:
        proc.stdin.write(code + "\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as e:
        return {"ok": False, "error": f"could not send code: {e}",
                "exit_code": None, "output": "".join(session["buf"])}
    # The reader thread captures the rest and sets done/returncode on exit.
    deadline = time.time() + 60
    while time.time() < deadline and not session["done"]:
        time.sleep(0.1)
    rc = session["returncode"]
    output = "".join(session["buf"])
    with _LOCK:
        SESSIONS.pop(sid, None)
    if not session["done"]:
        return {"ok": False, "error": "login did not finish in time",
                "exit_code": None, "output": output}
    return {"ok": rc == 0, "exit_code": rc, "output": output}


def _refresh(profile: str) -> dict:
    cmd = [MERIDIAN, "refresh-token"]
    if profile:
        cmd += ["--profile", profile]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "refresh timed out after 60s"}
    except FileNotFoundError:
        return {"ok": False, "error": f"{MERIDIAN!r} not found on PATH"}
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.returncode else "")
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
            "output": out.strip()[:2000]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or "{}")

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        if not self._authed():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"ok": False, "error": "bad JSON body"})
        _reap()

        if path == "/refresh":
            profile = str(body.get("profile") or "").strip()
            if profile and not _SAFE_PROFILE.match(profile):
                return self._send(400, {"ok": False, "error": "bad profile name"})
            return self._send(200, _refresh(profile))

        if path == "/login/start":
            profile = str(body.get("profile") or "").strip()
            if not _SAFE_PROFILE.match(profile):
                return self._send(400, {"ok": False,
                                        "error": "profile must match [A-Za-z0-9._-]"})
            try:
                return self._send(200, _start_login(profile))
            except FileNotFoundError:
                return self._send(500, {"ok": False,
                                        "error": f"{MERIDIAN!r} not found on PATH"})

        if path == "/login/complete":
            sid = str(body.get("session_id") or "")
            code = str(body.get("code") or "").strip()
            if not _SAFE_CODE.match(code):
                return self._send(400, {"ok": False, "error": "bad code format"})
            return self._send(200, _complete_login(sid, code))

        if path == "/login/cancel":
            sid = str(body.get("session_id") or "")
            with _LOCK:
                s = SESSIONS.pop(sid, None)
            if s and s.get("proc") and s["proc"].poll() is None:
                try:
                    s["proc"].kill()
                except Exception:
                    pass
            return self._send(200, {"ok": True})

        self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, *args):  # keep stdout quiet
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"meridian auth service on http://{BIND}:{PORT} "
          f"(token {'set' if TOKEN else 'NOT set — set MERIDIAN_AUTH_TOKEN'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()

"""The code-checks service: runs a Luau checker on a snippet and reports what it said.

A container of its own, so the router's image stays as it is and the whole thing
is optional: start it with `docker compose --profile code-checks up -d` and point
a rule at `http://code-checks:8099` (Routing and brain → Code checks).

    POST /run   {"command": "luau-lsp analyze {file}", "code": "...",
                 "suffix": ".luau", "timeout": 20}
             -> {"status": 0, "output": ""}            the code passed
             -> {"status": 1, "output": "..."}         the checker complained
             -> 400 {"error": "..."}                   the request was wrong
    GET /health -> {"ok": true, "programs": {"luau-lsp": "1.70.1", ...}}

Only the programs in ALLOWED are run, never a shell, and only on a file this
service wrote in its own temporary directory. The container has no volumes, no
credentials and drops every capability, because what it looks at is written by a
model: a checker that one day *executes* what it is given (a Blender script, say)
can be added here, and it will still be the only thing that runs it.

Standard library only; no configuration file.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8099"))
ALLOWED = {"luau-lsp", "selene", "stylua"}
MAX_CODE = 1_000_000
MAX_TIMEOUT = 120
AT_ONCE = threading.BoundedSemaphore(int(os.environ.get("AT_ONCE", "4")))
VERSION_ARG = {"luau-lsp": "--version", "selene": "--version", "stylua": "--version"}


def programs() -> dict:
    out = {}
    for name in sorted(ALLOWED):
        path = shutil.which(name)
        if not path:
            continue
        try:
            r = subprocess.run([path, VERSION_ARG[name]], capture_output=True, text=True, timeout=20)
            out[name] = (r.stdout or r.stderr or "").strip().splitlines()[0][:80]
        except Exception as e:                                    # noqa: BLE001
            out[name] = f"({type(e).__name__})"
    return out


def run(body: dict) -> tuple:
    """(http status, answer). Never raises."""
    command = str(body.get("command") or "").strip()
    code = body.get("code")
    if not command:
        return 400, {"error": "command is required"}
    if not isinstance(code, str) or not code.strip():
        return 400, {"error": "code is required"}
    if len(code) > MAX_CODE:
        return 400, {"error": f"the code is larger than {MAX_CODE // 1000} kB"}
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return 400, {"error": f"the command could not be read: {e}"}
    if not argv:
        return 400, {"error": "the command is empty"}
    name = os.path.basename(argv[0])
    if name not in ALLOWED:
        return 400, {"error": f"{name!r} is not one of this service's checkers "
                              f"({', '.join(sorted(ALLOWED))})"}
    path = shutil.which(name)
    if not path:
        return 400, {"error": f"{name} is not installed in this service"}
    try:
        timeout = max(1, min(MAX_TIMEOUT, int(body.get("timeout") or 20)))
    except (TypeError, ValueError):
        timeout = 20
    suffix = str(body.get("suffix") or ".luau")
    if not suffix.startswith(".") or len(suffix) > 10 or "/" in suffix or "\\" in suffix:
        suffix = ".luau"
    with AT_ONCE:
        d = tempfile.mkdtemp(prefix="check-")
        f = os.path.join(d, "snippet" + suffix)
        try:
            with open(f, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(code)
            args = [path] + [a.replace("{file}", f).replace("{dir}", d) for a in argv[1:]]
            if "{file}" not in command:
                args.append(f)
            try:
                p = subprocess.run(args, cwd=d, capture_output=True, text=True, timeout=timeout,
                                   env={"PATH": os.environ.get("PATH", ""), "HOME": d,
                                        "NO_COLOR": "1", "TMPDIR": d})
            except subprocess.TimeoutExpired:
                return 200, {"status": -1,
                             "output": f"{name} did not finish within {timeout}s"}
            except OSError as e:
                return 400, {"error": f"{name} could not be started: {e}"}
            out = ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
            return 200, {"status": p.returncode, "output": out.replace(f, "the file")[:8000]}
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "foundry-code-checks"

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):                                             # noqa: N802
        if self.path.split("?")[0] in ("/health", "/"):
            self._send(200, {"ok": True, "programs": programs()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):                                            # noqa: N802
        if self.path.split("?")[0] != "/run":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("the body must be a JSON object")
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        status, answer = run(body)
        self._send(status, answer)

    def log_message(self, fmt, *args):                            # quieter logs
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    print(f"code-checks on :{PORT} with {programs()}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Foundry media adapter: a local MCP server (stdio) that gives a coding agent
image tools and saves the results INTO YOUR PROJECT, on Windows or Linux.

  Kilo / Cline / Claude Code  --stdio-->  this adapter  --HTTP-->  Foundry /media/*
                                               |
                                               +--> writes files only inside the
                                                    authorised folders you list

Foundry runs the image job (budget, model and duplicate checks happen there);
this adapter submits it, waits a bounded time, downloads the finished image,
verifies its SHA-256, and saves it under an authorised folder with a small
provenance sidecar (<file>.foundry.json, status "unapproved"). The agent gets
back paths and sizes, never base64.

Python 3.8+ standard library only. No pip install needed.

Configuration (environment variables override the JSON file):
  FOUNDRY_URL              http://192.168.0.112:11435
  FOUNDRY_KEY              a Foundry client key with the "media" scope
  FOUNDRY_MEDIA_ROOTS      authorised folders, separated by ';' (Windows) or ':' (Linux)
  FOUNDRY_MEDIA_DEFAULT    where images go when the agent gives no path
  FOUNDRY_MEDIA_WAIT       seconds a tool call waits for the image (default 50)
  --config PATH            JSON file: {"foundry_url", "api_key", "roots": [...],
                           "default_dir", "wait_seconds", "sidecar", "allow_read_outside"}
                           default: foundry-media.json next to this script

  python foundry_media_mcp.py --check     verify the config and Foundry connection
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_INPUT_BYTES = 20 * 1024 * 1024
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_RUNNING_NOTE = ("The image is still being generated. Call image_job_status with this job_id "
                 "(it saves the file when done). Do NOT call generate_image/edit_image again "
                 "for this request: that would create and bill a second image.")

_out_lock = threading.Lock()
_cancelled: set = set()
_threads: list = []


def log(*a):
    print("[foundry-media]", *a, file=sys.stderr, flush=True)


# -- configuration ---------------------------------------------------------------

def load_config(argv) -> dict:
    path = None
    if "--config" in argv:
        i = argv.index("--config")
        path = Path(argv[i + 1]) if i + 1 < len(argv) else None
    if path is None:
        path = Path(__file__).with_name("foundry-media.json")
    cfg = {}
    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    env = os.environ
    cfg["foundry_url"] = (env.get("FOUNDRY_URL") or cfg.get("foundry_url")
                          or "http://127.0.0.1:11435").rstrip("/")
    cfg["api_key"] = env.get("FOUNDRY_KEY") or cfg.get("api_key") or ""
    roots = env.get("FOUNDRY_MEDIA_ROOTS")
    roots = [r for r in roots.split(os.pathsep) if r.strip()] if roots else cfg.get("roots") or []
    cfg["roots"] = [str(Path(os.path.expandvars(os.path.expanduser(r))).resolve()) for r in roots]
    default = env.get("FOUNDRY_MEDIA_DEFAULT") or cfg.get("default_dir")
    if default:
        default = os.path.expandvars(os.path.expanduser(default))
        if not os.path.isabs(default) and cfg["roots"]:
            default = os.path.join(cfg["roots"][0], default)
        cfg["default_dir"] = str(Path(default).resolve())
    elif cfg["roots"]:
        cfg["default_dir"] = os.path.join(cfg["roots"][0], "assets", "generated", "_unapproved")
    else:
        cfg["default_dir"] = ""
    cfg["wait_seconds"] = int(env.get("FOUNDRY_MEDIA_WAIT") or cfg.get("wait_seconds") or 50)
    cfg.setdefault("sidecar", True)
    cfg.setdefault("allow_read_outside", False)
    cfg["_config_file"] = str(path) if path.is_file() else None
    return cfg


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def inside_roots(path: str, roots) -> bool:
    rp = _norm(path)
    for r in roots:
        nr = _norm(r)
        try:
            if os.path.commonpath([rp, nr]) == nr:
                return True
        except ValueError:                 # different drives on Windows
            continue
    return False


# -- Foundry HTTP ------------------------------------------------------------------

class FoundryError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def http(cfg, method, path, body=None, timeout=90, raw=False):
    url = cfg["foundry_url"] + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", f"foundry-media-adapter/{VERSION}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cfg["api_key"]:
        req.add_header("Authorization", "Bearer " + cfg["api_key"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return (payload, dict(r.headers)) if raw else json.loads(payload or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}").get("error") or {}
        except ValueError:
            err = {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise FoundryError(msg or f"HTTP {e.code}", e.code,
                           err.get("code") if isinstance(err, dict) else None)
    except (urllib.error.URLError, OSError) as e:
        raise FoundryError(f"cannot reach Foundry at {cfg['foundry_url']}: "
                           f"{getattr(e, 'reason', e)}")


# -- files -------------------------------------------------------------------------

def sniff(data: bytes):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_input_image(cfg, path: str, base_dir: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(base_dir, path)
    p = os.path.realpath(p)
    if not cfg["allow_read_outside"] and not inside_roots(p, cfg["roots"]):
        raise ValueError(f"{path}: outside the authorised folders")
    if not os.path.isfile(p):
        raise ValueError(f"{path}: file not found")
    if os.path.getsize(p) > MAX_INPUT_BYTES:
        raise ValueError(f"{path}: larger than 20 MB")
    with open(p, "rb") as f:
        data = f.read()
    mime = sniff(data)
    if not mime:
        raise ValueError(f"{path}: not a PNG, JPEG or WebP image")
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:40].rstrip("-") or "image")


def plan_target(cfg, save_as, prompt, job_id, index, count, mime, overwrite) -> str:
    ext = _EXT.get(mime, ".png")
    if save_as:
        sa = os.path.expandvars(os.path.expanduser(save_as))
        base = sa if os.path.isabs(sa) else os.path.join(cfg["roots"][0], sa)
        if save_as.endswith(("/", "\\")) or os.path.isdir(base):
            base = os.path.join(base, f"{_slug(prompt)}-{job_id[-6:]}")
        else:
            base = os.path.splitext(base)[0]      # the real format decides the extension
    else:
        if not cfg["default_dir"]:
            raise ValueError("no save_as given and no default folder configured")
        base = os.path.join(cfg["default_dir"], f"{_slug(prompt)}-{job_id[-6:]}")
    if count > 1:
        base += f"-{index + 1}"
    target = base + ext
    if not inside_roots(os.path.dirname(target) or ".", cfg["roots"]):
        raise ValueError(f"{save_as or target}: outside the authorised folders "
                         f"({'; '.join(cfg['roots'])})")
    if not overwrite:
        n = 1
        while os.path.exists(target):
            target = f"{base}-v{n}{ext}"
            n += 1
    return target


_state_lock = threading.Lock()


def _state_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "state")
    d = Path(base) / "foundry-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / "saved.json"


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    items = sorted(state.items(), key=lambda kv: kv[1].get("t", 0))[-2000:]
    tmp = str(_state_path()) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(items), f)
    os.replace(tmp, _state_path())


def save_artifacts(cfg, job: dict, save_as, overwrite) -> list:
    """Download every artifact of a finished job into the project (once)."""
    saved = []
    arts = job.get("artifacts") or []
    for a in arts:
        with _state_lock:
            prev = _load_state().get(a["id"])
        want = plan_target(cfg, save_as, job.get("prompt") or "image", job["job_id"],
                           a["index"], len(arts), a.get("mime"), True) if save_as else None
        if prev and os.path.isfile(prev["path"]) and (not save_as or _same_target(prev["path"], want)):
            with open(prev["path"], "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == a["sha256"]:
                    saved.append({**_describe(a, prev["path"]), "already_saved": True})
                    continue
        data, _h = http(cfg, "GET", f"/media/artifacts/{job['job_id']}/{a['index']}",
                        timeout=120, raw=True)
        if hashlib.sha256(data).hexdigest() != a["sha256"]:
            raise FoundryError(f"artifact {a['id']}: checksum mismatch, not saved")
        target = plan_target(cfg, save_as, job.get("prompt") or "image", job["job_id"],
                             a["index"], len(arts), a.get("mime"), overwrite)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
        if cfg["sidecar"]:
            meta = {"status": "unapproved", "job_id": job["job_id"], "artifact": a["id"],
                    "model": job.get("model"), "op": job.get("op"), "prompt": job.get("prompt"),
                    "revised_prompt": a.get("revised_prompt"), "settings": job.get("settings"),
                    "width": a.get("width"), "height": a.get("height"), "sha256": a["sha256"],
                    "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "foundry": cfg["foundry_url"]}
            with open(target + ".foundry.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        with _state_lock:
            st = _load_state()
            st[a["id"]] = {"path": target, "t": time.time()}
            _save_state(st)
        saved.append(_describe(a, target))
    return saved


def _same_target(saved: str, want: str) -> bool:
    """saved is want itself or the -vN copy made because want already existed."""
    base, ext = os.path.splitext(want)
    return _norm(saved) == _norm(want) or bool(
        re.fullmatch(re.escape(_norm(base)) + r"-v\d+" + re.escape(os.path.normcase(ext)), _norm(saved)))


def _describe(a, path):
    return {"path": path, "width": a.get("width"), "height": a.get("height"),
            "bytes": a.get("bytes"), "mime": a.get("mime"), "sha256": a.get("sha256")}


def preview_blocks(saved: list, max_bytes=1_500_000) -> list:
    out = []
    for s in saved:
        try:
            if os.path.getsize(s["path"]) > max_bytes:
                continue
            with open(s["path"], "rb") as f:
                out.append({"type": "image", "mimeType": s["mime"] or "image/png",
                            "data": base64.b64encode(f.read()).decode()})
        except OSError:
            pass
    return out


# -- tools ----------------------------------------------------------------------

COMMON = {
    "save_as": {"type": "string", "description": "file or folder for the result. Relative paths "
                "are relative to the project root; must be inside the authorised folders. "
                "Default: the configured staging folder (unapproved assets)."},
    "model": {"type": "string", "description": "'final' (default, precise assets), 'draft' (fast "
              "concepts), or an allowed image model id"},
    "size": {"type": "string", "description": "WIDTHxHEIGHT, e.g. 1024x1024, 1536x1024, 1024x1536, or auto"},
    "quality": {"type": "string", "description": "low | medium | high | auto"},
    "background": {"type": "string", "description": "transparent | opaque | auto (transparent needs png/webp)"},
    "output_format": {"type": "string", "description": "png (default) | webp | jpeg"},
    "n": {"type": "integer", "minimum": 1, "maximum": 4, "description": "number of images"},
    "idempotency_key": {"type": "string", "description": "optional stable id for this request "
                        "(e.g. sprint-step-asset); the same key always returns the same job"},
    "allow_duplicate": {"type": "boolean", "description": "true = generate a new variation even "
                        "though the identical request ran recently"},
    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 600,
                     "description": "how long to wait for the image in this call"},
    "preview": {"type": "boolean", "description": "also return the saved image so a vision "
                "model can look at it (default false)"},
    "overwrite": {"type": "boolean", "description": "replace an existing file at save_as "
                  "(default false: a -v1, -v2 suffix is added)"},
}

TOOLS = [
    {"name": "generate_image",
     "description": "Generate an image through Foundry and save it into the project. Returns "
                    "the saved path(s). If the result isn't ready within wait_seconds, returns a "
                    "job_id: then call image_job_status, never generate_image again.",
     "inputSchema": {"type": "object", "required": ["prompt"],
                     "properties": {"prompt": {"type": "string"}, **COMMON}}},
    {"name": "edit_image",
     "description": "Edit or combine existing project images (reference images, optional mask "
                    "where transparent pixels mark the area to change) and save the result.",
     "inputSchema": {"type": "object", "required": ["prompt", "images"],
                     "properties": {"prompt": {"type": "string"},
                                    "images": {"type": "array", "items": {"type": "string"},
                                               "description": "paths of input images (project-relative or absolute)"},
                                    "mask": {"type": "string", "description": "path of a PNG mask"},
                                    "input_fidelity": {"type": "string", "description": "high = keep faces/details of the inputs"},
                                    **COMMON}}},
    {"name": "image_job_status",
     "description": "Check an image job; when it has finished, save the result into the project "
                    "and return the path. Never submits a new generation.",
     "inputSchema": {"type": "object", "required": ["job_id"],
                     "properties": {"job_id": {"type": "string"},
                                    "wait_seconds": COMMON["wait_seconds"],
                                    "save_as": COMMON["save_as"], "preview": COMMON["preview"],
                                    "overwrite": COMMON["overwrite"]}},
     "annotations": {"readOnlyHint": False, "idempotentHint": True}},
    {"name": "list_image_jobs",
     "description": "Recent image jobs from this client (status, model, prompt).",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1,
                                                                "maximum": 50}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "image_settings",
     "description": "Where images may be saved, the default folder, and the image models and "
                    "budget Foundry allows.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
]


def _wait_job(cfg, job: dict, wait: float, req_id, progress) -> dict:
    deadline = time.monotonic() + max(0.0, wait)
    while job.get("status") in ("queued", "running"):
        left = deadline - time.monotonic()
        if left <= 0 or req_id in _cancelled:
            break
        step = min(25.0, left)
        job = http(cfg, "GET", f"/media/jobs/{job['job_id']}?wait={int(max(1, step))}",
                   timeout=step + 30)
        if progress:
            progress(f"{job.get('status')} · {job.get('elapsed_s', 0)} s")
    return job


def _result(cfg, job, args, req_id=None) -> dict:
    status = job.get("status")
    summary = {"job_id": job.get("job_id"), "status": status, "model": job.get("model")}
    if job.get("deduplicated"):
        summary["note"] = job.get("note")
    if status == "succeeded":
        try:
            saved = save_artifacts(cfg, job, args.get("save_as"), bool(args.get("overwrite")))
        except (FoundryError, ValueError, OSError) as e:
            return _error(f"the image is ready (job {job.get('job_id')}) but saving failed: {e}. "
                          f"Fix save_as and call image_job_status again.")
        summary["saved"] = saved
        summary["next"] = ("Saved as an UNAPPROVED asset. Review it (and follow the project's "
                           "asset approval steps) before using it in the game.")
        content = [{"type": "text", "text": json.dumps(summary, indent=1)}]
        if args.get("preview"):
            content += preview_blocks(saved)
        return {"content": content, "isError": False}
    if status in ("queued", "running"):
        summary["elapsed_s"] = job.get("elapsed_s")
        summary["next"] = _RUNNING_NOTE
        return {"content": [{"type": "text", "text": json.dumps(summary, indent=1)}],
                "isError": False}
    summary["error"] = job.get("error")
    return {"content": [{"type": "text", "text": json.dumps(summary, indent=1)}], "isError": True}


def _error(msg) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def call_tool(cfg, name, args, req_id=None, progress=None) -> dict:
    wait = args.get("wait_seconds")
    wait = cfg["wait_seconds"] if wait is None else max(0, min(600, int(wait)))
    try:
        if name in ("generate_image", "edit_image"):
            if not cfg["roots"]:
                return _error("no authorised folders configured (FOUNDRY_MEDIA_ROOTS / roots)")
            try:                       # refuse a bad destination BEFORE paying for an image
                plan_target(cfg, args.get("save_as"), args.get("prompt") or "image",
                            "img_0000000000000000", 0, int(args.get("n") or 1), "image/png", True)
            except ValueError as e:
                return _error(str(e))
            body = {k: args[k] for k in ("prompt", "model", "size", "quality", "background",
                                         "output_format", "n", "idempotency_key",
                                         "allow_duplicate", "input_fidelity") if args.get(k)}
            body["op"] = "edit" if name == "edit_image" else "generate"
            if name == "edit_image":
                try:
                    body["images"] = [read_input_image(cfg, p, cfg["roots"][0])
                                      for p in args.get("images") or []]
                    if args.get("mask"):
                        body["mask"] = read_input_image(cfg, args["mask"], cfg["roots"][0])
                except ValueError as e:
                    return _error(str(e))
                if not body["images"]:
                    return _error("edit_image needs at least one input image path")
            body["wait_seconds"] = min(25, wait)
            job = http(cfg, "POST", "/media/jobs", body, timeout=120)
            job = _wait_job(cfg, job, wait - min(25, wait), req_id, progress)
            return _result(cfg, job, args, req_id)
        if name == "image_job_status":
            jid = str(args.get("job_id") or "")
            if not re.match(r"^img_[0-9a-f]{16}$", jid):
                return _error(f"not an image job id: {jid!r}")
            job = http(cfg, "GET", f"/media/jobs/{jid}", timeout=30)
            job = _wait_job(cfg, job, wait, req_id, progress)
            return _result(cfg, job, args, req_id)
        if name == "list_image_jobs":
            n = max(1, min(50, int(args.get("limit") or 10)))
            jobs = http(cfg, "GET", f"/media/jobs?limit={n}", timeout=30).get("jobs") or []
            rows = [{k: j.get(k) for k in ("job_id", "status", "model", "created_at", "prompt")}
                    for j in jobs]
            return {"content": [{"type": "text", "text": json.dumps(rows, indent=1)}],
                    "isError": False}
        if name == "image_settings":
            try:
                models = http(cfg, "GET", "/media/models", timeout=15)
            except FoundryError as e:
                models = {"error": str(e)}
            info = {"authorised_folders": cfg["roots"], "default_folder": cfg["default_dir"],
                    "wait_seconds": cfg["wait_seconds"], "foundry": cfg["foundry_url"],
                    "foundry_media": models}
            return {"content": [{"type": "text", "text": json.dumps(info, indent=1)}],
                    "isError": False}
        return _error(f"unknown tool {name}")
    except FoundryError as e:
        tail = ""
        if e.code in ("budget_denied", "quota_exceeded", "media_queue_full"):
            tail = " Do not retry with another model; tell the user."
        return _error(f"Foundry refused ({e.code or e.status or 'error'}): {e}{tail}")


# -- MCP stdio transport -----------------------------------------------------------

def send(msg: dict) -> None:
    data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def handle(cfg, msg: dict) -> None:
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if mid is None:                                  # notification
        if method == "notifications/cancelled":
            _cancelled.add(params.get("requestId"))
        return
    if method == "initialize":
        want = params.get("protocolVersion")
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "foundry-media", "version": VERSION},
            "instructions": ("Image generation through Foundry. Results are saved into the "
                             "project as unapproved assets; you get paths, not image data. "
                             "If a call returns status running, use image_job_status — never "
                             "resubmit.")}})
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        token = (params.get("_meta") or {}).get("progressToken")
        count = [0]

        def progress(message):
            if token is None:
                return
            count[0] += 1
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": token, "progress": count[0], "message": message}})

        def run():
            try:
                res = call_tool(cfg, params.get("name"), params.get("arguments") or {}, mid, progress)
            except Exception as e:                   # noqa: BLE001
                log("tool crashed:", repr(e))
                res = _error(f"adapter error: {e}")
            _cancelled.discard(mid)
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        t = threading.Thread(target=run, daemon=True)
        _threads.append(t)
        t.start()
        return
    send({"jsonrpc": "2.0", "id": mid,
          "error": {"code": -32601, "message": f"method not found: {method}"}})


def check(cfg) -> int:
    print(f"foundry-media adapter {VERSION}")
    print(f"  config file : {cfg['_config_file'] or '(none; environment only)'}")
    print(f"  Foundry     : {cfg['foundry_url']}  key: {'set' if cfg['api_key'] else 'NOT set'}")
    ok = True
    for r in cfg["roots"] or ["(none configured)"]:
        exists = os.path.isdir(r)
        ok &= exists
        print(f"  root        : {r}  {'ok' if exists else 'MISSING'}")
    print(f"  default dir : {cfg['default_dir'] or '(none)'}")
    try:
        m = http(cfg, "GET", "/media/models", timeout=15)
        print(f"  Foundry media: backend={m.get('backend')} enabled={m.get('enabled')} "
              f"used {m.get('used_5h')}/{m.get('jobs_per_5h') or '∞'} (5 h)")
        if m.get("backend_error"):
            print(f"  ! {m['backend_error']}")
            ok = False
        for mm in m.get("models") or []:
            print(f"    {mm['id']:<28} {mm.get('role') or '':<6} last success: "
                  f"{mm.get('verified_at') or 'never'}")
    except FoundryError as e:
        print(f"  ! Foundry: {e}")
        ok = False
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def main(argv) -> int:
    cfg = load_config(argv)
    if "--check" in argv:
        return check(cfg)
    if "--version" in argv:
        print(VERSION)
        return 0
    log(f"v{VERSION} Foundry={cfg['foundry_url']} roots={cfg['roots']}")
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "parse error"}})
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            if isinstance(m, dict):
                handle(cfg, m)
    # stdin closed: let quick in-flight calls answer, then exit (a client that
    # went away mid-wait doesn't need its reply; the Foundry job keeps running).
    deadline = time.monotonic() + 10
    for t in _threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

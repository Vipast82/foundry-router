"""Local API + settings page on 127.0.0.1 only. Every /api call needs the per-install
token (header X-Client-Token); the tray opens the page with the token in the URL
fragment, which never reaches the server or a log. Host-header check against DNS
rebinding. Secrets are write-only."""
from __future__ import annotations

import json
import os
import secrets as _rnd
import threading
from importlib import resources
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, autostart, config, doctor, secrets
from .config import COMPONENTS
from .manager import Manager

HOSTS = ("127.0.0.1", "localhost")


def token_path() -> Path:
    return config.config_dir() / "api.token"


def load_token() -> str:
    p = token_path()
    if p.is_file():
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    p.parent.mkdir(parents=True, exist_ok=True)
    t = _rnd.token_urlsafe(32)
    p.write_text(t, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return t


def create_app(mgr: Manager, token: str, cfg_path: Optional[Path] = None,
               lane=None, lane_token: str = "") -> FastAPI:
    """`lane` (a lane.LaneHost) + `lane_token`: this machine holds lanes for a coordinator
    elsewhere (Distributed Foundry phase b). The /api/lane routes then answer, to the LANE
    token only; the client token never opens them and the lane token opens nothing else."""
    app = FastAPI(title="Foundry Client", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in HOSTS:
            return JSONResponse({"error": "local access only"}, status_code=403)
        if request.url.path.startswith("/api/lane/"):
            if lane is None or not lane_token:
                return JSONResponse({"error": "this machine holds no lanes ([lane] is off)"},
                                    status_code=404)
            if not _rnd.compare_digest(request.headers.get("x-lane-token", ""), lane_token):
                return JSONResponse({"error": "missing or wrong lane token"}, status_code=401)
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            if not _rnd.compare_digest(request.headers.get("x-client-token", ""), token):
                return JSONResponse({"error": "missing or wrong client token"}, status_code=401)
        return await call_next(request)

    if lane is not None:
        _lane_routes(app, mgr, lane)

    @app.get("/", response_class=HTMLResponse)
    def page():
        return resources.files("foundry_client").joinpath("static/client.html").read_text("utf-8")

    @app.get("/api/status")
    def status():
        return {**mgr.status(), "version": __version__,
                "backend_url": mgr.cfg["client"]["backend_url"],
                "resources": getattr(getattr(mgr, "resources", None), "last", {}) or {}}

    @app.get("/api/mcp")
    def mcp(fresh: bool = False):
        """MCP servers Kilo can see here and the service registry (what the heartbeat reports)."""
        return mgr.mcp_status(max_age=0 if fresh else 30.0)

    @app.post("/api/components/{name}/{verb}")
    def control(name: str, verb: str, force: bool = False):
        if name not in COMPONENTS or name not in mgr.procs:
            raise HTTPException(404, f"no component {name!r}")
        if verb == "start":
            return mgr.start(name)
        if verb == "stop":
            return mgr.stop(name)
        if verb == "restart":
            return mgr.restart(name, force=force)
        if verb == "cancel-restart":
            return mgr.cancel_restart(name)
        raise HTTPException(404, f"unknown action {verb!r}")

    @app.post("/api/alerts/{key}/dismiss")
    def dismiss(key: str):
        return {"dismissed": mgr.dismiss_alert(key)}

    @app.post("/api/studio/reopen")
    async def studio_reopen(request: Request):
        """Start a Studio auto-reopen job (studio.py): {task?, dry_run?}. The job runs in
        the background; GET /api/studio/jobs/<id> reports it."""
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        return mgr.studio.start(task=str(body.get("task") or ""),
                                dry_run=bool(body.get("dry_run")))

    @app.get("/api/studio/jobs/{jid}")
    def studio_job(jid: str):
        j = mgr.studio.get(jid)
        return j if j else JSONResponse({"error": f"no job {jid}"}, status_code=404)

    @app.get("/api/studio/settings")
    def studio_settings():
        return mgr.studio.settings()

    @app.get("/api/observation")
    def observation():
        return mgr.observation()

    @app.get("/api/doctor")
    def run_doctor():
        return doctor.run(mgr)

    @app.post("/api/router-link/check")
    def check_link():
        return mgr.link.poll()

    @app.post("/api/update/check")
    def check_update():
        return mgr.update.poll()

    # v0.181.257 (Victor: "build this update feature in to the client, so it can update on
    # demand / autoupdate"): selfupdate.Updater moves the deploy checkout to a release at a
    # gap it makes, restarts the coordinator and then this client
    @app.get("/api/update")
    def update_status():
        return mgr.updater.status()

    @app.post("/api/update/apply")
    async def update_apply(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        out = mgr.updater.start(str((body or {}).get("tag") or ""))
        return JSONResponse(out, status_code=409) if out.get("error") else out

    @app.post("/api/update/cancel")
    def update_cancel():
        out = mgr.updater.cancel()
        return JSONResponse(out, status_code=409) if out.get("error") else out

    @app.post("/api/client/restart")
    def restart_client():
        """2026-10-28 (Victor: "why can't we have a restart option from the client"): start a
        fresh client that waits for this one's port to free, then exit WITHOUT stopping the
        components; the new client adopts the running Kilo, coordinator, Rojo and Blender,
        so no session is interrupted. Picks up new client code and settings."""
        from . import selfrestart
        try:
            pid = selfrestart.spawn_successor(mgr.cfg["client"]["api_port"])
        except Exception as e:                                    # noqa: BLE001
            return JSONResponse({"error": f"could not start a new client: {e}"}, status_code=500)
        selfrestart.exit_soon(mgr)
        return {"ok": True, "successor_pid": pid,
                "note": "the client restarts in a few seconds; components keep running"}

    @app.post("/api/kilo-config/apply")
    async def kilo_config_apply(request: Request):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        res = await run_in_threadpool(
            mgr.apply_kilo_config, str((body or {}).get("project") or ""),
            bool((body or {}).get("dry_run")), bool((body or {}).get("reload", True)))
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    def _rev() -> str:
        import hashlib
        return hashlib.sha256(json.dumps(mgr.cfg, sort_keys=True, default=str)
                              .encode()).hexdigest()[:16]

    @app.get("/api/settings")
    def get_settings():
        return {"settings": mgr.cfg, "path": str(cfg_path or config.config_path()),
                "problems": config.validate(mgr.cfg), "rev": _rev()}

    @app.put("/api/settings")
    async def put_settings(request: Request):
        """The body is merged onto the CURRENT settings, so a partial document changes only
        what it names. (A full document, as the settings page sends, replaces every value in it.)"""
        body = await request.json()
        incoming = body.get("settings") or {}
        # 2026-10-28: a Settings page left open saved its stale copy over newer settings
        # (Blender switched back off). A page that names the revision it loaded must match.
        if body.get("rev") and body["rev"] != _rev():
            return JSONResponse({"error": "the settings changed since this page was opened; "
                                          "reload the page and save again",
                                 "stale": True}, status_code=409)
        config.migrate(incoming)                   # an old page may still send a retired table
        return await run_in_threadpool(_save_settings, incoming)

    def _save_settings(incoming: dict):
        new = config._merge(mgr.cfg, incoming)
        problems = config.validate(new)
        if problems:
            return JSONResponse({"error": "; ".join(problems), "problems": problems},
                                status_code=400)
        with lock:
            config.save(new, cfg_path)
            mgr.reload(new)
            for n in COMPONENTS:                      # newly enabled components start
                if new["components"][n]["enabled"] and not mgr.procs[n].want:
                    mgr.procs[n].want = True
                if not new["components"][n]["enabled"] and mgr.procs[n].want:
                    mgr.stop(n)
        return {"ok": True, "note": "saved; running components use the new settings after "
                                    "their next restart"}

    @app.get("/api/secrets")
    def get_secrets():
        names = {s for c in mgr.cfg["components"].values() for s in c.get("secrets") or []}
        return {"secrets": secrets.status(names), "help": secrets.KNOWN,
                "store": secrets.store_in_use(),
                "file": secrets.secrets_file() if secrets.store_in_use() == "file" else ""}

    @app.put("/api/secrets/{name}")
    async def put_secret(name: str, request: Request):
        body = await request.json()
        try:
            secrets.set_secret(name, str(body.get("value") or ""))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "set": bool(body.get("value"))}

    @app.post("/api/secrets/import-legacy")
    def import_legacy():
        done = secrets.migrate_legacy(config.expand(mgr.cfg["client"]["state_dir"]))
        return {"imported": done}

    @app.get("/api/logs/{name}")
    def logs(name: str, lines: int = 200):
        if name not in COMPONENTS:
            raise HTTPException(404, f"no component {name!r}")
        return {"name": name, "text": mgr.tail(name, max(10, min(2000, lines)))}

    @app.get("/api/autostart")
    def get_autostart():
        # `enabled` is the sign-in entry this page toggles; `mode` also covers the
        # boot task (install-autostart --at-boot, CLI only: it needs a password prompt)
        m = autostart.mode()
        return {"enabled": autostart.is_enabled(), "mode": m,
                "mode_text": autostart.MODE_TEXT.get(m, "")}

    @app.post("/api/autostart")
    async def set_autostart(request: Request):
        body = await request.json()
        (autostart.enable if body.get("enabled") else autostart.disable)()
        return {"enabled": autostart.is_enabled()}

    return app


def _lane_routes(app: FastAPI, mgr, lane) -> None:
    """The lane agent's routes (foundry_client/lane.py; the guard checked the lane token)."""
    from .lane import LaneError

    async def body_of(request: Request) -> dict:
        try:
            b = await request.json()
        except ValueError:
            b = {}
        return b if isinstance(b, dict) else {}

    def answer(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except LaneError as e:
            return JSONResponse({"error": str(e)}, status_code=e.status)

    def comps() -> dict:
        try:
            return {c["name"]: {"state": c.get("state"), "healthy": c.get("healthy")}
                    for c in mgr.status()["components"] if c["name"] in ("kilo", "opencode")}
        except Exception:                                         # noqa: BLE001
            return {}

    @app.get("/api/lane/health")
    def lane_health():
        return answer(lane.health, comps())

    @app.post("/api/lane/git")
    async def lane_git(request: Request):
        b = await body_of(request)
        return await run_in_threadpool(answer, lane.git, b.get("project"), b.get("lane"),
                                       b.get("argv"), b.get("timeout") or 300, b.get("env"),
                                       b.get("input"))

    @app.post("/api/lane/read")
    async def lane_read(request: Request):
        b = await body_of(request)
        return await run_in_threadpool(answer, lane.read, b.get("project"), b.get("lane"),
                                       b.get("path"), b.get("max_bytes"))

    @app.post("/api/lane/stat")
    async def lane_stat(request: Request):
        b = await body_of(request)
        return await run_in_threadpool(answer, lane.stat, b.get("project"), b.get("lane"),
                                       b.get("paths"))

    @app.post("/api/lane/write")
    async def lane_write(request: Request):
        b = await body_of(request)
        return await run_in_threadpool(answer, lane.write, b.get("project"), b.get("lane"),
                                       b.get("path"), b.get("data"))

    @app.post("/api/lane/run")
    async def lane_run(request: Request):
        b = await body_of(request)
        return answer(lane.start_job, b.get("project"), b.get("lane"), str(b.get("job") or ""),
                      b.get("args") if isinstance(b.get("args"), dict) else {})

    @app.get("/api/lane/run/{rid}")
    def lane_run_status(rid: str, offset: int = 0):
        return answer(lane.run_status, rid, offset)

    @app.post("/api/lane/run_status")
    async def lane_run_status_post(request: Request):
        """The same as GET /api/lane/run/<id>, for callers that only POST (RemoteWorkspace's
        LaneAgent): {id, offset}."""
        b = await body_of(request)
        return answer(lane.run_status, str(b.get("id") or ""), int(b.get("offset") or 0))

    @app.post("/api/lane/run/{rid}/cancel")
    def lane_run_cancel(rid: str):
        return answer(lane.cancel, rid)

"""The first-run setup wizard (/ui/setup, setup.html).

The page walks through a new router in order: restore a backup or start fresh, protect
the web UI, the secret store passphrase, provider keys and backends, default personas,
budgets, the game, machines. Each step uses the same admin API as the matching tab, so
the wizard adds no second way to change anything. This module only reports where each
step stands and records when the wizard is finished.

The web UI opens the wizard by itself while `setup.completed` is false, which is only
the case for a config.yaml made from the example on a fresh install (config.py).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from .. import __version__

router = APIRouter()
PAGE = Path(__file__).resolve().parent / "static" / "setup.html"
EXAMPLE_HOSTS = ("192.168.1.50", "192.168.1.60", "192.168.1.40", "192.168.1.55",
                 "192.168.1.56", "192.168.1.57")


@router.get("/ui/setup")
async def setup_page():
    return FileResponse(PAGE, media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "Content-Security-Policy":
                                     "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                     "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                     "connect-src 'self'; frame-ancestors 'none'; "
                                     "base-uri 'none'; form-action 'none'",
                                 "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "same-origin"})


def _svc(request: Request):
    return request.app.state.services


def state(svc) -> dict:
    from .. import backup, profiles, provider_keys
    cfg = svc.config_store.config
    data = Path(svc.db.path).parent
    admins = [c.name for c in cfg.clients if "admin" in (c.scopes or []) and c.enabled]
    backends = svc.pool.backend_status()
    for b in backends:
        b["example"] = any(h in (b.get("url") or "") for h in EXAMPLE_HOSTS)
    try:
        profs = [{k: p[k] for k in ("id", "title", "description", "installed", "version")}
                 | {"personas": [x["persona"] for x in p.get("personas") or []]}
                 for p in profiles.list_profiles(svc)]
    except Exception:                                            # noqa: BLE001
        profs = []
    projects = [p["project"] for p in svc.automation.projects()
                if p.get("deployed_rev") and not str(p["project"]).startswith("_")]
    machines = svc.machines.list()
    store = svc.secret_store.state()
    keys = provider_keys.status(svc.config_store.raw, Path(svc.config_store.path).parent)
    steps = {
        "admin": {"done": bool(cfg.client_auth.protect_admin), "admins": admins,
                  "protect_admin": bool(cfg.client_auth.protect_admin)},
        "secrets": {"done": store != "uninitialized", "state": store},
        "backends": {"done": any(b.get("healthy") for b in backends), "backends": backends,
                     "provider_keys": keys},
        "personas": {"done": any(p["installed"] for p in profs), "profiles": profs,
                     "applied_at_start": list(cfg.persona_profiles)},
        "budgets": {"done": bool(cfg.budgets.enabled and cfg.budgets.rules),
                    "enabled": bool(cfg.budgets.enabled),
                    "rules": [{"name": r.name, "enabled": r.enabled, "mode": r.mode}
                              for r in cfg.budgets.rules]},
        "game": {"done": bool(projects), "projects": projects},
        "machines": {"done": bool(machines), "count": len(machines),
                     "ssh_keys": len(cfg.machines.slot_ssh_keys)
                     + len(svc.machines.workstations())},
    }
    return {"version": __version__, "completed": bool(cfg.setup.completed),
            "fresh": bool(getattr(svc.config_store, "fresh", False)),
            "restored": getattr(svc, "restored_from", None),
            "staged_restore": backup.staged(data),
            "https": {"enabled": bool(cfg.secrets.https_enabled), "port": cfg.secrets.https_port,
                      "allow_http": bool(cfg.secrets.allow_http)},
            "steps": steps}


@router.get("/admin/api/setup/wizard")
async def get_wizard(request: Request):
    return state(_svc(request))


@router.post("/admin/api/setup/wizard")
async def set_wizard(request: Request):
    """{completed: bool}: finish (or reopen) the wizard."""
    svc = _svc(request)
    try:
        body = await request.json()
    except Exception:                                            # noqa: BLE001
        body = {}
    done = bool(body.get("completed", True))

    def mutate(raw):
        raw["setup"] = {**(raw.get("setup") or {}), "completed": done}
    svc.config_store.save(mutate)
    svc.db.log_event("info", "setup", "setup wizard " + ("finished" if done else "reopened"))
    return state(svc)

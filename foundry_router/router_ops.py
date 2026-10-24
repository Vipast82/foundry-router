"""Router writes shared by the admin API and the operator chat (v0.140.0), so
the GUI button and the chat tool do exactly the same thing.

set_backend_enabled   park / unpark one backend (config.yaml + pool rebuild):
                      what the Pool tab and the Automation Routing page's
                      backend switch write
grant_persona         add a persona to one client key's persona allow-list
                      (what PATCH /admin/api/setup/clients/<name> writes)
clients_missing       client keys whose allow-list would refuse a persona
"""

from __future__ import annotations

from typing import Optional


class OpError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def set_backend_enabled(svc, name: str, enabled: bool, actor: str = "operator") -> list:
    raw_backends = svc.config_store.config.backend_pool.internal.backends or []
    if not any(b.name == name for b in raw_backends):
        raise OpError(f"no backend named {name!r} (have: "
                      f"{', '.join(b.name for b in raw_backends) or 'none'})", 404)

    def mutate(raw):
        for b in raw.setdefault("backend_pool", {}).setdefault("internal", {}).get("backends", []):
            if b.get("name") == name:
                b["enabled"] = bool(enabled)
    svc.config_store.save(mutate)
    await svc.rebuild_pool()
    svc.db.log_event("info", "backend_pool",
                     f"backend {name} {'enabled' if enabled else 'disabled'} by {actor}")
    return svc.pool.backend_status()


def clients_missing(svc, persona: str) -> list[dict]:
    """Enabled client keys with a persona allow-list that doesn't include
    `persona` (an empty allow-list allows every persona)."""
    out = []
    for c in svc.config_store.config.clients or []:
        allowed = list(c.personas or [])
        if c.enabled and allowed and persona not in allowed \
                and ("inference" in (c.scopes or []) or "media" in (c.scopes or [])):
            out.append({"client": c.name, "personas": len(allowed)})
    return out


def grant_persona(svc, client: str, persona: str, actor: str = "operator") -> Optional[list]:
    """Add `persona` to `client`'s allow-list. Returns the new list, or None
    when the client already allows it (or allows every persona)."""
    cur = next((c for c in svc.config_store.config.clients or [] if c.name == client), None)
    if cur is None:
        raise OpError(f"no client named {client!r}", 404)
    if svc.personas.get(persona) is None:
        raise OpError(f"no persona {persona!r}", 404)
    allowed = list(cur.personas or [])
    if not allowed or persona in allowed:
        return None
    new = allowed + [persona]

    def mutate(raw):
        for c in raw.get("clients") or []:
            if c.get("name") == client:
                c["personas"] = new
    svc.config_store.save(mutate)
    svc.db.log_event("info", "client_auth", f"client {client}: persona {persona} allowed by {actor}")
    return new

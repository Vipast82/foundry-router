"""Building a project's adapter and clients from its profile."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Optional

from .foundry import FoundryClient
from .git import Workspace
from .kilo import KiloClient
from .project import GenericAdapter
from .scripted import ScriptedClient


@dataclass
class Project:
    profile: object
    adapter: GenericAdapter
    client: object
    foundry: Optional[FoundryClient]
    ws: Workspace

    @property
    def id(self) -> str:
        return self.profile.id


def load_adapter(profile, state_dir: str) -> GenericAdapter:
    if not profile.adapter:
        return GenericAdapter(profile, state_dir)
    mod, _, cls = profile.adapter.partition(":")
    klass = getattr(importlib.import_module(mod), cls)
    if not issubclass(klass, GenericAdapter):
        raise TypeError(f"{profile.adapter} must subclass GenericAdapter")
    return klass(profile, state_dir)


def build_one(profile, c: dict):
    """One client from a [client] or [clients.<name>] table."""
    kind = c.get("kind", "kilo")
    if kind == "kilo":
        return KiloClient(c.get("url") or "http://127.0.0.1:4096",
                          password=profile.secret(c, "password"),
                          provider=c.get("provider") or "foundry")
    if kind == "opencode":
        from .opencode import OpenCodeClient
        return OpenCodeClient(c.get("url") or "http://127.0.0.1:4097",
                              password=profile.secret(c, "password"),
                              provider=c.get("provider") or "foundry",
                              api=c.get("api") or "auto")
    if kind == "scripted":
        return ScriptedClient(c.get("script"), project_id=profile.id)
    raise ValueError(f"client kind {kind!r} needs a client object")


def build_client(profile, client=None):
    """The project's coding client. Only [client]: that one client, exactly as before
    v0.181.247. With [clients.<name>] tables: a ClientSet that runs each role on the server
    [client_roles] names (adapters/clientset.py)."""
    if client is not None:
        return client
    base = build_one(profile, profile.client)
    raw = getattr(profile, "raw", None) or {}
    extra = raw.get("clients") or {}
    extra = extra if isinstance(extra, dict) else {}
    lanes = lane_members(profile)
    if not extra and not lanes:
        return base
    from .clientset import ClientSet
    members = {k: (build_one(profile, t), t) for k, t in extra.items() if isinstance(t, dict)}
    members.update(lanes)
    return ClientSet(base, members, roles=raw.get("client_roles") or {},
                     default_table=profile.client)


def lane_members(profile) -> dict:
    """Distributed Foundry phase b: {"<slot>/kilo" | "<slot>/opencode": (client, table)} for
    every slot with `[slots.<id>] lanes = true`; {} otherwise (today's behaviour). Also
    starts that slot's lane tunnel (lane_tunnel.ensure)."""
    try:
        from .. import lane_tunnel
        slots = lane_tunnel.ensure(profile)
    except Exception:                                             # noqa: BLE001
        return {}
    if not slots:
        return {}
    from .laneclient import TunnelledClient, lane_root
    from ..lane_fetch import MIRROR_DIR
    raw = getattr(profile, "raw", None) or {}
    main_ws = os.path.realpath(str(getattr(profile, "workspace", "") or "."))
    oc_table = next((t for t in (raw.get("clients") or {}).values()
                     if isinstance(t, dict) and t.get("kind") == "opencode"), {})
    out = {}
    for sid, s in slots.items():
        for kind in ("kilo", "opencode"):
            if (profile.client or {}).get("kind", "kilo") == kind:
                src = profile.client
            else:
                src = oc_table if kind == "opencode" else {}
            # worker_client: the router key the slot's server calls with is the slot's own
            # lane key (foundry_router/lane_keys.py), not this machine's kilo / opencode key;
            # the router trusts a session's purpose only from that client (v0.181.266)
            # phase c: a remote lane's folder on this machine is its read mirror
            # (<workspace>/.lanes/<slot>/<lane>); the slot's is <lanes_root>/<project>/<lane>
            local = os.path.join(main_ws, MIRROR_DIR, sid).replace("\\", "/")
            remote = f"{lane_root(s)}/{profile.id}"
            table = {**{k: v for k, v in (src or {}).items()
                        if k in ("agent", "agents", "provider")},
                     "kind": kind, "lane_host": sid, "lane_root": local,
                     "slot_root": remote, "worker_client": f"{sid}-{kind}"}
            if kind == "opencode" and not table.get("agent"):
                table["agent"] = "build"          # OpenCode's own default agent (agent_backends)
            client = TunnelledClient(sid, kind, lane_tunnel.MANAGER.endpoint,
                                     provider=str(table.get("provider") or "foundry"),
                                     dir_map=(local, remote))
            out[f"{sid}/{kind}"] = (client, table)
    return out


def build_project(profile, state_dir: str, client=None,
                  foundry: Optional[FoundryClient] = None) -> Project:
    f = foundry
    if f is None and profile.foundry.get("url"):
        f = FoundryClient(profile.foundry["url"], key=profile.secret(profile.foundry, "key"))
    return Project(profile=profile, adapter=load_adapter(profile, state_dir),
                   client=build_client(profile, client), foundry=f,
                   ws=Workspace(profile.workspace))


__all__ = ["Project", "build_project", "load_adapter", "build_client", "build_one", "os"]

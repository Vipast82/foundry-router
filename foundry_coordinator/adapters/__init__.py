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
    if not isinstance(extra, dict) or not extra:
        return base
    from .clientset import ClientSet
    members = {k: (build_one(profile, t), t) for k, t in extra.items() if isinstance(t, dict)}
    return ClientSet(base, members, roles=raw.get("client_roles") or {},
                     default_table=profile.client)


def build_project(profile, state_dir: str, client=None,
                  foundry: Optional[FoundryClient] = None) -> Project:
    f = foundry
    if f is None and profile.foundry.get("url"):
        f = FoundryClient(profile.foundry["url"], key=profile.secret(profile.foundry, "key"))
    return Project(profile=profile, adapter=load_adapter(profile, state_dir),
                   client=build_client(profile, client), foundry=f,
                   ws=Workspace(profile.workspace))


__all__ = ["Project", "build_project", "load_adapter", "build_client", "build_one", "os"]

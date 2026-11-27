"""A Studio slot's model keys for its own Kilo / OpenCode (Distributed Foundry phase b, B5).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. A slot that holds lanes runs its own `kilo serve`
and `opencode serve`, whose model calls come to this router. They need client keys of their
own: one per slot and kind, so each is revocable on its own and its usage shows per slot.

    Access → Machines → <slot> → Lanes: on
      * makes client keys `<slot>-kilo` (inference, media) and `<slot>-opencode` (inference),
        each with the persona allow-list the existing `kilo` / `opencode` key has now;
      * puts their values in the secret store, group `lane-<slot>`, as FOUNDRY_KEY_KILO /
        FOUNDRY_KEY_OPENCODE (files foundry_key_kilo.txt / foundry_key_opencode.txt);
      * the slot's own key (secrets scope, its `slots` list naming this slot) then pulls them
        with its bundle into /run/secrets (a tmpfs). No other slot's key can read the group.
    Lanes: off   deletes both keys and both values.
    Revoke       disables them with the slot's key; Delete removes them.

Values are never shown or logged. Turning lanes on needs the secret store unlocked: the
values must go somewhere the slot can pull them from, and nowhere else.
"""

from __future__ import annotations

import time

from . import client_keys
from .secret_store import GROUP_RE

# (suffix, scopes, the key whose personas it copies, secret name, file)
LANE_KEYS = (("-kilo", ["inference", "media"], "kilo", "FOUNDRY_KEY_KILO", "foundry_key_kilo.txt"),
             ("-opencode", ["inference"], "opencode", "FOUNDRY_KEY_OPENCODE",
              "foundry_key_opencode.txt"))
GROUP_PREFIX = "lane-"
NOTE = "Distributed Foundry lane key"


class LaneKeyError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def lane_group(slot: str) -> str:
    """The secret-store group a slot's lane keys live in."""
    g = GROUP_PREFIX + str(slot or "").lower()
    if not GROUP_RE.match(g):
        raise LaneKeyError(f"slot id {slot!r} is too long or not valid for a lane group "
                           f"(at most {40 - len(GROUP_PREFIX)} letters, digits, _ and -)")
    return g


def is_lane_group(group: str) -> bool:
    return str(group or "").startswith(GROUP_PREFIX)


def key_names(slot: str) -> list[str]:
    return [slot + suffix for suffix, *_ in LANE_KEYS]


def _client(cfg, name: str):
    return next((c for c in cfg.clients if c.name == name), None)


def state(svc, m: dict) -> dict:
    """{"on", "keys": {name: none|enabled|disabled}, "personas": {name: [..]},
    "values": {SECRET: set?}} for the Machines page (no values)."""
    cfg = svc.config_store.config
    keys, personas = {}, {}
    for n in key_names(m["slot"] or m["name"]):
        c = _client(cfg, n)
        keys[n] = "none" if c is None else "enabled" if c.enabled else "disabled"
        if c is not None:
            personas[n] = list(c.personas or [])
    values = {}
    try:
        g = lane_group(m["slot"] or m["name"])
        for row in svc.secret_store.list():
            if row["group"] == g:
                values[row["name"]] = bool(row["set"])
    except Exception:                                           # noqa: BLE001
        pass
    on = any(v != "none" for v in keys.values())
    return {"on": on, "keys": keys, "personas": personas, "values": values}


def turn_on(svc, m: dict, actor: str, rotate: bool = False) -> dict:
    """Make (or, with `rotate`, re-make) the slot's two lane keys. Idempotent: lanes already
    on and no `rotate` changes nothing."""
    if m.get("kind") != "slot":
        raise LaneKeyError("lanes are for Studio slots only")
    slot = m["slot"] or m["name"]
    g = lane_group(slot)
    cfg = svc.config_store.config
    names = key_names(slot)
    have = {n: _client(cfg, n) for n in names}
    for n, c in have.items():
        if c is not None and NOTE not in str(c.note or ""):
            raise LaneKeyError(f"a client key named {n!r} already exists and is not a lane key: "
                               "rename or delete it on Clients & Keys first", "name_taken", 409)
    if all(c is not None for c in have.values()) and not rotate:
        return {**state(svc, m), "changed": False}
    store = svc.secret_store
    if store.state() != "unlocked":
        raise LaneKeyError("unlock the secret store first (Access → Secrets): the slot pulls "
                           "its lane keys from there", "secrets_locked", 409)
    # the personas each key copies: the same kind's key, else the other one (OpenCode uses the
    # Foundry-Kilo-* personas through its own key). Never an empty list made up here: an empty
    # list means every persona, so a key gets one only when its source has none.
    sources = {like: _client(cfg, like) for (_s, _sc, like, _n, _f) in LANE_KEYS}
    if not any(sources.values()):
        raise LaneKeyError("there is no kilo or opencode client key to copy personas from "
                           "(Access → Clients & Keys)", "no_source_key", 409)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entries = []
    for (suffix, scopes, like, secret, file) in LANE_KEYS:
        key = client_keys.generate_key()
        src = sources[like] or next(c for c in sources.values() if c is not None)
        store.define(g, secret, file=file,
                     description=f"{slot}'s {like} model key (Distributed Foundry lanes)")
        store.set_value(g, secret, key, actor=actor)
        entries.append({"name": slot + suffix, "key_hash": client_keys.hash_key(key),
                        "key_hint": client_keys.hint(key), "scopes": list(scopes),
                        "projects": [m["project"]] if m.get("project") else [],
                        "slots": [slot], "secret_names": [],
                        "personas": list((src.personas or []) if src else []),
                        "enabled": True,
                        "note": f"{NOTE}: {slot}'s own {like} server ({now[:10]})",
                        "created": now})
        del key
    made = {e["name"] for e in entries}

    def mutate(raw):
        raw.setdefault("clients", [])
        if raw["clients"] is None:
            raw["clients"] = []
        raw["clients"] = [c for c in raw["clients"] if c.get("name") not in made] + entries
    svc.config_store.save(mutate)
    svc.machines.event(m["name"], "lanes_rotated" if rotate else "lanes_on", "", ", ".join(sorted(made)))
    return {**state(svc, m), "changed": True}


def turn_off(svc, m: dict, actor: str) -> dict:
    """Delete the slot's lane keys and their values."""
    slot = m["slot"] or m["name"]
    names = set(key_names(slot))
    cfg = svc.config_store.config
    if not any(_client(cfg, n) for n in names):
        gone = False
    else:
        gone = True

        def mutate(raw):
            raw["clients"] = [c for c in raw.get("clients") or [] if c.get("name") not in names
                              or NOTE not in str(c.get("note") or "")]
        svc.config_store.save(mutate)
    try:
        g = lane_group(slot)
        for _s, _sc, _l, secret, _f in LANE_KEYS:
            try:
                svc.secret_store.delete(g, secret)
                gone = True
            except Exception:                                   # noqa: BLE001
                pass
    except LaneKeyError:
        pass
    if gone:
        svc.machines.event(m["name"], "lanes_off", "", actor)
    return {**state(svc, m), "changed": gone}


def disable(raw: dict, slot: str) -> None:
    """In a config mutate: disable the slot's lane keys (Revoke)."""
    names = set(key_names(slot))
    for c in raw.get("clients") or []:
        if c.get("name") in names and NOTE in str(c.get("note") or ""):
            c["enabled"] = False


__all__ = ["LANE_KEYS", "LaneKeyError", "lane_group", "is_lane_group", "key_names", "state",
           "turn_on", "turn_off", "disable"]

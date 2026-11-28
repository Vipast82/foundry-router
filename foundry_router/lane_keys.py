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


def _personas_of(sources: dict, like: str) -> list:
    """The personas a lane key copies: the same kind's key, else the other one (OpenCode uses
    the Foundry-Kilo-* personas through its own key). Never an empty list made up here: an
    empty list means every persona, so a key gets one only when its source has none."""
    src = sources.get(like) or next((c for c in sources.values() if c is not None), None)
    return list((src.personas or []) if src else [])


def turn_on(svc, m: dict, actor: str, rotate: bool = False) -> dict:
    """Make (or, with `rotate`, re-make) the slot's two lane keys.

    Lanes already on and no `rotate`: no new key or value, but each lane key's personas are
    copied again from the kilo / opencode key (they change when the workstation's key does;
    every call re-syncs them, "changed" says whether anything did).

    Order and rollback (2026-11-27 review): the new key hashes are saved in the config FIRST,
    then the values go to the secret store. When a store write fails, the config gets its
    previous rows back and the store its previous values, so the hashes in the config and the
    values the slot pulls always belong together."""
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
    sources = {like: _client(cfg, like) for (_s, _sc, like, _n, _f) in LANE_KEYS}
    if all(c is not None for c in have.values()) and not rotate:
        return {**state(svc, m), "changed": _resync_personas(svc, m, slot, sources)}
    store = svc.secret_store
    if store.state() != "unlocked":
        raise LaneKeyError("unlock the secret store first (Access → Secrets): the slot pulls "
                           "its lane keys from there", "secrets_locked", 409)
    if not any(sources.values()):
        raise LaneKeyError("there is no kilo or opencode client key to copy personas from "
                           "(Access → Clients & Keys)", "no_source_key", 409)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entries, values = [], []
    for (suffix, scopes, like, secret, file) in LANE_KEYS:
        key = client_keys.generate_key()
        values.append((secret, file, like, key))
        entries.append({"name": slot + suffix, "key_hash": client_keys.hash_key(key),
                        "key_hint": client_keys.hint(key), "scopes": list(scopes),
                        "projects": [m["project"]] if m.get("project") else [],
                        "slots": [slot], "secret_names": [],
                        "personas": _personas_of(sources, like),
                        "enabled": True,
                        "note": f"{NOTE}: {slot}'s own {like} server ({now[:10]})",
                        "created": now})
        del key
    made = {e["name"] for e in entries}
    # the values the store holds now, to put back if a write fails half way (never shown)
    try:
        old_values = {r["name"]: r["value"] for r in store.bundle([g])}
    except Exception:                                           # noqa: BLE001
        old_values = {}
    prev_rows: list = []

    def mutate(raw):
        raw.setdefault("clients", [])
        if raw["clients"] is None:
            raw["clients"] = []
        import copy
        prev_rows[:] = [copy.deepcopy(c) for c in raw["clients"] if c.get("name") in made]
        raw["clients"] = [c for c in raw["clients"] if c.get("name") not in made] + entries
    svc.config_store.save(mutate)
    try:
        for secret, file, like, key in values:
            store.define(g, secret, file=file,
                         description=f"{slot}'s {like} model key (Distributed Foundry lanes)")
            store.set_value(g, secret, key, actor=actor)
    except Exception as e:                                      # noqa: BLE001
        def back(raw):
            raw["clients"] = [c for c in (raw.get("clients") or [])
                              if c.get("name") not in made] + prev_rows
        try:
            svc.config_store.save(back)
        except Exception:                                       # noqa: BLE001
            pass
        for secret, _f, _l, _k in values:
            try:
                if secret in old_values:
                    store.set_value(g, secret, old_values[secret], actor=actor)
                else:
                    store.delete(g, secret)
            except Exception:                                   # noqa: BLE001
                pass
        raise LaneKeyError(f"the lane keys' values could not be stored ({type(e).__name__}); "
                           f"nothing changed", "store_failed", 500) from None
    finally:
        values.clear()
    svc.machines.event(m["name"], "lanes_rotated" if rotate else "lanes_on", "", ", ".join(sorted(made)))
    return {**state(svc, m), "changed": True}


def _resync_personas(svc, m: dict, slot: str, sources: dict) -> bool:
    """Copy the kilo / opencode key's personas to the slot's lane keys again. True when one
    changed (the config is saved only then)."""
    want = {slot + suffix: _personas_of(sources, like) for (suffix, _sc, like, _n, _f) in LANE_KEYS}
    cfg = svc.config_store.config
    if all(list(getattr(_client(cfg, n), "personas", None) or []) == p for n, p in want.items()):
        return False
    if not any(sources.values()):
        return False                     # nothing to copy from: keep what they have

    def mutate(raw):
        for c in raw.get("clients") or []:
            if c.get("name") in want and NOTE in str(c.get("note") or ""):
                c["personas"] = list(want[c["name"]])
    svc.config_store.save(mutate)
    svc.machines.event(m["name"], "lanes_personas", "", ", ".join(sorted(want)))
    return True


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

"""Service and MCP registry (Team Lead piece 5).

A sprint declares what it needs (`studio`, `blender`, `audio`, `images`, `gpu`,
`backend`; a plan's `## Needs` block, or `data["needs"]` that the track analysis
sets). Before a session starts the engine asks `blocks()`, which asks `ready()`:
every service the needs map to (foundry_automation.service_map) must be up.
A missing service is not a failure: the task keeps its place, its next_action
says `waiting for <service>`, the check repeats with a doubling backoff (30 s ..
10 min, no attempt used), and when the service is a Foundry Client component the
coordinator leaves the client a `start_service` request (client_requests.py).

The `services` table keeps the last probe of every catalog service (name, kind,
check, provider, needed_by, status, detail, ts). It is created here on first use
(`ensure`), not in store.SCHEMA, so the table needs no schema migration. Status
is up | down | unknown; unknown (a server that is nowhere defined, a setting
that turned it off) never blocks.

Probes are cheap (a binary that exists, a port that accepts a connection, an
HTTP answer below 500), never an MCP handshake; see service_map's docstring for
the Kilo assumptions. A profile may override a service in a `[services.<name>]`
table: `port`, `host`, `url`, `enabled = false`.
"""

from __future__ import annotations

import json
import re
import time
from typing import Callable, Optional

from foundry_automation import service_map as SM
from foundry_automation.service_map import (NEEDS, NEEDS_ALL, NEEDS_MCP, NEEDS_SERVICES,  # noqa: F401
                                            SERVICES, normalize_needs, services_for)

TTL = 30.0               # a probe younger than this is trusted
BACKOFF_FIRST = 30
BACKOFF_MAX = 600
REQUEST_EVERY = 600      # a client start request per service at most this often

DDL = """
CREATE TABLE IF NOT EXISTS services (
  name TEXT PRIMARY KEY, kind TEXT, check_kind TEXT, provider TEXT, component TEXT,
  needed_by TEXT, status TEXT, detail TEXT, checked_ts REAL, since_ts REAL);
"""

Probe = Callable[[str], tuple]          # service name -> (ok: True|False|None, detail)


# --------------------------------------------------------------------------- table

def ensure(db) -> None:
    if db is not None and not getattr(db, "_services_ready", False):
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                db.x(stmt)
        db._services_ready = True


def _status(ok) -> str:
    return "up" if ok is True else "down" if ok is False else "unknown"


def make_probe(profile=None, mcp: Optional[dict] = None, overrides: Optional[dict] = None,
               global_path: Optional[str] = None, port_fn: Callable = SM.port_open,
               http_fn: Optional[Callable] = None, which: Callable = None,
               isfile: Callable = None) -> Probe:
    """The real probe: the project's kilo.jsonc (+ the global Kilo config) resolved once,
    each service checked on demand. Everything is injectable for tests."""
    kw = {}
    if which is not None:
        kw["which"] = which
    if isfile is not None:
        kw["isfile"] = isfile
    state: dict = {}

    def rows() -> dict:
        if "rows" not in state:
            servers = mcp
            if servers is None:
                path = ""
                if profile is not None:
                    try:
                        from .kilo_config import kilo_path
                        path = kilo_path(profile)
                    except Exception:                            # noqa: BLE001
                        path = ""
                servers = SM.merged_mcp(project_path=path, global_path=global_path)
            # one thread per server: an unreachable remote host costs its timeout once, not
            # once per server in a row
            from concurrent.futures import ThreadPoolExecutor
            names = list(servers)
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as ex:
                got = list(ex.map(lambda n: SM.probe_server(n, servers[n], port_fn=port_fn, **kw),
                                  names))
            state["rows"] = dict(zip(names, got))
        return state["rows"]

    ov = overrides
    if ov is None:
        ov = dict(((getattr(profile, "raw", None) or {}).get("services")) or {}) \
            if profile is not None else {}

    def probe(name: str):
        svc = SERVICES.get(name)
        if svc is None:
            return None, "not in the service catalog"
        return SM.check_service(svc, rows() if svc.check == "mcp" else {}, ov, port_fn, http_fn)

    return probe


def refresh(db, probe: Optional[Probe] = None, names=None, now: Optional[float] = None,
            profile=None) -> list[dict]:
    """Probe the named services (default all) and store the result; returns the rows."""
    ensure(db)
    probe = probe or make_probe(profile)
    now = time.time() if now is None else now
    out = []
    for name in names or list(SERVICES):
        svc = SERVICES.get(name)
        if svc is None:
            continue
        try:
            ok, detail = probe(name)
        except Exception as e:                                   # noqa: BLE001
            ok, detail = None, f"probe failed: {type(e).__name__}: {e}"[:200]
        status = _status(ok)
        old = db.q("SELECT status, since_ts FROM services WHERE name=?", (name,)) if db else []
        since = old[0]["since_ts"] if old and old[0]["status"] == status else now
        if db is not None:
            db.x("INSERT INTO services (name, kind, check_kind, provider, component, needed_by, "
                 "status, detail, checked_ts, since_ts) VALUES (?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, check_kind=excluded.check_kind,"
                 " provider=excluded.provider, component=excluded.component, "
                 "needed_by=excluded.needed_by, status=excluded.status, detail=excluded.detail, "
                 "checked_ts=excluded.checked_ts, since_ts=excluded.since_ts",
                 (name, svc.kind, svc.check, svc.provider, svc.component,
                  json.dumps(SM.needed_by(name)), status, str(detail)[:300], now, since))
        out.append({"name": name, "status": status, "detail": str(detail)[:300], "checked_ts": now})
    return out


def snapshot(db, profile=None, probe: Optional[Probe] = None, now: Optional[float] = None,
             max_age: float = TTL) -> list[dict]:
    """The registry rows (catalog info + last status), refreshed when older than `max_age`."""
    ensure(db)
    now = time.time() if now is None else now
    have = {r["name"]: r for r in db.q("SELECT * FROM services")}
    stale = [n for n in SERVICES if n not in have
             or now - float(have[n]["checked_ts"] or 0) > max_age]
    if stale:
        refresh(db, probe, stale, now, profile)
        have = {r["name"]: r for r in db.q("SELECT * FROM services")}
    out = []
    for name, svc in SERVICES.items():
        r = have.get(name) or {}
        out.append({"name": name, "label": svc.label, "kind": svc.kind, "check": svc.check,
                    "provider": svc.provider, "component": svc.component,
                    "needed_by": SM.needed_by(name), "status": r.get("status") or "unknown",
                    "detail": r.get("detail") or "", "checked_ts": r.get("checked_ts"),
                    "since_ts": r.get("since_ts")})
    return out


# --------------------------------------------------------------------------- ready

def ready(needs, db=None, now: Optional[float] = None, probe: Optional[Probe] = None,
          profile=None, ttl: float = TTL) -> tuple[bool, list[str]]:
    """(ok, missing): are the services for `needs` up? `missing` names what is down (an
    any-of group is listed as 'a/b' when all of it is down). Unknown never counts as missing.
    With a `db` the stored probes are used while younger than `ttl`; without one every
    service is probed now."""
    wanted = services_for(needs)
    if not wanted:
        return True, []
    now = time.time() if now is None else now
    probe = probe or make_probe(profile)
    results: dict = {}
    if db is not None:
        ensure(db)
        have = {r["name"]: r for r in db.q("SELECT name, status, checked_ts FROM services")}
        stale = [n for n in wanted if n not in have
                 or now - float(have[n]["checked_ts"] or 0) > ttl]
        if stale:
            refresh(db, probe, stale, now, profile)
            have = {r["name"]: r for r in db.q("SELECT name, status, checked_ts FROM services")}
        for n in wanted:
            st = (have.get(n) or {}).get("status")
            results[n] = True if st == "up" else False if st == "down" else None
    else:
        for n in wanted:
            try:
                results[n] = probe(n)[0]
            except Exception:                                    # noqa: BLE001
                results[n] = None
    return SM.evaluate(needs, results)


# --------------------------------------------------------------------------- needs of a task

_HEAD = re.compile(r"^#{1,6}\s*needs\b.*$", re.I | re.M)
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")


def parse_needs(text: str) -> list[str]:
    """Need words from a plan's `## Needs` block (words separated by commas, spaces or list
    bullets; `none` alone = nothing). The free-text `**NEEDS (MCP):**` line is NOT read: its
    ON/OFF forms vary and name servers, not needs, so a guess could hold a sprint for a
    service it never uses. A plan without the block declares nothing."""
    m = _HEAD.search(text or "")
    if not m:
        return []
    body = (text or "")[m.end():]
    nxt = re.search(r"^#{1,6}\s", body, re.M)
    words = re.sub(r"[`*\-\u2022]", " ", body[:nxt.start()] if nxt else body)
    return normalize_needs(_NAME.findall(words))


def needs_of(t: dict, spec=None) -> list[str]:
    """A task's needs, first declaration wins: data["needs"] (the track analysis, the chat),
    the spec's data["needs"] (the plan parser), `needs:x` tags, the plan's `## Needs` block."""
    data = (t or {}).get("data") or {}
    if data.get("needs") is not None:
        return normalize_needs(data.get("needs"))
    sdata = getattr(spec, "data", None) or {}
    if sdata.get("needs") is not None:
        return normalize_needs(sdata.get("needs"))
    tags = [x for x in (getattr(spec, "tags", None) or []) if str(x).startswith("needs:")]
    if tags:
        return normalize_needs(tags)
    return parse_needs(str(getattr(spec, "spec", "") or ""))


# --------------------------------------------------------------------------- the engine hook

def _profile(P):
    return getattr(P, "profile", None)


def _request_start(c, P, names: list[str], reason: str) -> list[str]:
    """Leave the client a start request for each missing service that a client component runs.
    Rate limited per service. Returns the services asked for."""
    from . import client_requests
    asked: list[str] = []
    for n in names:
        for name in n.split("/"):
            svc = SERVICES.get(name)
            if svc is None or not svc.component:
                continue
            key = f"svc_req:{name}"
            last = float(c.store.meta(key) or 0)
            if c.clock() - last < REQUEST_EVERY:
                continue
            try:
                client_requests.request(c.state_dir, "start_service", P.id, reason,
                                        {"service": name, "component": svc.component},
                                        store=c.store, clock=c.clock, key=name)
            except Exception:                                    # noqa: BLE001
                continue
            c.store.set_meta(key, str(c.clock()))
            asked.append(name)
    return asked


def waiting(c, P, t: dict) -> Optional[str]:
    """The note while a scheduled service re-check is not due yet (else None)."""
    w = ((t.get("data") or {}).get("svc_wait") or {})
    if w and c.clock() < float(w.get("retry_at") or 0):
        return (f"waiting for {', '.join(w.get('missing') or ['a service'])}; next check in "
                f"{int(float(w['retry_at']) - c.clock())} s (no attempt is used)")
    return None


def blocks(c, P, t: dict, spec=None) -> bool:
    """Called before an implementer session starts. True = a needed service is missing (the
    task waits, nothing started or charged). Never fails the task."""
    needs = needs_of(t, spec)
    data = t.get("data") or {}
    if (data.get("needs") or []) != needs and needs:
        c.store.merge_data(P.id, t["task_id"], needs=needs)
    if not needs:
        return False
    note = waiting(c, P, t)
    if note:
        c._note(P, t, note)
        return True
    if images_hold(c, P, t, needs):
        return True
    ok, missing = ready(needs, db=c.store, now=c.clock(), profile=_profile(P))
    if ok:
        if data.get("svc_wait"):
            c.store.merge_data(P.id, t["task_id"], svc_wait=None)
            c.store.event(P.id, t["task_id"], "service_ready", detail={"needs": needs})
        return False
    w = dict(data.get("svc_wait") or {})
    n = int(w.get("n") or 0)
    delay = min(BACKOFF_MAX, BACKOFF_FIRST * (2 ** n))
    changed = sorted(w.get("missing") or []) != sorted(missing)
    w = {"n": n + 1, "retry_at": c.clock() + delay, "missing": missing, "needs": needs,
         "since": w.get("since") or c.clock()}
    c.store.merge_data(P.id, t["task_id"], svc_wait=w)
    asked = _request_start(c, P, missing, f"{t['task_id']} needs {', '.join(needs)}: "
                                          f"{', '.join(missing)} is down")
    if changed or n == 0:
        c.store.event(P.id, t["task_id"], "service_wait",
                      detail={"missing": missing, "needs": needs, "asked_client": asked,
                              "retry_in_s": delay})
    c._note(P, t, f"waiting for {', '.join(missing)}"
                  + (" (asked the Foundry Client to start it)" if asked else "")
                  + f"; next check in {delay} s (no attempt is used)")
    return True


# --------------------------------------------------------------------------- per-track kilo.jsonc

def track_kilo_config(track, *, profile=None, role_rules: Optional[dict] = None,
                      source: str = "", workspace: str = "") -> str:
    """Write the track's kilo.jsonc (MCP allow-list = its needs) and return the path."""
    from . import kilo_config
    return kilo_config.track_kilo_config(track, profile=profile, role_rules=role_rules,
                                         source=source, workspace=workspace)


# --------------------------------------------------------------------------- 2D assets

IMG_CHATGPT = "I will provide the 2D assets (ChatGPT); run it without Media_Gen image tools"
IMG_MEDIA_GEN = "Use Media_Gen's image tools for the 2D assets"
IMG_HOLD = "Hold this sprint"
IMG_OPTIONS = [IMG_CHATGPT, IMG_MEDIA_GEN, IMG_HOLD]


def images_decision(c, pid: str, tid: str):
    """The newest open or answered 2D-asset decision of the task, or None."""
    rows = [d for d in c.obligations.active_decisions(pid, tid, "approval")
            if list(d.get("options") or []) == IMG_OPTIONS]
    return rows[-1] if rows else None


def images_choice(c, pid: str, tid: str) -> str:
    """'chatgpt' | 'media_gen' | 'hold' (answered) | 'open' | '' (never asked)."""
    d = images_decision(c, pid, tid)
    if d is None:
        return ""
    if d.get("status") != "answered":
        return "open"
    from .decision_policy import match_option
    ans = d.get("answer")
    if isinstance(ans, dict):
        ans = ans.get("option") or ans.get("choice") or ""
    pick = match_option(IMG_OPTIONS, ans)
    return {IMG_CHATGPT: "chatgpt", IMG_MEDIA_GEN: "media_gen"}.get(pick, "hold")


def images_mode(P) -> str:
    from .automation import get as auto_get
    try:
        mode = str(auto_get(P, "media_gen_images") or "ask").strip().lower()
    except KeyError:
        mode = "ask"
    return mode if mode in ("ask", "allow", "deny") else "ask"


def images_hold(c, P, t: dict, needs: list) -> bool:
    """2026-10-27 (Victor): "if there is anything that needs 2D assets, let me know
    first" (they come from ChatGPT for now). A sprint that needs `images` waits on an
    operator decision unless [automation] media_gen_images is 'allow' or 'deny'. The
    answer lives in the decision itself (task data can be rewritten by a transition);
    "Hold" keeps the sprint waiting. True = do not start it now."""
    if "images" not in needs or images_mode(P) != "ask":
        return False
    tid = t["task_id"]
    choice = images_choice(c, P.id, tid)
    if choice in ("chatgpt", "media_gen"):
        return False
    d = images_decision(c, P.id, tid)
    if choice == "hold":
        c._note(P, t, f"held by the operator: this sprint needs 2D assets "
                      f"(decision {d['decision_id']})")
        return True
    if d is None:
        q = (f"{tid} needs 2D image assets (icons, textures, decals, portraits or UI art). "
             f"They come from you (ChatGPT) for now: provide them, let Media_Gen make them, "
             f"or hold the sprint?")
        did = c.obligations.open_decision(P.id, tid, "approval", q, evidence={"needs": needs},
                                          options=list(IMG_OPTIONS), recommendation=IMG_CHATGPT,
                                          blocks=[tid])
        c.store.event(P.id, tid, "operator_notice", detail={
            "text": f"{tid} needs 2D image assets and waits for your answer (decision {did}): "
                    f"provide them (ChatGPT), use Media_Gen, or hold.", "decision_id": did})
    else:
        did = d["decision_id"]
    c._note(P, t, f"waiting for the operator: 2D image assets (decision {did})")
    return True

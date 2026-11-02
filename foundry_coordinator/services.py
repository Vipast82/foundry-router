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


def widen_track_needs(c, P, t: dict, spec=None) -> list[str]:
    """A track sprint's plan may declare needs its track lacks: the track's needs (and so its
    kilo.jsonc MCP allow-list) were set when the track started, before the sprint wrote its
    plan. 2026-11-02 S54 (audio-local, needs [studio]): its plan says `## Needs: studio,
    audio`, Media_Gen's audio tools stayed denied and the session stopped BLOCKED; the same
    happened to the assets-local track on 2026-10-28. Before the session starts, the missing
    needs join the track (set_needs flags the Kilo reload the session start applies).
    `studio` never joins a track without a Studio slot: its sessions would reach the
    workstation's Studio. Returns the needs added."""
    try:
        if not c.tracks.is_track(P):
            return []
        name = (getattr(P.profile, "__dict__", {}) or {}).get("track")
        main = c.tracks.main_of(P)
        row = c.store.track(main.id, name) if name else None
        if row is None:
            return []
        declared = needs_of({}, spec) if spec is not None else []
        have = list(row.get("needs") or [])
        slot = (row.get("data") or {}).get("slot")
        add = [n for n in declared if n not in have and (n != "studio" or slot)]
        if not add:
            return []
        c.tracks.set_needs(main, name, have + add,
                           f"coordinator ({t['task_id']}'s plan needs {', '.join(add)})")
        c.store.event(P.id, t["task_id"], "track_needs_widened",
                      detail={"track": name, "added": add, "needs": have + add})
        return add
    except Exception as e:                                        # noqa: BLE001
        try:
            c.log(f"{P.id}/{t.get('task_id')}: track needs check failed: {type(e).__name__}: {e}")
        except Exception:                                         # noqa: BLE001
            pass
        return []


def blocks(c, P, t: dict, spec=None) -> bool:
    """Called before an implementer session starts. True = a needed service is missing (the
    task waits, nothing started or charged). Never fails the task."""
    widen_track_needs(c, P, t, spec)
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
    if images_hold(c, P, t, needs, spec):
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

# 2026-10-27 (Victor): 2D images come from ChatGPT first (the foundry-media tools and the
# foundry-images skill), with Media_Gen's image tools as the fallback, and only after the
# owner's answer. The three options of the 2D decision:
IMG_GENERATE = "Generate them: ChatGPT first (foundry-media), Media_Gen fallback"
IMG_PROVIDE = "I will provide the 2D assets myself"
IMG_HOLD = "Hold this sprint"
IMG_OPTIONS = [IMG_GENERATE, IMG_PROVIDE, IMG_HOLD]
# the v0.148-v0.150 options, still read from decisions opened before this change
IMG_CHATGPT = "I will provide the 2D assets (ChatGPT); run it without Media_Gen image tools"
IMG_MEDIA_GEN = "Use Media_Gen's image tools for the 2D assets"
IMG_OPTIONS_V148 = [IMG_CHATGPT, IMG_MEDIA_GEN, IMG_HOLD]
_CHOICE = {IMG_GENERATE: "generate", IMG_PROVIDE: "provide",
           IMG_CHATGPT: "provide", IMG_MEDIA_GEN: "generate"}


def images_decision(c, pid: str, tid: str):
    """The newest open or answered 2D-asset decision of the task, or None."""
    rows = [d for d in c.obligations.active_decisions(pid, tid, "approval")
            if list(d.get("options") or []) in (IMG_OPTIONS, IMG_OPTIONS_V148)]
    return rows[-1] if rows else None


def images_choice(c, pid: str, tid: str) -> str:
    """'generate' | 'provide' | 'hold' (answered) | 'open' | '' (never asked)."""
    d = images_decision(c, pid, tid)
    if d is None:
        return ""
    if d.get("status") != "answered":
        return "open"
    from .decision_policy import match_option
    ans = d.get("answer")
    if isinstance(ans, dict):
        ans = ans.get("option") or ans.get("choice") or ""
    pick = match_option(list(d.get("options") or IMG_OPTIONS), ans)
    return _CHOICE.get(pick, "hold")


def images_mode(P) -> str:
    from .automation import get as auto_get
    try:
        mode = str(auto_get(P, "media_gen_images") or "ask").strip().lower()
    except KeyError:
        mode = "ask"
    return mode if mode in ("ask", "allow", "deny") else "ask"


_REF_VIEWS = re.compile(r"\bref[_\- ]?views?\b", re.I)


def ref_views_declared(t: dict, spec=None) -> bool:
    """TURNAROUND_RECIPE_PLAN D-T3: does the sprint say its images are reference views drawn
    from an approved 3D sheet? Task data `ref_views: true`, a `needs:ref_views` tag, or the
    word `ref_views` in the plan's `## Needs` block."""
    data = (t or {}).get("data") or {}
    sdata = getattr(spec, "data", None) or {}
    if data.get("ref_views") or sdata.get("ref_views"):
        return True
    for tag in getattr(spec, "tags", None) or []:
        if _REF_VIEWS.fullmatch(str(tag).replace("needs:", "").strip()):
            return True
    return ref_views_in_text(str(getattr(spec, "spec", "") or ""))


def ref_views_in_text(text: str) -> bool:
    """The word `ref_views` in a plan's `## Needs` block (the heading line or its body). A
    project adapter that condenses the plan into the task spec records it as task data
    `ref_views` (v0.179.0: the AU adapter's spec has no Needs block)."""
    m = _HEAD.search(text or "")
    if not m:
        return False
    body = text[m.end():]
    nxt = re.search(r"^#{1,6}\s", body, re.M)
    return bool(_REF_VIEWS.search(m.group(0) + "\n" + (body[:nxt.start()] if nxt else body)))


def ref_views_mode(P) -> str:
    """[automation] ref_views_images: 'ask' (default: the 2D decision as for any images) or
    'allow' (D3's standing OK for reference views drawn from an approved 3D sheet)."""
    from .automation import get as auto_get
    try:
        mode = str(auto_get(P, "ref_views_images") or "ask").strip().lower()
    except KeyError:
        mode = "ask"
    return mode if mode in ("ask", "allow") else "ask"


def ref_views_ok(P, t: dict, needs: list, spec=None) -> bool:
    """The standing OK applies: the sprint needs `images` and declares `ref_views`, the owner
    switched ref_views_images to 'allow', and media_gen_images is 'ask' ('deny' still wins)."""
    return ("images" in needs and images_mode(P) == "ask" and ref_views_mode(P) == "allow"
            and ref_views_declared(t, spec))


def images_allowed(c, P, t: dict, needs: list, spec=None) -> bool:
    """May this sprint's session generate 2D images (foundry-media first, Media_Gen's image
    tools as the fallback)? Only a sprint that needs `images`, and only when
    [automation] media_gen_images is 'allow', the owner answered "Generate them", or the
    reference-view standing OK applies (ref_views_ok)."""
    if "images" not in needs:
        return False
    mode = images_mode(P)
    if mode == "allow":
        return True
    if mode == "deny":
        return False
    if ref_views_ok(P, t, needs, spec):
        return True
    return images_choice(c, P.id, t["task_id"]) == "generate"


def images_hold(c, P, t: dict, needs: list, spec=None) -> bool:
    """2026-10-27 (Victor): "if there is anything that needs 2D assets, let me know
    first". A sprint that needs `images` waits on an operator decision unless
    [automation] media_gen_images is 'allow' or 'deny', or the reference-view standing OK
    applies (D3: ref_views_ok). The answer lives in the
    decision itself (task data can be rewritten by a transition); "Hold" keeps the
    sprint waiting. True = do not start it now."""
    if "images" not in needs or images_mode(P) != "ask":
        return False
    tid = t["task_id"]
    if ref_views_ok(P, t, needs, spec):
        key = f"ref_views_ok:{P.id}:{tid}"
        if not c.store.meta(key):
            c.store.set_meta(key, str(c.clock()))
            c.store.event(P.id, tid, "ref_views_standing_ok", detail={
                "needs": needs, "text": f"{tid} draws reference views from an approved 3D "
                "sheet: started under the standing OK (ref_views_images = allow, D3)"})
        return False
    choice = images_choice(c, P.id, tid)
    if choice in ("generate", "provide"):
        return False
    d = images_decision(c, P.id, tid)
    if choice == "hold":
        c._note(P, t, f"held by the operator: this sprint needs 2D assets "
                      f"(decision {d['decision_id']})")
        return True
    if d is None:
        q = (f"{tid} needs 2D image assets (icons, textures, decals, portraits or UI art). "
             f"Generate them (ChatGPT first through foundry-media, Media_Gen's image tools as "
             f"the fallback), provide them yourself, or hold the sprint?")
        did = c.obligations.open_decision(P.id, tid, "approval", q, evidence={"needs": needs},
                                          options=list(IMG_OPTIONS), recommendation=IMG_GENERATE,
                                          blocks=[tid])
        c.store.event(P.id, tid, "operator_notice", detail={
            "text": f"{tid} needs 2D image assets and waits for your answer (decision {did}): "
                    f"generate them (ChatGPT first, Media_Gen fallback), provide them yourself, "
                    f"or hold.", "decision_id": did})
    else:
        did = d["decision_id"]
    c._note(P, t, f"waiting for the operator: 2D image assets (decision {did})")
    return True


REF_VIEWS_BRIEF = (
    "\n## 2D images (reference views only)\nThis sprint runs under the owner's standing OK for "
    "reference views drawn from an already-approved 3D concept sheet. Use the \"Turnaround "
    "views\" recipe of the `foundry-images` skill (the foundry-media tools: ChatGPT images): a "
    "three-quarter view first, then each missing view, always with the approved sheet as a "
    "reference. Save under `assets/source/<cat>/<ID>/ref/drawn/`; never overwrite or rename an "
    "approved `<ID>_ref_v<n>.png`. At most six images per asset, one redraw per view. Check a "
    "running job once per completed step (never a polling loop), and never call "
    "generate_image / edit_image twice for the same request. The OK covers nothing else: an "
    "icon, UI art, a texture or a decal in this sprint is reported BLOCKED `user_action` naming "
    "the file. Drawn views are unapproved: list them in your report; sizes come from "
    "`ref_align.py` and the bible, never from a drawn picture alone.\n")


def images_brief(c, P, t: dict, needs: list, spec=None) -> str:
    """The BUILD prompt's 2D paragraph for a sprint that needs `images` ('' otherwise)."""
    if "images" not in needs:
        return ""
    if ref_views_ok(P, t, needs, spec):
        return REF_VIEWS_BRIEF
    if images_allowed(c, P, t, needs, spec):
        return ("\n## 2D images\nThe owner allowed image generation for this sprint. Use the "
                "`foundry-images` skill (the foundry-media tools: ChatGPT images) first; use "
                "Media_Gen's image tools only when foundry-media refuses or fails. Check a "
                "running job once per completed step (never a polling loop), and never call "
                "generate_image / edit_image twice for the same request. Generated images are "
                "unapproved: stage them, list them in your report, and wire in only what the "
                "plan says.\n")
    return ("\n## 2D images\nThe owner provides this sprint's 2D assets: do not generate images "
            "(the image tools are off). Use the files the plan or the owner names; if one is "
            "missing, report BLOCKED `user_action` naming the exact file.\n")

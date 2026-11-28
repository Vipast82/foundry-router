"""Fleet (router → Fleet → Fleet): every machine that works for Foundry, across all projects,
in one table (Distributed Foundry, docs/DISTRIBUTED_FOUNDRY.md).

    GET /admin/api/fleet

Read-only. It joins what the router already holds; nothing here talks to a machine:
  * Access → Machines (machines.py): the slots and workstations added there, their enrolment,
    their client key's last request, their lane keys (lane_keys.state);
  * the "which workstation runs each game" claims (one active workstation per project);
  * the heartbeats of Foundry Clients (source kind `client`: version, components) and of
    coordinators (source kind `coordinator`: version, and the `tracks` block per project with
    its tracks, `lane_tunnels`, `slots` (lanes, os, studio, disk %, the slot's Foundry version)
    and `junction` (this workstation's disk));
  * each project's deployed automation settings (slots.<id>.lanes, [automation] remote_lanes,
    [client] kind).

Words used on the page: a **slot** is a machine that runs Studio and lanes; a **lane** is a
track whose worktree and sessions live on a slot; the **junction** is the main workstation (the
coordinator, the main checkout, the only machine that publishes).

Each row has the three lane checks a slot needs, kept apart so the page can say which one is
missing: lane keys on (Access → Machines → Lanes), the slot's `lanes` setting on, and the lane
agent answering (the coordinator's tunnel to it is up). `warnings` lists what does not fit
together (see `_warnings`).
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
A = "/admin/api/fleet"
MACHINES_LINK = "/ui#Machines"
TRACKS_LINK = "/ui/automation#tracks"
SETTINGS_LINK = "/ui/automation#settings"
LIVE_TRACK_STATES = ("planned", "running", "merging", "failed")


def _v(s) -> str:
    return str(s or "").strip().lstrip("vV")


def _project_settings(svc, project: str) -> dict:
    try:
        dep = svc.automation.deployed(project) or {}
    except Exception:                                            # noqa: BLE001
        return {}
    s = ((dep.get("doc") or {}).get("settings") or {})
    return s if isinstance(s, dict) else {}


def _projects(svc) -> list[str]:
    try:
        from .secrets_api import _projects as P
        return list(P(svc))
    except Exception:                                            # noqa: BLE001
        return []


def _sources(svc) -> list[dict]:
    try:
        return list(svc.control.sources())
    except Exception:                                            # noqa: BLE001
        return []


def _coordinator_tracks(sources: list) -> dict:
    """{project: (status, source)} from the freshest coordinator that reports the project."""
    out: dict = {}
    for s in sources:
        if s.get("kind") != "coordinator":
            continue
        tr = (s.get("detail") or {}).get("tracks")
        if not isinstance(tr, dict):
            continue
        for pid, st in tr.items():
            if not isinstance(st, dict) or st.get("error"):
                continue
            cur = out.get(pid)
            if cur is None or (s.get("last_seen") or 0) > (cur[1].get("last_seen") or 0):
                out[pid] = (st, s)
    return out


class _Row(dict):
    """One machine; `_names` / `_keys` are how heartbeats and claims are matched to it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._names: set = set()
        self._keys: set = set()

    def matches(self, host: str = "", key: str = "") -> bool:
        return bool((host and host.lower() in self._names) or (key and key in self._keys))


def _new_row(name: str, kind: str) -> _Row:
    return _Row(name=name, kind=kind, projects=[], os="", studio="", host="", source="",
                enrolled_ts=None, enrolled_hostname="", state="", last_seen=None,
                version="", version_from="", version_ok=None, disk_pct=None, lanes=None,
                tunnel=None, running=[], components={}, active_for=[], standby_for=[],
                note="", links={"machines": MACHINES_LINK, "tracks": TRACKS_LINK})


def _state(view: dict) -> str:
    if view.get("key") == "disabled":
        return "revoked"
    if not view.get("enrolled_ts"):
        return "waiting for its installer" if view.get("code_pending") else "not installed"
    if (view.get("settings") or {}).get("enabled") is False:
        return "installed, not ready yet"
    return "connected"


def build(svc, now: Optional[float] = None) -> dict:
    from . import __version__ as router_version
    from . import client_keys
    from . import machines as M
    now = now or time.time()
    sources = _sources(svc)
    coord = _coordinator_tracks(sources)
    projects = _projects(svc)
    for pid in coord:
        if pid not in projects:
            projects.append(pid)
    psets = {p: _project_settings(svc, p) for p in projects}
    try:
        seen = client_keys.activity.snapshot()["seen"]
    except Exception:                                            # noqa: BLE001
        seen = {}
    try:
        mrows = svc.machines.list()
    except Exception:                                            # noqa: BLE001
        mrows = []
    rows: list[_Row] = []
    slots_by_id: dict = {}
    # ---- machines added on Access → Machines
    for m in mrows:
        try:
            view = M._view(svc, m, seen)
        except Exception:                                        # noqa: BLE001
            view = {k: m.get(k) for k in ("name", "kind", "project", "slot", "host",
                                          "enrolled_ts", "enrolled_hostname")}
        r = _new_row(m["name"], "slot" if m.get("kind") == "slot" else "workstation")
        r._names |= {str(m["name"]).lower()}
        if m.get("enrolled_hostname"):
            r._names.add(str(m["enrolled_hostname"]).lower())
        r.update(projects=[m["project"]] if m.get("project") else [], host=m.get("host") or "",
                 source="machines", enrolled_ts=m.get("enrolled_ts"),
                 enrolled_hostname=m.get("enrolled_hostname") or "", state=_state(view),
                 last_seen=view.get("last_seen"), note=m.get("note") or "")
        settings = view.get("settings") or {}
        if r["kind"] == "slot":
            sid = m.get("slot") or m["name"]
            r["slot"] = sid
            r._keys |= {m["name"], f"{sid}-kilo", f"{sid}-opencode"}
            os_ = str(settings.get("os") or m.get("os") or "linux")
            r.update(os=os_, studio=str(settings.get("studio")
                                        or ("native" if os_ == "windows" else "container")))
            lp = view.get("last_pull") or {}
            if lp.get("ts") and (r["last_seen"] or 0) < lp["ts"]:
                r["last_seen"] = lp["ts"]
            r["lanes"] = {"keys": bool((view.get("lanes") or {}).get("on")),
                          "setting": settings.get("lanes") is True,
                          "agent": None,
                          "remote_lanes": ((psets.get(m.get("project")) or {})
                                           .get("automation") or {}).get("remote_lanes") is True,
                          "enabled": settings.get("enabled") is not False}
            slots_by_id[(m.get("project"), sid)] = r
        else:
            r._keys |= set(M.ws_key_names(m["name"]))
            r.update(os=str(m.get("os") or ""),
                     studio=(f"slot {view.get('studio_slot')}" if view.get("studio_slot")
                             else ""))
            for k in M.ws_key_names(m["name"]):
                ts = (seen.get(k) or {}).get("last_ts")
                if ts and (r["last_seen"] or 0) < ts:
                    r["last_seen"] = ts
        rows.append(r)
    # ---- workstations that checked in to run a game (one active per project)
    try:
        claims = svc.machines.claims()
    except Exception:                                            # noqa: BLE001
        claims = []
    for c in claims:
        r = next((x for x in rows if x["kind"] == "workstation"
                  and x.matches(c.get("name") or "", c.get("client") or "")), None)
        if r is None:
            r = _new_row(c.get("name") or c.get("client") or "?", "workstation")
            r.update(source="check-in", state="checked in")
            rows.append(r)
        if c.get("name"):
            r._names.add(str(c["name"]).lower())
        if c.get("client"):
            r._keys.add(c["client"])
        (r["active_for"] if c.get("active") else r["standby_for"]).append(c["project"])
        if c["project"] not in r["projects"]:
            r["projects"].append(c["project"])
        if c.get("last_ts") and (r["last_seen"] or 0) < c["last_ts"]:
            r["last_seen"] = c["last_ts"]
    # ---- heartbeats: Foundry Clients and coordinators
    for s in sources:
        if s.get("kind") not in ("client", "coordinator"):
            continue
        host, key = str(s.get("host") or ""), str(s.get("client_id") or "")
        r = next((x for x in rows if x["kind"] == "slot" and x.matches(host, key)), None) \
            or next((x for x in rows if x["kind"] == "workstation" and x.matches(host, key)), None)
        if r is None:
            r = _new_row(host or s.get("source_id") or "?", "workstation")
            r.update(source="heartbeat", state="reporting")
            rows.append(r)
        if host:
            r._names.add(host.lower())
        if s.get("last_seen") and (r["last_seen"] or 0) < s["last_seen"]:
            r["last_seen"] = s["last_seen"]
        d = s.get("detail") or {}
        if s["kind"] == "client":
            ver = _v(d.get("client_version") or s.get("version"))
            if ver:
                r.update(version=ver, version_from="Foundry Client heartbeat")
            r["components"] = {c.get("name"): {"enabled": c.get("enabled"), "state": c.get("state")}
                               for c in d.get("components") or [] if isinstance(c, dict)}
            if r["kind"] == "workstation" and r["state"] in ("", "reporting"):
                r["state"] = "reporting" if s.get("fresh") else "not reporting"
        else:
            r["coordinator"] = s.get("source_id")
            ver = _v(s.get("version") or d.get("foundry_version") or d.get("coordinator_version"))
            if ver and not r["version"]:
                r.update(version=ver, version_from="coordinator heartbeat")
            for pid in d.get("projects") or []:
                if isinstance(pid, str) and pid not in r["projects"]:
                    r["projects"].append(pid)
    # ---- what each coordinator says about its projects' lanes and slots
    for pid, (st, src) in coord.items():
        ws = next((x for x in rows if x.get("coordinator") == src.get("source_id")), None)
        if ws is not None:
            j = st.get("junction") or {}
            if j.get("disk_pct") is not None:
                ws["disk_pct"] = j["disk_pct"]
            main = (st.get("main") or {}).get("sprint")
            if main:
                ws["running"].append({"project": pid, "track": "main", "sprint": main,
                                      "state": "running"})
        for t in st.get("tracks") or []:
            if t.get("state") not in LIVE_TRACK_STATES:
                continue
            item = {"project": pid, "track": t.get("name"), "sprint": t.get("current_sprint") or "",
                    "state": t.get("state")}
            host = str(t.get("host") or "")
            if host:
                r = slots_by_id.get((pid, host))
                if r is not None:
                    r["running"].append({**item, "lane": True})
            elif ws is not None:
                ws["running"].append(item)
            if t.get("slot") and t.get("slot") != host:
                r = slots_by_id.get((pid, t["slot"]))
                if r is not None:
                    r["running"].append({**item, "studio_only": True})
        tun = st.get("lane_tunnels") or {}
        for sid, info in (st.get("slots") or {}).items():
            r = slots_by_id.get((pid, sid))
            if r is None:
                continue
            if info.get("disk_pct") is not None:
                r["disk_pct"] = info["disk_pct"]
            if info.get("foundry") and "." in str(info["foundry"]):
                r.update(version=_v(info["foundry"]), version_from="slot upkeep")
        for sid, tv in tun.items():
            r = slots_by_id.get((pid, sid))
            if r is None or not isinstance(tv, dict):
                continue
            r["tunnel"] = {"state": tv.get("state"), "since": tv.get("since"),
                           "error": tv.get("error") or "", "failures": tv.get("failures") or 0}
            if r["lanes"] is not None:
                r["lanes"]["agent"] = tv.get("state") == "up"
    rv = _v(router_version)
    for r in rows:
        if r["version"]:
            r["version_ok"] = r["version"] == rv
    proj = []
    for p in projects:
        s = psets.get(p) or {}
        slots = s.get("slots") if isinstance(s.get("slots"), dict) else {}
        st = (coord.get(p) or ({}, {}))[0]
        active = next((x["name"] for x in rows if p in x["active_for"]), "")
        proj.append({"id": p, "client_kind": str((s.get("client") or {}).get("kind") or "kilo"),
                     "remote_lanes": (s.get("automation") or {}).get("remote_lanes") is True,
                     "slots": {sid: {"lanes": (v or {}).get("lanes") is True,
                                     "enabled": (v or {}).get("enabled") is not False,
                                     "os": str((v or {}).get("os") or "linux")}
                               for sid, v in sorted(slots.items()) if isinstance(v, dict)},
                     "workstation": active, "reported": bool(st),
                     "coordinator_age_s": round(now - ((coord.get(p) or ({}, {}))[1]
                                                       .get("last_seen") or now))
                     if p in coord else None})
    out_rows = [dict(r) for r in sorted(rows, key=lambda x: (x["kind"] != "workstation",
                                                            x["name"]))]
    return {"router_version": rv, "now": now, "projects": proj, "machines": out_rows,
            "warnings": _warnings(rows, proj, rv),
            "links": {"machines": MACHINES_LINK, "tracks": TRACKS_LINK,
                      "settings": SETTINGS_LINK}}


def _warnings(rows: list, projects: list, rv: str) -> list[dict]:
    """What does not fit together, in plain words, each with the machine / project it is about:
    a project on OpenCode whose workstation runs no OpenCode server; remote lanes on with no
    slot that has lanes on; a slot with lane keys but its `lanes` setting off, or the reverse;
    a machine on another Foundry version than this router."""
    out = []
    by_name = {r["name"]: r for r in rows}
    for p in projects:
        if p["client_kind"] == "opencode" and p["workstation"]:
            ws = by_name.get(p["workstation"]) or {}
            oc = (ws.get("components") or {}).get("opencode")
            if oc is not None and oc.get("enabled") is False:
                out.append({"kind": "opencode_off", "project": p["id"], "machine": ws["name"],
                            "text": f"{p['id']} uses OpenCode, but the OpenCode server is off on "
                                    f"its workstation {ws['name']} (Foundry Client → Settings → "
                                    f"OpenCode server)."})
        # (a slot with lanes on that is still switched off, its installer not done, shows that
        # in its own row)
        if p["remote_lanes"] and not any(s["lanes"] for s in p["slots"].values()):
            out.append({"kind": "remote_lanes_no_slot", "project": p["id"],
                        "text": f"{p['id']} has remote_lanes on, but none of its slots has lanes "
                                f"on, so every new track still runs on the workstation."})
    for r in rows:
        ln = r.get("lanes")
        if r["kind"] == "slot" and ln is not None:
            if ln["keys"] and not ln["setting"]:
                out.append({"kind": "lane_keys_setting_off", "machine": r["name"],
                            "project": (r["projects"] or [""])[0],
                            "text": f"{r['name']} has its lane keys, but its slots.{r['slot']}"
                                    f".lanes setting is off: it holds no lanes yet."})
            elif ln["setting"] and not ln["keys"]:
                out.append({"kind": "lane_setting_no_keys", "machine": r["name"],
                            "project": (r["projects"] or [""])[0],
                            "text": f"{r['name']} has slots.{r['slot']}.lanes on, but no lane "
                                    f"keys (Access → Machines → Lanes): its Kilo and OpenCode "
                                    f"cannot call the router."})
            elif ln["setting"] and ln["remote_lanes"] and ln["agent"] is False:
                out.append({"kind": "lane_agent_down", "machine": r["name"],
                            "project": (r["projects"] or [""])[0],
                            "text": f"{r['name']} should hold lanes, but its lane agent does not "
                                    f"answer (tunnel {((r.get('tunnel') or {}).get('state'))})."})
        if r.get("version_ok") is False:
            out.append({"kind": "version_mismatch", "machine": r["name"],
                        "text": f"{r['name']} runs Foundry v{r['version']} ({r['version_from']}), "
                                f"this router v{rv}."})
    return out


@router.get(A)
async def fleet(request: Request):
    return JSONResponse(build(request.app.state.services), headers={"Cache-Control": "no-store"})


__all__ = ["router", "build"]

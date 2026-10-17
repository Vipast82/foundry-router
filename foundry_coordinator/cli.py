"""Command line for the coordinator.

  python -m foundry_coordinator [--state DIR] [--profiles PATH ...] <command>

State lives in --state (default: $FOUNDRY_COORD_STATE or ~/.foundry-coordinator).
Profiles: --profiles (files or folders of <project>/project.toml), or
$FOUNDRY_COORD_PROFILES (os.pathsep-separated).

Commands
  doctor [PROJECT]                         read-only checks: profile, task source, Kilo,
                                           Foundry personas, git; changes nothing
  queue show [PROJECT]
  queue approve PROJECT TASK [--deps A,B] --by NAME [--note TEXT]
  status [--json]
  tick                                     one engine step for every task
  run [--poll S] [--max-ticks N] [--until-settled] [--control] [--telemetry]
                                           --control: apply control-center actions (pause,
                                           answer, approve, instructions...) from Foundry
                                           inside this engine loop, and heartbeat to Foundry
                                           --telemetry: also run the read-only bridge below
  bridge [--poll S] [--replay N] [--once] [--no-events]
                                           read-only telemetry bridge for the control center:
                                           Kilo + this store -> Foundry. Safe to run next to a
                                           live `run`; changes nothing, makes no model calls
  pause PROJECT TASK --reason TEXT         stop a task (its session is aborted)
  resume PROJECT TASK
  cancel PROJECT TASK --reason TEXT
  pause-project PROJECT [--note]  /  resume-project PROJECT
  unblock PROJECT TASK --resolution TEXT [--reset-attempts]
  approve PROJECT TASK --kind KIND --by NAME [--note]     a human approval
  reject PROJECT TASK --by NAME --finding TEXT [...]      send back with findings
  answer PROJECT TASK TEXT...              answer the coding session's question
  history PROJECT TASK  /  events [PROJECT]  /  reviews PROJECT TASK
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import states as S
from .adapters import build_project
from .engine import Coordinator
from .profile import ProfileError, find_profiles
from .store import Store


# Single-model aliases from Foundry's animaluprising-kilo profile v1 (same list
# as foundry_router/kilo_roles.py; the coordinator stays independent of Foundry).
_LEGACY_ALIASES = ("Foundry-Kilo-Haiku", "Foundry-Kilo-Sonnet", "Foundry-Kilo-Opus",
                   "Foundry-Kilo-Luna", "Foundry-Kilo-Sol", "Foundry-Kilo-Astra")


def _state_dir(a) -> str:
    return os.path.abspath(os.path.expanduser(
        a.state or os.environ.get("FOUNDRY_COORD_STATE") or "~/.foundry-coordinator"))


def _profile_paths(a) -> list[str]:
    if a.profiles:
        return a.profiles
    env = os.environ.get("FOUNDRY_COORD_PROFILES") or ""
    return [p for p in env.split(os.pathsep) if p]


def build(a, clients=None, foundry=None) -> Coordinator:
    sd = _state_dir(a)
    profiles = find_profiles(_profile_paths(a))
    store = Store(os.path.join(sd, "coordinator.sqlite"))
    projects = [build_project(p, sd, client=(clients or {}).get(p.id),
                              foundry=(foundry or {}).get(p.id)) for p in profiles]
    return Coordinator(store, projects, sd, max_writers=int(a.max_writers or 1),
                       log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))


def _fmt_task(t: dict) -> str:
    extra = t.get("blocker") or t.get("awaiting") or t.get("pause_reason") or t.get("next_action") or ""
    sha = (t.get("head_sha") or "")[:10]
    return (f"  {t['task_id']:<14} {t['state']:<17} attempt {t['attempt']}  "
            f"{('@' + sha) if sha else '':<12} {extra[:110]}")


def _bridge(c: Coordinator, a):
    """The read-only telemetry bridge for this store's projects. Foundry
    credentials: [telemetry] key_env in a profile (a telemetry-scoped key), else
    the profile's [foundry] key (orchestration scope is accepted for telemetry)."""
    from .adapters.foundry import FoundryClient
    from .telemetry import TelemetryBridge
    P0 = next((P for P in c.projects.values() if P.profile.foundry.get("url")), None)
    if P0 is None:
        print("error: the bridge needs a [foundry] url in a profile", file=sys.stderr)
        return None
    tel = (getattr(P0.profile, "raw", None) or {}).get("telemetry")
    key = ""
    if isinstance(tel, dict) and tel.get("key_env"):
        key = os.environ.get(tel["key_env"], "")
    key = key or P0.profile.secret(P0.profile.foundry, "key")
    f = FoundryClient(P0.profile.foundry["url"], key=key, timeout=30)
    return TelemetryBridge(list(c.projects.values()), c.store.path, f,
                           poll_s=float(getattr(a, "poll", 2) or 2),
                           replay_sessions=int(getattr(a, "replay", 40) or 40),
                           use_events=not getattr(a, "no_events", False), log=c.log)


def doctor(c: Coordinator, project: str = "") -> int:
    bad = 0
    for pid, P in sorted(c.projects.items()):
        if project and pid != project:
            continue
        print(f"== {pid} ({P.profile.name}) profile v{P.profile.version} {P.profile.hash}")
        rows = [("PASS", "profile", P.profile.path)]
        rows += [("PASS" if os.path.isdir(P.profile.workspace) else "FAIL", "workspace",
                  P.profile.workspace)]
        try:
            rows.append(("PASS", "git", f"HEAD {P.ws.head()[:12]} on {P.ws.branch()}, "
                                        f"{len(P.ws.status())} uncommitted path(s)"))
        except Exception as e:                                    # noqa: BLE001
            rows.append(("FAIL", "git", str(e)))
        rows += P.adapter.doctor()
        try:
            rows += P.client.preflight(P.profile.workspace)
        except Exception as e:                                    # noqa: BLE001
            rows.append(("FAIL", "client", str(e)))
        if P.foundry is None:
            rows.append(("WARN", "foundry", "no [foundry] url: no reviews, bindings or budgets"))
        else:
            try:
                models, _ = P.foundry._req("GET", "/v1/models", timeout=20)
                ids = {m.get("id"): m for m in models.get("data") or []}
                worker_ids = ids
                if callable(getattr(P.client, 'inference_models', None)):
                    try:
                        worker_ids = P.client.inference_models(P.profile.workspace)
                    except Exception as e:
                        worker_ids = {}
                        rows.append(('FAIL', 'worker inference', str(e)))
                needed = {"implementer"} | ({"reviewer"} if P.profile.review.get("required", True)
                                            else set())
                for role, persona in sorted(P.profile.roles.items()):
                    direct_review = role in ('reviewer', 'deep_reviewer')
                    m = (ids if direct_review else worker_ids).get(persona)
                    source = 'coordinator key' if direct_review else 'worker inference key'
                    if not m:
                        hint = ""
                        if direct_review and worker_ids is not ids and persona in worker_ids:
                            # it exists and is enabled (Kilo's key sees it): the
                            # coordinator client's allowlist is what's missing
                            legacy = sorted(x for x in ids if x in _LEGACY_ALIASES)
                            hint = (f"; Kilo's key can use it, so add {persona} to the coordinator "
                                    f"client's allowed personas (Foundry → Setup → Clients & keys)"
                                    + (f" — {', '.join(legacy)} {'is' if len(legacy) == 1 else 'are'} "
                                       f"allowed but legacy aliases don't authorize {persona}"
                                       if legacy else ""))
                        rows.append(("FAIL" if role in needed else "WARN", f"persona {role}",
                                     f"{persona} not offered to {source} (missing, disabled or not allowed)"
                                     + hint
                                     + ("" if role in needed else "; only needed when used")))
                    else:
                        fm = m.get("foundry") or {}
                        val = fm.get("validation") or "not validated"
                        val = val.get("status") if isinstance(val, dict) else val
                        rows.append(("PASS" if val == "pass" else "WARN", f"persona {role}",
                                     f"{persona} -> {fm.get('backend') or '?'} / "
                                     f"{fm.get('served_model') or '?'} "
                                     f"(routing {fm.get('routing') or '?'}, {val}; {source})"))
            except Exception as e:                                # noqa: BLE001
                rows.append(("FAIL", "foundry", str(e)))
        for st, name, detail in rows:
            bad += st == "FAIL"
            print(f"  {st:<4} {name:<22} {detail}")
    return 1 if bad else 0


def main(argv=None, clients=None, foundry=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry_coordinator", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--profiles", action="append")
    ap.add_argument("--max-writers", type=int, default=1)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor"); d.add_argument("project", nargs="?", default="")
    q = sub.add_parser("queue"); qs = q.add_subparsers(dest="qcmd", required=True)
    q1 = qs.add_parser("show"); q1.add_argument("project", nargs="?", default="")
    q2 = qs.add_parser("approve"); q2.add_argument("project"); q2.add_argument("task")
    q2.add_argument("--deps", default=""); q2.add_argument("--by", required=True)
    q2.add_argument("--note", default="")
    q2.add_argument("--adopt-existing", action="append", default=[], metavar="PATH",
                    help="explicitly adopt this dirty task-scope file, with verified external backup; repeat per file")
    st = sub.add_parser("status"); st.add_argument("--json", action="store_true")
    sub.add_parser("tick")
    r = sub.add_parser("run"); r.add_argument("--poll", type=float, default=5)
    r.add_argument("--max-ticks", type=int); r.add_argument("--until-settled", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("--control", action="store_true",
                   help="apply control-center operator actions from Foundry in this loop")
    r.add_argument("--telemetry", action="store_true",
                   help="also run the read-only telemetry bridge in this process")
    b = sub.add_parser("bridge")
    b.add_argument("--poll", type=float, default=2)
    b.add_argument("--replay", type=int, default=40,
                   help="existing sessions to replay from Kilo at start (most recent first)")
    b.add_argument("--once", action="store_true", help="one pass, then exit")
    b.add_argument("--no-events", action="store_true", help="poll only; don't use Kilo's /event")
    for name in ("pause", "cancel"):
        x = sub.add_parser(name); x.add_argument("project"); x.add_argument("task")
        x.add_argument("--reason", required=True)
    x = sub.add_parser("resume"); x.add_argument("project"); x.add_argument("task")
    x = sub.add_parser("pause-project"); x.add_argument("project"); x.add_argument("--note", default="")
    x = sub.add_parser("resume-project"); x.add_argument("project")
    x = sub.add_parser("unblock"); x.add_argument("project"); x.add_argument("task")
    x.add_argument("--resolution", required=True); x.add_argument("--reset-attempts", action="store_true")
    x = sub.add_parser("approve"); x.add_argument("project"); x.add_argument("task")
    x.add_argument("--kind", required=True); x.add_argument("--by", required=True)
    x.add_argument("--note", default="")
    x = sub.add_parser("reject"); x.add_argument("project"); x.add_argument("task")
    x.add_argument("--by", required=True); x.add_argument("--finding", action="append", required=True)
    x = sub.add_parser("answer"); x.add_argument("project"); x.add_argument("task")
    x.add_argument("text", nargs="+")
    for name in ("history", "reviews"):
        x = sub.add_parser(name); x.add_argument("project"); x.add_argument("task")
    x = sub.add_parser("events"); x.add_argument("project", nargs="?", default="")
    a = ap.parse_args(argv)
    try:
        c = build(a, clients=clients, foundry=foundry)
    except (ProfileError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        return _dispatch(a, c)
    except (ValueError, S.TransitionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _dispatch(a, c: Coordinator) -> int:
    s = c.store
    if a.cmd == "doctor":
        return doctor(c, a.project)
    if a.cmd == "queue":
        if a.qcmd == "approve":
            deps = [d.strip() for d in a.deps.split(",") if d.strip()]
            e = c.approve(a.project, a.task, deps, a.by, a.note, a.adopt_existing)
            print(f"approved {a.project}/{a.task} (position {e['position']}, deps {e['deps']})")
            return 0
        for p in s.projects():
            if a.project and p["project_id"] != a.project:
                continue
            print(f"{p['project_id']}:")
            for e in s.queue(p["project_id"]):
                t = s.task(p["project_id"], e["task_id"])
                print(f"  {e['position']:>3}. {e['task_id']:<14} {t['state']:<17} deps "
                      f"{','.join(e['deps']) or '-'}  approved by {e['approved_by']}")
        return 0
    if a.cmd == "status":
        st = c.status()
        if a.json:
            print(json.dumps(st, indent=2, default=str))
            return 0
        for p in st["projects"]:
            print(f"{p['project_id']}{' (PAUSED)' if p['paused'] else ''}:")
            for t in p["tasks"]:
                print(_fmt_task(t))
        if st["locks"]:
            print("locks: " + ", ".join(f"{l['resource']}={l['project_id']}/{l['task_id']}"
                                        for l in st["locks"]))
        return 0
    if a.cmd == "tick":
        for line in c.tick():
            print(line)
        return 0
    if a.cmd == "run":
        me = s.claim_owner(force=a.force)
        bridge = None
        if a.control:
            from .control import ControlChannel
            ch = ControlChannel(c, log=c.log)
            if ch.foundry is None:
                print("error: --control needs a [foundry] url and key in a profile", file=sys.stderr)
                s.release_owner(me)
                return 2
            c.after_tick.append(ch.poll)
        if a.telemetry:
            import threading
            bridge = _bridge(c, a)
            if bridge is None:
                s.release_owner(me)
                return 2
            threading.Thread(target=bridge.run, daemon=True, name="telemetry-bridge").start()
        try:
            c.run(poll_seconds=a.poll, max_ticks=a.max_ticks, until_settled=a.until_settled,
                  owner=me)
        except KeyboardInterrupt:
            print("stopped (state is saved; run again to resume)")
        finally:
            if bridge is not None:
                bridge.stop()
            s.release_owner(me)
        return 0
    if a.cmd == "bridge":
        br = _bridge(c, a)
        if br is None:
            return 2
        if a.once:
            print(json.dumps(br.step()))
            return 0
        print(f"telemetry bridge {br.source_id}: {', '.join(sorted(br.projects))} -> "
              f"{br.foundry.url} (read-only; Ctrl+C stops it)", flush=True)
        try:
            br.run()
        except KeyboardInterrupt:
            br.stop()
        return 0
    if a.cmd == "pause":
        c.pause(a.project, a.task, a.reason)
    elif a.cmd == "resume":
        c.resume(a.project, a.task)
    elif a.cmd == "cancel":
        c.cancel(a.project, a.task, a.reason)
    elif a.cmd == "pause-project":
        s.set_project_paused(a.project, True, a.note)
    elif a.cmd == "resume-project":
        s.set_project_paused(a.project, False)
    elif a.cmd == "unblock":
        c.unblock(a.project, a.task, a.resolution, reset_attempts=a.reset_attempts)
    elif a.cmd == "approve":
        print(c.approve_human(a.project, a.task, a.kind, a.by, a.note))
    elif a.cmd == "reject":
        c.reject_human(a.project, a.task, a.finding, a.by)
    elif a.cmd == "answer":
        c.answer(a.project, a.task, [[" ".join(a.text)]])
    elif a.cmd == "history":
        for h in s.history(a.project, a.task):
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(h['ts']))} "
                  f"{h['from_state'] or '-':>16} -> {h['to_state']:<16} [{h['actor']}] "
                  f"{json.dumps(h['evidence'], default=str)[:300]}")
        return 0
    elif a.cmd == "reviews":
        for r in s.reviews(a.project, a.task):
            print(json.dumps({k: r[k] for k in ("review_id", "reviewed_sha", "role", "persona",
                                                "verdict", "valid", "error")}, default=str))
            if r["valid"]:
                print(json.dumps(r["result"], indent=2)[:4000])
        return 0
    elif a.cmd == "events":
        for e in reversed(s.events(a.project or None, 60)):
            print(f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['project_id']}/"
                  f"{e['task_id'] or '-'} {e['kind']} {e['state'] or ''} "
                  f"{json.dumps(e['detail'], default=str)[:200]}")
        return 0
    t = s.task(a.project, a.task) if hasattr(a, "task") else None
    if t:
        print(_fmt_task(t))
    return 0

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


class _StopRequested(BaseException):
    """Raised from the after-tick hook so `Coordinator.run` (which only catches
    Exception in hooks) unwinds cleanly at a tick boundary."""


class _ProcessFiles:
    """Process files the upgrade supervisor (supervisor.py) reads: a pid file
    (supervisor.write_pid_file when available: pid + creation time + exe), a
    heartbeat after every tick ({pid, ts, ok, lead_ready, version}; ok is
    False when the tick logged errors) and a stop-request file it creates to
    ask for a graceful exit. A stop is honored only between ticks, so no tick
    is cut short, and only when it is a real supervisor request: JSON naming
    a known upgrade id, or naming this process / an upgrade id while a
    supervisor is configured ([lead_manager] supervisor = "supervisor").
    Garbage and stale (> STALE_S) stop files are removed and ignored."""

    STALE_S = 600

    def __init__(self, state_dir: str, coordinator):
        from . import supervisor as SV
        self.SV = SV
        self.c = coordinator
        self.pid_path = os.path.join(state_dir, SV.PID_FILE)
        self.hb_path = os.path.join(state_dir, SV.HEARTBEAT_FILE)
        self.stop_path = os.path.join(state_dir, SV.STOP_FILE)
        self.ignored: list[str] = []
        self._last_ts = time.time()
        os.makedirs(state_dir, exist_ok=True)
        self._drop_stale()
        wp = getattr(SV, "write_pid_file", None)
        if callable(wp):
            try:
                wp(self.pid_path)
            except Exception:                                    # noqa: BLE001
                self._write(self.pid_path, str(os.getpid()))
        else:
            self._write(self.pid_path, str(os.getpid()))

    def _write(self, path: str, text: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def _request_live(self, req: dict) -> bool:
        """A JSON stop request {upgrade_id, supervisor_pid, pid, ts}: its
        supervisor is alive and it names this process (when it names one)."""
        spid = req.get("supervisor_pid")
        if spid not in (None, ""):
            alive = getattr(self.SV, "pid_alive", None)
            try:
                if callable(alive) and not alive(int(spid)):
                    return False
            except (TypeError, ValueError):
                return False
        pid = req.get("pid")
        if pid not in (None, "") and str(pid) != str(os.getpid()):
            return False
        return True

    def _drop_stale(self) -> None:
        try:
            if not os.path.exists(self.stop_path):
                return
            if os.path.getmtime(self.stop_path) < time.time() - self.STALE_S:
                os.remove(self.stop_path)
                self.ignored.append("stale")
                return
            with open(self.stop_path, encoding="utf-8") as f:
                raw = f.read(4096)
            try:
                req = json.loads(raw)
            except ValueError:
                return
            if isinstance(req, dict) and not self._request_live(req):
                os.remove(self.stop_path)
                self.ignored.append("dead supervisor")
        except OSError:
            pass

    def _supervisor_configured(self) -> bool:
        lead = getattr(self.c, "lead", None)
        if lead is None:
            return False
        for P in (getattr(self.c, "projects", None) or {}).values():
            try:
                if lead.enabled(P) and lead.settings(P).get("supervisor") == "supervisor":
                    return True
            except Exception:                                    # noqa: BLE001
                continue
        return False

    def _known_upgrade(self, uid: str) -> bool:
        store = getattr(self.c, "store", None)
        if not uid or store is None:
            return False
        try:
            return bool(self.SV.get_request(store, uid))
        except Exception:                                        # noqa: BLE001
            return False

    def _stop_requested(self) -> bool:
        """Read (and consume) the stop file: True only for a valid request."""
        self._drop_stale()
        if not os.path.exists(self.stop_path):
            return False
        try:
            with open(self.stop_path, encoding="utf-8") as f:
                raw = f.read(4096).strip()
        except OSError:
            return False
        req = None
        try:
            req = json.loads(raw)
        except ValueError:
            req = None
        configured = self._supervisor_configured()
        ok = False
        if isinstance(req, dict) and not self._request_live(req):
            req = "dead"                 # its supervisor is gone or it targets another pid
        if isinstance(req, dict):
            uid = str(req.get("upgrade_id") or "")
            pid = req.get("pid", req.get("coordinator_pid"))
            if uid and self._known_upgrade(uid):
                ok = True
            elif configured and (uid or str(pid) == str(os.getpid())
                                 or req.get("supervisor_pid")):
                ok = True
        elif configured and raw == str(os.getpid()):
            ok = True                    # an older supervisor writes the bare target pid
        try:
            os.remove(self.stop_path)
        except OSError:
            pass
        if not ok:
            self.ignored.append(raw[:80] or "(empty)")
        return ok

    def _tick_errors(self) -> int:
        """Error events the engine logged since the last heartbeat."""
        store = getattr(self.c, "store", None)
        if store is None:
            return 0
        try:
            r = store.q("SELECT COUNT(*) AS n FROM events WHERE kind='error' AND ts>?",
                        (self._last_ts,))
            return int(r[0]["n"] or 0)
        except Exception:                                        # noqa: BLE001
            return 0

    def tick(self) -> None:
        lead_ready, detail = None, ""
        ok, problems = True, []
        lead = getattr(self.c, "lead", None)
        if lead is not None:
            try:
                on = [P for P in self.c.projects.values() if lead.enabled(P)]
                if on:
                    res = [r for P in on for r in lead.readiness(P)]
                    bad = [r for r in res if r.get("blocking") and r.get("ok") is False]
                    lead_ready = not bad
                    detail = "; ".join(f"{r.get('name')}: {r.get('detail')}" for r in bad)[:500]
            except Exception as e:                            # noqa: BLE001
                lead_ready, detail = None, f"readiness error: {type(e).__name__}"
        errs = self._tick_errors()
        if errs:
            ok = False
            problems.append(f"{errs} engine error event(s) since the last heartbeat")
        self._last_ts = time.time()
        from . import __version__
        try:
            from foundry_router import __version__ as release
        except Exception:                                        # noqa: BLE001
            release = ""
        self._write(self.hb_path, json.dumps({
            "pid": os.getpid(), "ts": time.time(), "ok": ok, "errors": problems,
            "release": release, "version": __version__, "lead_ready": lead_ready,
            "lead_detail": detail}))
        if self._stop_requested():
            raise _StopRequested()

    def close(self) -> None:
        for p in (self.pid_path,):
            try:
                if not os.path.exists(p):
                    continue
                with open(p, encoding="utf-8") as f:
                    raw = f.read().strip()
                mine = raw == str(os.getpid())
                rp = getattr(self.SV, "read_pid_file", None)
                if not mine and callable(rp):
                    try:
                        v = rp(p)
                        v = v.get("pid") if isinstance(v, dict) else \
                            getattr(v, "pid", v[0] if isinstance(v, (tuple, list)) else v)
                        mine = int(v or 0) == os.getpid()
                    except Exception:                            # noqa: BLE001
                        mine = False
                if not mine:
                    try:
                        mine = int(json.loads(raw).get("pid")) == os.getpid()
                    except (ValueError, TypeError, AttributeError):
                        mine = False
                if mine:
                    os.remove(p)
            except OSError:
                pass


def _state_dir(a) -> str:
    return os.path.abspath(os.path.expanduser(
        a.state or os.environ.get("FOUNDRY_COORD_STATE") or "~/.foundry-coordinator"))


def _profile_paths(a) -> list[str]:
    if a.profiles:
        return a.profiles
    env = os.environ.get("FOUNDRY_COORD_PROFILES") or ""
    return [p for p in env.split(os.pathsep) if p]


def load_profiles(paths: list[str], state_dir: str, log=lambda m: None) -> list:
    """find_profiles plus the W2.4 startup overlay: each TOML is loaded with the
    last applied router revision (its restart-scoped keys included) on top,
    from <state_dir>/config/<project>.json, BEFORE any object is built."""
    import tomllib
    from .config_pull import startup_overlay
    from .profile import check_isolation, from_raw, load
    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(p, n, "project.toml") for n in sorted(os.listdir(p))
                      if os.path.isfile(os.path.join(p, n, "project.toml"))]
        else:
            files.append(p)
    out = []
    for path in files:
        try:
            with open(path, "rb") as f:
                raw = tomllib.loads(f.read().decode("utf-8"))
            merged, cache = startup_overlay(state_dir, raw)
        except Exception:                                         # noqa: BLE001
            out.append(load(path))                               # profile.load reports it
            continue
        if cache is None:
            out.append(load(path))
            continue
        prof = from_raw(merged, path)
        log(f"{prof.id}: startup overlay applied (router revision "
            f"{cache.get('rev')}, restart-scoped keys included)")
        out.append(prof)
    check_isolation(out)
    return out


def build(a, clients=None, foundry=None) -> Coordinator:
    sd = _state_dir(a)
    profiles = load_profiles(_profile_paths(a), sd,
                             log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    store = Store(os.path.join(sd, "coordinator.sqlite"))
    projects = [build_project(p, sd, client=(clients or {}).get(p.id),
                              foundry=(foundry or {}).get(p.id)) for p in profiles]
    return Coordinator(store, projects, sd, max_writers=int(a.max_writers) if a.max_writers else None,
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


# Foundry has reported validation as "pass" and, live, as "passed"; both (and
# the generic ok/valid) mean the persona validated. Anything else is shown as-is.
_VALIDATION_PASS = ("pass", "passed", "ok", "valid")
# Roles served by Foundry's media adapter (/media/*), not the chat catalog
# (/v1/models intentionally leaves media-only personas out).
_MEDIA_ROLES = ("image_generator",)
_MEDIA_NOT_CHECKED = ("media: not checked by doctor (media personas are served by the media "
                      "adapter, not the chat catalog)")


def _validation_status(val) -> str:
    return "PASS" if isinstance(val, str) and val.strip().lower() in _VALIDATION_PASS else "WARN"


def _media_row(P, persona: str, label: str, needed: bool, cache: dict) -> tuple:
    """Diagnose a media persona through Foundry's read-only media catalog
    (GET /media/models, what the media adapter's `check` reads). Never submits
    a job. Unreachable or unrecognised -> INFO/WARN, never FAIL: a healthy
    setup must not look broken because its media persona isn't a chat model."""
    if "media" not in cache:
        try:
            m, _ = P.foundry._req("GET", "/media/models", timeout=15)
            cache["media"] = (m if isinstance(m, dict) and "enabled" in m else None, "")
        except Exception as e:                                    # noqa: BLE001
            cache["media"] = (None, str(e)[:200])
    m, err = cache["media"]
    if m is None:
        return (("WARN" if needed else "INFO"), label,
                f"{persona}: {_MEDIA_NOT_CHECKED}" + (f"; /media/models: {err}" if err else ""))
    models = ", ".join(str(x.get("id")) for x in m.get("models") or [] if isinstance(x, dict))
    detail = (f"{persona} -> media adapter (backend {m.get('backend') or '?'}, "
              f"models {models or 'none'}; resolved per job, not via the chat catalog)")
    if not m.get("enabled"):
        return ("WARN", label, f"{persona}: Foundry media is disabled"
                + ("" if needed else "; only needed when used"))
    if m.get("backend_error"):
        return ("WARN", label, f"{persona}: media backend error: {str(m['backend_error'])[:200]}")
    return ("PASS", label, detail)


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
                selections = [(role, persona, "") for role, persona in P.profile.roles.items()]
                for name, override in (P.profile.raw.get("role_overrides") or {}).items():
                    selections.extend((role, persona, name) for role, persona in override.items()
                                      if role != "tasks")
                media_cache: dict = {}
                for role, persona, override_name in sorted(selections):
                    label = f"persona {role}" + (f" [{override_name}]" if override_name else "")
                    if role in _MEDIA_ROLES:
                        rows.append(_media_row(P, persona, label, role in needed, media_cache))
                        continue
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
                        rows.append(("FAIL" if role in needed else "WARN", label,
                                     f"{persona} not offered to {source} (missing, disabled or not allowed)"
                                     + hint
                                     + ("" if role in needed else "; only needed when used")))
                    else:
                        fm = m.get("foundry") or {}
                        val = fm.get("validation") or "not validated"
                        val = val.get("status") if isinstance(val, dict) else val
                        rows.append((_validation_status(val), label,
                                     f"{persona} -> {fm.get('backend') or '?'} / "
                                     f"{fm.get('served_model') or '?'} "
                                     f"(routing {fm.get('routing') or '?'}, {val}; {source})"))
            except Exception as e:                                # noqa: BLE001
                rows.append(("FAIL", "foundry", str(e)))
        # [lead_manager]: the activation checks (persona, strict route chain,
        # client grant, Kilo limits/filter/agent); only when the lead is enabled
        lead = getattr(c, "lead", None)
        if lead is not None:
            try:
                if lead.enabled(P):
                    for r in lead.readiness(P, fresh=True):
                        ok = r.get("ok")
                        st = "PASS" if ok is True else ("FAIL" if r.get("blocking") else "WARN")
                        rows.append((st, f"lead {r.get('name')}", str(r.get("detail") or "")[:300]))
            except Exception as e:                                # noqa: BLE001
                rows.append(("FAIL", "lead readiness", f"{type(e).__name__}: {e}"[:300]))
        if P.foundry is not None:
            rows.extend(_fallback_rows(P))
        # W1.7: profile values that contradict the revamp decisions
        try:
            from . import profile_drift
            rows.extend(profile_drift.checks(P))
        except Exception as e:                                    # noqa: BLE001
            rows.append(("WARN", "profile drift", f"{type(e).__name__}: {e}"[:300]))
        for st, name, detail in rows:
            bad += st == "FAIL"
            print(f"  {st:<4} {name:<22} {detail}")
    return 1 if bad else 0


def _provider(model: str) -> str:
    m = (model or "").lower()
    return ("claude" if "claude" in m else "gpt" if m.startswith(("gpt", "o1", "o3", "o4"))
            or "gpt" in m else "local" if any(x in m for x in ("qwen", "llama", "gemma", "local"))
            else m.split("-")[0] or "?")


def _fallback_rows(P) -> list:
    """Every persona the coordinator can call, with its full route chain, WARN when the
    chain has a single frontier provider (2026-10-20: at Claude's 95% cutoff the Sonnet
    implementer, the vision reviewer and retry guidance had no GPT fallback and stalled)."""
    used: dict = {}
    for role, persona in P.profile.roles.items():
        used.setdefault(persona, set()).add(role)
    for _n, ov in (P.profile.raw.get("role_overrides") or {}).items():
        for role, persona in ov.items():
            if role != "tasks":
                used.setdefault(persona, set()).add(role)
    raw = P.profile.raw
    for key, role in (("frontier_primary", "frontier_primary"), ("retry_guidance", "retry_guidance"),
                      ("lead_manager", "lead")):
        persona = (raw.get(key) or {}).get("persona")
        if persona and (raw.get(key) or {}).get("enabled", True):
            used.setdefault(persona, set()).add(role)
    vis = ((raw.get("lanes") or {}).get("visual") or {}).get("persona")
    if vis:
        used.setdefault(vis, set()).add("lanes.visual")
    for backend, persona in ((raw.get("helpers") or {}).get("personas") or {}).items():
        used.setdefault(persona, set()).add(f"helper:{backend}")
    out = []
    for persona in sorted(used):
        roles = ",".join(sorted(used[persona]))
        try:
            info = P.foundry.persona_info(persona) or {}
        except Exception as e:                                    # noqa: BLE001
            out.append(("WARN", f"fallback {persona}"[:22], f"routes unreadable: {e}"[:200]))
            continue
        models = [str(r.get("model") or "?") for r in info.get("routes") or []]
        provs = {_provider(m) for m in models}
        frontier = provs - {"local"}
        chain = " -> ".join(models) or "(no routes)"
        if "helper:" in roles and len(used[persona]) == 1:
            st = "PASS"                       # helpers already span backends via [helpers] order
        elif provs == {"local"}:
            st = "PASS"                       # a local primary: the frontier handoff covers it
        elif len(frontier) >= 2:
            st = "PASS"
        else:
            st = "WARN"
        note = "" if st == "PASS" else "  <- single provider: add a fallback model to this route"
        out.append((st, f"fallback {persona}"[:40], f"[{roles}] {chain}{note}"))
    return out


def main(argv=None, clients=None, foundry=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry_coordinator", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state")
    ap.add_argument("--profiles", action="append")
    ap.add_argument("--max-writers", type=int, default=None,
                    help="override the live [automation] max_agents setting")
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
    x = sub.add_parser("decision", help="lifecycle decisions")
    xs = x.add_subparsers(dest="dcmd", required=True)
    x1 = xs.add_parser("reevaluate", help="re-run the no-progress check of an open no_progress "
                                          "decision; withdraws it (kept, with an event) only if "
                                          "progress is now recognized")
    x1.add_argument("decision_id"); x1.add_argument("--by", required=True)
    x1.add_argument("--reason", required=True); x1.add_argument("--dry-run", action="store_true")
    for name in ("history", "reviews"):
        x = sub.add_parser(name); x.add_argument("project"); x.add_argument("task")
    x = sub.add_parser("events"); x.add_argument("project", nargs="?", default="")
    x = sub.add_parser("frontier", help="optional frontier-primary phase (default off)")
    x.add_argument("action", choices=["request", "return", "status"])
    x.add_argument("project"); x.add_argument("task")
    x.add_argument("--reason", default=""); x.add_argument("--by", default="operator")
    x = sub.add_parser("kilo-config", help="regenerate kilo.jsonc agent permission blocks from "
                                           "the role rules (dry run unless --apply)")
    x.add_argument("project"); x.add_argument("--apply", action="store_true")
    x.add_argument("--path", default="", help="kilo.jsonc (default <workspace>/kilo.jsonc)")
    x = sub.add_parser("config-pull", help="pull and apply the router's deployed settings now")
    x.add_argument("project", nargs="?", default="")
    x = sub.add_parser("tracks", help="parallel tracks (Team Lead): analyze, propose, start, "
                                      "stop, merge, status, set-max-agents")
    xs = x.add_subparsers(dest="tcmd", required=True)
    x1 = xs.add_parser("analyze", help="which sprints can run side by side, and why not")
    x1.add_argument("project"); x1.add_argument("--json", action="store_true")
    x1 = xs.add_parser("propose", help="a track plan for an agent count (nothing starts)")
    x1.add_argument("project"); x1.add_argument("--max-agents", type=int, default=0)
    x1.add_argument("--json", action="store_true")
    x1 = xs.add_parser("start", help="create a track (worktree + branch) and queue its sprints")
    x1.add_argument("project"); x1.add_argument("name"); x1.add_argument("sprints", nargs="*")
    x1.add_argument("--plan", default="", help="start every track of a proposal instead")
    x1.add_argument("--by", default="operator")
    x1 = xs.add_parser("stop"); x1.add_argument("project"); x1.add_argument("name")
    x1.add_argument("--reason", default=""); x1.add_argument("--by", default="operator")
    x1 = xs.add_parser("merge"); x1.add_argument("project"); x1.add_argument("name")
    x1.add_argument("--by", default="operator")
    x1 = xs.add_parser("status"); x1.add_argument("project"); x1.add_argument("--json", action="store_true")
    x1 = xs.add_parser("set-max-agents"); x1.add_argument("project"); x1.add_argument("n", type=int)
    x1.add_argument("--by", default="operator")
    m = sub.add_parser("memory", help="project memory: docs/memory in the game repo, indexed "
                                      "here and sliced per session")
    ms = m.add_subparsers(dest="mcmd", required=True)
    x = ms.add_parser("init", help="create docs/memory (idempotent) and commit the new files")
    x.add_argument("project"); x.add_argument("--by", default="operator")
    x = ms.add_parser("import-state", help="one-time: fold docs/progress/STATE.md, state/DECISIONS.md, "
                                           "state/KNOWN_ISSUES.md and the operator notes into "
                                           "docs/memory, as one coordinator commit")
    x.add_argument("project"); x.add_argument("--by", default="operator")
    x.add_argument("--force", action="store_true", help="import again (appends)")
    x = ms.add_parser("reindex", help="re-parse every memory file into the index")
    x.add_argument("project")
    x = ms.add_parser("status", help="files, entry counts, settings")
    x.add_argument("project")
    x = ms.add_parser("search", help="full-text search of the index")
    x.add_argument("project"); x.add_argument("query", nargs="+")
    x = ms.add_parser("slice", help="the slice a sprint's session would get")
    x.add_argument("project"); x.add_argument("task")
    x.add_argument("--track", default=""); x.add_argument("--budget", type=int, default=0)
    x.add_argument("--role", default="implementer")
    x = ms.add_parser("add", help="append an entry under a heading (a coordinator commit)")
    x.add_argument("project"); x.add_argument("file"); x.add_argument("heading")
    x.add_argument("text", nargs="+"); x.add_argument("--by", default="operator")
    x.add_argument("--task", default="")
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


def _kilo_config(c: Coordinator, a) -> int:
    """Regenerate kilo.jsonc agent permission blocks: the role rules of the
    router revision this coordinator last applied (its cache), else the
    profile's own [role_rules]."""
    from .config_pull import ConfigPuller
    from .kilo_config import KiloConfigError, regenerate
    P = c.projects.get(a.project)
    if P is None:
        print(f"error: unknown project {a.project!r}", file=sys.stderr)
        return 2
    cached = ConfigPuller(c).cached(P.id)
    rules = ((cached or {}).get("doc") or {}).get("role_rules")
    try:
        out = regenerate(P.profile, rules, apply=a.apply, path=a.path, store=c.store)
    except KiloConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


def _memory_cmd(c: Coordinator, a) -> int:
    P = c.projects.get(a.project)
    if P is None:
        print(f"error: unknown project {a.project!r}", file=sys.stderr)
        return 2
    mem = c.memory
    if a.mcmd == "init":
        out = mem.init(P, a.by)
    elif a.mcmd == "import-state":
        out = mem.import_state(P, a.by, force=a.force)
    elif a.mcmd == "reindex":
        out = mem.reindex(P, force=True)
    elif a.mcmd == "status":
        out = mem.status(P)
    elif a.mcmd == "search":
        mem.reindex(P)
        out = [{k: e[k] for k in ("file", "heading", "text")} for e in
               mem.search(P, " ".join(a.query), 20)]
    elif a.mcmd == "slice":
        mem.reindex(P)
        sl = mem.slice(P, P.adapter.describe(a.task), a.track, a.budget or None, role=a.role)
        print(sl["text"])
        out = {"entries": len(sl["entries"]), "tokens": sl["tokens"], "budget": sl["budget"],
               "dropped": sl["dropped"], "query": sl["query"],
               "why": [f"{e['file']} > {e['heading']}: {e['why']}" for e in sl["entries"]]}
    else:
        out = mem.add(P, a.file, a.heading, " ".join(a.text), a.by, task_id=a.task)
    print(json.dumps(out, indent=2, default=str))
    return 0 if not (isinstance(out, dict) and (out.get("error") or out.get("ok") is False)) else 1


def _tracks(c: Coordinator, a) -> int:
    """`tracks analyze PROJECT` prints the ordered tracks (deterministic, no model
    call); the other subcommands drive tracks/manager.py like the chat does."""
    from .tracks import analysis as AN
    from .tracks.manager import TrackError
    P = c.projects.get(a.project)
    if P is None:
        print(f"error: unknown project {a.project!r}", file=sys.stderr)
        return 2
    tm = c.tracks
    try:
        if a.tcmd == "analyze":
            out = tm.analyze(P)
            print(json.dumps(out, indent=2, default=str) if a.json else AN.render(out))
            if not a.json:
                print(f"max_agents {tm.max_agents()}")
        elif a.tcmd == "propose":
            plan = tm.propose(P, a.max_agents or None, actor="cli")
            if a.json:
                print(json.dumps(plan, indent=2, default=str))
            else:
                print(f"proposal {plan['plan_id']}: {plan['agents']} agent(s) of max_agents "
                      f"{plan['max_agents']}")
                if plan["main"]:
                    print(f"  main   {' -> '.join(plan['main']['sprints'])}")
                for t in plan["tracks"]:
                    print(f"  track  {t['name']:<14} {' -> '.join(t['sprints'])}  needs "
                          f"{', '.join(t['needs'])}")
                for n in plan["notes"]:
                    print(f"  note: {n}")
                print(f"start with: tracks start {P.id} --plan {plan['plan_id']}")
        elif a.tcmd == "start":
            if a.plan:
                rows = tm.start_plan(P, a.plan, a.by)
            else:
                tm.create(P, a.name, a.sprints, None, a.by)
                rows = [tm.start(P, a.name, a.by)]
            for r in rows:
                print(f"track {r['name']} {r['state']}: {r['branch']} at {r['worktree']}; "
                      f"sprints {', '.join(r['sprints'])}")
        elif a.tcmd == "stop":
            r = tm.stop(P, a.name, a.by, a.reason)
            print(f"track {r['name']} {r['state']}")
        elif a.tcmd == "merge":
            r = tm.merger.request(P, a.name, a.by)
            print(f"track {r['name']} {r['state']} (merges at the main sprint's next quiet point)")
        elif a.tcmd == "status":
            st = tm.status(P)
            if a.json:
                print(json.dumps(st, indent=2, default=str))
            else:
                print(f"max_agents {st['max_agents']}, running {st['agents_running']}; main "
                      f"{st['main']['sprint'] or '-'} "
                      f"{'quiescent' if st['main']['quiescent'] else st['main']['why']}")
                for t in st["tracks"]:
                    print(f"  {t['name']:<14} {t['state']:<8} {t['current_sprint'] or '-':<10} "
                          f"needs {', '.join(t['needs']) or '-'}  "
                          + ", ".join(f"{s['task_id']} {s['state']}" for s in t["sprints"]))
                sv = st["studio"]
                print(f"studio: {sv['in_use'] or 'idle'}; lease "
                      f"{(sv['lease'] or {}).get('track') or '-'}")
        elif a.tcmd == "set-max-agents":
            print(f"max_agents {tm.set_max_agents(P, a.n, a.by)}")
    except TrackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _dispatch(a, c: Coordinator) -> int:
    s = c.store
    if a.cmd == "doctor":
        return doctor(c, a.project)
    if a.cmd == "tracks":
        return _tracks(c, a)
    if a.cmd == "memory":
        return _memory_cmd(c, a)
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
        # router-deployed settings (C1 config store): pulled each tick, applied at
        # a safe point; off per project with [config_pull] enabled = false
        from .config_pull import ConfigPuller
        puller = ConfigPuller(c, log=c.log)
        if any(puller.enabled(pid) for pid in c.projects):
            c.after_tick.append(puller.poll)
        if a.telemetry:
            import threading
            bridge = _bridge(c, a)
            if bridge is None:
                s.release_owner(me)
                return 2
            threading.Thread(target=bridge.run, daemon=True, name="telemetry-bridge").start()
        life = None
        try:
            # inside the try: the owner record is released even if the process
            # files cannot be written
            life = _ProcessFiles(_state_dir(a), c)
            c.after_tick.append(life.tick)
            c.run(poll_seconds=a.poll, max_ticks=a.max_ticks, until_settled=a.until_settled,
                  owner=me)
        except KeyboardInterrupt:
            print("stopped (state is saved; run again to resume)")
        except _StopRequested:
            print("stopped at a tick boundary on the supervisor's stop request "
                  "(state is saved)")
        finally:
            if bridge is not None:
                bridge.stop()
            s.release_owner(me)
            if life is not None:
                life.close()
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
    elif a.cmd == "decision":
        out = c.decision_reevaluate(a.decision_id, a.by, a.reason, dry_run=a.dry_run)
        print(json.dumps(out, indent=1, default=str)[:6000])
        return 0
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
    elif a.cmd == "frontier":
        if a.action == "request":
            if not a.reason:
                print("error: --reason is required (why local reasoning is the bottleneck)",
                      file=sys.stderr)
                return 2
            try:
                ph = c.frontier.request(a.project, a.task, a.by, a.reason)
            except ValueError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 2
            print(f"requested {ph['phase_id']}: starts at the primary's next idle safe point")
        elif a.action == "return":
            ph = c.frontier.request_return(a.project, a.task, a.by)
            print("no live phase" if ph is None else f"{ph['phase_id']}: {ph['state']} "
                                                    f"(an active phase returns at the next idle point)")
        for ph in c.frontier.phases(a.project, a.task):
            print(json.dumps({k: ph[k] for k in ("phase_id", "state", "persona", "turns",
                                                 "verified_model", "head", "end_reason")},
                             default=str))
        return 0
    elif a.cmd == "kilo-config":
        return _kilo_config(c, a)
    elif a.cmd == "config-pull":
        from .config_pull import ConfigPuller
        cp = ConfigPuller(c, log=c.log)
        for pid in sorted(c.projects):
            if a.project and pid != a.project:
                continue
            if not cp.enabled(pid):
                print(f"{pid}: config pull off (no [foundry] url, or [config_pull] enabled = false)")
                continue
            print(f"{pid}: {cp.sync(pid, force=True)}")
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

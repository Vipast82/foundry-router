"""End-to-end coordinator pilot: real `kilo serve`, real Foundry, two projects.

What is real:  the coordinator (CLI), Kilo CLI's headless server and its tools
               (write/read/bash run for real in the workspaces), Foundry Router
               (strict personas, work bindings, budget ledger, request log),
               git, the AnimalUprising adapter and AnimalUprising's own
               tools/guard/done_check.py (in a sandbox clone).
What is not:   the models. mock_backends.py plays "Strata" (a scripted coder)
               and "Meridian" (a scripted reviewer). No real account is used
               and nothing is pushed anywhere but local bare repositories.

  python run_pilot.py --kilo /path/to/kilo --au /path/to/AnimalUprising-clone --out DIR

Phases (each printed and recorded in DIR/report.json + DIR/report.md):
  1 setup: AnimalUprising sandbox (shared clone, local remote, the proposed
    rule amendment applied there only, two tier-C pilot plans) and the
    generic fixture-game repo; Foundry with the animaluprising-kilo personas;
    Kilo configured from Foundry's own /admin/api/personas/kilo-config
  2 doctor + queue approval (+ a refused cross-project dependency)
  3 run a few ticks, stop (simulated crash), run again until settled
  4 a shared Claude-account budget across both projects runs out: the next
    review is refused, the task pauses; the operator raises the limit and
    resumes it
  5 evidence: transitions, sessions, reviews, Foundry request log, backend
    calls, git logs and remotes
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PORTS = {"strata": 18601, "meridian": 18602, "foundry": 18611, "kilo": 18620}
COORD_KEY, KILO_KEY, KILO_PW = "pilot-coordinator-key", "pilot-kilo-key", "pilot-kilo-pw"
PROCS: list[subprocess.Popen] = []
RULES = [
    {"name": "one-frontier-helper", "match": {"persona": "Foundry-Kilo-*", "paid": True},
     "max_concurrent": 1, "max_active": 1, "active_by": "task", "idle_seconds": 5},
    # at most 2 review calls per sprint attempt (bounded review)
    {"name": "review-per-run", "match": {"role": "reviewer"}, "per": "run", "max_calls": 2,
     "window_minutes": 0},
    # ONE Claude subscription shared by both projects: 4 calls in total
    {"name": "pilot-shared-claude", "match": {"account": "meridian:claude*"}, "max_calls": 4,
     "window_minutes": 0},
    # a per-project allowance on top of the shared one
    {"name": "fixture-project-cap", "match": {"project": "fixture-game"}, "max_calls": 400,
     "window_minutes": 0},
]
REPORT: dict = {"phases": []}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="pilot", GIT_AUTHOR_EMAIL="pilot@local",
               GIT_COMMITTER_NAME="pilot", GIT_COMMITTER_EMAIL="pilot@local")


def sh(cmd, cwd=None, env=None, check=True, timeout=600) -> str:
    r = subprocess.run(cmd, cwd=cwd, env=env or GIT_ENV, capture_output=True, text=True,
                       timeout=timeout, shell=isinstance(cmd, str))
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}: {r.stderr or r.stdout}")
    return r.stdout.strip()


def phase(name: str, **data):
    print(f"\n=== {name}", flush=True)
    for k, v in data.items():
        print(f"  {k}: {v if not isinstance(v, (dict, list)) else json.dumps(v)[:2000]}")
    REPORT["phases"].append({"phase": name, **data})


def http(method, url, body=None, key=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    req.add_header("content-type", "application/json")
    if key:
        req.add_header("authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def wait_http(url, seconds=60, auth=None):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            req = urllib.request.Request(url)
            if auth:
                import base64
                req.add_header("authorization", "Basic " + base64.b64encode(auth.encode()).decode())
            urllib.request.urlopen(req, timeout=3).read()
            return
        except Exception:                                         # noqa: BLE001
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


def start(cmd, log, env=None, cwd=None):
    f = open(log, "w")
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=cwd,
                         start_new_session=True)
    PROCS.append(p)
    return p


def stop_all():
    for p in PROCS:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:                                         # noqa: BLE001
            pass


# --------------------------------------------------------------------------- setup

def _identity(root):
    """Kilo runs with its own HOME here, so the repositories carry a local
    git identity (on the workstation, the user's global one applies)."""
    sh(["git", "config", "user.name", "Kilo (coordinator pilot)"], cwd=root)
    sh(["git", "config", "user.email", "kilo-pilot@local"], cwd=root)


def setup_fixture(out: str) -> str:
    root = os.path.join(out, "fixture-game")
    remote = os.path.join(out, "fixture-game-remote.git")
    sh(["git", "init", "-q", "--bare", "-b", "main", remote])
    os.makedirs(os.path.join(root, "src"))
    sh(["git", "init", "-q", "-b", "main", root])
    for name in ("tasks.json", "verify.py"):
        shutil.copy(os.path.join(HERE, "fixture-game", name), os.path.join(root, name))
    open(os.path.join(root, "src", ".keep"), "w").close()
    open(os.path.join(root, "README.md"), "w").write("Fixture game for the coordinator pilot.\n")
    _identity(root)
    sh("git add -A && git commit -q -m init", cwd=root)
    sh(["git", "remote", "add", "origin", remote], cwd=root)
    sh(["git", "push", "-q", "origin", "main"], cwd=root)
    return root


def setup_au(out: str, au_src: str) -> str:
    root = os.path.join(out, "au-sandbox")
    remote = os.path.join(out, "au-sandbox-remote.git")
    sh(["git", "clone", "-q", "--shared", au_src, root])
    sh(["git", "checkout", "-q", "-B", "master"], cwd=root)
    sh(["git", "clone", "-q", "--bare", root, remote])
    sh(["git", "remote", "set-url", "origin", remote], cwd=root)
    _identity(root)
    amend = open(os.path.join(REPO, "foundry_coordinator", "projects", "animaluprising",
                              "RULE_AMENDMENT.md")).read()
    section = amend[amend.index("## §1a"):amend.index("## .clinerules")]
    with open(os.path.join(root, "AGENT_RULES.md"), "a", encoding="utf-8") as f:
        f.write("\n\n" + section.replace("YYYY-MM-DD", time.strftime("%Y-%m-%d")
                                         + " (SANDBOX PILOT ONLY)"))
    for tid in ("PILOT-C1", "PILOT-C2"):
        shutil.copy(os.path.join(HERE, "au-sandbox", f"{tid}_PLAN.md"),
                    os.path.join(root, "docs", "progress", f"{tid}_PLAN.md"))
    sh("git add AGENT_RULES.md docs/progress/PILOT-C1_PLAN.md docs/progress/PILOT-C2_PLAN.md && "
       "git commit -q -m 'HK-PILOT: coordinator amendment + pilot plans (sandbox only)' && "
       "git push -q origin master", cwd=root)
    return root


def foundry_config(out: str) -> str:
    cfg = textwrap.dedent(f"""\
        server: {{host: 127.0.0.1, port: {PORTS['foundry']}}}
        agent_brain: {{provider: ollama, endpoint: "http://127.0.0.1:9", model: b,
                       routing_mode: passthrough, direct_stream: false}}
        backend_pool:
          mode: internal
          health_check_interval_seconds: 5
          internal:
            backends:
              - {{name: strata, type: openai-compatible, url: "http://127.0.0.1:{PORTS['strata']}/v1",
                  flavor: llamacpp, priority: 100, max_concurrent: 1}}
              - {{name: meridian, type: anthropic-compatible,
                  url: "http://127.0.0.1:{PORTS['meridian']}", api_key: pilot, priority: 90}}
        registry: {{research: {{enabled: false}}}}
        mcp_servers: []
        persona_profiles: [animaluprising-kilo]
        clients:
          - {{name: kilo, key: {KILO_KEY}, scopes: [inference]}}
          - {{name: coordinator, key: {COORD_KEY}, scopes: [inference, orchestration]}}
        """)
    cfg += "budgets:\n  enabled: true\n  rules: " + json.dumps(RULES) + "\n"
    path = os.path.join(out, "foundry.yaml")
    open(path, "w").write(cfg)
    return path


def kilo_config(workspace: str) -> None:
    """kilo.jsonc from Foundry's own generator (+ unattended permissions)."""
    gen = http("GET", f"http://127.0.0.1:{PORTS['foundry']}/admin/api/personas/kilo-config"
                      f"?base_url=http://127.0.0.1:{PORTS['foundry']}")
    gen["provider"]["foundry"]["options"]["apiKey"] = KILO_KEY
    cfg = {"$schema": "https://app.kilo.ai/config.json", **gen,
           "model": "foundry/Foundry-Kilo-Local", "small_model": "foundry/Foundry-Kilo-Local",
           "permission": {"bash": "allow", "edit": "allow", "read": "allow"},
           "autoupdate": False, "share": "disabled"}
    open(os.path.join(workspace, "kilo.jsonc"), "w").write(json.dumps(cfg, indent=2))


def profile(out, pid, template_text) -> str:
    d = os.path.join(out, "profiles", pid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "project.toml")
    open(path, "w").write(template_text)
    return path


# --------------------------------------------------------------------------- coordinator

def coord(out, *args, check=True, timeout=1800) -> str:
    env = dict(GIT_ENV, FOUNDRY_COORD_STATE=os.path.join(out, "coordinator-state"),
               FOUNDRY_COORD_PROFILES=os.path.join(out, "profiles"),
               FOUNDRY_COORDINATOR_KEY=COORD_KEY, KILO_SERVER_PASSWORD=KILO_PW,
               PYTHONPATH=REPO)
    r = subprocess.run([sys.executable, "-m", "foundry_coordinator", *args], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=timeout)
    text = (r.stdout + r.stderr).strip()
    if check and r.returncode not in (0,):
        raise RuntimeError(f"coordinator {' '.join(args)} exited {r.returncode}: {text[-2000:]}")
    return text


def store(out):
    sys.path.insert(0, REPO)
    from foundry_coordinator.store import Store
    return Store(os.path.join(out, "coordinator-state", "coordinator.sqlite"))


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kilo", required=True, help="path to the kilo CLI binary")
    ap.add_argument("--au", required=True, help="AnimalUprising clone with history")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    try:
        run(a, out)
    finally:
        stop_all()
        json.dump(REPORT, open(os.path.join(out, "report.json"), "w"), indent=2, default=str)
        print(f"\nreport: {os.path.join(out, 'report.json')}")


def run(a, out):
    fx = setup_fixture(out)
    au = setup_au(out, a.au)
    calls = os.path.join(out, "backend-calls.jsonl")
    start([sys.executable, os.path.join(HERE, "mock_backends.py"), "--strata", str(PORTS["strata"]),
           "--meridian", str(PORTS["meridian"]), "--log", calls], os.path.join(out, "mocks.log"))
    wait_http(f"http://127.0.0.1:{PORTS['strata']}/v1/models")
    fenv = dict(os.environ, FOUNDRY_CONFIG=foundry_config(out), FOUNDRY_DATA_DIR=os.path.join(out, "foundry-data"),
                PYTHONPATH=REPO)
    start([sys.executable, "-c", textwrap.dedent(f"""
        import uvicorn
        from pathlib import Path
        from foundry_router.main import create_app
        app = create_app(config_path=Path({foundry_config(out)!r}),
                         database_path=Path({os.path.join(out, 'foundry.sqlite')!r}))
        uvicorn.run(app, host="127.0.0.1", port={PORTS['foundry']}, log_level="warning")
        """)], os.path.join(out, "foundry.log"), env=fenv, cwd=REPO)
    wait_http(f"http://127.0.0.1:{PORTS['foundry']}/api/version")
    time.sleep(6)                    # first health check finds the backends' models
    F = f"http://127.0.0.1:{PORTS['foundry']}"
    # The operator enables the frontier reviewer one at a time (profile ships them disabled).
    http("POST", f"{F}/admin/api/personas", {"virtual_name": "Foundry-Kilo-Sonnet", "enabled": 1})
    personas = {p["virtual_name"]: p for p in http("GET", f"{F}/admin/api/personas")["personas"]}
    phase("1 setup", fixture=fx, au_sandbox=au,
          personas={n: {"backend": p.get("backend"), "model": json.loads(p.get("pinned_models") or "[]"),
                        "routing": p.get("routing"), "enabled": p.get("enabled")}
                    for n, p in personas.items() if n.startswith("Foundry-Kilo")})
    for ws in (fx, au):
        kilo_config(ws)
    khome = os.path.join(out, "kilo-home")
    os.makedirs(khome)
    start([a.kilo, "serve", "--port", str(PORTS["kilo"]), "--hostname", "127.0.0.1"],
          os.path.join(out, "kilo.log"), env=dict(os.environ, HOME=khome, KILO_SERVER_PASSWORD=KILO_PW),
          cwd=fx)
    wait_http(f"http://127.0.0.1:{PORTS['kilo']}/global/health", auth=f"kilo:{KILO_PW}")
    kurl = f"http://127.0.0.1:{PORTS['kilo']}"
    profile(out, "fixture-game", open(os.path.join(HERE, "fixture-game", "project.toml.template"))
            .read().replace("@WORKSPACE@", fx).replace("@KILO_URL@", kurl).replace("@FOUNDRY_URL@", F))
    au_prof = open(os.path.join(REPO, "foundry_coordinator", "projects", "animaluprising",
                                "project.toml")).read()
    au_prof = (au_prof.replace("C:/Roblox/AnimalUprising", au)
               .replace('url = "http://127.0.0.1:4096"', f'url = "{kurl}"')
               .replace('url = "http://192.168.0.112:11435"', f'url = "{F}"')
               .replace("session_timeout_minutes = 240", "session_timeout_minutes = 20"))
    profile(out, "animaluprising", au_prof)

    # 2 doctor + queue
    phase("2 doctor", output=coord(out, "doctor", check=False))
    refused = coord(out, "queue", "approve", "fixture-game", "FX-2", "--deps", "animaluprising:PILOT-C1",
                    "--by", "victor", check=False)
    for args in (("animaluprising", "PILOT-C1"), ("animaluprising", "PILOT-C2", "--deps", "PILOT-C1"),
                 ("fixture-game", "FX-1"), ("fixture-game", "FX-2", "--deps", "FX-1")):
        coord(out, "queue", "approve", *args, "--by", "victor", "--note", "pilot queue")
    phase("2 queue", cross_project_dependency=refused, queue=coord(out, "queue", "show"))

    # 3 crash + recovery
    t0 = time.time()
    first = coord(out, "run", "--poll", "1", "--max-ticks", "4")
    mid = coord(out, "status")
    second = coord(out, "run", "--poll", "1", "--until-settled", timeout=3000)
    phase("3 run (stopped after 4 ticks, then restarted)", first_run=first, status_at_stop=mid,
          second_run=second, status=coord(out, "status"), seconds=round(time.time() - t0))

    # 4 shared budget: whichever task was refused
    s = store(out)
    paused = [t for t in s.tasks() if t["state"] == "PAUSED"]
    if paused:
        p = paused[0]
        monitor = http("GET", f"{F}/admin/api/budgets")
        rules = json.loads(json.dumps(RULES))
        for r in rules:
            if r["name"] == "pilot-shared-claude":
                r["max_calls"] = 10                # the operator decides to allow more
        http("POST", f"{F}/admin/api/config/budgets", {"rules": rules})
        resumed = coord(out, "resume", p["project_id"], p["task_id"])
        third = coord(out, "run", "--poll", "1", "--until-settled", timeout=3000)
        phase("4 shared budget refusal -> pause -> operator raises the limit -> resume",
              paused_task=f"{p['project_id']}/{p['task_id']}", pause_reason=p["pause_reason"],
              resume_state=p["resume_state"], denials=(monitor.get("denials") or [])[:5],
              resumed=resumed, third_run=third, status=coord(out, "status"))
    else:
        phase("4 shared budget", note="no task was paused (the shared limit was not reached)")
    collect(out, F, fx, au, calls)


def collect(out, F, fx, au, calls):
    s = store(out)
    tasks = {}
    for t in s.tasks():
        key = f"{t['project_id']}/{t['task_id']}"
        tasks[key] = {
            "state": t["state"], "attempt": t["attempt"], "sessions": t["sessions"],
            "head": t["head_sha"], "reviewed": t["reviewed_sha"],
            "path": [f"{h['from_state'] or '-'}->{h['to_state']}" for h in s.history(*key.split("/"))],
            "session_ids": [f"{x['session_id']} ({x['phase']}, {x['role']}, {x['persona']}, {x['outcome']})"
                            for x in s.sessions(t["project_id"], t["task_id"])],
            "reviews": [{"sha": r["reviewed_sha"][:12], "verdict": r["verdict"], "persona": r["persona"],
                         "valid": r["valid"]} for r in s.reviews(t["project_id"], t["task_id"])],
            "verification": [f"{v['check_name']}={v['result']}" for v in
                             s.verification(t["verification_id"] or "", t["project_id"], t["task_id"])],
            "closeout": [f"{v['check_name']}={v['result']}" for v in
                         s.verification(t["closeout_id"] or "", t["project_id"], t["task_id"])],
        }
    import sqlite3
    db = sqlite3.connect(os.path.join(out, "foundry.sqlite"))
    db.row_factory = sqlite3.Row
    reqs = [dict(r) for r in db.execute(
        "SELECT persona, served_backend, served_model, routing, outcome, client_id, session_id, "
        "project_id, work_task_id, run_id, role FROM request_log ORDER BY id")]
    by_route = {}
    for r in reqs:
        k = (f"{r['client_id']} {r['persona']} -> {r['served_backend']}/{r['served_model']} "
             f"[{r['routing']}] role={r['role'] or '-'} project={r['project_id'] or '-'} outcome={r['outcome']}")
        by_route[k] = by_route.get(k, 0) + 1
    ledger = [dict(r) for r in db.execute(
        "SELECT status, rule, persona, account, project_id, work_task_id, role, count(*) n FROM usage_events "
        "GROUP BY status, rule, persona, account, project_id, work_task_id, role ORDER BY project_id")]
    backend = {}
    for line in open(calls):
        c = json.loads(line)
        k = f"{c['backend']} {c['model']}"
        backend[k] = backend.get(k, 0) + 1
    work = http("GET", f"{F}/admin/api/work")
    phase("5 evidence", tasks=tasks, foundry_routes=by_route, ledger=ledger, backend_calls=backend,
          foundry_work_view={"projects": work["projects"],
                             "tasks": {f"{t['project_id']}/{t['task_id']}": t["state"] for t in work["tasks"]},
                             "sessions": len(work["sessions"]), "events": len(work["events"])},
          fixture_git=sh(["git", "log", "--oneline", "-8"], cwd=fx),
          fixture_remote=sh(["git", "ls-remote", "origin", "refs/heads/main"], cwd=fx),
          fixture_head=sh(["git", "rev-parse", "HEAD"], cwd=fx),
          au_git=sh(["git", "log", "--oneline", "-9"], cwd=au),
          au_remote=sh(["git", "ls-remote", "origin", "refs/heads/master"], cwd=au),
          au_head=sh(["git", "rev-parse", "HEAD"], cwd=au),
          au_untracked_preserved=sh(["git", "status", "--porcelain"], cwd=au))


if __name__ == "__main__":
    main()

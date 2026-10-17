"""Control-center development fixture: the whole path, isolated, on localhost.

    python -m foundry_coordinator.devtools.control_fixture [--port 18766] [--dir PATH] [--lanes]

Starts, in one process and a throwaway directory:
  * Foundry (this checkout) on 127.0.0.1:PORT with a temporary config/DB and
    three generated test clients (operator, coordinator, bridge)
  * a fake `kilo serve` (devtools/fake_kilo.py) — not Kilo
  * a throwaway git project with one visual task and the coordinator engine
    (real KiloClient against the fake, real ControlChannel over HTTP)
  * the real telemetry bridge over HTTP
  * a scripted scenario: streaming text and published reasoning, tools (one
    attributed to an MCP server), a todo list, a planner helper, a question,
    a repair session (follow-mode switch), a permission request, Foundry
    ledger rows with a fallback route, an in-flight call and a media job

With --lanes the fixture profile enables [helpers] and the proactive lanes
(visual + failure triage) and the scenario completes a synthetic capture and a
failing test, so the "Proactive lanes" panel shows real lane units. The
fixture Foundry has no subscription quota source and no visual persona, so
those units are DEFERRED with Foundry's actual reason: nothing is inferred.

The operator test key is written to <dir>/operator-key.txt (never printed).
Nothing touches the real Kilo, Studio, coordinator store or Foundry.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid

os.environ.setdefault("FOUNDRY_DISABLE_BACKGROUND", "1")

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@localhost",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@localhost")


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          env=GIT_ENV, check=True).stdout.strip()


LANES_TOML = """
[helpers]
enabled = true
order = ["meridian"]
agent = "au-helper"
worker_client = "kilo"
poll_seconds = 20

[helpers.personas]
meridian = "Foundry-Kilo-Helper-Claude"

[lanes]
enabled = true
transcript_poll_seconds = 2

[lanes.visual]
enabled = true
debounce_seconds = 2
capture_globs = ["docs/evidence/{task}/*.png"]

[lanes.failure_triage]
enabled = true
"""


def png_bytes(w: int = 48, h: int = 32) -> bytes:
    import struct
    import zlib
    raw = b"".join(bytes([0]) + b"".join(bytes(((x * 5) % 256, (y * 7) % 256, 160))
                                         for x in range(w)) for y in range(h))

    def chunk(t, body):
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body) & 0xffffffff)
    return (bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_project(root: str, lanes: bool = False) -> str:
    ws = os.path.join(root, "game")
    remote = os.path.join(root, "game-remote.git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True, env=GIT_ENV)
    os.makedirs(os.path.join(ws, "docs", "evidence", "UI-01"))
    git(root, "init", "-q", "-b", "main", ws)
    tasks = [{"id": "UI-01", "title": "UI frame sizes and gallery",
              "spec": "Capture the UI frames at the required sizes and pass the runtime gate.",
              "scope": ["src/ui.lua", "docs/evidence/UI-01/*"], "tags": ["visual"]},
             {"id": "UI-00", "title": "Earlier finished task", "spec": "done", "scope": ["src/old.lua"]}]
    with open(os.path.join(ws, "tasks.json"), "w") as f:
        json.dump(tasks, f, indent=1)
    with open(os.path.join(ws, "verify.py"), "w") as f:
        f.write("print('ALL PASS')\n")
    with open(os.path.join(ws, "docs", "evidence", "UI-01", "gallery_phone.png"), "wb") as f:
        f.write(b"\x89PNG fixture bytes")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "init")
    git(ws, "remote", "add", "origin", remote)
    git(ws, "push", "-q", "origin", "main")
    prof_dir = os.path.join(root, "profiles", "fixturegame")
    os.makedirs(prof_dir)
    with open(os.path.join(prof_dir, "project.toml"), "w") as f:
        f.write(textwrap.dedent(f'''\
            schema_version = 1
            id = "fixturegame"
            name = "Fixture game"
            version = "1"

            [workspace]
            path = "{ws.replace(chr(92), '/')}"
            allowed_dirs = ["src", "docs"]
            branch = "main"
            remote = "origin"

            [tasks]
            source = "json"
            file = "tasks.json"

            [client]
            kind = "fake"
            agent = "code"

            [roles]
            implementer = "Foundry-Kilo-Local"
            reviewer = "Foundry-Kilo-Review"
            planner = "Foundry-Kilo-Planner"

            [foundry]
            url = "http://unused.invalid"

            [review]
            required = true

            [publish]
            mode = "none"

            [limits]
            max_attempts = 3
            max_review_calls = 3

            [telemetry]
            context_checkpoints = [140000, 190000]

            [[approval.rules]]
            match = "visual"
            kind = "visual"
            stage = "after_close"
            artifacts = ["docs/evidence/{{task}}/*.png"]

            [[verify.checks]]
            name = "project-check"
            command = ["{{python}}", "verify.py"]
            pass_regex = "^ALL PASS$"
            ''') + (LANES_TOML if lanes else ""))
    return os.path.join(prof_dir, "project.toml")


def foundry_config(root: str, port: int, keys: dict) -> str:
    path = os.path.join(root, "config.yaml")
    with open(path, "w") as f:
        f.write(textwrap.dedent(f"""\
            server: {{host: 127.0.0.1, port: {port}}}
            agent_brain: {{provider: ollama, endpoint: "http://127.0.0.1:9", model: fixture-brain}}
            backend_pool: {{mode: internal, internal: {{backends: []}}}}
            guardrails: {{authority: internal}}
            registry: {{research: {{enabled: false}}}}
            mcp_servers: []
            clients:
              - {{name: victor, key: "{keys['operator']}", scopes: [operator]}}
              - {{name: coordinator, key: "{keys['coordinator']}", scopes: [inference, orchestration]}}
              - {{name: bridge, key: "{keys['bridge']}", scopes: [telemetry]}}
            """))
    return path


def tok(i, o, r=0, cr=0, cw=0):
    return {"input": i, "output": o, "reasoning": r, "cache": {"read": cr, "write": cw}}


class Scenario:
    def __init__(self, fake, db, log, workspace: str = "", lanes: bool = False):
        self.fake, self.db, self.log = fake, db, log
        self.n = 0
        self.workspace, self.lanes = workspace, lanes

    def lane_events(self, sid, mid):
        """A finished capture and a failing test, as the capture procedure and
        Lune would print them (synthetic image, no screen capture)."""
        data = png_bytes()
        rel = "docs/evidence/UI-01/menu_phone.png"
        with open(os.path.join(self.workspace, rel), "wb") as f:
            f.write(data)
        out = "\n".join(["TOP-PRE ok", "VALIDATE V1 size 48x32 PASS",
                         f"CAPTURE menu_phone 48x32 meanLum=90 litFraction=0.8 distinctColors=300 "
                         f"minLum=0 maxLum=255 format=png fileBytes={len(data)} hash=0f1e2d3c",
                         "VALID=OK", f"Saved: {os.path.join(self.workspace, rel)}"])
        self.tool(sid, mid, "bash", {"command": f"pwsh tools/capture-window.ps1 -Out {rel}"}, out, 1.0,
                  "capture 390x844 frame")
        self.tool(sid, mid, "bash", {"command": "lune run tests/lune/run.luau"},
                  "LUNE 41/42 FAIL\nFAIL S45 client arm order (0.4s)", 1.0, "lune")

    def ledger(self, sid, persona, backend, model, prompt, out, decode_s, attempts=None, inflight=0.0):
        rid = f"req-{uuid.uuid4().hex[:12]}"
        if inflight:
            self.db.execute("INSERT INTO usage_events (ts, request_id, session_id, persona, backend, "
                            "model, status) VALUES (?,?,?,?,?,?,?)",
                            (time.time(), rid, sid, persona, backend, model, "reserved"))
            time.sleep(inflight)
            self.db.execute("UPDATE usage_events SET status='settled', input_tokens=?, output_tokens=?, "
                            "settled_ts=? WHERE request_id=?", (prompt, out, time.time(), rid))
        mu = attempts or [{"model": model, "backend": backend}]
        self.db.execute("INSERT INTO request_log (ts, persona, request_id, session_id, served_backend, "
                        "served_model, status, outcome, attempts, models_used, duration_ms, mode) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), persona, rid, sid, backend,
                         model, "ok", "ok", len(mu), json.dumps(mu), int(decode_s * 1000 + 1400), "direct"))
        self.db.execute("INSERT INTO perf_samples (ts, model, backend, persona, prompt_tokens, "
                        "completion_tokens, cached_tokens, decode_tps, prefill_tps, decode_ms, prefill_ms, "
                        "ttft_ms, eff_tps, wall_ms, timing_src, request_id, start_ms, router_ms) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), model, backend, persona,
                         prompt, out, int(prompt * 0.8), out / decode_s, 1800.0, decode_s * 1000, 900.0,
                         1100.0, out / (decode_s + 1.4), int(decode_s * 1000 + 1400), "server", rid,
                         180.0, 12.0))

    def stream(self, sid, mid, kind, text, chunk=6, delay=0.12):
        part = self.fake.add_part(sid, {"type": kind, "text": "",
                                        "time": {"start": int(time.time() * 1000)}}, mid)
        words = text.split(" ")
        for i in range(0, len(words), chunk):
            self.fake.delta(sid, part["id"], (" " if i else "") + " ".join(words[i:i + chunk]))
            time.sleep(delay)
        self.fake.update_part(sid, part["id"], time={"start": part["time"]["start"],
                                                     "end": int(time.time() * 1000)})

    def tool(self, sid, mid, name, inp, out, secs=1.5, title=""):
        t0 = int(time.time() * 1000)
        p = self.fake.add_part(sid, {"type": "tool", "tool": name, "callID": f"call_{uuid.uuid4().hex[:8]}",
                                     "state": {"status": "running", "input": inp, "title": title,
                                               "time": {"start": t0}}}, mid)
        time.sleep(secs)
        self.fake.update_part(sid, p["id"], state={"status": "completed", "input": inp, "output": out,
                                                   "title": title,
                                                   "time": {"start": t0, "end": int(time.time() * 1000)}})

    def wait_reply(self, rid, store):
        while rid in store:
            time.sleep(0.5)

    def on_prompt(self, fake, sid, text):
        self.n += 1
        try:
            if "Operator instructions" in text and "continue" in text.lower() and self.n > 2:
                return self.steer(sid, text)
            if self.n == 1:
                return self.first(sid)
            return self.repair(sid)
        except Exception as e:                                   # noqa: BLE001
            self.log(f"scenario error: {e}")

    def first(self, sid):
        f = self.fake
        f.set_todo(sid, [{"id": "1", "content": "Consult au-planner", "status": "in_progress"},
                         {"id": "2", "content": "Reproduce S45 in a fresh runtime VM", "status": "pending"},
                         {"id": "3", "content": "Capture 390x844 frame", "status": "pending"},
                         {"id": "4", "content": "Vision check and OBSERVATION", "status": "pending"}])
        mid = f.add_message(sid, "assistant", [], completed=False, model="Foundry-Kilo-Local",
                            tokens=tok(0, 0))
        self.stream(sid, mid, "reasoning", "The plan says S45 must arm the client before server work. "
                    "I'll ask the planner for the frame order first, then reproduce S45 in a fresh VM. "
                    "Nothing here allows widening scope.")
        self.stream(sid, mid, "text", "Starting **UI-01**. First I'll consult `au-planner`, then reproduce "
                    "the S45 failure:\n\n```lua\nlocal Players = game:GetService(\"Players\")\n"
                    "-- arm the client before server work\nreturn Players.LocalPlayer ~= nil\n```")
        kid = f.new_session(title="Plan UI-01 frame capture order", agent="au-planner",
                            model="Foundry-Kilo-Planner", parent=sid)
        tp = f.add_part(sid, {"type": "tool", "tool": "task", "callID": "call_task1",
                              "state": {"status": "running", "title": "Plan UI-01 frame capture order",
                                        "input": {"subagent_type": "au-planner",
                                                  "description": "Plan UI-01 frame capture order"},
                                        "metadata": {"sessionId": kid},
                                        "time": {"start": int(time.time() * 1000)}}}, mid)
        f.set_status(kid, "busy")
        f.add_message(kid, "user", [{"type": "text", "text": "Order the remaining UI-01 frames."}],
                      agent="au-planner")
        kmid = f.add_message(kid, "assistant", [], completed=False, model="Foundry-Kilo-Planner",
                             agent="au-planner")
        threading.Thread(target=self.ledger, args=(kid, "Foundry-Kilo-Planner", "cliproxyapi",
                                                   "gpt-6-astra", 18000, 900, 9.0,
                                                   [{"model": "claude-opus-5-5", "backend": "meridian",
                                                     "error": "usage window exhausted (fallback)"},
                                                    {"model": "gpt-6-astra", "backend": "cliproxyapi"}],
                                                   6.0), daemon=True).start()
        self.stream(kid, kmid, "reasoning", "Phone frame first because it is the binding size; then "
                    "tablet, then desktop. S46 timing pair must not be split.", delay=0.2)
        self.stream(kid, kmid, "text", "1. 390x844 phone\n2. 844x390 landscape phone\n3. 1024x768\n"
                    "4. 1366x768\n5. 1920x1080\n\nKeep S46 server setup + client check adjacent.", delay=0.2)
        f.complete_last(kid, tok(17000, 900, 300, 1000))
        f.set_status(kid, None)
        f.update_part(sid, tp["id"], state={"status": "completed", "title": "Plan UI-01 frame capture order",
                                            "input": {"subagent_type": "au-planner"},
                                            "output": "Planner: phone first; keep S46 pair adjacent.",
                                            "metadata": {"sessionId": kid},
                                            "time": {"start": tp["state"]["time"]["start"],
                                                     "end": int(time.time() * 1000)}})
        f.set_todo(sid, [{"id": "1", "content": "Consult au-planner", "status": "completed"},
                         {"id": "2", "content": "Reproduce S45 in a fresh runtime VM", "status": "in_progress"},
                         {"id": "3", "content": "Capture 390x844 frame", "status": "pending"},
                         {"id": "4", "content": "Vision check and OBSERVATION", "status": "pending"}])
        threading.Thread(target=self.ledger, args=(sid, "Foundry-Kilo-Local", "strata",
                                                   "qwen3.8-flash-next-iq3_s", 61000, 1400, 22.0, None,
                                                   8.0), daemon=True).start()
        self.tool(sid, mid, "bash", {"command": "git status --short"},
                  " M docs/progress/state/DECISIONS.md\nAuthorization: Bearer abcdefghijklmnop123456 "
                  "(a fake secret the redactor must remove)\n<script>alert('x')</script>", 1.2,
                  "git status")
        self.tool(sid, mid, "Roblox_Studio_run_code", {"code": "return workspace:GetChildren()"},
                  json.dumps({"ok": True, "children": 42}), 4.0, "Studio: list workspace")
        if self.lanes:
            self.lane_events(sid, mid)
            time.sleep(6)                     # let the coordinator ingest them while busy
        self.db.execute("INSERT INTO media_jobs (id, created_ts, op, status, session_id, backend, model) "
                        "VALUES (?,?,?,?,?,?,?)", (f"img_{uuid.uuid4().hex[:16]}", time.time(), "generate",
                                                   "succeeded", sid, "comfy", "flux-dev"))
        f.complete_last(sid, tok(61000, 1400, 420, 52000, 0))
        rid = f.ask_question(sid, [{"question": "S45 still fails in a fresh VM after arming the client "
                                                "first. Should I capture the 390x844 frame anyway, or stop "
                                                "and debug S45 first?", "header": "S45 vs frames",
                                    "options": [{"label": "Debug S45 first",
                                                 "description": "Keep the gate binding"},
                                                {"label": "Capture frames now",
                                                 "description": "Frames don't depend on S45"}]}])
        self.wait_reply(rid, f.questions)
        mid = f.add_message(sid, "assistant", [], completed=False, tokens=tok(0, 0))
        self.stream(sid, mid, "text", "Understood. Following your answer. I'll stop here and report; "
                    "nothing was committed in this attempt.")
        f.complete_last(sid, tok(63000, 200, 0, 60000))
        f.finish(sid, "```coordinator-report\n{\"status\": \"NOT DONE\", \"summary\": \"S45 still "
                      "failing\"}\n```", tokens=tok(64000, 80, 0, 63000))

    def repair(self, sid):
        f = self.fake
        f.set_todo(sid, [{"id": "1", "content": "Fix S45 client arm order", "status": "in_progress"},
                         {"id": "2", "content": "Re-run UI_01 gate chain", "status": "pending"}])
        mid = f.add_message(sid, "assistant", [], completed=False, tokens=tok(0, 0))
        self.stream(sid, mid, "reasoning", "Repair attempt. The coordinator's findings say nothing was "
                    "committed. Fix the arm order, then commit.", delay=0.15)
        self.stream(sid, mid, "text", "Repair session: fixing the S45 arm order first.", delay=0.15)
        rid = f.ask_permission(sid, "bash", ["git push origin main"],
                               {"command": "git push origin main", "description": "push the fix"})
        self.wait_reply(rid, f.permissions)
        aborted = f.aborts.count(sid)
        for i in range(400):        # keep a long-running, quiet-ish worker for the viewer
            if f.aborts.count(sid) != aborted:
                return                                   # paused / steered: this turn ended
            mid = f.add_message(sid, "assistant", [], completed=False, tokens=tok(0, 0))
            threading.Thread(target=self.ledger, args=(sid, "Foundry-Kilo-Local", "strata",
                                                       "qwen3.8-flash-next-iq3_s", 70000 + i * 900,
                                                       600, 14.0, None, 5.0), daemon=True).start()
            self.stream(sid, mid, "text", f"Step {i + 1}: re-running the gate chain segment {i + 1} "
                        "with the corrected ordering and recording raw evidence.", delay=0.4)
            self.tool(sid, mid, "robloxstudio-mcp_eval_client_runtime", {"script": f"check({i})"},
                      f"PASS segment {i + 1}", 3.0, f"client runtime check {i + 1}")
            f.complete_last(sid, tok(70000 + i * 900, 600, 0, 68000 + i * 800))
            time.sleep(4)

    def steer(self, sid, text):
        f = self.fake
        mid = f.add_message(sid, "assistant", [], completed=False, tokens=tok(0, 0))
        self.stream(sid, mid, "text", "Received the operator instruction; adjusting the plan accordingly.")
        f.complete_last(sid, tok(80000, 60, 0, 79000))
        return self.repair(sid)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18766)
    ap.add_argument("--dir", default="")
    ap.add_argument("--lanes", action="store_true", help="enable the proactive lanes scenario")
    a = ap.parse_args(argv)
    root = a.dir or tempfile.mkdtemp(prefix="cc-fixture-")
    os.makedirs(root, exist_ok=True)
    keys = {k: f"fixture-{k}-{secrets.token_urlsafe(18)}" for k in ("operator", "coordinator", "bridge")}
    with open(os.path.join(root, "operator-key.txt"), "w") as f:
        f.write(keys["operator"])
    cfg = foundry_config(root, a.port, keys)

    import uvicorn
    from foundry_router.main import create_app
    from foundry_coordinator.adapters import build_project
    from foundry_coordinator.adapters.foundry import FoundryClient
    from foundry_coordinator.adapters.kilo import KiloClient
    from foundry_coordinator.control import ControlChannel
    from foundry_coordinator.devtools.fake_kilo import FakeKilo
    from foundry_coordinator.engine import Coordinator
    from foundry_coordinator.profile import load
    from foundry_coordinator.store import Store
    from foundry_coordinator.telemetry import KiloObserver, TelemetryBridge

    def log(m):
        print(time.strftime("%H:%M:%S"), m, flush=True)

    from pathlib import Path
    app = create_app(config_path=Path(cfg), database_path=Path(root) / "foundry.sqlite")
    svc = app.state.services
    url = f"http://127.0.0.1:{a.port}"

    fake = FakeKilo(password=secrets.token_urlsafe(16))
    kurl = fake.start()
    prof = load(make_project(root, lanes=a.lanes))
    state = os.path.join(root, "coord")
    coord_foundry = FoundryClient(url, key=keys["coordinator"], timeout=20)
    project = build_project(prof, state, client=KiloClient(kurl, password=fake.password),
                            foundry=coord_foundry)
    store = Store(os.path.join(state, "coordinator.sqlite"))
    c = Coordinator(store, [project], state, log=log)
    scen = Scenario(fake, svc.db, log, workspace=prof.workspace, lanes=a.lanes)
    fake.on_prompt = scen.on_prompt
    # an earlier, finished task so history/queue views aren't empty
    store.approve("fixturegame", "UI-00", [], "fixture")
    store.update_task("fixturegame", "UI-00", state="COMPLETE", head_sha=git(prof.workspace, "rev-parse", "HEAD"))

    def worker():
        time.sleep(2.0)                                  # let uvicorn come up
        c.approve("fixturegame", "UI-01", [], "victor (fixture)")
        ch = ControlChannel(c, foundry=FoundryClient(url, key=keys["coordinator"], timeout=20),
                            source_id="coordinator:fixture", log=log)
        c.after_tick.append(ch.poll)
        me = store.claim_owner(force=True)
        c.run(poll_seconds=2, owner=me)

    def bridge():
        time.sleep(3.0)
        br = TelemetryBridge([project], store.path, FoundryClient(url, key=keys["bridge"], timeout=20),
                             observers={"fixturegame": KiloObserver(kurl, password=fake.password)},
                             source_id="bridge:fixture", poll_s=1.0, log=log)
        br.run()

    def watchdog():
        while True:
            time.sleep(10)
            try:
                svc.control.watchdog()
            except Exception as e:                       # noqa: BLE001
                log(f"watchdog: {e}")

    for fn in (worker, bridge, watchdog):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    log(f"control-center fixture: {url}/ui/control  (operator test key in {root}"
        f"{os.sep}operator-key.txt)")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

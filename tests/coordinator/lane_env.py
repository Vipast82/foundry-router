"""Shared fixtures for proactive-lane tests: a real coordinator driving the
fake `kilo serve` through the Kilo adapter, a fake Foundry with simulated quota
admission and a recording vision endpoint, a simulated clock, and synthetic
PNG/JPEG bytes. No model calls, no network beyond localhost fakes."""

from __future__ import annotations

import json
import struct
import time
import zlib

from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import FakeFoundry, make_repo, review_json, write_profile

CLAUDE, GPT, VISION = "Foundry-Kilo-Helper-Claude", "Foundry-Kilo-Helper-GPT", \
    "Foundry-Kilo-Visual-Review"

HELPERS = """
[helpers]
enabled = true
order = ["meridian", "cpa"]
selective = ["cpa"]
agent = "au-helper"
worker_client = "kilo"
max_running = 3
poll_seconds = 30
handoff_wait_minutes = 15

[helpers.personas]
meridian = "Foundry-Kilo-Helper-Claude"
cpa = "Foundry-Kilo-Helper-GPT"
"""

LANES_ALL = """
[lanes]
enabled = true
transcript_poll_seconds = 0

[lanes.visual]
enabled = true
debounce_seconds = 0
capture_globs = ["docs/evidence/{task}/*.png"]
reference_globs = ["docs/design/{task}_ref.png"]

[lanes.proposer]
enabled = true

[lanes.failure_triage]
enabled = true

[lanes.incremental_review]
enabled = true
min_interval_seconds = 0

[lanes.test_design]
enabled = true

[lanes.research]
enabled = true

[lanes.evidence]
enabled = true

[lanes.handoff]
enabled = true
context_threshold = 100000

[lanes.preflight]
enabled = true
"""

T2 = {"id": "T2", "title": "Menu layout", "spec": "Fix the S45 menu layout.",
      "scope": ["src/menu.luau", "src/tests/*", "docs/evidence/T2/*"]}


def png(w: int = 4, h: int = 3, rgb=(10, 20, 30), truncate: int = 0) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, body):
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body)
                                                                     & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return data[:-truncate] if truncate else data


def jpeg(w=8, h=8, complete=True) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    body = b"\xff\xd8" + sof + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x12\x34\x56"
    return body + (b"\xff\xd9" if complete else b"")


def capture_output(path: str, data: bytes, w: int, h: int, valid: bool = True) -> str:
    return "\n".join([
        "TOP-PRE ok", f"VALIDATE V1 size {w}x{h} PASS",
        f"CAPTURE menu {w}x{h} meanLum=90 litFraction=0.8 distinctColors=300 minLum=0 maxLum=255 "
        f"format=png fileBytes={len(data)} hash=abcdef0123",
        "VALID=OK" if valid else "VALID=INVALID(occluded by Discord)",
        f"Saved: {path}" if valid else f"Saved (invalid): {path} (kept for diagnosis; an invalid "
                                       f"capture is never evidence)"])


class LaneFoundry(FakeFoundry):
    """Fake Foundry: admission per persona (with per-route modality), binding
    log, a recording chat endpoint whose answer is set per test."""

    def __init__(self):
        super().__init__()
        self.reviewer = lambda packet: review_json(packet)
        self.admission = {}
        self.bind_kw = []
        self.chat_calls = []
        self.vision_answer = None             # callable(messages) -> (text, meta)
        self.refuse_purposes = set()

    def bind(self, session_id, project_id, task_id, run_id, role, persona, parent="", meta=None,
             purpose="", worker_client="", assignment=None):
        if purpose in self.refuse_purposes:
            from foundry_coordinator.adapters.foundry import FoundryError
            raise FoundryError(f"purpose must be one of ... ({purpose} unknown)")
        self.bind_kw.append(dict(session_id=session_id, role=role, persona=persona,
                                 purpose=purpose, parent=parent, assignment=assignment))
        return super().bind(session_id, project_id, task_id, run_id, role, persona, parent,
                            meta, purpose, worker_client, assignment)

    def chat(self, persona, messages, *, session_id="", max_tokens=4000, schema=None):
        if session_id.startswith("lane-"):
            self.chat_calls.append({"persona": persona, "messages": messages,
                                    "session_id": session_id, "schema": schema})
            if self.deny is not None:
                d, self.deny = self.deny, None
                raise d
            return self.vision_answer(messages) if self.vision_answer else (
                "```helper-result\n{}\n```", {"served_model": "vision-model"})
        return super().chat(persona, messages, session_id=session_id, max_tokens=max_tokens,
                            schema=schema)


def admit(state="admit", reason="fixture", nxt=None, vision=None, routes=None):
    r = routes if routes is not None else [{"backend": "meridian", "model": "claude-sonnet",
                                            "source": "fixture", "vision": vision}]
    return {"admit": state == "admit", "state": state, "reason": reason, "next_eligible": nxt,
            "routes": r, "streams_free": 1}


def setup(tmp_path, lanes: str = LANES_ALL, tasks=None, behind: float = 3600.0,
          extra_files=None):
    fake = FakeKilo()
    url = fake.start()
    seq = []
    fake.on_prompt = lambda f, sid, text: seq.append(("prompt", sid, text))
    files = {"src/menu.luau": "local M = {}\nreturn M\n", "src/tests/menu.spec.luau": "-- t\n",
             "docs/design/T2_ref.png": "", "docs/evidence/T2/.keep": "",
             **(extra_files or {})}
    root, _ = make_repo(tmp_path, "game", tasks or [T2], extra_files=files)
    (root / "docs" / "design" / "T2_ref.png").write_bytes(png(4, 3, (200, 0, 0)))
    from tests.coordinator.helpers import git
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "ref image")
    prof = load(str(write_profile(tmp_path, "game", root, extra=HELPERS + lanes)))
    clock = [time.time() - behind]
    ff = LaneFoundry()
    state = str(tmp_path / "state")

    def make():
        P = build_project(prof, state, client=KiloClient(url, password=fake.password), foundry=ff)
        return Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [P], state,
                           clock=lambda: clock[0])
    c = make()
    c.approve("game", (tasks or [T2])[0]["id"], [], "victor")
    c.tick()
    c.tick()
    tid = (tasks or [T2])[0]["id"]
    assert c.store.task("game", tid)["state"] == "IMPLEMENTING"
    return {"fake": fake, "c": c, "ff": ff, "clock": clock, "make": make, "root": root,
            "seq": seq, "primary": c.store.task("game", tid)["session_id"], "tid": tid}


def tool_part(command: str, output: str, status: str = "completed", exit_code=0, tool="bash"):
    return {"type": "tool", "tool": tool, "callID": f"call_{abs(hash(command + output)) % 10**8}",
            "state": {"status": status, "input": {"command": command}, "output": output,
                      "metadata": {"exit": exit_code}, "time": {"start": 1, "end": 2}}}


def primary_tool(env, command, output, status="completed", exit_code=0, tool="bash"):
    """The busy primary completes a tool call (a real Kilo message part)."""
    fake, sid = env["fake"], env["primary"]
    fake.set_status(sid, "busy")
    return fake.add_message(sid, "assistant", [tool_part(command, output, status, exit_code, tool)],
                            completed=False)


def finish_primary(env, report: dict, tokens=None):
    env["fake"].finish(env["primary"], "Work so far.\n```coordinator-report\n"
                       + json.dumps(report) + "\n```", tokens=tokens)


def helper_sessions(env):
    return [s for s in env["c"].store.sessions("game", env["tid"]) if s["role"] == "helper"]


def answer_helper(env, sid, result: dict, prose="Proposal."):
    env["fake"].finish(sid, prose + "\n```helper-result\n" + json.dumps(result) + "\n```")

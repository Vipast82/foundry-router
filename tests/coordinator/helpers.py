"""Test fixtures for the coordinator: throwaway git projects with a bare
remote, generic profiles, a scripted coding client and a fake Foundry."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap

from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.foundry import FoundryDenied
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
os.environ.update({k: GIT_ENV[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                           "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")})

VERIFY_PY = textwrap.dedent('''\
    """Project check: every task file listed in tasks.json that exists must
    contain the word OK. Prints ALL PASS or FAIL lines."""
    import json, os, sys
    tasks = json.load(open("tasks.json"))
    bad = []
    for t in tasks:
        for f in t.get("scope", []):
            if os.path.isfile(f) and "OK" not in open(f).read():
                bad.append(f)
    for b in bad:
        print("FAIL:", b)
    print("ALL PASS" if not bad else "SOME FAIL")
    sys.exit(1 if bad else 0)
    ''')


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          env=GIT_ENV, check=True).stdout.strip()


def make_repo(tmp, name: str, tasks: list[dict], extra_files: dict | None = None,
              task_file: str = "tasks.json"):
    root = tmp / name
    remote = tmp / f"{name}-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    if task_file.endswith(".json"):
        (root / task_file).write_text(json.dumps(tasks, indent=1))
    else:
        lines = []
        for t in tasks:
            lines.append("[[tasks]]")
            for k, v in t.items():
                lines.append(f"{k} = {json.dumps(v)}")
        (root / task_file).write_text("\n".join(lines) + "\n")
    (root / "verify.py").write_text(VERIFY_PY)
    for rel, content in (extra_files or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "origin", "main")
    return root, remote


def write_profile(tmp, pid: str, workspace, extra: str = "", task_file: str = "tasks.json",
                  publish: str = "push", review: bool = True, checks: str | None = None):
    d = tmp / "profiles" / pid
    d.mkdir(parents=True, exist_ok=True)
    checks = checks if checks is not None else textwrap.dedent('''\
        [[verify.checks]]
        name = "project-check"
        command = ["{python}", "verify.py"]
        pass_regex = "^ALL PASS$"
        ''')
    (d / "project.toml").write_text(textwrap.dedent(f'''\
        schema_version = 1
        id = "{pid}"
        name = "{pid} game"
        version = "3"

        [workspace]
        path = "{str(workspace).replace(chr(92), "/")}"
        allowed_dirs = ["src", "docs", "art"]
        branch = "main"
        remote = "origin"

        [tasks]
        source = "{task_file.rsplit(".", 1)[1]}"
        file = "{task_file}"

        [client]
        kind = "fake"
        agent = "code"

        [roles]
        implementer = "Foundry-Kilo-Local"
        reviewer = "Foundry-Kilo-Sonnet"
        deep_reviewer = "Foundry-Kilo-Opus"

        [foundry]
        url = "http://foundry.invalid"

        [review]
        required = {"true" if review else "false"}
        deep_when = ["risk:high"]

        [publish]
        mode = "{publish}"

        [limits]
        max_attempts = 3
        max_review_calls = 4

        [closeout]
        exempt_paths = ["docs/progress/{{task}}_*"]

        ''') + checks + "\n" + textwrap.dedent(extra))
    return d / "project.toml"


class FakeFoundry:
    """Stands in for Foundry: records bindings/events, answers reviews with
    a per-test function, and can refuse like Foundry does."""

    def __init__(self, reviewer=None):
        self.binds, self.ended, self.events, self.tasks, self.chats = [], [], [], {}, []
        self.reviewer = reviewer or approve_all
        self.deny = None                     # FoundryDenied to raise on the next chat
        self.admission = {}                  # persona -> helper admission answer

    def bind(self, session_id, project_id, task_id, run_id, role, persona, parent="", meta=None,
             purpose="", worker_client="", assignment=None):
        self.binds.append(dict(session_id=session_id, project_id=project_id, task_id=task_id,
                               run_id=run_id, role=role, persona=persona, parent=parent,
                               purpose=purpose, meta=meta or {}))
        return {}

    def helper_admission(self, persona, **need):
        # need: the helper's route requirements (vision, models, purpose), if any
        self.admission_asks = getattr(self, "admission_asks", []) + [(persona, need)]
        ans = (self.admission or {}).get(persona)
        if callable(ans):
            ans = ans(**need)
        return ans or {"admit": False, "state": "unknown", "reason": "no fake admission set"}

    def end(self, session_id, outcome):
        self.ended.append((session_id, outcome))

    def put_task(self, project_id, task_id, data):
        self.tasks[(project_id, task_id)] = data

    def push_events(self, events):
        self.events += events
        return len(events)

    def chat(self, persona, messages, *, session_id="", max_tokens=4000, schema=None):
        self.chats.append({"persona": persona, "session_id": session_id,
                           "packet": messages[-1]["content"]})
        if self.deny is not None:
            d, self.deny = self.deny, None
            raise d
        return self.reviewer(messages[-1]["content"]), {"served_model": persona}


def _head_from_packet(packet: str) -> str:
    for line in packet.splitlines():
        if line.startswith("Head commit (review exactly this): "):
            return line.split(": ", 1)[1].strip()
    raise AssertionError("no head in packet")


def _task_from_packet(packet: str) -> tuple[str, str]:
    first = packet.splitlines()[0]          # "# Review packet — project P, task T: title"
    proj = first.split("project ", 1)[1].split(",")[0]
    task = first.split("task ", 1)[1].split(":")[0]
    return proj, task


def review_json(packet: str, verdict: str = "APPROVED", findings=None, sha=None, task=None,
                missing=None) -> str:
    proj, t = _task_from_packet(packet)
    return json.dumps({"sprint_id": task or t, "project_id": proj,
                       "reviewed_commit_sha": sha or _head_from_packet(packet),
                       "verdict": verdict, "findings": findings or [],
                       "evidence_inspected": ["diff", "project-check"],
                       "missing_evidence": missing or [], "required_repairs": [],
                       "residual_risks": []})


def approve_all(packet):
    return review_json(packet)


def write_ok(path: str, text: str = "OK"):
    """A scripted 'agent' that writes one file and commits it."""
    def script(ctx):
        return {"writes": {path: text}, "commit": f"{ctx['task_id']}: work",
                "final": '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'}
    return script


def by_task(mapping: dict):
    """Dispatch a script per task id (and phase)."""
    def script(ctx):
        fn = mapping.get((ctx["task_id"], ctx["phase"])) or mapping.get(ctx["task_id"])
        return fn(ctx) if fn else {"final": "nothing to do"}
    return script


def coordinator(tmp, profiles: list, scripts: dict, foundry: dict | None = None,
                clock=None, max_writers: int = 1, store_path=None):
    store = Store(str(store_path or tmp / "state" / "c.sqlite"))
    projects = []
    fakes = foundry or {}
    for path in profiles:
        p = load(str(path))
        client = scripts[p.id] if isinstance(scripts[p.id], ScriptedClient) else \
            ScriptedClient(scripts[p.id], project_id=p.id)
        projects.append(build_project(p, str(tmp / "state"), client=client,
                                      foundry=fakes.get(p.id) or FakeFoundry()))
    kw = {"clock": clock} if clock else {}
    return Coordinator(store, projects, str(tmp / "state"), max_writers=max_writers, **kw)


def run_until(c, pred, limit=40):
    for _ in range(limit):
        c.tick()
        if pred():
            return True
    return False


def denial(category="budget", retry=60):
    return FoundryDenied(category, {"budget": "budget_denied", "pause": "paused",
                                    "route": "route_unavailable", "config": "persona_disabled"}[category],
                         f"refused ({category})", retry)


def refuse_auto_reject(client):
    """Make a client refuse the COORDINATOR's own permission reject (the reply
    that carries a message), so a test can exercise the fallback where the ask
    still reaches a person (W1 follow-up: implementer permission asks are
    auto-rejected otherwise). Operator replies (one item) go through."""
    orig = client.answer

    def answer(session_id, request_id, answers, *, directory, kind="question"):
        if kind == "permission" and len(answers) > 1 and answers[0] == "reject":
            raise RuntimeError("Kilo refused the coordinator's reject (test)")
        return orig(session_id, request_id, answers, directory=directory, kind=kind)
    client.answer = answer
    return client

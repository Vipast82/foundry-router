"""The coordinator engine runs a task end to end on OpenCode 2.x (v0.181.247): a real git
repo, the generic adapter, the fake `opencode serve` 2.x server answering for the model
(it writes the file and commits, as an agent would) and a fake Foundry. The same lifecycle
a Kilo task goes through: session, prompt, poll, verify, review, complete."""

import subprocess

from foundry_coordinator import states as S
from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.opencode import OpenCodeClient
from foundry_coordinator.devtools.fake_opencode import FakeOpenCode2
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import FakeFoundry, make_repo, run_until, write_profile

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
REPORT = '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'


def test_a_task_completes_on_opencode_2x(tmp_path):
    root, _ = make_repo(tmp_path, "pilot", [T1])
    prof = load(str(write_profile(tmp_path, "pilot", root, publish="none")))
    fake = FakeOpenCode2(password="pw")
    url = fake.start()

    def agent(fake_, sid, text):
        # the model's turn: edit the file, commit, report (on the server's own thread)
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "a.txt").write_text("OK", encoding="utf-8")
        subprocess.run(["git", "add", "src/a.txt"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "T1: work"], cwd=root, check=True,
                       capture_output=True)
        fake_.finish(sid, "wrote it\n" + REPORT,
                     tools=[{"name": "write", "input": {"filePath": "src/a.txt"}}])
    fake.on_prompt = agent
    try:
        client = OpenCodeClient(url, password="pw")
        ff = FakeFoundry()
        P = build_project(prof, str(tmp_path / "state"), client=client, foundry=ff)
        c = Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [P],
                        str(tmp_path / "state"))
        c.approve("pilot", "T1", [], "victor")
        assert run_until(c, lambda: (c.store.task("pilot", "T1") or {}).get("state") == S.COMPLETE,
                         limit=60), c.store.task("pilot", "T1")
        assert client.dialect == "oc2"
        sess = [s for s in fake.sessions.values() if not s.get("parentID")]
        assert len(sess) >= 1
        first = sess[0]
        assert first["location"]["directory"].replace("\\", "/").lower() == \
            str(root).replace("\\", "/").lower()
        assert first["metadata"]["coordinator"]["task"] == "T1"
        assert first["model"]["id"] == "Foundry-Kilo-Local"
        assert fake.prompts and "src/a.txt" in fake.prompts[0]["text"]
        rows = c.store.q("SELECT client FROM sessions WHERE project_id='pilot'")
        assert rows and all(r["client"] == "opencode" for r in rows)
        # the router binding was made for the session OpenCode created
        assert any(b["session_id"] == first["id"] for b in ff.binds)
        assert ff.chats, "the review ran (a direct router call, as for Kilo)"
    finally:
        fake.stop()

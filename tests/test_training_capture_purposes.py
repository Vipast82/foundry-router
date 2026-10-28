"""v0.153.0 capture of every model call: each row's `purpose` (agent_turn,
brain_route, frontier_assist, research, agent_external, media, operator_chat),
the purpose switches, the operator chat's own opt-in, the brain / external
agent / media hooks, inline images stored as references, and stores written
before the purpose column existed."""

import asyncio
import base64
import json
import sqlite3

import httpx

from foundry_router import capture as C
from foundry_router.agents import AgentManager
from foundry_router.brain.client import BrainClient
from foundry_router.config import AgentBrainConfig, AgentConfig, TrainingCaptureConfig
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult
from tests.test_agents import hermes_handler


def _cap(tmp_path, **cfg):
    conf = TrainingCaptureConfig(enabled=True, include_unbound=True, **cfg)
    holder = {"cfg": conf}
    cap = C.Capture(lambda: holder["cfg"], tmp_path)
    cap.holder = holder
    return cap


def _call(cap, purpose=None, backend="strata"):
    """One pool-style call record (begin + finish), as the pool writes it."""
    C.reset_request()
    tok = cap.begin_call(backend, persona="", purpose=purpose)
    if tok is None:
        return None
    cap.finish_call(tok, model="m", backend=backend, messages=[{"role": "user", "content": "q"}],
                    tools=None, settings={}, result=ChatResult(content="a"))
    return tok


def test_rows_carry_their_purpose_and_can_be_filtered(tmp_path):
    cap = _cap(tmp_path)
    _call(cap)
    with C.purpose("frontier_assist"):
        _call(cap)
    _call(cap, purpose="research")
    rows = cap.records()
    assert [r["purpose"] for r in rows] == ["agent_turn", "frontier_assist", "research"]
    assert [r["purpose"] for r in cap.records(purpose="frontier_assist")] == ["frontier_assist"]
    assert cap.counts_today()["purposes"] == {"agent_turn": 1, "frontier_assist": 1,
                                              "research": 1}
    st = cap.status()
    assert "brain_route" in st["known_purposes"] and st["operator_chat"] is False


def test_purpose_include_and_exclude_globs(tmp_path):
    cap = _cap(tmp_path, exclude_purposes=["brain_*"])
    assert _call(cap, purpose="brain_route") is None
    assert _call(cap) is not None
    cap.holder["cfg"] = TrainingCaptureConfig(enabled=True, include_unbound=True,
                                              purposes=["media", "agent_external"])
    assert _call(cap) is None                     # agent_turn not listed
    assert cap.record_event("media_job", {"request": {}}, purpose="media")
    assert [r["purpose"] for r in cap.records()] == ["agent_turn", "media"]


def test_operator_chat_needs_its_own_switch(tmp_path):
    cap = _cap(tmp_path)                          # include_unbound on, operator_chat off
    with C.purpose("operator_chat"):
        assert _call(cap) is None
    cap.holder["cfg"] = TrainingCaptureConfig(enabled=True, operator_chat=True)
    with C.purpose("operator_chat"):
        assert _call(cap) is not None             # captured even with include_unbound off
    assert [r["purpose"] for r in cap.records()] == ["operator_chat"]
    cap.holder["cfg"] = TrainingCaptureConfig(enabled=False, operator_chat=True)
    with C.purpose("operator_chat"):
        assert _call(cap) is None                 # capture off is off


def test_internal_clients_are_still_never_captured(tmp_path):
    from foundry_router import client_auth
    cap = _cap(tmp_path)
    tok = client_auth._client.set({"client_id": "foundry:health", "internal": True})
    try:
        assert _call(cap) is None
        assert _call(cap, purpose="brain_route") is None
    finally:
        client_auth._client.reset(tok)


def test_brain_calls_are_captured_as_brain_route(tmp_path):
    cap = _cap(tmp_path)

    class Proto:
        async def chat(self, model, messages, **kw):
            return ChatResult(content='{"route": "x"}', thinking="pick x",
                              prompt_tokens=5, completion_tokens=3)
    brain = BrainClient(AgentBrainConfig(provider="ollama", model="brain-m",
                                         endpoint="http://brain:11434"), httpx.AsyncClient())
    brain.protocol = Proto()
    brain.capture = cap
    C.reset_request()
    asyncio.run(brain.complete("route this", aux=True))
    row, = cap.records()
    assert row["purpose"] == "brain_route" and row["backend"] == "brain:ollama"
    assert row["model"] == "brain-m"
    assert row["body"]["response"]["content"] == '{"route": "x"}'
    assert row["body"]["response"]["reasoning"] == "pick x"
    assert row["body"]["request"]["settings"]["brain_purpose"] == "background"
    # capture off: the brain call still works and nothing is written
    cap.holder["cfg"] = TrainingCaptureConfig(enabled=False)
    C.reset_request()
    assert asyncio.run(brain.complete("again", aux=True)) == '{"route": "x"}'
    assert len(cap.records()) == 1


def test_external_agent_task_is_one_agent_run_record(tmp_path):
    cap = _cap(tmp_path)
    db = Database(tmp_path / "a.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: hermes_handler(r)))
    mgr = AgentManager([AgentConfig(name="hermes", url="http://hermes:8642", api_key="k")],
                       http, db)
    mgr.capture = cap

    async def go():
        C.reset_request()
        out = []
        async for ev in mgr.chat_stream("hermes", [{"role": "user", "content": "fix the test"}]):
            out.append(ev)
        return out
    asyncio.run(go())
    row, = cap.records()
    assert row["kind"] == "agent_run" and row["purpose"] == "agent_external"
    assert row["backend"] == "agent:hermes" and row["status"] == "ok"
    body = row["body"]
    assert body["request"]["messages"] == [{"role": "user", "content": "fix the test"}]
    assert body["response"]["content"]               # the agent's final text


def test_media_job_records_metadata_and_hashes_only(app, client):
    svc = app.state.services
    saved = svc.config_store.config.training_capture
    svc.config_store.config.training_capture = TrainingCaptureConfig(enabled=True,
                                                                     include_unbound=True)
    try:
        C.reset_request()
        jid = svc.media.new_tool_row("tool", "comfy", "run", {"prompt": "a red fox"})
        art = [{"id": f"{jid}-0", "mime": "image/png", "bytes": 12, "sha256": "ab" * 32}]
        svc.media.finish_tool_row(jid, status="succeeded", artifacts=art)
        row, = svc.capture.records(kind="media_job")
        assert row["purpose"] == "media" and row["status"] == "ok"
        assert row["body"]["response"]["artifacts"][0]["sha256"] == "ab" * 32
        assert "comfy/run" in row["body"]["request"]["prompt"]
        assert "b64" not in json.dumps(row["body"])
    finally:
        svc.config_store.config.training_capture = saved


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000


def test_inline_images_become_references_with_bytes_on_disk(tmp_path):
    cap = _cap(tmp_path)
    C.reset_request()
    url = "data:image/png;base64," + base64.b64encode(PNG).decode()
    msgs = [{"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": url}}]},
            {"role": "user", "content": "and", "images": [base64.b64encode(PNG).decode()]}]
    tok = cap.begin_call("strata", persona="")
    cap.finish_call(tok, model="m", backend="strata", messages=msgs, tools=None, settings={},
                    result=ChatResult(content="a fox"))
    row, = cap.records()
    got = row["body"]["request"]["messages"]
    ref = got[0]["content"][1]["image_url"]["url"]
    assert ref.startswith(C.IMAGE_REF) and ref.endswith(f":image/png:{len(PNG)}")
    assert got[1]["images"][0] == ref               # the same bytes, the same reference
    assert row["body"]["images_externalized"] == 2 and not row["truncated"]
    sha = ref.split(":")[2]
    assert (cap.store.path.parent / "capture-media" / f"{sha}.png").read_bytes() == PNG
    # small inline images stay inline
    small = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    out, n = C.externalize_images({"url": small}, None)
    assert n == 0 and out["url"] == small


def test_a_store_written_before_purposes_is_migrated(tmp_path):
    path = tmp_path / "training" / "capture.sqlite"
    path.parent.mkdir(parents=True)
    old = C.SCHEMA.replace("CREATE INDEX IF NOT EXISTS idx_capture_purpose", "--")
    con = sqlite3.connect(path)
    con.executescript(old)
    con.execute("INSERT INTO capture_records (record_uid, ts, kind, body, redaction) "
                "VALUES ('u1', 1.0, 'backend_call', '{}', '{}')")
    con.commit()
    con.close()
    assert "purpose" not in {r[1] for r in sqlite3.connect(path).execute(
        "PRAGMA table_info(capture_records)")}
    cap = _cap(tmp_path)
    row, = cap.records()
    assert row["purpose"] == "agent_turn"            # old rows were all agent turns
    assert [r["record_uid"] for r in cap.records(purpose="agent_turn")] == ["u1"]


def test_training_tab_saves_purposes_and_the_operator_chat_switch(app, client):
    svc = app.state.services
    saved = svc.config_store.config.training_capture
    try:
        r = client.post("/admin/api/training/capture", json={
            "enabled": True, "purposes": ["agent_turn", "brain_route"], "operator_chat": True})
        assert r.status_code == 200
        cfg = svc.config_store.config.training_capture
        assert cfg.purposes == ["agent_turn", "brain_route"] and cfg.operator_chat is True
        st = r.json()["capture"]
        assert st["purposes"] == ["agent_turn", "brain_route"] and st["operator_chat"] is True
        # a purpose list does not switch the operator chat off: its own box decides
        C.reset_request()
        with C.purpose("operator_chat"):
            assert svc.capture.allowed("strata", "") is True
        assert client.post("/admin/api/training/capture",
                           json={"purposes": "agent_turn"}).status_code == 400
    finally:
        svc.config_store.config.training_capture = saved

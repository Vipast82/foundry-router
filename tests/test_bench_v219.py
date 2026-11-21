"""v0.181.219: what the brain lane's profile lost to the router and the budget.

Read case by case, its 83.9 against flash's 93.0 had two causes that were not
the model. All 11 code-reasoning misses were EMPTY answers at ~41 s - 2,048
tokens of thinking at its decode speed, cut off before it said anything. And
three cases (one after the full 1,800 s timeout) failed with "<asyncio.locks.
Event ...> is bound to a different event loop": a benchmark runs on its own
asyncio.run() loop in a thread, and shared the server loop's httpx client.
"""
import asyncio
import json
import threading
from pathlib import Path

import httpx

from foundry_router import bench, benchcard
from foundry_router.db import Database
from foundry_router.loopclient import LoopLocalClient, close_loop_client, run_on_own_loop
from foundry_router.pool.protocols import ChatResult


# -- one client per loop ---------------------------------------------------------------

def test_each_loop_gets_its_own_client_and_the_server_loop_keeps_its_own():
    made = []

    def factory():
        c = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        made.append(c)
        return c

    http = LoopLocalClient(factory)

    async def which():
        return http.for_loop(), http.for_loop()

    a1, a2 = asyncio.run(which())
    assert a1 is a2, "one loop, one client"
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("b", asyncio.run(which())[0]))
    t.start()
    t.join()
    assert out["b"] is not a1, "a benchmark thread's loop never shares the server's"
    # both loops have ended, so their clients are dropped the next time
    asyncio.run(which())
    assert http.loops() == 1


def test_calls_go_through_to_the_loops_own_client():
    http = LoopLocalClient(lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": r.url.path}))))

    async def go():
        r = await http.get("http://backend/v1/models")
        assert http.is_closed is False
        await close_loop_client(http)
        return r.json()

    assert asyncio.run(go()) == {"ok": "/v1/models"}
    assert http.loops() == 0, "closed on the loop that used it"


def test_two_loops_at_once_against_one_real_server_both_answer():
    """The fault itself: the server loop holds the only connection while a
    benchmark thread's loop asks too. One shared AsyncClient made the second
    wait on the first loop's Event; with a client per loop both just answer."""
    import http.server
    import socketserver
    import time

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            time.sleep(0.3)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    http = LoopLocalClient(lambda: httpx.AsyncClient(
        limits=httpx.Limits(max_connections=1), timeout=10.0))
    results, errors = [], []

    def worker():
        async def many():
            for _ in range(3):
                results.append((await http.get(url)).status_code)
            await close_loop_client(http)
        try:
            asyncio.run(many())
        except Exception as e:                                    # noqa: BLE001
            errors.append(repr(e))

    try:
        ts = [threading.Thread(target=worker) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
    finally:
        srv.shutdown()
    assert errors == []
    assert results == [200] * 9


def test_every_background_run_closes_its_loops_client():
    closed = []

    class Fake(LoopLocalClient):
        async def aclose(self):
            closed.append(True)

    svc = type("S", (), {"http": Fake(lambda: None)})()

    async def run():
        return 7

    assert run_on_own_loop(svc, run()) == 7 and closed == [True]
    root = Path(bench.__file__).parent
    for name in ("bench.py", "benchprofile.py", "judgeproto.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "run_on_own_loop(svc," in src and "asyncio.run(" not in src, name


def test_the_service_uses_a_client_per_loop():
    src = (Path(bench.__file__).parent / "main.py").read_text(encoding="utf-8")
    assert "self.http = LoopLocalClient(" in src


# -- room to think -------------------------------------------------------------------------

class _Pool:
    def __init__(self, *results):
        self.results = list(results)
        self.asked = []

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        self.asked.append(max_tokens)
        return self.results.pop(0), "brain-lane"


def _svc(tmp_path, pool):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _thought(n):
    return ChatResult(content="", thinking="hmm " * 50, finish_reason="length",
                      completion_tokens=n)


def test_an_answer_cut_off_while_thinking_is_asked_again_with_room(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)
    pool = _Pool(_thought(2048), ChatResult(content="(6, 2)", finish_reason="stop",
                                            completion_tokens=3100))
    got = asyncio.run(bench.ask(_svc(tmp_path, pool), "brain", {"messages": []}))
    assert pool.asked == [2048, bench.THINK_ROOM]
    assert got["content"] == "(6, 2)" and got["out_of_room"] == 0
    assert got["budget"] == bench.THINK_ROOM


def test_a_short_or_finished_answer_is_asked_once(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)
    for r in (ChatResult(content="(6, 2)", finish_reason="length", completion_tokens=2048),
              ChatResult(content="", tool_calls=[{"name": "read"}], finish_reason="length"),
              ChatResult(content="", finish_reason="stop", completion_tokens=12)):
        pool = _Pool(r)
        asyncio.run(bench.ask(_svc(tmp_path, pool), "m", {"messages": []}))
        assert pool.asked == [2048], r
    # a backend that sends no reason: every token used and nothing said
    assert bench.out_of_room(ChatResult(content="<think>x</think>", completion_tokens=2045), 2048)


def test_out_of_room_twice_is_its_own_miss_not_a_wrong_value(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)
    pool = _Pool(_thought(2048), _thought(8192))
    svc = _svc(tmp_path, pool)
    d = bench._dir(svc)
    case = {"id": "CODE-closure_counters", "task_id": "closure_counters", "tags": ["code"],
            "messages": [{"role": "user", "content": "what does it print?"}], "tools": [],
            "reference": {"must_include": ["(6, 2)"]}}
    (d / "codereason.jsonl").write_text(json.dumps(case) + "\n", encoding="utf-8")
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    out = asyncio.run(bench._run(svc, "codereason", "brain", "", 0))
    row = svc.db.query_one("SELECT * FROM bench_results WHERE run_id=?", (out["run_id"],))
    assert row["score"] == 0.0
    assert row["code_detail"].startswith("out_of_room=8192")
    assert benchcard.miss_kind("codereason", row) == "ran out of tokens before answering"


def test_the_finder_path_gets_room_too(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)
    pool = _Pool(_thought(2048), _thought(8192))
    svc = _svc(tmp_path, pool)
    svc.tool_registry = type("R", (), {"mcp_tools_for_persona": staticmethod(lambda p: [])})()
    got = asyncio.run(bench.ask_through_finder(
        svc, "brain", {"messages": [], "reference": {"finder_tool": "x"}}))
    assert pool.asked == [2048, bench.THINK_ROOM] and got["out_of_room"] == bench.THINK_ROOM


def test_a_finder_miss_shows_what_each_search_showed(tmp_path, monkeypatch):
    """Two brain-lane misses searched "video generation", and the live finder
    ranks the wanted video tool 6th and 8th of 8 in a tie - whether it was
    cut off at run time could not be told from the record."""
    from types import SimpleNamespace as NS

    from foundry_router import tool_finder
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)
    tools = [NS(name=n, mcp_tool=n, server="Media_Gen", description=d, kind="mcp",
                parameters={"type": "object", "properties": {}})
             for n, d in (("generate_image", "Create an image"),
                          ("create_music_video", "A music video"),
                          ("create_tutorial_video", "Narrated how-to from screenshots"))]
    monkeypatch.setattr(bench, "_granted_tools", lambda svc, p=None: tools)
    search = ChatResult(content="", tool_calls=[{"name": tool_finder.FIND_TOOL,
                                                 "arguments": {"query": "image generation"}}])
    pool = _Pool(search, ChatResult(content="I could not find it.", finish_reason="stop"))
    got = asyncio.run(bench.ask_through_finder(
        _svc(tmp_path, pool), "brain",
        {"messages": [], "reference": {"finder_tool": "create_tutorial_video"}}))
    assert got["finder"]["shown"] == [["generate_image"]] and got["finder"]["tools"] == 3
    text = bench.answer_of(got)
    assert '"image generation" showed: generate_image' in text and "(out of 3 tools)" in text

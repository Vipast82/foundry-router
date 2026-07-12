"""Observed-telemetry scoring: signal the router already collects (per-model
tool-call validity, and warm inference tokens/sec) folded into the registry as
measured/observed benchmark rows — more trustworthy than the estimated/
community_report rows that dominate the registry, and free to capture.

Key constraint under test: WARM eval time feeds scoring; cold load_duration
NEVER does (a shared pool means most workers aren't resident on any given call,
so raw latency would punish a model for not happening to be warm)."""

import pytest

from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult, OllamaProtocol
from foundry_router.registry.models_db import (LATENCY_TARGET_TPS, ModelRegistry,
                                              observed_confidence)


# -- confidence scaling -----------------------------------------------------------

def test_observed_confidence_scales_with_sample_size():
    assert observed_confidence(0) == 0.0
    assert observed_confidence(2) < observed_confidence(10) < observed_confidence(200)
    assert observed_confidence(10) == pytest.approx(0.5, abs=0.01)
    assert observed_confidence(10_000) <= 0.95     # never certain


# -- tool-calling reliability -> benchmark ----------------------------------------

def _reg(tmp_path, name="t.sqlite"):
    return ModelRegistry(Database(tmp_path / name))


def test_record_tool_call_writes_observed_benchmark(tmp_path):
    reg = _reg(tmp_path)
    for _ in range(9):
        reg.record_tool_call("m", ok=True)
    reg.record_tool_call("m", ok=False)  # 9/10 ok
    bench = [b for b in reg.benchmarks("m") if b["category"] == "tool_calling"]
    assert len(bench) == 1                              # replaced, not duplicated
    b = bench[0]
    assert b["score"] == pytest.approx(90.0)
    assert b["score_type"] == "measured" and b["source_type"] == "observed"
    assert b["confidence"] == observed_confidence(10)


def test_tool_calling_benchmark_stays_current_not_duplicated(tmp_path):
    reg = _reg(tmp_path)
    for _ in range(5):
        reg.record_tool_call("m", ok=True)
    reg.record_tool_call("m", ok=False)
    rows = [b for b in reg.benchmarks("m") if b["category"] == "tool_calling"]
    assert len(rows) == 1
    assert rows[0]["score"] == pytest.approx(100.0 * 5 / 6)


def test_more_samples_earn_more_confidence(tmp_path):
    a, b = _reg(tmp_path, "a.sqlite"), _reg(tmp_path, "b.sqlite")
    for _ in range(2):
        a.record_tool_call("x", ok=True)          # 2/2 lucky
    for _ in range(200):
        b.record_tool_call("x", ok=True)          # 200/200
    ca = [r for r in a.benchmarks("x") if r["category"] == "tool_calling"][0]["confidence"]
    cb = [r for r in b.benchmarks("x") if r["category"] == "tool_calling"][0]["confidence"]
    assert cb > ca                                # the 200-call model is trusted more


# -- warm latency -> benchmark; cold load excluded --------------------------------

def test_note_inference_records_warm_tps_and_latency_benchmark(tmp_path):
    reg = _reg(tmp_path)
    # 120 tokens over 2.0s warm = 60 tok/s == LATENCY_TARGET_TPS -> score 100
    reg.note_inference("m", eval_count=120, eval_duration_ns=2_000_000_000)
    row = reg.get("m")
    assert row["eval_tps_avg"] == pytest.approx(60.0)
    assert row["eval_samples"] == 1
    bench = [b for b in reg.benchmarks("m") if b["category"] == "latency"][0]
    assert bench["score"] == pytest.approx(100.0)
    assert bench["source_type"] == "observed" and bench["score_type"] == "measured"


def test_latency_score_scales_and_caps(tmp_path):
    reg = _reg(tmp_path)
    # 30 tok/s -> half of target -> score 50
    reg.note_inference("slow", eval_count=30, eval_duration_ns=1_000_000_000)
    slow = [b for b in reg.benchmarks("slow") if b["category"] == "latency"][0]
    assert slow["score"] == pytest.approx(50.0)
    # 240 tok/s -> 4x target -> capped at 100
    reg.note_inference("fast", eval_count=240, eval_duration_ns=1_000_000_000)
    fast = [b for b in reg.benchmarks("fast") if b["category"] == "latency"][0]
    assert fast["score"] == pytest.approx(100.0)


def test_warm_tps_is_a_running_mean(tmp_path):
    reg = _reg(tmp_path)
    reg.note_inference("m", eval_count=60, eval_duration_ns=1_000_000_000)   # 60 tps
    reg.note_inference("m", eval_count=120, eval_duration_ns=1_000_000_000)  # 120 tps
    row = reg.get("m")
    assert row["eval_tps_avg"] == pytest.approx(90.0)   # mean(60,120)
    assert row["eval_samples"] == 2


def test_cold_load_tracked_separately_never_in_score(tmp_path):
    reg = _reg(tmp_path)
    # A cold call: warm eval is fast (60 tps) but load_duration is huge (70s).
    reg.note_inference("m", eval_count=60, eval_duration_ns=1_000_000_000,
                       load_duration_ns=70_000_000_000)
    row = reg.get("m")
    # cold load lands ONLY in its informational field
    assert row["cold_load_ms_avg"] == pytest.approx(70_000.0)
    assert row["cold_load_samples"] == 1
    # latency score reflects the WARM 60 tps (=100), utterly unaffected by the
    # 70s cold load — the whole point of the constraint
    bench = [b for b in reg.benchmarks("m") if b["category"] == "latency"][0]
    assert bench["score"] == pytest.approx(100.0)
    assert row["eval_tps_avg"] == pytest.approx(60.0)


def test_warm_calls_dont_pollute_cold_load_stat(tmp_path):
    reg = _reg(tmp_path)
    # already-resident model: load_duration 0 -> no cold-load sample recorded
    reg.note_inference("m", eval_count=60, eval_duration_ns=1_000_000_000,
                       load_duration_ns=0)
    row = reg.get("m")
    assert (row["cold_load_samples"] or 0) == 0
    assert row["cold_load_ms_avg"] is None


def test_note_inference_noop_for_non_ollama_zeros(tmp_path):
    reg = _reg(tmp_path)
    # Claude/OpenAI report no timing -> nothing recorded, no benchmark row
    reg.note_inference("claude", eval_count=0, eval_duration_ns=0)
    assert reg.get("claude") is None
    assert reg.benchmarks("claude") == []


# -- protocol carries the timing fields -------------------------------------------

class FakeResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data


class CaptureClient:
    def __init__(self, data):
        self._data = data

    async def post(self, url, json=None, headers=None):
        return FakeResp(self._data)


async def test_ollama_protocol_carries_eval_and_load_durations():
    client = CaptureClient({
        "message": {"content": "hi"},
        "eval_count": 100, "eval_duration": 2_000_000_000,
        "load_duration": 5_000_000_000, "prompt_eval_duration": 300_000_000})
    proto = OllamaProtocol("http://x", None, client)
    r = await proto.chat("m", [{"role": "user", "content": "q"}])
    assert r.eval_duration_ns == 2_000_000_000
    assert r.load_duration_ns == 5_000_000_000
    assert r.prompt_eval_duration_ns == 300_000_000


def test_non_ollama_chatresult_defaults_to_zero_timing():
    # Anthropic/OpenAI adapters never set these — default 0 keeps note_inference
    # a clean no-op for them.
    r = ChatResult(content="x", completion_tokens=10)
    assert r.eval_duration_ns == 0 and r.load_duration_ns == 0

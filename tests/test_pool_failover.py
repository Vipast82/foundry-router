"""Backend Pool failover (design doc §4.3) — priority ordering, failure
threshold, unhealthy marking, and model-map exclusion."""

import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig
from foundry_router.db import Database
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.internal import InternalPool
from foundry_router.pool.protocols import ChatResult, ProtocolError


class FakeProtocol:
    def __init__(self, name, models, fail=False):
        self.name = name
        self.models = models
        self.fail = fail
        self.calls = 0

    async def list_models(self):
        if self.fail:
            raise ProtocolError(f"{self.name} down")
        return self.models

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096):
        self.calls += 1
        if self.fail:
            raise ProtocolError(f"{self.name} down")
        return ChatResult(content=f"hi from {self.name}", prompt_tokens=1,
                          completion_tokens=2)


@pytest.fixture()
def pool(tmp_path):
    db = Database(tmp_path / "pool.sqlite")
    cfg = BackendPoolConfig(failure_threshold=3, cooldown_seconds=60)
    backends = [
        BackendConfig(name="primary", type="ollama", url="http://a", priority=100),
        BackendConfig(name="secondary", type="ollama", url="http://b", priority=90),
    ]
    p = InternalPool(backends, cfg, client=None, db=db)
    p.backends["primary"].protocol = FakeProtocol("primary", ["m", "only-on-a"])
    p.backends["secondary"].protocol = FakeProtocol("secondary", ["m"])
    return p


async def test_priority_order_and_discovery(pool):
    await pool.check_all()
    assert pool.backends["primary"].healthy
    assert pool.available_models()["m"] == ["primary", "secondary"]
    result, backend = await pool.chat("m", [{"role": "user", "content": "x"}])
    assert backend == "primary"
    assert result.content == "hi from primary"


async def test_failover_to_lower_priority(pool):
    await pool.check_all()
    pool.backends["primary"].protocol.fail = True
    result, backend = await pool.chat("m", [{"role": "user", "content": "x"}])
    assert backend == "secondary"
    assert result.content == "hi from secondary"
    # one live-call failure recorded, but below threshold => still healthy
    assert pool.backends["primary"].consecutive_failures == 1
    assert pool.backends["primary"].healthy


async def test_unhealthy_after_threshold_and_model_map_excludes(pool):
    await pool.check_all()
    pool.backends["primary"].protocol.fail = True
    for _ in range(3):  # failure_threshold = 3
        await pool.chat("m", [{"role": "user", "content": "x"}])
    assert not pool.backends["primary"].healthy
    # §4.2: tool removal grace == this same threshold; once unhealthy, its
    # exclusive models drop out of the available map
    assert "only-on-a" not in pool.available_models()
    assert "m" in pool.available_models()  # still served by secondary


async def test_all_backends_failed_raises(pool):
    await pool.check_all()
    pool.backends["primary"].protocol.fail = True
    pool.backends["secondary"].protocol.fail = True
    with pytest.raises(AllBackendsFailed):
        await pool.chat("m", [{"role": "user", "content": "x"}])


async def test_unknown_model_raises(pool):
    await pool.check_all()
    with pytest.raises(AllBackendsFailed):
        await pool.chat("nope", [{"role": "user", "content": "x"}])

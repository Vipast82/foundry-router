"""v0.181.193: the eval per-prompt timeout is a setting, not a constant.

It was `PER_PROMPT_TIMEOUT = 420` in the module, which was fine until the local
personas were given tools. A persona that searches before it answers takes
longer, and in the first five-repeat campaign twelve prompts got no answer inside
it - every one of the twelve an honesty prompt, the category where searching and
finding nothing is the correct answer.

That makes it the number deciding how much of the hardest category gets scored at
all, since an unanswered prompt counts against one of the board's two rates and
is left out of the other. A number like that belongs in the console.
"""
import pytest

from foundry_router import evalharness as E
from foundry_router.config import EvalsConfig


class _Store:
    def __init__(self, seconds=None):
        self.config = type("C", (), {
            "evals": EvalsConfig(**({"per_prompt_timeout": seconds}
                                    if seconds else {}))})()


def _svc(seconds=None):
    return type("S", (), {"config_store": _Store(seconds)})()


def test_the_default_is_the_old_constant():
    """So the change moves nothing until somebody asks it to."""
    assert EvalsConfig().per_prompt_timeout == 420
    assert E.per_prompt_timeout(_svc()) == 420


def test_the_setting_is_used_when_it_is_set():
    assert E.per_prompt_timeout(_svc(900)) == 900


@pytest.mark.parametrize("bad", [0, 29, 3601, -1])
def test_a_timeout_outside_the_range_is_refused(bad):
    """Thirty seconds is too short for a cold load and an hour is long enough
    that a stuck run looks like a slow one."""
    with pytest.raises(Exception):
        EvalsConfig(per_prompt_timeout=bad)


def test_a_service_with_no_config_falls_back_rather_than_failing():
    """Several tests build a bare service object; the harness must not need a
    config store to run at all."""
    assert E.per_prompt_timeout(type("S", (), {})()) == 420
    assert E.per_prompt_timeout(None) == 420


def test_the_run_reads_it_once_rather_than_per_prompt():
    """Two prompts of one run answering to different limits would make the
    run's own results incomparable."""
    import inspect
    src = inspect.getsource(E.EvalHarness.run)
    assert "limit = per_prompt_timeout(self.svc)" in src
    assert "timeout=limit" in src
    assert "timeout=PER_PROMPT_TIMEOUT" not in src

"""2026-10-31 GF-SEC3: task_base pointed into a failed track; the sprint restarted on main
(base_sha) and every verification failed coordinator.history ("history rewritten")."""
from types import SimpleNamespace

import pytest

from foundry_coordinator.engine import Coordinator


class _Stop(Exception):
    pass


def _run(task_base, base_sha, ancestors):
    seen = {}

    def commits(base, head):
        seen["base"] = base
        raise _Stop

    ws = SimpleNamespace(is_ancestor=lambda a, b: a in ancestors, commits=commits)
    P = SimpleNamespace(ws=ws, adapter=SimpleNamespace(exempt_paths=lambda spec: []))
    me = SimpleNamespace(tracks=SimpleNamespace(effective_base=lambda P, b, h: b))
    t = {"base_sha": base_sha, "data": {"task_base": task_base}}
    try:
        out = Coordinator._generic_checks_uncached(me, P, t, None, "head", "verify")
    except _Stop:
        return "PASS", seen["base"]
    return out[0].result, None


def test_a_task_base_left_on_a_failed_track_falls_back_to_the_main_base():
    assert _run("trackbase", "mainbase", {"mainbase"}) == ("PASS", "mainbase")


def test_history_still_fails_when_neither_base_is_an_ancestor():
    res, _ = _run("trackbase", "mainbase", set())
    assert res == "FAIL"


def test_a_valid_task_base_is_kept():
    assert _run("taskbase", "mainbase", {"taskbase", "mainbase"}) == ("PASS", "taskbase")

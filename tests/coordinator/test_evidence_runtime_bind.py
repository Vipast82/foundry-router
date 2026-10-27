"""0.147.0: a Studio record whose runtime has no loaded fingerprint borrows the
fingerprint of the nearest verifyLive PASS of the same session, against identical
code, within the window, with no failed verifyLive in between (GF-GATE1 printed
boot, suites and verifyLive in separate calls, each naming its own runtime, so its
boot and suite records were never fresh)."""

import pytest

from foundry_coordinator.evidence import EvidenceLedger
from foundry_coordinator.store import Store

from .fixtures_evidence import FakeAdapter, commit, make_P, make_ws, spec


@pytest.fixture()
def env(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    led = EvidenceLedger(store, str(tmp_path / "state"), clock=lambda: 50_000.0)
    ws, r1 = make_ws(tmp_path, {"src/a.lua": "a", "docs/x.md": "x"})
    ad = FakeAdapter(deps={"boot:server": ["src/**"], "runtime:verify_live": ["src/**"]})
    P = make_P("p", ws, {}, ad)
    led._runtime(P, "", {"runtime_id": "rt-fp", "kind": "studio", "loaded_fp": "FP1",
                         "place_id": "77"})
    led._runtime(P, "", {"runtime_id": "rt-boot", "kind": "studio", "place_id": "77"})
    return led, P, ws, r1


def rec(led, cid, rev, rid, ts, sid="s1", result="PASS", truncated=False):
    rid_, _ = led.record(project_id="p", task_id="T1", session_id=sid, call_id=f"{cid}@{ts}",
                         check_id=cid, result=result, tier="observed", source_revision=rev,
                         runtime_id=rid, started_ts=ts, ended_ts=ts, exec_status="completed",
                         truncated=truncated, check_kind="boot")
    return led.get(rid_)


def fresh(led, P, r, rev):
    return led.freshness(P, r, rev, spec(), needs_runtime=True)


def test_a_boot_record_binds_to_the_sessions_verify_live(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000)
    boot = rec(led, "boot:server", r1, "rt-boot", 1013)
    st, why = fresh(led, P, boot, r1)
    assert st == "fresh", why
    assert "runtime bound to rt-fp" in why and "13 s before" in why
    # a docs-only commit later keeps it fresh at the new head
    r2 = commit(ws, {"docs/x.md": "y"})
    assert fresh(led, P, boot, r2)[0] == "fresh"


def test_no_verify_live_in_the_session_stays_unknown(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000, sid="other-session")
    boot = rec(led, "boot:server", r1, "rt-boot", 1013)
    st, why = fresh(led, P, boot, r1)
    assert st == "unknown" and "no loaded fingerprint" in why


def test_a_failed_verify_live_in_between_blocks_the_binding(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000)
    rec(led, "runtime:verify_live", r1, "rt-fp", 1010, result="FAIL")
    boot = rec(led, "boot:server", r1, "rt-boot", 1013)
    assert fresh(led, P, boot, r1)[0] == "unknown"


def test_a_truncated_verify_live_does_not_bind(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000, truncated=True)
    boot = rec(led, "boot:server", r1, "rt-boot", 1013)
    assert fresh(led, P, boot, r1)[0] == "unknown"


def test_code_changed_between_verify_and_record_does_not_bind(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000)
    r2 = commit(ws, {"src/a.lua": "changed"})
    boot = rec(led, "boot:server", r2, "rt-boot", 1013)
    assert fresh(led, P, boot, r2)[0] == "unknown"


def test_outside_the_window_or_window_off_does_not_bind(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000)
    late = rec(led, "boot:server", r1, "rt-boot", 1000 + 1801)
    assert fresh(led, P, late, r1)[0] == "unknown"
    near = rec(led, "boot:server", r1, "rt-boot", 1005)
    assert fresh(led, P, near, r1)[0] == "fresh"
    P.profile.raw = {"lifecycle": {"runtime_bind_window_s": 0}}
    assert fresh(led, P, near, r1)[0] == "unknown"


def test_a_different_place_does_not_bind(env):
    led, P, ws, r1 = env
    led._runtime(P, "", {"runtime_id": "rt-live", "kind": "studio", "place_id": "999"})
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000)
    boot = rec(led, "boot:server", r1, "rt-live", 1013)
    assert fresh(led, P, boot, r1)[0] == "unknown"


def test_a_bound_fingerprint_still_meets_the_expected_one(env):
    led, P, ws, r1 = env
    rec(led, "runtime:verify_live", r1, "rt-fp", 1000)
    boot = rec(led, "boot:server", r1, "rt-boot", 1013)
    P.adapter.expected = {r1: "FP-OTHER"}
    st, why = fresh(led, P, boot, r1)
    assert st == "stale" and "revision expects FP-OTHER" in why
    P.adapter.expected = {r1: "FP1"}
    assert fresh(led, P, boot, r1)[0] == "fresh"


def test_a_verify_live_after_the_record_also_binds(env):
    led, P, ws, r1 = env
    boot = rec(led, "boot:server", r1, "rt-boot", 1000)
    rec(led, "runtime:verify_live", r1, "rt-fp", 1066)
    st, why = fresh(led, P, boot, r1)
    assert st == "fresh" and "66 s after" in why

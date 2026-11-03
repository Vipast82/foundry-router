"""Owner Studio reservation (v0.181.21). 2026-11-02 GF-RT4 live test: a hand-written studio.lock
was judged stale after 30 minutes and broken; a track merge's Studio batch then ran on the owner's
Studio, went red and reopened two finished sprints. The reservation lives in the coordinator's store."""
from foundry_coordinator import states as S
from foundry_coordinator import studio_reserve as SR
from tests.coordinator.helpers import coordinator
from tests.coordinator.test_engine import game, write_ok  # noqa: F401  (fixture)


def test_a_reservation_has_an_end_and_a_release(game):              # noqa: F811
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    assert SR.get(c.store, "game") is None and SR.blocks(c, P) == ""
    r = SR.reserve(c, "game", 30, "two-account live test", "victor")
    assert "Studio reserved for the owner" in SR.blocks(c, P)
    assert SR.get(c.store, "game", now=r["until"] + 1) is None        # expired
    assert SR.release(c, "game", "victor") is True
    assert SR.blocks(c, P) == "" and SR.release(c, "game") is False


def test_no_main_session_starts_and_no_merge_takes_studio_while_reserved(game):  # noqa: F811
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", state=S.READY)
    SR.reserve(c, "game", 60, "live test", "victor")
    c._start_implementation(P, c.store.task("game", "T1"))
    t1 = c.store.task("game", "T1")
    assert t1["state"] == S.READY and "Studio reserved for the owner" in (t1["next_action"] or "")
    assert not c.store.holds("game", "T1")
    ok, why = c.tracks.resources.admit_studio(P, "audio", "merge Studio batch")
    assert not ok and "Studio reserved for the owner" in why
    SR.release(c, "game", "victor")
    ok, why = c.tracks.resources.admit_studio(P, "audio", "merge Studio batch")
    assert "Studio reserved" not in why, why


def test_a_writing_main_task_is_held_by_the_reservation_and_freed_with_it(game):  # noqa: F811
    tmp, _, _, prof = game
    c = coordinator(tmp, [prof], {"game": write_ok("src/b.txt")})
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)
    r = SR.reserve(c, "game", 60, "live test", "victor")
    assert r["held"] == ["T1"]
    t1 = c.store.task("game", "T1")
    assert t1["state"] == S.PAUSED and t1["data"]["hold"]["owner"] == "studio_reserve:game"
    assert SR._owner_alive(c, "game") is True
    SR.release(c, "game", "victor")
    assert SR._owner_alive(c, "game") is False                       # the sweep resumes it

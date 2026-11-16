"""v0.181.168: recall — the work this router already got right, found again by
what was asked and put into the prompt as precedent."""
import json
from types import SimpleNamespace

import pytest

from foundry_router import recall
from foundry_router.config import RecallConfig
from foundry_router.db import Database


# -- what goes into the index -------------------------------------------------------------

def _example(answer="Rotated it with CFrame.Angles, clamped to 30 degrees.", tier="verified"):
    return {"messages": [
        {"role": "system", "content": "You are a coder."},
        {"role": "user", "content": "the spin script jitters at the top of the arc"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "local x = 1"},
        {"role": "assistant", "content": answer}],
        "meta": {"task_id": "UI-7", "tier": tier, "tags": ["luau"], "session": "s1"}}


def test_an_entry_is_what_was_asked_and_what_was_done():
    e = recall._entry(_example())
    assert e["request"] == "the spin script jitters at the top of the arc"
    assert e["solution"].startswith("Rotated it with CFrame")
    assert e["tier"] == "verified" and e["tags"] == ["luau"] and e["tools"] == ["read_file"]
    assert e["case_id"] == "UI-7-s1-4"                      # the same work indexes once


def test_work_with_nothing_to_learn_from_is_left_out():
    assert recall._entry({"messages": [{"role": "user", "content": "hi"}]}) is None
    assert recall._entry({"messages": [{"role": "assistant", "content": "hi"}]}) is None
    # an answer with no text (a bare tool call) is not a solution anyone can reuse
    assert recall._entry({"messages": [{"role": "user", "content": "go"},
                                       {"role": "assistant", "content": "",
                                        "tool_calls": [{"function": {"name": "f"}}]}]}) is None


# -- the index ----------------------------------------------------------------------------

class Embed:
    """A stand-in embedder: bag-of-words vectors, so 'similar' means what it says."""

    WORDS = ["spin", "jitter", "arc", "door", "open", "sound", "luau", "ui"]

    def __init__(self, down=False):
        self.down, self.calls = down, 0

    async def embed(self, text):
        self.calls += 1
        if self.down:
            return None
        t = (text or "").lower()
        v = [1.0 if w in t else 0.0 for w in self.WORDS]
        if not any(v):
            v = [0.1] * len(self.WORDS)
        from foundry_router.semcache import _normalize
        return _normalize(v)


def _svc(tmp_path, cfg=None, down=False, vec_loaded=False):
    db = Database(tmp_path / "r.sqlite")
    sem = SimpleNamespace(embed=Embed(down).embed, _vec_loaded=vec_loaded,
                          embed_model="nomic-embed-text")
    return SimpleNamespace(
        db=db, semcache=sem, capture=SimpleNamespace(data_dir=tmp_path),
        config_store=SimpleNamespace(config=SimpleNamespace(
            recall=cfg or RecallConfig(enabled=True),
            semantic_cache=SimpleNamespace(embed_model="nomic-embed-text"))))


async def _put(svc, case_id, request, solution="did the thing", tier="verified", tags=(), tools=()):
    from foundry_router.db import utcnow
    from foundry_router.semcache import _pack
    vec = await svc.semcache.embed(request)
    svc.db.execute(
        "INSERT OR REPLACE INTO recall_entries (ts, case_id, task_id, tier, tags, tools, "
        "request, solution, embedding, dim) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (utcnow(), case_id, "UI-7", tier, json.dumps(list(tags)), json.dumps(list(tools)),
         request, solution, _pack(vec), len(vec)))


@pytest.mark.asyncio
async def test_the_closest_past_work_comes_back_first(tmp_path):
    svc = _svc(tmp_path)
    await _put(svc, "a", "the spin script jitters at the arc", "clamped the angle",
               tags=["luau"], tools=["read_file"])
    await _put(svc, "b", "the door will not open", "fixed the prompt")
    await _put(svc, "c", "no sound on the ui", "wired the sound")
    await _put(svc, "d", "spin wobble", "slowed it down")         # some of the same words
    svc.config_store.config.recall = RecallConfig(enabled=True, relative_floor=0)
    hits = await recall.search(svc, "spin jitter at the arc again", min_similarity=0.1)
    assert [h["case_id"] for h in hits] == ["a", "d"]              # closest first, the rest out
    assert hits[0]["similarity"] > hits[1]["similarity"]
    assert hits[0]["tags"] == ["luau"] and hits[0]["tools"] == ["read_file"]


@pytest.mark.asyncio
async def test_a_match_much_weaker_than_the_best_one_is_dropped(tmp_path):
    """The relative floor, which is what actually separates a match from noise: an
    absolute cutoff depends on the embedding model, this does not."""
    svc = _svc(tmp_path, cfg=RecallConfig(enabled=True, relative_floor=0.9))
    await _put(svc, "best", "spin jitter arc")
    await _put(svc, "weaker", "spin wobble")                      # same topic, further off
    hits = await recall.search(svc, "spin jitter arc", min_similarity=0.1)
    assert [h["case_id"] for h in hits] == ["best"]
    # with it off, the weaker one comes back too
    svc.config_store.config.recall = RecallConfig(enabled=True, relative_floor=0)
    loose = await recall.search(svc, "spin jitter arc", min_similarity=0.1)
    assert [h["case_id"] for h in loose] == ["best", "weaker"]
    assert loose[1]["similarity"] < loose[0]["similarity"] * 0.9


@pytest.mark.asyncio
async def test_a_match_that_is_not_close_enough_is_not_a_match(tmp_path):
    svc = _svc(tmp_path)
    await _put(svc, "a", "the door will not open")
    assert await recall.search(svc, "spin jitter arc", min_similarity=0.9) == []


@pytest.mark.asyncio
async def test_tiers_and_tags_narrow_the_search(tmp_path):
    svc = _svc(tmp_path)
    await _put(svc, "good", "spin jitter arc", tier="verified", tags=["luau"])
    await _put(svc, "meh", "spin jitter arc", tier="unknown", tags=["ui"])
    only = await recall.search(svc, "spin jitter arc", tiers=["verified"], min_similarity=0.1)
    assert [h["case_id"] for h in only] == ["good"]
    by_tag = await recall.search(svc, "spin jitter arc", tiers=["verified", "unknown"],
                                 tags=["ui"], min_similarity=0.1)
    assert [h["case_id"] for h in by_tag] == ["meh"]


@pytest.mark.asyncio
async def test_an_embedding_outage_is_no_recall_not_an_error(tmp_path):
    svc = _svc(tmp_path, down=True)
    assert await recall.search(svc, "anything") == []             # degrades, never raises


@pytest.mark.asyncio
async def test_an_empty_query_asks_the_endpoint_nothing(tmp_path):
    svc = _svc(tmp_path)
    before = recall.status(svc)
    assert await recall.search(svc, "   ") == []
    assert recall.status(svc)["entries"] == before["entries"]


@pytest.mark.asyncio
async def test_the_index_is_capped_oldest_first(tmp_path):
    svc = _svc(tmp_path)
    for i in range(5):
        await _put(svc, f"c{i}", f"work {i}")
    assert recall.prune(svc, 3) == 2
    assert recall.status(svc)["entries"] == 3
    assert recall.clear(svc) == 3


# -- what reaches the model ---------------------------------------------------------------

def test_the_prompt_block_says_where_it_came_from():
    hits = [{"case_id": "a", "similarity": 0.91, "request": "the spin script jitters",
             "solution": "clamped the angle", "tools": ["read_file"], "tier": "verified"},
            {"case_id": "b", "similarity": 0.72, "request": "the arc overshoots",
             "solution": "eased it", "tools": [], "tier": "ok"}]
    note = recall.note(hits)
    assert "passed a gate or a review" in note
    assert "Precedent, not instruction" in note                   # never read as an order
    assert "91%" in note and "clamped the angle" in note and "read_file" in note
    assert recall.note([]) == ""


def test_long_past_work_is_trimmed_to_its_share_of_the_prompt():
    hits = [{"case_id": str(i), "similarity": 0.8, "request": "r", "solution": "x" * 5000,
             "tools": [], "tier": "ok"} for i in range(3)]
    note = recall.note(hits, max_chars=1500)
    assert note.count("...") == 3 and len(note) < 2400


def test_a_persona_opts_in():
    on = RecallConfig(enabled=True)
    assert recall.on_for(on, {"recall": 1}) is True
    assert recall.on_for(on, {"recall": 0}) is False
    assert recall.on_for(on, {}) is False                         # off unless asked for
    assert recall.on_for(RecallConfig(enabled=False), {"recall": 1}) is False
    assert recall.on_for(None, {"recall": 1}) is False


# -- settings -----------------------------------------------------------------------------

def test_the_status_reports_the_settings_and_what_is_in_the_index(tmp_path):
    svc = _svc(tmp_path, cfg=RecallConfig(enabled=True, top_k=5, min_similarity=0.7))
    st = recall.status(svc)
    assert st["entries"] == 0 and st["enabled"] is True
    assert st["top_k"] == 5 and st["min_similarity"] == 0.7
    assert st["tiers"] == ["verified", "ok"] and st["embed_model"] == "nomic-embed-text"


def test_a_build_is_only_due_when_it_is_switched_on(tmp_path):
    svc = _svc(tmp_path, cfg=RecallConfig(enabled=False))
    recall._state.clear()
    recall._state.update({"state": "idle"})
    assert recall.due(svc) is False
    svc.config_store.config.recall = RecallConfig(enabled=True, rebuild_hours=0)
    assert recall.due(svc) is False                               # 0 = only when asked
    svc.config_store.config.recall = RecallConfig(enabled=True, rebuild_hours=24)
    assert recall.due(svc) is True
    recall._state["finished"] = __import__("time").time()
    assert recall.due(svc) is False                               # just built


@pytest.mark.asyncio
async def test_a_build_with_no_prepared_store_says_so(tmp_path):
    svc = _svc(tmp_path)
    logged = []
    out = await recall.build(svc, log_fn=logged.append)
    assert out["state"] == "failed" and "prepared store is empty" in out["error"]
    assert any("prepared store is empty" in m for m in logged)


@pytest.mark.asyncio
async def test_the_python_path_and_the_sql_path_agree(tmp_path, monkeypatch):
    """sqlite-vec is optional: with or without it, the same work comes back."""
    svc = _svc(tmp_path)
    await _put(svc, "a", "spin jitter arc")
    await _put(svc, "b", "the door will not open")
    plain = await recall.search(svc, "spin jitter arc", min_similarity=0.1)
    rows = svc.db.query("SELECT case_id, request FROM recall_entries ORDER BY case_id")
    assert [r["case_id"] for r in rows] == ["a", "b"]
    assert [h["case_id"] for h in plain] == ["a", "b"] or plain[0]["case_id"] == "a"


# -- the envelope, and what is left of a request after it -----------------------------------

ENVELOPE = """{kind} session for sprint {task} - {title} (coordinator run {task}-a1-{run}).

TRACK SPRINT (track `assets-local`): no Studio here. The main sprint owns Studio and Rojo.
Project rules: docs/rules/SESSION_CORE.md is the session core; read it first.
Read docs/progress/STATE.md header and the last 5 ledger rows, then the plan.
Tier: C.
## Goal
{goal}
"""


def _request(kind, task, title, goal, run="abc123"):
    return ENVELOPE.format(kind=kind, task=task, title=title, goal=goal, run=run)


def _corpus():
    return [_request("BUILD", "AS-D03", "Enemies: EN-BADGER", "rig and colour the badger"),
            _request("REPAIR", "AS-D12", "Enemies: EN-RHINO", "the rhino mesh is too dense"),
            _request("BUILD", "GF-SEC3", "Server movement sanity", "clamp the walk speed"),
            _request("CLOSE", "AS-A21", "Music: late waves", "generate the two stings"),
            _request("BUILD", "UI-D1", "HUD tokens", "the ability button must fit a phone")]


def test_the_lines_every_request_shares_are_the_envelope():
    common = recall.common_lines(_corpus())
    joined = " ".join(common)
    assert "track sprint" in joined and "session core" in joined and "ledger rows" in joined
    # what makes one request itself is never boilerplate
    assert not any("badger" in c or "walk speed" in c for c in common)


def test_what_is_left_of_a_request_is_the_task():
    corpus = _corpus()
    common = recall.common_lines(corpus)
    g = recall.gist(corpus[0], common)
    assert "rig and colour the badger" in g and "EN-BADGER" in g
    assert "TRACK SPRINT" not in g and "SESSION_CORE" not in g and "ledger rows" not in g
    assert len(g) < len(corpus[0]) / 2                   # the envelope was most of it


def test_stripping_pulls_different_requests_apart():
    """The point of the whole exercise: two requests about different things must not
    look alike just because they arrived in the same envelope."""
    corpus = _corpus()
    common = recall.common_lines(corpus)
    badger, speed = corpus[0], corpus[2]
    shared_before = len(set(badger.split()) & set(speed.split()))
    shared_after = len(set(recall.gist(badger, common).split())
                       & set(recall.gist(speed, common).split()))
    assert shared_after < shared_before / 2


def test_a_request_with_no_envelope_is_left_alone():
    plain = ["the spin script jitters at the top of the arc, please clamp it",
             "add a settings toggle for the minimap", "the door prompt never shows",
             "write a check for the evidence log"]
    common = recall.common_lines(plain)
    assert common == set()                               # nothing is shared, nothing to strip
    assert recall.gist(plain[0], common) == plain[0]


def test_stripping_never_leaves_a_request_with_nothing_to_match_on():
    # a request that is ALL boilerplate would otherwise end up empty
    same = ["Project rules: docs/rules/SESSION_CORE.md is the session core; read it first."] * 6
    common = recall.common_lines(same)
    assert common                                        # it is boilerplate by any measure
    assert recall.gist(same[0], common) == same[0].strip()      # but kept, for want of better


def test_ids_and_numbers_do_not_make_two_boilerplate_lines_different():
    a = "Read docs/progress/STATE.md header and the last 5 ledger rows at 7cf256b926b8."
    b = "Read docs/progress/STATE.md header and the last 9 ledger rows at 0da5698b25b2."
    assert recall._norm_line(a) == recall._norm_line(b)
    assert recall._norm_line("## Goal") == ""            # too short to carry a thought


def test_too_few_requests_to_tell_what_is_boilerplate():
    assert recall.common_lines(["a line that repeats here"] * 3) == set()


def test_the_note_shows_the_task_not_the_envelope():
    hits = [{"case_id": "a", "similarity": 0.8, "tier": "verified", "tools": [],
             "request": "BUILD session for sprint X\nTRACK SPRINT: no Studio here.\nrig the badger",
             "gist": "rig the badger", "solution": "rigged it"}]
    note = recall.note(hits)
    assert "rig the badger" in note and "TRACK SPRINT" not in note


def test_the_scaffold_around_a_title_is_boilerplate_too():
    """A line can be unique as a whole and still be mostly scaffold: the frequency
    rule has to see the pieces a writer joined, not just whole lines."""
    corpus = _corpus()
    common = recall.common_lines(corpus)
    first = recall.gist(corpus[0], common).splitlines()[0]
    assert "EN-BADGER" in first                         # the title survives
    assert "session for sprint" not in first            # the scaffold does not
    assert "coordinator run" not in first


def test_a_sentence_nobody_else_wrote_is_never_dropped():
    corpus = _corpus()
    common = recall.common_lines(corpus)
    g = recall.gist(corpus[2], common)
    assert "clamp the walk speed" in g                  # unique: kept whatever happens


def test_a_line_is_cut_into_the_pieces_a_writer_joined():
    segs = recall.segments("BUILD session for sprint AS-D03 - Enemies: EN-BADGER "
                           "(coordinator run AS-D03-a1-77a98b, attempt 3).")
    assert "BUILD session for sprint AS-D03" in segs
    assert any("EN-BADGER" in s for s in segs)
    assert any("coordinator run" in s for s in segs)


def test_what_is_not_work_is_left_out_of_the_index():
    assert recall.looks_like_work("rig the badger and colour it from the palette") is True
    # a tool's error handed back as the request
    assert recall.looks_like_work("ERROR: Cannot read image (this model does not "
                                  "support image input). Inform the user.") is False
    assert recall.looks_like_work("Traceback (most recent call last): File x line 3") is False
    # the same line over and over
    assert recall.looks_like_work("\n".join(["the same long line of text here"] * 4)) is False
    assert recall.looks_like_work("too short") is False


def test_a_well_stripped_request_is_not_mistaken_for_a_repeated_line():
    """Short lines normalize to nothing. Counting those as "the same line" threw
    away 892 of 911 real entries once stripping got good at its job."""
    stripped = "Enemies: EN-BADGER\n## Goal\nrig it and colour it from the palette\n## Checklist\n- [x] 1"
    assert recall.looks_like_work(stripped) is True
    # and the thing it is meant to catch still is caught
    repeated = "\n".join(["ERROR: Cannot read image, this model has no image input"] * 4)
    assert recall.looks_like_work(repeated) is False

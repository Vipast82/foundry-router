"""The training store keeps each message once (v0.181.145): blobs.py, the store's
packing, and the compact command that rebuilds an old store beside itself."""

import json

import pytest

from foundry_training import blobs, compact
from foundry_training.schema import Event
from foundry_training.store import TrainingStore


def _msgs(n, tag="t"):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"{tag} message {i} " + "x" * 300} for i in range(n)]


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "d" * 300,
                                            "parameters": {"type": "object"}}}]


def _call(n, i=0, tag="t"):
    return Event("foundry", f"capture:{tag}-{n}-{i}", "model_call", project_id="p", timestamp=float(n),
                 session_id=f"s-{tag}", actor_role="assistant",
                 facts={"messages": _msgs(n, tag), "tools": TOOLS, "content": f"reply {n}",
                        "settings": {"temperature": 0.2}, "tool_calls": []})


def test_what_goes_in_comes_out(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    b = st.blobs
    obj = {"messages": _msgs(70) + [{"role": "user", "content": "hi"}], "tools": TOOLS,
           "system": "S" * 5000, "small": "kept", "n": 3, "nested": {"messages": _msgs(3, "n")},
           "unicode": "é — 日本", "empty": [], "none": None}
    packed = b.pack(obj)
    text = json.dumps(packed)
    assert "x" * 300 not in text and "S" * 5000 not in text          # the text is elsewhere
    assert packed["small"] == "kept" and packed["messages"][-1] == {"role": "user", "content": "hi"}
    assert blobs.is_ref(packed["tools"]) and blobs.is_ref(packed["system"])
    assert [blobs.is_span(m) for m in packed["messages"][:2]] == [True, True]     # 64 of 70 in two runs
    assert len(packed["messages"]) == 2 + 6 + 1
    assert b.unpack(packed) == obj
    assert b.unpack(b.pack(packed)) == obj                           # packing twice changes nothing
    assert b.unpack({"plain": [1, 2]}) == {"plain": [1, 2]}           # a record never packed
    fresh = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)   # another process, cold cache
    assert fresh.blobs.unpack(packed) == obj
    got = fresh.blobs.unpack(packed)
    got["messages"][0]["content"] = "edited"                          # a caller's edit...
    assert fresh.blobs.unpack(packed) == obj                          # ...never reaches the cache


def test_a_growing_conversation_is_stored_once(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    for n in range(1, 81):                       # 80 calls of one session: 1, 2, ... 80 messages
        st.put_events([_call(n)])
    c = st.blobs.count()
    whole = sum(len(json.dumps(_call(n).facts)) for n in range(1, 81))
    # 80 distinct messages + 1 tool list + 2 runs of 32, not 3,240 messages
    assert c["blobs"] == 80 + 1 + 2
    assert c["stored_bytes"] < whole / 50
    rows = st.norm.q("SELECT SUM(LENGTH(facts)) AS n FROM events")[0]["n"]
    assert rows < whole / 5                    # the rows hold references, not text
    evs = st.events("p")
    assert len(evs) == 80 and evs[-1]["facts"]["messages"] == _msgs(80)
    assert evs[39]["facts"] == json.loads(json.dumps(_call(40).facts))


def test_the_raw_store_shares_the_same_text(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    payload = {"body": {"request": {"messages": _msgs(10), "tools": TOOLS}, "response": {"content": "ok"}}}
    ref = st.put_raw("foundry", "capture:1", "p", payload, {"status": "clean"}, {})
    before = st.blobs.count()["blobs"]
    st.put_events([_call(10)])
    assert st.blobs.count()["blobs"] == before                       # nothing new: same messages, same tools
    row = st.raw.q("SELECT * FROM raw_records")[0]
    assert "x" * 300 not in row["payload"] and st.raw_payload(row) == payload
    from foundry_training.schema import content_hash
    assert ref.endswith(content_hash(payload)[:16])                   # identity is of the record as received


def test_packing_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_TRAINING_PACK", "0")
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    st.put_events([_call(5)])
    assert "x" * 300 in st.norm.q("SELECT facts FROM events")[0]["facts"]
    assert st.blobs.count()["blobs"] == 0
    assert st.events("p")[0]["facts"]["messages"] == _msgs(5)


def test_a_missing_blob_is_an_error_not_an_empty_message(tmp_path):
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    with pytest.raises(blobs.MissingBlob):
        st.blobs.unpack({"messages": [{"$blob": "0" * 64}]})


def test_human_preference_reads_packed_events(tmp_path):
    import inspect

    from foundry_training import curate
    assert "store.unpack(json.loads(r[\"facts\"]))" in inspect.getsource(curate.record_human_preference)


# -- compact: an old store rebuilt beside itself --------------------------------------------------

def _old_store(tmp_path, monkeypatch, calls=40):
    monkeypatch.setenv("FOUNDRY_TRAINING_PACK", "0")
    old = TrainingStore(str(tmp_path / "old"), allow_in_repo=True)
    for n in range(1, calls + 1):
        old.put_events([_call(n)])
        old.put_raw("foundry", f"capture:{n}", "p",
                    {"body": {"request": {"messages": _msgs(n), "tools": TOOLS}}}, {}, {"n": n})
    old.put_events([Event("kilo", "k1", "tool_result", project_id="p", timestamp=1.0, session_id="s-t",
                          tool_call_id="tc1", facts={"output": "short"})])
    old.gap("p", "s-t", "kilo", "missing_tool_result", "one")
    old.correlate("session_task", "p", "s-t", "T1", "observed", 1.0, True)
    old.set_checkpoint("foundry:capture", "123")
    old.cur.x("INSERT INTO episodes (episode_id, project_id, kind, status) VALUES ('e1','p','trajectory','selected')")
    (old.root / "artifacts" / "ab").mkdir(parents=True)
    (old.root / "artifacts" / "ab" / "abc.png").write_bytes(b"png")
    monkeypatch.delenv("FOUNDRY_TRAINING_PACK")
    return old


def test_compact_rebuilds_the_store_smaller_and_identical(tmp_path, monkeypatch):
    old = _old_store(tmp_path, monkeypatch)
    before = {t: old.norm.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("events", "gaps", "correlations")}
    logs = []
    r = compact.compact(str(tmp_path / "old"), str(tmp_path / "new"), batch=7, verify_every=3, log=logs.append)
    assert r["events"]["rows"] == 41 and r["raw_records"]["rows"] == 40
    assert r["events"]["verified"] > 5 and r["raw_records"]["verified"] > 5
    assert r["new_bytes"] < r["old_bytes"] / 3
    new = TrainingStore(str(tmp_path / "new"), allow_in_repo=True)
    a, b = old.events("p"), new.events("p")
    assert [{k: v for k, v in e.items() if k != "ingested_ts"} for e in a] == \
           [{k: v for k, v in e.items() if k != "ingested_ts"} for e in b]
    assert [e["ingested_ts"] for e in a] == [e["ingested_ts"] for e in b]          # kept, not re-stamped
    ro = sorted(json.dumps(old.raw_payload(x), sort_keys=True) for x in old.raw.q("SELECT * FROM raw_records"))
    rn = sorted(json.dumps(new.raw_payload(x), sort_keys=True) for x in new.raw.q("SELECT * FROM raw_records"))
    assert ro == rn
    assert "x" * 300 not in new.raw.q("SELECT payload FROM raw_records ORDER BY id DESC LIMIT 1")[0]["payload"]
    assert new.gaps("p") == old.gaps("p") and new.correlations("p") == old.correlations("p")
    assert new.checkpoint("foundry:capture") == "123"
    assert new.cur.q("SELECT episode_id FROM episodes") == [{"episode_id": "e1"}]
    assert (new.root / "artifacts" / "ab" / "abc.png").read_bytes() == b"png"
    # the old store is as it was
    assert {t: old.norm.q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in before} == before
    assert "x" * 300 in old.norm.q("SELECT facts FROM events WHERE event_type='model_call' LIMIT 1")[0]["facts"]


def test_compact_continues_where_it_stopped_and_catches_up(tmp_path, monkeypatch):
    old = _old_store(tmp_path, monkeypatch, calls=20)
    r1 = compact.compact(str(tmp_path / "old"), str(tmp_path / "new"), batch=5, limit=10, log=lambda m: None)
    assert r1["events"]["rows"] == 10
    r2 = compact.compact(str(tmp_path / "old"), str(tmp_path / "new"), batch=5, log=lambda m: None)
    assert r2["events"]["from"] == 10 and r2["events"]["rows"] == 11
    monkeypatch.setenv("FOUNDRY_TRAINING_PACK", "0")
    old.put_events([_call(21)])                                        # the old store keeps receiving
    monkeypatch.delenv("FOUNDRY_TRAINING_PACK")
    r3 = compact.compact(str(tmp_path / "old"), str(tmp_path / "new"), log=lambda m: None)
    assert r3["events"]["rows"] == 1 and r3["raw_records"]["rows"] == 0
    new = TrainingStore(str(tmp_path / "new"), allow_in_repo=True)
    assert len(new.events("p")) == len(old.events("p")) == 22
    with pytest.raises(ValueError):
        compact.compact(str(tmp_path / "old"), str(tmp_path / "old"))
    with pytest.raises(ValueError):
        compact.compact(str(tmp_path / "nothing"), str(tmp_path / "x"))


def test_compact_stops_when_a_row_does_not_come_back_the_same(tmp_path, monkeypatch):
    _old_store(tmp_path, monkeypatch, calls=6)
    monkeypatch.setattr(blobs.BlobStore, "unpack", lambda self, obj: {"wrong": True})
    with pytest.raises(RuntimeError, match="nothing was deleted"):
        compact.compact(str(tmp_path / "old"), str(tmp_path / "new"), verify_every=1, log=lambda m: None)


def test_a_small_cache_never_drops_what_is_being_read(tmp_path):
    """Found on the live conversion: with the cache full, a large record's own messages
    were evicted between loading them and filling them in (KeyError)."""
    st = TrainingStore(str(tmp_path / "s"), allow_in_repo=True)
    st.blobs._cache_max = 2000                       # far smaller than one record
    big = {"messages": _msgs(150, "big"), "tools": TOOLS}
    other = {"messages": _msgs(90, "other")}
    pb, po = st.blobs.pack(big), st.blobs.pack(other)
    for _ in range(3):
        assert st.blobs.unpack(pb) == big
        assert st.blobs.unpack(po) == other
    assert st.blobs.keys(pb["messages"])[:5] == st.blobs.keys(st.blobs.pack({"messages": _msgs(5, "big")})["messages"])


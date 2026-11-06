"""v0.181.70: review/judge schemas go out in OpenAI's strict structured-output form."""

from foundry_coordinator import review as R
from foundry_coordinator import visual_approval as VA
from foundry_coordinator.adapters.foundry import strict_schema


def _objects(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("properties"), dict):
            out.append(node)
        for v in node.values():
            _objects(v, out)
    elif isinstance(node, list):
        for v in node:
            _objects(v, out)
    return out


def test_every_object_is_closed_and_fully_required():
    """2026-11-06 VR-AS21: gpt-6.1-sol refused the review schema with HTTP 400
    ('additionalProperties' is required to be supplied and to be false), so a review whose
    Claude window was used up fell through to the busy local model."""
    for schema in (R.SCHEMA, VA.JUDGE_SCHEMA):
        out = strict_schema(schema)
        objs = _objects(out, [])
        assert objs
        for o in objs:
            assert o["additionalProperties"] is False
            assert sorted(o["required"]) == sorted(o["properties"])
    # the original is untouched, and the field shapes are kept
    assert "additionalProperties" not in R.SCHEMA
    f = strict_schema(R.SCHEMA)["properties"]["findings"]["items"]
    assert f["properties"]["line"] == {"type": ["integer", "null"]}
    assert "file" in f["required"] and "project_id" in strict_schema(R.SCHEMA)["required"]


def test_the_chat_body_carries_the_strict_schema(monkeypatch):
    from foundry_coordinator.adapters import foundry as F
    sent = {}
    client = F.FoundryClient.__new__(F.FoundryClient)

    def req(method, path, body=None, headers=None, timeout=None):
        sent.update(body or {})
        return {"choices": [{"message": {"content": "{}"}}]}, {}
    monkeypatch.setattr(client, "_req", req, raising=False)
    client.chat("Foundry-Kilo-Review", [{"role": "user", "content": "x"}], schema=R.SCHEMA)
    js = sent["response_format"]["json_schema"]["schema"]
    assert js["additionalProperties"] is False

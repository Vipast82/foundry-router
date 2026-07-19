"""prefer_loaded routing: keep the model that's already in VRAM. When a persona
opts in, an already-loaded candidate is floated to the front of the acceptable
set (marked _loaded for the brain) so the router doesn't force a slow reload —
but pins still lead, and it never introduces a model that wasn't already
an acceptable candidate. Also: CORS headers are present (the PhishGuard fix)."""

from foundry_router.brain.agent import _prefer_loaded


def _r(mid, **extra):
    return {"id": mid, "score": 50, **extra}


def test_loaded_candidate_floats_to_front_and_is_marked():
    ranked = [_r("big:35b"), _r("small:8b"), _r("mid:14b")]
    out = _prefer_loaded(ranked, {"mid:14b"})
    assert out[0]["id"] == "mid:14b" and out[0]["_loaded"] is True
    assert [r["id"] for r in out[1:]] == ["big:35b", "small:8b"]   # order preserved


def test_pins_still_lead_over_merely_loaded():
    ranked = [_r("pinned:x", _pinned=True), _r("a:1"), _r("loaded:y")]
    out = _prefer_loaded(ranked, {"loaded:y"})
    assert out[0]["id"] == "pinned:x"          # explicit pin outranks "happens to be loaded"
    assert out[1]["id"] == "loaded:y" and out[1].get("_loaded")


def test_no_loaded_candidate_is_a_noop():
    ranked = [_r("a:1"), _r("b:2")]
    assert _prefer_loaded(ranked, {"not-a-candidate"}) == ranked
    assert _prefer_loaded(ranked, set()) == ranked


def test_only_acceptable_candidates_are_considered():
    # a loaded model that is NOT in the ranked (acceptable) set is never added
    ranked = [_r("a:1"), _r("b:2")]
    out = _prefer_loaded(ranked, {"c:3"})
    assert [r["id"] for r in out] == ["a:1", "b:2"]


# -- CORS (PhishGuard's NetworkError) ---------------------------------------------

def test_cors_headers_present(client):
    # a browser preflight for a cross-origin POST must be answered with ACAO
    r = client.options("/v1/chat/completions", headers={
        "Origin": "https://phishguard.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") in ("*", "https://phishguard.example")

    # and a simple GET carries the allow-origin header so the browser exposes it
    g = client.get("/v1/models", headers={"Origin": "https://phishguard.example"})
    assert g.headers.get("access-control-allow-origin") in ("*", "https://phishguard.example")

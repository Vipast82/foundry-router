"""Server support for the coordinator's lead manager: the served-model ledger
view of a session, a persona's route/grant readiness view, the lead_manager
binding purpose, and the coordinator FoundryClient methods for both."""

import io
import json
import time
import urllib.error

import pytest

from foundry_coordinator.adapters import foundry as fadapter
from foundry_coordinator.adapters.foundry import FoundryClient, FoundryError
from foundry_router.budget import CallCtx
from foundry_router.config import ClientConfig
from tests.test_persona_profiles import fleet  # noqa: F401 (fixture)

COORD = {"Authorization": "Bearer coord-key"}
LIMITED = {"Authorization": "Bearer limited-key"}
LEAD = "Foundry-Kilo-Lead-Manager"


def _clients(svc):
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration"]),
        ClientConfig(name="limited", key="limited-key", scopes=["orchestration"],
                     projects=["game-one"], personas=["Foundry-Kilo-Review"]),
        ClientConfig(name="kilo", key="kilo-key", scopes=["inference"], personas=[LEAD]),
        ClientConfig(name="kilo-other", key="ko-key", scopes=["inference"],
                     personas=["Foundry-Kilo-Review"]),
        ClientConfig(name="inference-only", key="inf-key", scopes=["inference"])]


def _ledger(db, sid, backend, model, status="settled", ts=None, project="game-one"):
    db.execute("INSERT INTO usage_events (ts, session_id, persona, backend, model, status, "
               "project_id) VALUES (?,?,?,?,?,?,?)",
               (ts or time.time(), sid, LEAD, backend, model, status, project))


def _bind(client, sid, project, purpose="", headers=COORD):
    body = {"session_id": sid, "project_id": project, "task_id": "T1", "run_id": "r1",
            "role": "coordinator", "persona": LEAD}
    if purpose:
        body["purpose"] = purpose
    return client.post("/v1/foundry/work/sessions", headers=headers, json=body)


# ------------------------------------------------------------------ served

def test_served_models_in_first_use_order(app, client):
    svc = app.state.services
    _clients(svc)
    assert _bind(client, "lead-1", "game-one").status_code == 200
    t = 1_800_000_000.0
    _ledger(svc.db, "lead-1", "meridian", "claude-opus-5-5", ts=t)
    _ledger(svc.db, "lead-1", "cliproxy", "gpt-6-astra", ts=t + 1)
    _ledger(svc.db, "lead-1", "meridian", "claude-opus-5-5", ts=t + 2)
    _ledger(svc.db, "lead-1", "strata", "qwen-local", status="error", ts=t + 3)
    _ledger(svc.db, "lead-1", "meridian", "claude-opus-5-5", status="denied", ts=t + 4)
    _ledger(svc.db, "other", "strata", "qwen-local", ts=t + 5)
    r = client.get("/v1/foundry/work/sessions/lead-1/served", headers=COORD)
    assert r.status_code == 200
    d = r.json()
    assert d["session_id"] == "lead-1" and d["project_id"] == "game-one"
    # an errored call reached its backend: it counts (and its model is named),
    # so a call on an unexpected route can't hide behind an error; a denial
    # never reached a model
    assert d["calls"] == 4 and d["settled"] == 3 and d["errors"] == 1 and d["denied"] == 1
    assert d["pending"] == 0
    assert d["served_models"] == ["claude-opus-5-5", "gpt-6-astra", "qwen-local"]
    assert d["backends"] == ["meridian", "cliproxy", "strata"]
    assert d["routes"] == [{"backend": "meridian", "model": "claude-opus-5-5"},
                           {"backend": "cliproxy", "model": "gpt-6-astra"},
                           {"backend": "strata", "model": "qwen-local"}]
    assert d["last_ts"] == t + 3
    # project-restricted key sees its own project's session
    assert client.get("/v1/foundry/work/sessions/lead-1/served",
                      headers=LIMITED).json()["calls"] == 4


def test_served_counts_in_flight_calls_as_pending(app, client):
    svc = app.state.services
    _clients(svc)
    assert _bind(client, "lead-3", "game-one").status_code == 200
    t = 1_800_000_000.0
    _ledger(svc.db, "lead-3", "meridian", "claude-opus-5-5", ts=t)
    _ledger(svc.db, "lead-3", "strata", "qwen-local", status="reserved", ts=t + 1)
    d = client.get("/v1/foundry/work/sessions/lead-3/served", headers=COORD).json()
    assert d["pending"] == 1 and d["calls"] == 2 and d["settled"] == 1
    assert d["served_models"] == ["claude-opus-5-5", "qwen-local"]
    svc.db.execute("UPDATE usage_events SET status='settled' WHERE session_id='lead-3'")
    assert client.get("/v1/foundry/work/sessions/lead-3/served",
                      headers=COORD).json()["pending"] == 0


def test_served_refuses_another_projects_session_and_needs_orchestration(app, client):
    svc = app.state.services
    _clients(svc)
    assert _bind(client, "lead-2", "game-two").status_code == 200
    _ledger(svc.db, "lead-2", "meridian", "claude-opus-5-5", project="game-two")
    r = client.get("/v1/foundry/work/sessions/lead-2/served", headers=LIMITED)
    assert r.status_code == 403 and r.json()["code"] == "project_forbidden"
    assert "claude-opus" not in r.text
    assert client.get("/v1/foundry/work/sessions/lead-2/served",
                      headers={"Authorization": "Bearer inf-key"}).status_code == 403
    assert client.get("/v1/foundry/work/sessions/lead-2/served").status_code == 401
    # an unbound session's ledger rows are not shown to a restricted key
    _ledger(svc.db, "loose", "meridian", "claude-opus-5-5", project="")
    assert client.get("/v1/foundry/work/sessions/loose/served",
                      headers=LIMITED).json()["calls"] == 0
    assert client.get("/v1/foundry/work/sessions/loose/served",
                      headers=COORD).json()["calls"] == 1


def test_served_unknown_session_is_empty_not_404(app, client):
    _clients(app.state.services)
    r = client.get("/v1/foundry/work/sessions/nope/served", headers=COORD)
    assert r.status_code == 200
    d = r.json()
    assert d["calls"] == 0 and d["served_models"] == [] and d["backends"] == []
    assert d["last_ts"] is None


# ------------------------------------------------------------------ persona routes

def _persona(svc, name=LEAD, enabled=1, routing="strict",
             chain=("claude-opus-5-5", "gpt-6-astra")):
    svc.personas.upsert(name, enabled=enabled, routing=routing, model_allowlist=list(chain),
                        pinned_models=list(chain))


def test_enabled_strict_persona_with_two_routes(fleet, client):  # noqa: F811
    _clients(fleet)
    _persona(fleet)
    r = client.get(f"/v1/foundry/personas/{LEAD}/routes", headers=COORD)
    assert r.status_code == 200
    d = r.json()
    assert d["exists"] and d["enabled"] and d["strict"]
    assert [(x["backend"], x["model"]) for x in d["routes"]] == [
        ("meridian", "claude-opus-5-5"), ("cliproxy", "gpt-6-astra")]
    assert all(x["source"] == "subscription" for x in d["routes"])
    assert all("vision" in x for x in d["routes"])
    assert d["client_allowed"] is True and d["client"] == "coordinator"   # unrestricted key
    assert d["reason"] == "ready"
    # the worker client's grant can be checked by name
    assert client.get(f"/v1/foundry/personas/{LEAD}/routes?client=kilo",
                      headers=COORD).json()["client_allowed"] is True
    # never any key material
    assert "coord-key" not in r.text and "kilo-key" not in r.text and "key_hash" not in r.text


def test_disabled_persona_is_reported_not_404(fleet, client):  # noqa: F811
    _clients(fleet)
    _persona(fleet, enabled=0)
    r = client.get(f"/v1/foundry/personas/{LEAD}/routes", headers=COORD)
    assert r.status_code == 200
    d = r.json()
    assert d["exists"] is True and d["enabled"] is False
    assert "disabled" in d["reason"]
    r = client.get("/v1/foundry/personas/No-Such-Persona/routes", headers=COORD)
    assert r.status_code == 200 and r.json()["exists"] is False
    assert "does not exist" in r.json()["reason"]


def test_persona_not_granted_to_client(fleet, client):  # noqa: F811
    _clients(fleet)
    _persona(fleet)
    d = client.get(f"/v1/foundry/personas/{LEAD}/routes", headers=LIMITED).json()
    assert d["client_allowed"] is False and "not granted" in d["reason"]
    d = client.get(f"/v1/foundry/personas/{LEAD}/routes?client=kilo-other",
                   headers=COORD).json()
    assert d["client_allowed"] is False and "kilo-other" in d["reason"]
    d = client.get(f"/v1/foundry/personas/{LEAD}/routes?client=ghost", headers=COORD).json()
    assert d["client_allowed"] is False and "not configured" in d["reason"]
    assert client.get(f"/v1/foundry/personas/{LEAD}/routes",
                      headers={"Authorization": "Bearer inf-key"}).status_code == 403


def test_client_param_needs_a_shared_project_or_admin(fleet, client):  # noqa: F811
    _clients(fleet)
    fleet.config_store.config.clients += [
        ClientConfig(name="other-game", key="og-key", scopes=["inference"],
                     projects=["game-two"], personas=[LEAD]),
        ClientConfig(name="same-game", key="sg-key", scopes=["inference"],
                     projects=["game-one"], personas=[LEAD]),
        ClientConfig(name="admin", key="admin-key", scopes=["admin"])]
    _persona(fleet)
    url = f"/v1/foundry/personas/{LEAD}/routes?client="
    # a project-restricted caller can't probe another project's client ...
    r = client.get(url + "other-game", headers=LIMITED)
    assert r.status_code == 403 and r.json()["code"] == "client_forbidden"
    assert "routes" not in r.json()
    # ... nor learn whether a client exists
    assert client.get(url + "ghost", headers=LIMITED).status_code == 403
    # a client sharing its project (or serving every project) is fine
    assert client.get(url + "same-game", headers=LIMITED).json()["client_allowed"] is True
    assert client.get(url + "kilo", headers=LIMITED).json()["client_allowed"] is True
    # an unrestricted or admin key may inspect any client
    assert client.get(url + "other-game", headers=COORD).json()["client_allowed"] is True
    assert client.get(url + "other-game", headers={"Authorization": "Bearer admin-key"}) \
        .json()["client_allowed"] is True


def test_flexible_vs_strict(fleet, client):  # noqa: F811
    _clients(fleet)
    _persona(fleet, name="Flex-Lead", routing="")
    d = client.get("/v1/foundry/personas/Flex-Lead/routes", headers=COORD).json()
    assert d["strict"] is False and "flexible routing" in d["reason"]
    _persona(fleet, name="Strict-Empty", chain=("no-such-model",))
    d = client.get("/v1/foundry/personas/Strict-Empty/routes", headers=COORD).json()
    assert d["strict"] is True and d["routes"] == []
    assert "strict routing refuses" in d["reason"]


# ------------------------------------------------------------------ purpose

def test_lead_manager_purpose_is_required_and_bindable(app, client):
    from foundry_router.orchestration import OPTIONAL_PURPOSES, REQUIRED_PURPOSES
    assert "lead_manager" in REQUIRED_PURPOSES and "lead_manager" not in OPTIONAL_PURPOSES
    svc = app.state.services
    _clients(svc)
    assert _bind(client, "lead-p", "game-one", purpose="lead_manager").status_code == 200
    assert svc.work.lookup("lead-p")["purpose"] == "lead_manager"
    gate = svc.quota_gate
    # never capped by the per-run helper allowance (unlike blocker_diagnosis)
    for i in range(5):
        svc.db.execute("INSERT INTO work_sessions (session_id, project_id, run_id, purpose, "
                       "created_ts) VALUES (?,?,?,?,?)", (f"lp{i}", "g", "rX", "lead_manager", i))
    assert gate.effective_purpose(CallCtx(purpose="lead_manager", run_id="rX",
                                          session_id="lp4")) == "required"
    # ... only by a high per-run backstop (default 20 lead sessions per run)
    for i in range(5, 21):
        svc.db.execute("INSERT INTO work_sessions (session_id, project_id, run_id, purpose, "
                       "created_ts) VALUES (?,?,?,?,?)", (f"lp{i}", "g", "rX", "lead_manager", i))
    assert gate.effective_purpose(CallCtx(purpose="lead_manager", run_id="rX",
                                          session_id="lp19")) == "required"
    assert gate.effective_purpose(CallCtx(purpose="lead_manager", run_id="rX",
                                          session_id="lp20")) == "optional"
    gate.pol.__dict__["lead_manager_sessions_per_run"] = 50
    assert gate.effective_purpose(CallCtx(purpose="lead_manager", run_id="rX",
                                          session_id="lp20")) == "required"


# ------------------------------------------------------------------ client methods

class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()
        self.headers = {}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError("http://f/x", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode()))


@pytest.fixture()
def fake_http(monkeypatch):
    calls, answers = [], []

    def urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        a = answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return _Resp(a)
    monkeypatch.setattr(fadapter.urllib.request, "urlopen", urlopen)
    return calls, answers


def test_client_served_models(fake_http):
    calls, answers = fake_http
    fc = FoundryClient("http://f/", key="k")
    answers.append({"session_id": "s/1", "calls": 2, "served_models": ["a"], "backends": ["b"]})
    assert fc.served_models("s/1")["served_models"] == ["a"]
    assert calls[0] == ("GET", "http://f/v1/foundry/work/sessions/s%2F1/served", "Bearer k")
    answers.append(_http_error(404, {"detail": "Not Found"}))
    assert fc.served_models("s") is None
    answers.append(urllib.error.URLError("refused"))
    assert fc.served_models("s") is None
    answers.append(ConnectionRefusedError("refused"))
    assert fc.served_models("s") is None
    answers.append(_http_error(403, {"error": "nope", "code": "project_forbidden"}))
    with pytest.raises(FoundryError, match="403 project_forbidden"):
        fc.served_models("s")
    answers.append(_http_error(500, {"error": "boom"}))
    with pytest.raises(FoundryError):
        fc.served_models("s")


def test_client_persona_info(fake_http):
    calls, answers = fake_http
    fc = FoundryClient("http://f", key="k")
    answers.append({"name": LEAD, "exists": True, "enabled": False, "strict": True,
                    "routes": [], "client_allowed": False, "reason": "persona is disabled"})
    d = fc.persona_info(LEAD, client="kilo")
    assert d["enabled"] is False and d["reason"] == "persona is disabled"
    assert calls[0][1] == f"http://f/v1/foundry/personas/{LEAD}/routes?client=kilo"
    answers.append(_http_error(404, {"detail": "Not Found"}))
    assert fc.persona_info(LEAD) is None
    answers.append(_http_error(401, {"error": {"message": "bad key", "code": "invalid_api_key"}}))
    with pytest.raises(FoundryError, match="invalid_api_key"):
        fc.persona_info(LEAD)

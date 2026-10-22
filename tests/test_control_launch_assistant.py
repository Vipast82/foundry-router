"""Launch assistant actions in the router (2026-10-21): project-scoped, operator
sessions only, validated payloads, offered only to a coordinator that declares them."""
import pytest  # noqa: F401

from tests.test_control_center import cc, login  # noqa: F401
from tests.test_control_project_pause import _claim  # noqa: F401


def _post(client, h, **body):
    return client.post("/control/api/actions", headers=h, json={"project_id": "proj", **body})


def test_plan_and_run_are_accepted_with_checked_payloads(client, cc):
    h = login(client)
    r = _post(client, h, kind="assistant_plan", text="start the next sprint",
              idempotency_key="ask-12345678")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["kind"] == "assistant_plan" and a["task_id"] == "" and a["status"] == "accepted"
    assert "launch assistant" in a["summary"] and "start the next" not in a["summary"]
    r = _post(client, h, kind="assistant_run", plan_id="asp-0123456789ab", steps=[0, 2],
              idempotency_key="run-12345678")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("body,msg", [
    ({"kind": "assistant_plan", "text": "   "}, "say what"),
    ({"kind": "assistant_plan", "text": "x" * 2001}, "over"),
    ({"kind": "assistant_run", "plan_id": "nope"}, "plan_id"),
    ({"kind": "assistant_run", "plan_id": "asp-0123456789ab", "steps": ["a"]}, "steps"),
])
def test_bad_payloads_are_refused(client, cc, body, msg):
    h = login(client)
    r = _post(client, h, idempotency_key="bad-12345678", **body)
    assert r.status_code == 400, r.text
    assert msg in r.text

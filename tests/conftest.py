"""Test fixtures: an app instance with background loops disabled, no real
backends, and a brain endpoint pointing at a guaranteed-refused port — which
is exactly what the brain-unreachable fallback tests need."""

import os

os.environ["FOUNDRY_DISABLE_BACKGROUND"] = "1"  # before any app import

import pytest

TEST_CONFIG = """
server: {host: 127.0.0.1, port: 11435}
agent_brain:
  provider: ollama
  endpoint: "http://127.0.0.1:9"   # nothing listens here — brain is unreachable
  model: "test-brain"
backend_pool:
  mode: internal
  failure_threshold: 3
  cooldown_seconds: 60
  internal:
    backends: []
guardrails:
  authority: internal
  max_steps_per_request: 5
  max_paid_calls_per_request: 2
registry:
  research:
    enabled: false
mcp_servers: []
"""


@pytest.fixture()
def app(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(TEST_CONFIG, encoding="utf-8")
    from foundry_router.main import create_app
    return create_app(config_path=cfg_path, database_path=tmp_path / "test.sqlite")


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c

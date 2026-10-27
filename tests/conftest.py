"""Test fixtures: an app instance with background loops disabled, no real
backends, and a brain endpoint pointing at a guaranteed-refused port — which
is exactly what the brain-unreachable fallback tests need."""

import os

os.environ["FOUNDRY_DISABLE_BACKGROUND"] = "1"  # before any app import
# no background training runs from coordinator tests (tests opt in explicitly)
os.environ["FOUNDRY_TRAINING_AUTO"] = "0"

import pytest

if os.name == "nt":
    # no test subprocess (git, pwsh, fake coordinators, venv launchers) may open
    # a console window and steal focus from the desktop
    import subprocess as _sp

    _NO_WINDOW = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)
    _DETACHED = getattr(_sp, "DETACHED_PROCESS", 0x00000008)
    _CREATE_NEW_CONSOLE = getattr(_sp, "CREATE_NEW_CONSOLE", 0x00000010)
    _real_popen_init = _sp.Popen.__init__

    def _hidden_popen_init(self, *a, **kw):
        flags = kw.get("creationflags", 0) or 0
        flags &= ~(_DETACHED | _CREATE_NEW_CONSOLE)
        kw["creationflags"] = flags | _NO_WINDOW
        _real_popen_init(self, *a, **kw)

    _sp.Popen.__init__ = _hidden_popen_init

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


_REAL_APPDATA = os.environ.get("APPDATA")
_REAL_XDG = os.environ.get("XDG_CONFIG_HOME")


@pytest.fixture(autouse=True)
def _no_live_foundry_client(tmp_path_factory, monkeypatch):
    """No test may reach the Foundry Client running on this machine (0.145.0: its
    API can restart Rojo and reopen Studio). The coordinator's client discovery
    looks in an empty folder unless a test points APPDATA / XDG_CONFIG_HOME
    somewhere itself."""
    from foundry_coordinator import client_api
    real = client_api.config_dir
    empty = tmp_path_factory.mktemp("no-client")

    def guarded():
        if os.environ.get("APPDATA") == _REAL_APPDATA and \
                os.environ.get("XDG_CONFIG_HOME") == _REAL_XDG:
            return empty / "FoundryClient"
        return real()
    monkeypatch.setattr(client_api, "config_dir", guarded)


@pytest.fixture(autouse=True)
def _no_live_kilo_services(monkeypatch):
    """No test reads this machine's global Kilo config (0.145.3): its MCP servers
    (blender-mcp, roblox) are probed on live ports, so a track test passed with
    Blender open and waited on 'blender-mcp' with it closed. A test that wants a
    global config passes global_path or patches global_kilo_paths itself."""
    from foundry_automation import service_map as SM
    monkeypatch.setattr(SM, "global_kilo_paths", lambda: [])

"""Test fixtures: an app instance with background loops disabled, no real
backends, and a brain endpoint pointing at a guaranteed-refused port — which
is exactly what the brain-unreachable fallback tests need."""

import os

os.environ["FOUNDRY_DISABLE_BACKGROUND"] = "1"  # before any app import
# no background training runs from coordinator tests (tests opt in explicitly)
os.environ["FOUNDRY_TRAINING_AUTO"] = "0"

import pytest

# A worker plus the git / pytest / fake-coordinator subprocesses its tests start
# peaks around this much memory (workers sit near 200 MB resident).
_WORKER_MB = 400


def pytest_xdist_auto_num_workers(config):
    """`-n auto` (the default in pyproject.toml): one worker per physical core,
    capped by the memory that is free now (this workstation also runs Studio,
    Kilo and the live stack), and no workers at all for a run of one named test
    (`pytest tests/x.py::test_y` stays a plain serial run). Running a handful of
    named tests gets at most one worker per test. PYTEST_XDIST_AUTO_NUM_WORKERS
    still overrides all of this; `-n 0` turns xdist off."""
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None                                   # xdist's own hook reads it
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
        by_memory = int(psutil.virtual_memory().available / 2**20 // _WORKER_MB)
    except ImportError:
        cores = max(1, (os.cpu_count() or 2) // 2)
        by_memory = cores
    n = max(1, min(cores, by_memory))
    args = [a for a in config.args if a.strip()]
    if args and all("::" in a for a in args):
        n = min(n, len(args))
    return n if n > 1 else 0

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
def _no_live_services(monkeypatch):
    """No test may depend on which services happen to run on this machine. The
    coordinator's service probe (a sprint's `needs`) read the real global Kilo
    config and probed real ports and LAN hosts, so a track sprint needing Blender
    passed while Blender was open and waited forever once it was closed. Here
    the global Kilo config is empty, an unconfigured port check is "cannot
    tell" (never blocks) and an http check is unreachable. Tests that exercise
    the probe pass their own port_fn / http_fn / mcp (tests/coordinator/
    test_services.py); tests/client/conftest.py adds its own guards."""
    from foundry_automation import service_map as SM
    from foundry_coordinator import services as SV
    monkeypatch.setattr(SM, "global_kilo_paths", lambda: [])
    real = SV.make_probe

    def hermetic_probe(*a, **kw):
        kw.setdefault("port_fn", lambda host, port, *x, **y: None)
        kw.setdefault("http_fn", lambda url: "no live services in tests")
        return real(*a, **kw)
    monkeypatch.setattr(SV, "make_probe", hermetic_probe)

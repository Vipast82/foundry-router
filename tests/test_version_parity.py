"""One release version for the whole repo: the router, the coordinator and the
client must agree. v0.138.1 - v0.141.0 bumped only the router and left the
coordinator reporting 0.138.0; this fails the next time that happens."""
import re
from pathlib import Path

import foundry_client
import foundry_coordinator
import foundry_router

ROOT = Path(__file__).resolve().parents[1]


def test_router_coordinator_and_client_versions_agree():
    v = foundry_router.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), v
    assert foundry_coordinator.__version__ == v, (
        f"foundry_coordinator/__init__.py says {foundry_coordinator.__version__}, "
        f"foundry_router says {v}: bump both")
    assert getattr(foundry_client, "__version__", v) == v


def test_newest_release_notes_entry_is_this_version():
    notes = (ROOT / "docs" / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    first = re.search(r"^## v(\d+\.\d+\.\d+)", notes, re.M)
    assert first and first.group(1) == foundry_router.__version__

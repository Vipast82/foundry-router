"""The web UI is one inline script: a single syntax error leaves the page
stuck on "loading..." (v0.112.0 shipped a raw newline inside a string)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "index.html"


def _scripts():
    return re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", UI.read_text(encoding="utf-8"), re.S)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_ui_inline_script_parses(tmp_path):
    for i, code in enumerate(_scripts()):
        f = tmp_path / f"ui{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:2000]


def test_ui_strings_have_no_raw_line_breaks():
    """Without node: catch the common case, a quote opened and the line
    ending inside it, e.g. .join(" followed by a newline."""
    bad = [n for n, line in enumerate(UI.read_text(encoding="utf-8").splitlines(), 1)
           if re.search(r'\.join\("$|\.join\(\'$', line.rstrip())]
    assert not bad, f"string broken across lines at index.html:{bad}"


def test_list_fields_use_the_multi_picker():
    """Fields that take several values are pickers (chips + a dropdown that
    stays open), not single-choice datalists or comma/JSON text boxes."""
    html = UI.read_text(encoding="utf-8")
    for field in ("nc-personas", "ed-personas", "pe-allow", "pe-pins", "pe-reqtags", "pe-trig",
                  "me-tags", "md-models", "ep-checks"):
        m = re.search(r'<input id="%s"[^>]*>' % field, html)
        assert m and "data-multi=" in m.group(0), field
    assert "(comma-separated)</label>" not in html and "(JSON list)</label>" not in html.replace("rules (JSON list)</label>", "").replace("markers (JSON list)</label>", "")
    assert not re.search(r'<input id="(nc|ed)-personas"[^>]*list=', html)

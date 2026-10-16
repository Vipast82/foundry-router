"""The web UI is one inline script: a single syntax error leaves the page
stuck on "loading..." (v0.112.0 shipped a raw newline inside a string)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "index.html"


def _scripts():
    return re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", UI.read_text(), re.S)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_ui_inline_script_parses(tmp_path):
    for i, code in enumerate(_scripts()):
        f = tmp_path / f"ui{i}.js"
        f.write_text(code)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:2000]


def test_ui_strings_have_no_raw_line_breaks():
    """Without node: catch the common case, a quote opened and the line
    ending inside it, e.g. .join(" followed by a newline."""
    bad = [n for n, line in enumerate(UI.read_text().splitlines(), 1)
           if re.search(r'\.join\("$|\.join\(\'$', line.rstrip())]
    assert not bad, f"string broken across lines at index.html:{bad}"

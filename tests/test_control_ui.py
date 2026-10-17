"""The control-center page (/ui/control): its inline script parses, its
Markdown/highlight renderer never turns model or tool output into live HTML,
and it keeps keys, transcripts and drafts out of browser storage."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "control.html"


def _script() -> str:
    return re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", PAGE.read_text(encoding="utf-8"),
                      re.S)[0]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_control_page_script_parses(tmp_path):
    f = tmp_path / "cc.js"
    f.write_text(_script(), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:2000]


HOSTILE = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[click](javascript:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    '```html\n<script>alert("x")</script>\n```',
    "`<b onmouseover=alert(1)>`",
    "| a | b |\n|---|---|\n| <svg onload=alert(1)> | ok |",
    "> <iframe src=//evil></iframe>",
    '**"><img src=x onerror=alert(1)>**',
    "[ok](https://example.com/\"onmouseover=\"alert(1))",
]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_markdown_renderer_never_emits_live_html(tmp_path):
    src = _script()
    block = src[src.index("/*md-start*/"):src.index("/*md-end*/")]
    prelude = ('const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;"};\n'
               'const esc = s => String(s ?? "").replace(/[&<>"\']/g, c => ESC_MAP[c]);\n')
    f = tmp_path / "md.js"
    f.write_text(prelude + block + f"\nconsole.log(JSON.stringify({json.dumps(HOSTILE)}.map(md)));",
                 encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for inp, out in zip(HOSTILE, json.loads(r.stdout)):
        tags = re.findall(r"<([a-zA-Z][a-zA-Z0-9]*)([^>]*)>", out)
        allowed = {"p", "br", "pre", "code", "span", "strong", "em", "del", "a", "h1", "h2", "h3",
                   "h4", "ul", "ol", "li", "blockquote", "table", "tr", "td", "th"}
        for name, attrs in tags:
            assert name.lower() in allowed, (inp, out)
            assert not re.search(r"\son\w+\s*=", attrs), (inp, out)
            for href in re.findall(r'href="([^"]*)"', attrs):
                assert href.startswith(("https://", "http://", "mailto:")), (inp, out)
                assert '"' not in href
        assert "javascript:" not in re.sub(r"&[a-z#0-9]+;", "", out.split("href=")[0]) or "<a" not in out


def test_browser_storage_holds_preferences_only():
    s = _script()
    keys = set(re.findall(r'localStorage\.(?:setItem|getItem)\("([^"]+)"', s))
    keys |= set(re.findall(r"localStorage\.(?:setItem|getItem)\((PREF_KEY)", s))
    assert keys <= {"PREF_KEY", "fr.cc.played", "fr.cc.audioOwner"}, keys
    assert "sessionStorage" not in s
    assert "foundryAdminKey" not in s                 # the admin UI's key is never read here
    # the key typed at sign-in goes to the login endpoint only, then the field is cleared
    assert '$("#login-key").value = "";' in s


def test_work_tab_links_to_the_control_center():
    ui = (PAGE.parent / "index.html").read_text(encoding="utf-8")
    assert "/ui/control" in ui

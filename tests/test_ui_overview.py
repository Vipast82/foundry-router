"""The admin UI's Overview landing page (v0.128): its plain-language verdict,
the "needs attention" list (down backends, hot subscription windows, advisor
warnings, failed calls), hourly trend buckets, and the navigation that puts
the overview first without dropping a tab. Run in node against the real
inline script."""

import json
import re
import shutil
import subprocess

import pytest

from tests.test_ui_script import UI, _scripts

node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _block(start, end):
    s = _scripts()[0]
    i = s.index(start)
    return s[i:s.index(end, i)]


def run(tmp_path, body):
    s = _scripts()[0]
    tslabel = s[s.index("function _tsLabel("):s.index("function _num(")]
    code = ('const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;"};\n'
            'const esc = v => String(v ?? "").replace(/[&<>"]/g, c => ESC[c]);\n'
            + tslabel + _block("/*overview-start*/", "/*overview-end*/") + body)
    f = tmp_path / "ov.js"
    f.write_text(code, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:3000]
    return json.loads(r.stdout.strip().splitlines()[-1])


ST = {"version": "0.128.0", "brain": {"health": {"healthy": True}},
      "backends": [{"name": "strata", "healthy": True}, {"name": "meridian", "healthy": True},
                   {"name": "old", "healthy": False, "enabled": False}]}


@node
def test_healthy_fleet_reads_as_one_calm_sentence(tmp_path):
    out = run(tmp_path, """
const rows = [{ts: "2026-10-19T10:00:00Z", wall_ms: 1000}, {ts: "2026-10-19T10:01:00Z", wall_ms: 3000},
              {ts: "2026-10-19T10:02:00Z", wall_ms: 2000}];
const m = overviewModel(%s, {series: rows, summary: {samples: 3}}, {totals: {"24h": {calls: 1200}}}, {findings: []}, {});
console.log(JSON.stringify({v: m.verdict, mood: m.mood, attn: m.attn.length, p50: m.p50}));
""" % json.dumps(ST))
    # a disabled backend doesn't count; the measured 24h total wins over timed samples
    assert out["v"] == "All 2 backends are up. 1.2k requests in the last 24 h, typically 2.0 s each."
    assert out["mood"] == "ok" and out["attn"] == 0 and out["p50"] == 2000


@node
def test_attention_list_orders_problems_worst_first(tmp_path):
    st = json.loads(json.dumps(ST))
    st["backends"][1].update(healthy=False, last_error="connect timeout")
    bu = {"totals": {"24h": {"calls": 5, "errors": 2, "quota_errors": 1}},
          "accounts": [{"account": "meridian:claude", "provider_windows": [
              {"name": "weekly", "used_pct": 88, "resets_in_s": 7200}, {"name": "5-hour", "used_pct": 40}]}]}
    adv = {"findings": [{"severity": "info", "title": "tip"},
                        {"severity": "warning", "title": "TTFT climbs", "summary": "x"}]}
    out = run(tmp_path, """
const m = overviewModel(%s, null, %s, %s, null);
console.log(JSON.stringify({v: m.verdict, mood: m.mood, attn: m.attn}));
""" % (json.dumps(st), json.dumps(bu), json.dumps(adv)))
    titles = [a["title"] for a in out["attn"]]
    assert titles[0] == "Backend meridian is down" and out["attn"][0]["sev"] == "bad"
    assert "meridian:claude: weekly window 88% used" in titles
    assert "TTFT climbs" in titles and "tip" not in titles          # info findings stay off
    assert "2 failed calls in the last 24 h" in titles
    assert next(a for a in out["attn"] if "weekly" in a["title"])["detail"] == "resets in 2.0 h"
    assert out["mood"] == "bad" and out["v"].startswith("1 of 2 backends is down.")


@node
def test_missing_data_is_said_not_invented(tmp_path):
    out = run(tmp_path, """
const a = overviewModel(null, null, null, null, null);
const b = overviewModel({backends: [], brain: {}}, {series: [], summary: {samples: 0}}, null, null, null);
console.log(JSON.stringify({a: [a.verdict, a.mood], b: [b.verdict, b.mood, b.p50]}));
""")
    assert out["a"] == ["Foundry's status is unavailable. No requests in the last 24 h.", "bad"]
    assert out["b"][0].startswith("No backends are configured.") and out["b"][1] == "warn"
    assert out["b"][2] is None


@node
def test_hourly_buckets_and_trend_gaps(tmp_path):
    out = run(tmp_path, """
const now = Date.parse("2026-10-19T12:00:00Z");
const rows = [{ts: "2026-10-19T11:30:00Z"}, {ts: "2026-10-19T11:10:00Z"}, {ts: "2026-10-19T09:30:00Z"},
              {ts: "2026-10-18T09:00:00Z"}, {ts: "garbage"}];
const b = _buckets(rows, 4, 4, x => x.length, now);
const svg = trendSpark([1, null, 3, 4], "red");
console.log(JSON.stringify({b, gap: (svg.match(/M/g) || []).length, empty: trendSpark([null, 2], "red")}));
""")
    assert out["b"] == [0, 1, 0, 2]                   # outside the window / unparsable: dropped
    assert out["gap"] == 2                            # a missing hour breaks the line
    assert "spark-empty" in out["empty"]


def test_nav_puts_overview_first_and_keeps_every_tab():
    s = _scripts()[0]
    tabs = re.findall(r'^  "([^"]+)":\s+tab\w+,$', s[s.index("const TABS = {"):s.index("};", s.index("const TABS = {"))], re.M)
    groups = s[s.index("const GROUPS = ["):s.index("];", s.index("const GROUPS = ["))]
    listed = re.findall(r'"([^"]+)"', groups)
    assert listed[:2] == ["Overview", "Overview"]
    for t in tabs:
        assert f'"{t}"' in groups, t                   # every tab is reachable from the nav
    assert 'let current = _hashTab() || "Overview";' in s
    html = UI.read_text(encoding="utf-8")
    assert 'href="/ui/control"' in html                # header link to the control center

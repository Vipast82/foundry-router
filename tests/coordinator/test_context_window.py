"""2026-10-27: every context threshold is a share of the session model's usable window
(Kilo limit.context - limit.output): handoff 95%, the handoff lane 90%, Amber 75% and
Red 90% for the project's checkpoint tool. GF-GATE1 (local Qwen, 262,144 / 32,768) was
handed off three times at 165k-212k by the old fixed 160k ceiling."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from foundry_coordinator import context_window as CW
from foundry_coordinator import infra

KILO = """{
  // the project's Kilo config: model limits per persona
  "provider": {"foundry": {"models": {
    "Foundry-Kilo-Local": {"limit": {"context": 262144, "output": 32768}},
    "Foundry-Kilo-Sonnet": {"limit": {"context": 200000, "output": 8192}}
  }}}
}"""


def project(tmp_path, automation=None):
    (tmp_path / "kilo.jsonc").write_text(KILO, encoding="utf-8")
    raw = {"automation": automation or {}}
    prof = SimpleNamespace(workspace=str(tmp_path), raw=raw, id="game")
    return SimpleNamespace(id="game", profile=prof)


def test_windows_and_ceilings_per_model(tmp_path):
    P = project(tmp_path)
    assert CW.model_limits(P, "Foundry-Kilo-Local") == (262144, 32768)
    assert CW.usable(P, "Foundry-Kilo-Local") == 229376
    assert CW.ceiling(P, "Foundry-Kilo-Local") == int(229376 * 0.95)       # ~217.9k
    assert CW.ceiling(P, "Foundry-Kilo-Sonnet") == int(191808 * 0.95)      # ~182k
    assert CW.ceiling(P, "Unknown-Persona") == 160000                     # fixed fallback
    lane = {"context_threshold": 140000, "context_pct": 90}
    assert CW.lane_threshold(P, "Foundry-Kilo-Local", lane) == int(229376 * 0.90)
    assert CW.lane_threshold(P, "Unknown-Persona", lane) == 140000


def test_ratio_zero_and_ceiling_off(tmp_path):
    P = project(tmp_path, {"context_ceiling_pct": 0})
    assert CW.ceiling(P, "Foundry-Kilo-Local") == 160000
    P = project(tmp_path, {"context_ceiling_tokens": 0})
    assert CW.ceiling(P, "Foundry-Kilo-Local") == 0                      # handoff turned off


def test_the_local_model_is_not_handed_off_at_165k_any_more(tmp_path):
    P = project(tmp_path)
    s = SimpleNamespace(context=164991)
    rep = {"status": "PARTIAL", "summary": "step 6 next"}
    assert infra.over_ceiling(P, s, rep, persona="Foundry-Kilo-Local") is None
    s = SimpleNamespace(context=220000)
    why = infra.over_ceiling(P, s, rep, persona="Foundry-Kilo-Local")
    assert why and "ceiling 217907" in why
    # a 200k model still hands off before Kilo's own compaction (~192k)
    s = SimpleNamespace(context=185000)
    assert infra.over_ceiling(P, s, rep, persona="Foundry-Kilo-Sonnet")


def test_the_window_file_for_the_checkpoint_tool(tmp_path):
    P = project(tmp_path)
    doc = CW.write_file(P, "Foundry-Kilo-Local", "ses_1")
    on_disk = json.loads((tmp_path / ".plan" / "tmp" / "context_window.json").read_text())
    assert on_disk == doc
    assert doc["usable"] == 229376 and doc["amber"] == 172032 and doc["red"] == 206438
    assert doc["handoff"] == 217907 and doc["session_id"] == "ses_1"
    assert CW.write_file(P, "Unknown-Persona") is None
    assert os.path.exists(tmp_path / ".plan" / "tmp" / "context_window.json")

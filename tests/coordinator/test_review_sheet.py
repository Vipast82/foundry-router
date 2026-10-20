"""review_sheet: the per-sprint UI contact sheet built from a fake gate verdict."""

import json
import os
import struct
import zlib

from foundry_coordinator.projects.animaluprising import review_sheet as R

HEAD = "abcdef0123456789abcdef0123456789abcdef01"


def tiny_png(w=2, h=2) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_ws(tmp_path, *, vision=None, sprint="UI-02a"):
    ws = tmp_path / "ws"
    ev = ws / "docs" / "evidence" / "UI-v3" / sprint
    ev.mkdir(parents=True)
    refs = ws / "docs" / "design" / "ui-overhaul" / "v3" / "references"
    refs.mkdir(parents=True)
    caps, checks = [], []
    for story in ("Hud.normal", "Shop.empty"):
        for size in R.SIZES:
            if story == "Shop.empty" and size == "390x844":
                continue                                     # a missing capture cell
            p = ev / f"{story}@{size}.png"
            p.write_bytes(tiny_png())
            bad = story == "Hud.normal" and size == "844x390"
            caps.append({"story": story, "size": size, "path": f"docs/evidence/UI-v3/{sprint}/{p.name}",
                         "geometry_defects": 2 if bad else 0, "capture_size": size,
                         "layout_lines": ["PASS: UI-LAYOUT BOUNDS Hud/x - ok",
                                          "FAIL: UI-LAYOUT TEXTFIT Hud/Top/Title - 120>100 <b>"] if bad else
                                         ["PASS: UI-LAYOUT BOUNDS Hud/x - ok"]})
            checks.append({"id": f"story.{story}@{size}", "result": "FAIL" if bad else "PASS",
                           "required": True, "detail": "2 layout defect(s)" if bad else "ok", "artifacts": []})
    (refs / "Hud.normal@1920x1080.png").write_bytes(tiny_png())
    checks.append({"id": "host.clean_tree", "result": "PASS", "required": True, "detail": "", "artifacts": []})
    checks.append({"id": "console.errors", "result": "FAIL", "required": True, "detail": "1 new: boom",
                   "artifacts": []})
    v = {"schema": 1, "sprint": sprint, "head": HEAD, "mode": "sprint", "started": 1.0e9, "finished": 1.0e9 + 60,
         "result": "FAIL", "checks": checks, "captures": caps, "regression": None,
         "summary_line": f"GATE {sprint} FAIL", "error": None}
    gate = ws / ".plan" / "tmp" / "gate" / sprint
    gate.mkdir(parents=True)
    (gate / f"{HEAD[:12]}.json").write_text(json.dumps(v), encoding="utf-8")
    (gate / "latest.json").write_text(json.dumps(v), encoding="utf-8")
    if vision is not None:
        (gate / f"{HEAD[:12]}.vision.json").write_text(json.dumps(vision), encoding="utf-8")
    return ws, gate


def test_sheet_layout_and_header(tmp_path):
    ws, gate = make_ws(tmp_path)
    path = R.build_sheet(str(ws), str(gate / "latest.json"))
    assert path == os.path.join(str(ws), ".plan", "tmp", "review", "UI-02a", "index.html")
    page = open(path, encoding="utf-8").read()
    assert "UI-02a" in page and HEAD in page and 'badge FAIL' in page
    assert "console.errors" in page and "1 new: boom" in page          # failed checks listed
    assert "host.clean_tree" not in page                                # passed ones not
    assert page.count('<div class="cell"') == 10                       # 2 stories x 5 sizes
    assert page.index('id="Hud.normal"') < page.index('id="Shop.empty"')
    for s in R.SIZES:                                                   # columns in plan order
        assert f'id="Hud.normal@{s}"' in page
    assert page.index("Hud.normal@1920x1080") < page.index("Hud.normal@390x844")
    assert "no capture" in page                                         # Shop.empty@390x844
    # FAIL layout lines only, escaped; PASS lines dropped
    assert "FAIL: UI-LAYOUT TEXTFIT Hud/Top/Title - 120&gt;100 &lt;b&gt;" in page
    assert "PASS: UI-LAYOUT BOUNDS" not in page
    assert "geometry defects: <b>2</b>" in page
    assert "vision verdict: pending" in page and "vision pending" in page
    assert "prefers-color-scheme:dark" in page and 'name="viewport"' in page


def test_images_embedded_under_cap_and_reference_paired(tmp_path):
    ws, gate = make_ws(tmp_path)
    page = open(R.build_sheet(str(ws), str(gate / "latest.json")), encoding="utf-8").read()
    assert page.count("data:image/png;base64,") >= 10                  # 9 captures + 1 ref, each in src+href
    assert "no reference render" in page                                # other cells: placeholder
    cell = page.split('id="Hud.normal@1920x1080"')[1].split('<div class="cell"')[0]
    assert "reference" in cell and cell.count("<img") == 2              # capture + reference side by side


def test_images_linked_relatively_over_cap(tmp_path):
    ws, gate = make_ws(tmp_path)
    page = open(R.build_sheet(str(ws), str(gate / "latest.json"), embed_cap=0), encoding="utf-8").read()
    assert "data:image" not in page
    assert 'src="../../../../docs/evidence/UI-v3/UI-02a/Hud.normal@1920x1080.png"' in page
    assert 'src="../../../../docs/design/ui-overhaul/v3/references/Hud.normal@1920x1080.png"' in page


def test_vision_findings_fill_cells(tmp_path):
    vision = {"verdict": "CHANGES", "findings": [
        {"n": 1, "story": "Hud.normal", "size": "844x390", "text": "title clipped <here>"},
        {"n": 2, "text": "tokens inconsistent overall"}]}
    ws, gate = make_ws(tmp_path, vision=vision)
    page = open(R.build_sheet(str(ws), str(gate / f"{HEAD[:12]}.json")), encoding="utf-8").read()
    assert "vision CHANGES" in page and "vision verdict: pending" not in page
    cell = page.split('id="Hud.normal@844x390"')[1].split('<div class="cell"')[0]
    assert "#1</b> title clipped &lt;here&gt;" in cell
    assert "vision findings (general)" in page and "tokens inconsistent overall" in page
    other = page.split('id="Hud.normal@1920x1080"')[1].split('<div class="cell"')[0]
    assert "vision: no findings (CHANGES)" in other


def test_custom_out_and_references_dir(tmp_path):
    ws, gate = make_ws(tmp_path)
    refs = tmp_path / "otherrefs"
    refs.mkdir()
    (refs / "Shop.empty@1024x768.png").write_bytes(tiny_png())
    out = tmp_path / "sheet"
    path = R.build_sheet(str(ws), str(gate / "latest.json"), str(refs), str(out))
    assert path == str(out / "index.html")
    cell = open(path, encoding="utf-8").read().split('id="Shop.empty@1024x768"')[1].split('<div class="cell"')[0]
    assert cell.count("<img") == 2


def test_cli_uses_latest(tmp_path):
    ws, gate = make_ws(tmp_path)
    lines = []
    assert R.main(["--workspace", str(ws), "--sprint", "UI-02a"], out=lines.append) == 0
    assert lines[-1].startswith("SHEET ") and os.path.isfile(lines[-1][6:])
    lines.clear()
    assert R.main(["--workspace", str(ws), "--sprint", "UI-99"], out=lines.append) == 2
    assert "no verdict" in lines[-1]


def test_empty_verdict_still_renders(tmp_path):
    vp = tmp_path / "v.json"
    vp.write_text(json.dumps({"sprint": "UI-X", "head": None, "result": "ERROR", "error": "studio down",
                              "checks": [], "captures": []}), encoding="utf-8")
    page = open(R.build_sheet(str(tmp_path), str(vp)), encoding="utf-8").read()
    assert "no captures in this verdict" in page and "studio down" in page

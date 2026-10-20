"""reference_render: v3 HTML mock -> PNG references (subprocess mocked; one optional live test)."""

import json
import os
import re
import struct
import zlib

import pytest

from foundry_coordinator.projects.animaluprising import reference_render as RR


def tiny_png(w, h) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


def make_ws(tmp_path, mapping=None):
    v3 = tmp_path / "docs" / "design" / "ui-overhaul" / "v3"
    (v3 / "prototype").mkdir(parents=True, exist_ok=True)
    (v3 / "prototype" / "hud.html").write_text("<html><body>hud</body></html>", encoding="utf-8")
    (v3 / "prototype" / "shop.html").write_text("<html><body>shop</body></html>", encoding="utf-8")
    (v3 / "references.json").write_text(json.dumps(mapping or {
        "Hud.normal": "prototype/hud.html#normal", "Shop.empty": "prototype/shop.html?state=empty"}))
    return tmp_path


class FakeBrowser:
    """Stands in for subprocess: writes a PNG of the --window-size (or a wrong size)."""

    def __init__(self, wrong=None, write=True):
        self.calls, self.wrong, self.write = [], wrong or set(), write

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        opt = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv[1:-1] if "=" in a}
        w, h = map(int, opt["--window-size"].split(","))
        if self.write:
            if (w, h) in self.wrong:
                w, h = w * 2, h * 2                           # e.g. display scaling leaked in
            with open(opt["--screenshot"], "wb") as f:
                f.write(tiny_png(w, h))
        return 0, ""


@pytest.fixture(autouse=True)
def _no_file_wait(monkeypatch, request):
    if request.node.name != "test_live_render_390x844":
        monkeypatch.setattr(RR, "FILE_WAIT", 0.0)          # fakes write synchronously


def always(_p):
    return True


def test_png_size_reads_header():
    assert RR.png_size(tiny_png(390, 844)) == (390, 844)
    assert RR.png_size(b"not a png at all, no sir....") is None


def test_render_all_sizes(tmp_path):
    ws = make_ws(tmp_path)
    fb = FakeBrowser()
    res = RR.render_references(str(ws), browser="C:/fake/chrome.exe", run=fb, exists=always, out=lambda s: None)
    assert len(res) == 10 and all(r["ok"] for r in res)
    out = ws / "docs" / "design" / "ui-overhaul" / "v3" / "references"
    for story in ("Hud.normal", "Shop.empty"):
        for w, h in RR.SIZES:
            p = out / f"{story}@{w}x{h}.png"
            assert RR.png_file_size(str(p)) == (w, h)
    assert not list(out.glob("*.tmp.png"))
    argv = fb.calls[0]
    assert argv[0] == "C:/fake/chrome.exe"
    for flag in ("--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1920,1080"):
        assert flag in argv
    assert any(a.startswith("--user-data-dir=") for a in argv)
    assert argv[-1].startswith("file:///") and argv[-1].endswith("prototype/hud.html#normal")
    assert any(c[-1].endswith("prototype/shop.html?state=empty") for c in fb.calls)


def test_story_filter_and_size_mismatch(tmp_path):
    ws = make_ws(tmp_path)
    fb = FakeBrowser(wrong={(390, 844)})
    lines = []
    res = RR.render_references(str(ws), ["Hud.normal"], browser="x", run=fb, exists=always, out=lines.append)
    assert len(fb.calls) == 5 and {r["story"] for r in res} == {"Hud.normal"}
    bad = [r for r in res if not r["ok"]]
    assert len(bad) == 1 and "780x1688, expected 390x844" in bad[0]["detail"]
    assert not os.path.exists(bad[0]["path"])
    assert any(ln.startswith("FAIL: REF Hud.normal@390x844") for ln in lines)


def test_no_png_written_is_a_failure(tmp_path):
    ws = make_ws(tmp_path)
    res = RR.render_references(str(ws), ["Hud.normal"], [(390, 844)], browser="x",
                               run=FakeBrowser(write=False), exists=always, out=lambda s: None)
    assert not res[0]["ok"] and "no PNG written" in res[0]["detail"]


def test_wait_png_polls_until_written(tmp_path):
    p = str(tmp_path / "late.png")
    ticks = []

    def sleep(_s):                         # the "browser" writes the file after a few polls
        ticks.append(1)
        if len(ticks) == 3:
            with open(p, "wb") as f:
                f.write(tiny_png(10, 20))
    assert RR.wait_png(p, 30.0, sleep) == (10, 20)


def test_find_browser():
    env = {"PROGRAMFILES(X86)": r"C:\PF86", "PROGRAMFILES": r"C:\PF", "LOCALAPPDATA": r"C:\LAD"}
    edge = os.path.join(r"C:\PF86", r"Microsoft\Edge\Application\msedge.exe")
    assert RR.find_browser(exists=lambda p: p == edge, env=env) == edge
    with pytest.raises(RR.RenderError, match="no Chromium-family browser"):
        RR.find_browser(exists=lambda p: False, env=env)
    with pytest.raises(RR.RenderError, match="browser not found at"):
        RR.find_browser("C:/nope.exe", exists=lambda p: False)


def test_setup_errors(tmp_path):
    lines = []
    assert RR.main(["--workspace", str(tmp_path)], out=lines.append) == 2
    assert "mapping file missing" in lines[-1]
    ws = make_ws(tmp_path)
    assert RR.main(["--workspace", str(ws), "--story", "Nope.x", "--browser", "x"], out=lines.append,
                   run=FakeBrowser(), exists=always) == 2
    assert "Nope.x" in lines[-1]
    make_ws(tmp_path, {"Hud.gone": "prototype/missing.html"})
    assert RR.main(["--workspace", str(ws), "--browser", "x"], out=lines.append,
                   run=FakeBrowser(), exists=always) == 2
    assert "mock page not found" in lines[-1]
    assert RR.main(["--workspace", str(ws), "--size", "big"], out=lines.append) == 2


def test_cli_ok_and_sizes(tmp_path):
    ws = make_ws(tmp_path)
    lines, fb = [], FakeBrowser()
    rc = RR.main(["--workspace", str(ws), "--story", "Shop.empty", "--size", "844x390", "--browser", "x"],
                 out=lines.append, run=fb, exists=always)
    assert rc == 0 and len(fb.calls) == 1 and lines[-1] == "REFERENCES 1/1 rendered"


def _browser_or_none():
    try:
        return RR.find_browser()
    except RR.RenderError:
        return None


@pytest.mark.skipif(_browser_or_none() is None, reason="no Chromium-family browser installed")
def test_live_render_390x844(tmp_path):
    ws = make_ws(tmp_path, {"Live.page": "prototype/hud.html#live"})
    res = RR.render_references(str(ws), sizes=[(390, 844)], out=lambda s: None)
    assert res[0]["ok"], res[0]["detail"]
    assert RR.png_file_size(res[0]["path"]) == (390, 844)
    assert re.search(r"Live\.page@390x844\.png$", res[0]["path"])


def solid_png(w, h, band=0, strip=0, page=(0, 255, 0), root=(255, 0, 0)) -> bytes:
    """An RGB PNG of the page colour with `band` rows / `strip` columns of root colour at the
    bottom / right (Up-filtered rows, like Chrome's encoder)."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    rows, prev = [], bytes(w * 3)
    for y in range(h):
        line = b"".join(bytes(root if (y >= h - band or x >= w - strip) else page) for x in range(w))
        rows.append(b"\x02" + bytes((a - b) & 255 for a, b in zip(line, prev)))
        prev = line
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


class BandBrowser(FakeBrowser):
    """UI-D1's broken shell: the frame is WxH but the page viewport is 18px/96px short."""

    def __init__(self, band=96, strip=18, flaky=0):
        super().__init__()
        self.band, self.strip, self.flaky = band, strip, flaky

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        opt = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv[1:-1] if "=" in a}
        w, h = map(int, opt["--window-size"].split(","))
        bad = self.flaky == 0 or len(self.calls) <= self.flaky
        with open(opt["--screenshot"], "wb") as f:
            f.write(solid_png(w, h, *((self.band, self.strip) if bad else (0, 0))))
        return 0, ""


def test_png_pixels_reads_filtered_rows(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(solid_png(40, 30, band=5, strip=3))
    assert RR.png_pixels(str(p), [(0, 0), (39, 0), (0, 29), (20, 10)]) == \
        [(0, 255, 0), (255, 0, 0), (255, 0, 0), (0, 255, 0)]


def test_calibration_catches_the_short_viewport(monkeypatch):
    monkeypatch.setattr(RR, "_CALIBRATED", {})
    why = RR.calibrate("chrome.exe", 120, 100, run=BandBrowser(band=10, strip=4))
    assert why and "does not fill the frame" in why and "119,99" in why
    monkeypatch.setattr(RR, "_CALIBRATED", {})
    assert RR.calibrate("chrome.exe", 120, 100, run=BandBrowser(band=0, strip=0)) is None
    monkeypatch.setattr(RR, "_CALIBRATED", {})
    flaky = BandBrowser(band=10, strip=4, flaky=1)                # first launch only: retried
    assert RR.calibrate("chrome.exe", 120, 100, run=flaky) is None and len(flaky.calls) == 2


def test_short_viewport_is_noted_on_each_frame(tmp_path, monkeypatch):
    """A diagnostic, never a refusal: a mock that sizes itself from the outer window
    (UI-D1 b8fd0e6) still renders correctly in such a shell."""
    ws = make_ws(tmp_path)
    monkeypatch.setattr(RR, "_CALIBRATED", {})
    band = BandBrowser(band=10, strip=4)
    monkeypatch.setattr(RR, "default_run", band)
    [r] = RR.render_references(str(ws), ["Hud.normal"], [(120, 100)], browser="chrome.exe",
                               exists=always, run=band)
    assert r["ok"] and "does not fill the frame" in r["viewport_note"]
    monkeypatch.setattr(RR, "_CALIBRATED", {})
    [r] = RR.render_references(str(ws), ["Hud.normal"], [(120, 100)], browser="chrome.exe",
                               exists=always, run=FakeBrowser())       # fakes: no calibration
    assert "viewport_note" not in r

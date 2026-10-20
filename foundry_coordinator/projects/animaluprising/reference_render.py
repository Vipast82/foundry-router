"""Reference renderer for the AU UI rework (UI-T0; plan AU_UI_REWORK_PLAN.md 6.1 / 6.5).

Renders the approved HTML mock pages to PNG at the five AU device sizes with a locally
installed Chromium-family browser in headless mode (msedge.exe / chrome.exe; one
subprocess per frame), so the contact sheet (review_sheet.py) and the vision approval
can put the reference next to each Studio capture. Stdlib only.

Mapping file <workspace>/docs/design/ui-overhaul/v3/references.json:
  {"Hud.normal": "prototype/hud.html#normal", "Shop.empty": "prototype/shop.html?state=empty"}
Page paths are relative to the mapping file's directory (absolute paths allowed); an
optional '#fragment' or '?query' selects the state. Output, by default:
  <workspace>/docs/design/ui-overhaul/v3/references/<story>@<WxH>.png

Usage:
  py -m foundry_coordinator.projects.animaluprising.reference_render --workspace W [--story S ...]
      [--size 390x844 ...] [--browser PATH] [--out DIR]
Exit codes: 0 every frame rendered at the exact size, 1 some frame failed, 2 setup error
(no browser, no mapping file, unknown story).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

SIZES = [(1920, 1080), (1366, 768), (1024, 768), (844, 390), (390, 844)]
REF_SUBDIR = os.path.join("docs", "design", "ui-overhaul", "v3")
MAPPING_NAME = "references.json"
RENDER_TIMEOUT = 60.0
FILE_WAIT = 20.0          # seconds to wait for a PNG after the browser process returned


class RenderError(RuntimeError):
    pass


def png_size(data: bytes) -> Optional[tuple]:
    """(width, height) from a PNG's IHDR chunk, or None when it isn't a PNG."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def png_file_size(path: str) -> Optional[tuple]:
    try:
        with open(path, "rb") as f:
            return png_size(f.read(24))
    except OSError:
        return None


def size_name(w: int, h: int) -> str:
    return f"{w}x{h}"


def parse_size(s: str) -> tuple:
    m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", s or "")
    if not m:
        raise ValueError(f"bad size {s!r} (want WxH)")
    return int(m.group(1)), int(m.group(2))


def safe_name(story: str) -> str:
    """A story name as a Windows-safe file stem ('Hud.normal' stays as is)."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", story).strip(" .") or "_"


def ref_filename(story: str, w: int, h: int) -> str:
    return f"{safe_name(story)}@{size_name(w, h)}.png"


def browser_candidates(env=None) -> list:
    """Standard Windows install locations for Edge and Chrome (per-machine and per-user),
    then PATH lookups; non-Windows names last so the module still works elsewhere."""
    env = os.environ if env is None else env
    roots = [env.get("PROGRAMFILES(X86)"), env.get("PROGRAMFILES"), env.get("LOCALAPPDATA"),
             r"C:\Program Files (x86)", r"C:\Program Files"]
    # Chrome first: its --screenshot run is synchronous; msedge.exe can return before the
    # renderer has written the file (render_one waits for it either way).
    rel = [r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
           r"Chromium\Application\chrome.exe", r"BraveSoftware\Brave-Browser\Application\brave.exe"]
    out = []
    for r in rel:
        for root in roots:
            if root:
                p = os.path.join(root, r)
                if p not in out:
                    out.append(p)
    for name in ("msedge", "chrome", "chromium", "google-chrome", "chromium-browser", "microsoft-edge"):
        p = shutil.which(name)
        if p and p not in out:
            out.append(p)
    return out


def find_browser(explicit: Optional[str] = None, *, exists: Callable = os.path.isfile, env=None) -> str:
    if explicit:
        if exists(explicit):
            return explicit
        raise RenderError(f"browser not found at {explicit}")
    env_b = (os.environ if env is None else env).get("AU_REFERENCE_BROWSER")
    if env_b and exists(env_b):
        return env_b
    for p in browser_candidates(env):
        if exists(p):
            return p
    raise RenderError("no Chromium-family browser found (looked for msedge.exe / chrome.exe in "
                      "Program Files, Program Files (x86), %LOCALAPPDATA% and PATH); install Edge or "
                      "Chrome, or pass --browser PATH / set AU_REFERENCE_BROWSER")


def page_url(page: str, base_dir: str) -> str:
    """'relative/page.html#state' -> file:///abs/page.html#state (query kept too)."""
    m = re.match(r"^([^?#]*)(.*)$", page)
    path, suffix = m.group(1), m.group(2)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", page):
        return page                                    # already a URL (file://, http://localhost)
    full = path if os.path.isabs(path) else os.path.join(base_dir, path)
    full = os.path.abspath(full)
    if not os.path.isfile(full):
        raise RenderError(f"mock page not found: {full}")
    return Path(full).as_uri() + suffix


def browser_argv(browser: str, url: str, out_png: str, w: int, h: int, profile_dir: str) -> list:
    # Own throwaway profile: never attaches to (or disturbs) the user's running browser.
    return [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--force-device-scale-factor=1", f"--user-data-dir={profile_dir}",
            f"--window-size={w},{h}", f"--screenshot={out_png}", url]


def default_run(argv: list, timeout: float) -> tuple:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as e:
        return 127, f"cannot run {argv[0]}: {e}"


def wait_png(path: str, wait: float, sleep: Callable = time.sleep) -> Optional[tuple]:
    """PNG size once the file exists with a readable header and a stable length."""
    deadline, last = time.monotonic() + wait, -1
    while True:
        got = png_file_size(path)
        n = os.path.getsize(path) if got else -1
        if got and n == last:
            return got
        last = n
        if time.monotonic() >= deadline:
            return got
        sleep(0.25)


def render_one(browser: str, url: str, out_png: str, w: int, h: int, *,
               run: Callable = default_run, timeout: float = RENDER_TIMEOUT,
               file_wait: Optional[float] = None, sleep: Callable = time.sleep) -> dict:
    """Render one frame; result {"path","size","ok","detail"}. The PNG header must say WxH."""
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    tmp_png = out_png + ".tmp.png"
    if os.path.exists(tmp_png):
        os.remove(tmp_png)
    profile = tempfile.mkdtemp(prefix="au-refrender-")
    try:
        rc, text = run(browser_argv(browser, url, os.path.abspath(tmp_png), w, h, profile), timeout)
        wait = (FILE_WAIT if file_wait is None else file_wait) if rc == 0 else 0.0
        got = wait_png(tmp_png, wait, sleep)
    finally:
        shutil.rmtree(profile, ignore_errors=True)    # Edge may still hold it: best effort
    res = {"path": out_png, "size": size_name(w, h), "ok": False, "detail": ""}
    if got is None:
        res["detail"] = f"no PNG written (exit {rc}): {(text or '').strip()[-200:]}"
        return res
    if got != (w, h):
        os.remove(tmp_png)
        res["detail"] = f"PNG is {size_name(*got)}, expected {size_name(w, h)}"
        return res
    os.replace(tmp_png, out_png)
    res.update(ok=True, detail="ok")
    return res


def mapping_path(workspace: str) -> str:
    return os.path.join(workspace, REF_SUBDIR, MAPPING_NAME)


def default_out_dir(workspace: str) -> str:
    return os.path.join(workspace, REF_SUBDIR, "references")


def load_mapping(workspace: str, path: Optional[str] = None) -> dict:
    path = path or mapping_path(workspace)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RenderError(f"mapping file missing: {path} (want {{story: \"relative/page.html#state\"}})")
    except (OSError, ValueError) as e:
        raise RenderError(f"mapping file unreadable: {path}: {e}")
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise RenderError(f"mapping file must be an object of story -> page string: {path}")
    return data


def render_references(workspace: str, stories: Optional[list] = None, sizes: Optional[list] = None, *,
                      browser: Optional[str] = None, out_dir: Optional[str] = None,
                      mapping_file: Optional[str] = None, run: Callable = default_run,
                      exists: Callable = os.path.isfile, out=print) -> list:
    """Render every (story, size) frame; returns the per-frame result dicts (raises
    RenderError on setup problems: no browser, no/bad mapping, unknown story, missing page)."""
    workspace = os.path.abspath(workspace)
    mfile = mapping_file or mapping_path(workspace)
    mapping = load_mapping(workspace, mfile)
    if stories:
        unknown = [s for s in stories if s not in mapping]
        if unknown:
            raise RenderError(f"stories not in {MAPPING_NAME}: {', '.join(unknown)}")
        mapping = {s: mapping[s] for s in stories}
    exe = find_browser(browser, exists=exists)
    out_dir = out_dir or default_out_dir(workspace)
    base = os.path.dirname(os.path.abspath(mfile))
    urls = {s: page_url(p, base) for s, p in mapping.items()}       # fail before any render
    results = []
    for story, url in urls.items():
        for w, h in sizes or SIZES:
            r = render_one(exe, url, os.path.join(out_dir, ref_filename(story, w, h)), w, h, run=run)
            r["story"] = story
            results.append(r)
            out(f"{'PASS' if r['ok'] else 'FAIL'}: REF {story}@{r['size']} - {r['detail']}")
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="reference_render", description="Render v3 HTML mocks to PNG references.")
    ap.add_argument("--workspace", required=True, help="AU checkout (holds docs/design/ui-overhaul/v3/)")
    ap.add_argument("--story", action="append", help="story name from references.json (repeatable)")
    ap.add_argument("--size", action="append", help="WxH (repeatable; default the five AU sizes)")
    ap.add_argument("--browser", help="path to msedge.exe / chrome.exe")
    ap.add_argument("--out", help="output directory (default <v3>/references)")
    ap.add_argument("--mapping", help="mapping file (default <v3>/references.json)")
    return ap


def main(argv=None, out=print, **inject) -> int:
    a = build_parser().parse_args(argv)
    try:
        sizes = [parse_size(s) for s in a.size] if a.size else None
        res = render_references(a.workspace, a.story, sizes, browser=a.browser, out_dir=a.out,
                                mapping_file=a.mapping, out=out, **inject)
    except (RenderError, ValueError) as e:
        out(f"ERROR: {e}")
        return 2
    bad = [r for r in res if not r["ok"]]
    out(f"REFERENCES {len(res) - len(bad)}/{len(res)} rendered")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

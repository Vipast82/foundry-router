"""Per-sprint contact sheet for the AU studio gate (UI-T0; plan AU_UI_REWORK_PLAN.md 6.1 / 6.5).

One self-contained HTML page per gate verdict: rows = story (screen.state), columns = the
five AU sizes; each cell shows the Studio capture next to the matching v3 reference render
(reference_render.py), the geometry defect count, the FAIL layout lines, and the vision
verdict's findings for that cell when `<head12>.vision.json` sits next to the verdict:
  {"verdict": "APPROVED|CHANGES|...", "findings": [{"n": 1, "story": "Hud.normal",
                                                   "size": "390x844", "text": "..."}]}
(findings without story/size are listed in the header). Images are embedded as data URIs
when under `embed_cap` bytes, otherwise referenced by a path relative to the sheet.
Victor can open it any time; nothing reads it back. Stdlib only.

Output: <workspace>/.plan/tmp/review/<SPRINT>/index.html (git-ignored under tmp/).

Usage:
  py -m foundry_coordinator.projects.animaluprising.review_sheet --workspace W --sprint ID
      [--verdict PATH] [--references DIR] [--out DIR] [--embed-cap BYTES]
(default verdict: <workspace>/.plan/tmp/gate/<ID>/latest.json). Prints the sheet path.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

SIZES = ["1920x1080", "1366x768", "1024x768", "844x390", "390x844"]
REF_SUBDIR = os.path.join("docs", "design", "ui-overhaul", "v3", "references")
EMBED_CAP = 300_000          # bytes per image; larger files are linked relatively
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def safe_name(story: str) -> str:
    """Same file stem rule as reference_render.safe_name (kept local: no cross-import)."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", story).strip(" .") or "_"


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def vision_path(verdict_path: str, verdict: dict) -> str:
    head = str(verdict.get("head") or "")[:12]
    stem = head or os.path.basename(verdict_path).split(".")[0]
    return os.path.join(os.path.dirname(os.path.abspath(verdict_path)), f"{stem}.vision.json")


def img_src(path: str, out_dir: str, embed_cap: int) -> Optional[str]:
    """data: URI under the cap, else a sheet-relative URL (file: URI across drives)."""
    if not path or not os.path.isfile(path):
        return None
    mime = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    try:
        if os.path.getsize(path) <= embed_cap:
            with open(path, "rb") as f:
                return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
    except OSError:
        return None
    try:
        return os.path.relpath(path, out_dir).replace("\\", "/").replace(" ", "%20").replace("#", "%23")
    except ValueError:
        return Path(os.path.abspath(path)).as_uri()


def ordered(values, preferred: list) -> list:
    seen = list(dict.fromkeys(v for v in values if v))
    return [p for p in preferred if p in seen] + [v for v in seen if v not in preferred]


def fail_lines(cap: dict) -> list:
    return [ln for ln in cap.get("layout_lines") or [] if isinstance(ln, str) and ln.lstrip().startswith("FAIL")]


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mute:#5d6672;--line:#d8dde3;--pass:#1f7a3a;
--fail:#b42318;--warn:#9a6700;--chip:#eef1f4;--code:#f2f4f7}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--fg:#e6e9ed;--mute:#9aa4af;
--line:#2c333b;--pass:#4ac26b;--fail:#ff7b72;--warn:#d29922;--chip:#242b33;--code:#20262d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header,main{padding:12px 16px}
header{border-bottom:1px solid var(--line);background:var(--card)}
h1{font-size:18px;margin:0 0 6px}
h2{font-size:16px;margin:18px 0 8px}
.meta{color:var(--mute);font-size:13px;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-weight:600;font-size:12px;background:var(--chip)}
.PASS,.APPROVED{color:var(--pass)}.FAIL,.ERROR,.CHANGES{color:var(--fail)}.UNKNOWN,.SKIP,.PENDING{color:var(--warn)}
ul.fails{margin:6px 0 0;padding-left:18px}
.row{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}
.cell{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;min-width:0}
.cell h3{font-size:13px;margin:0 0 6px;display:flex;justify-content:space-between;gap:6px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.pair.solo{grid-template-columns:1fr}
figure{margin:0;min-width:0}
figcaption{font-size:11px;color:var(--mute)}
img{display:block;max-width:100%;height:auto;border:1px solid var(--line);border-radius:4px;background:#000}
.missing{display:flex;align-items:center;justify-content:center;min-height:60px;border:1px dashed var(--line);
border-radius:4px;color:var(--mute);font-size:12px;text-align:center;padding:6px}
pre{margin:6px 0 0;padding:6px;background:var(--code);border-radius:4px;font-size:11px;white-space:pre-wrap;
word-break:break-word;max-height:160px;overflow:auto}
.vision{margin-top:6px;padding:6px;border-left:3px solid var(--line);background:var(--code);font-size:12px}
.vision.pending{color:var(--mute);font-style:italic}
@media (max-width:420px){.row{grid-template-columns:1fr}.pair{grid-template-columns:1fr}}
"""


def _figure(src: Optional[str], label: str, missing: str) -> str:
    if src:
        return (f'<figure><a href="{esc(src)}" target="_blank" rel="noopener"><img loading="lazy" '
                f'src="{esc(src)}" alt="{esc(label)}"></a><figcaption>{esc(label)}</figcaption></figure>')
    return f'<figure><div class="missing">{esc(missing)}</div><figcaption>{esc(label)}</figcaption></figure>'


def _cell(story: str, size: str, cap: Optional[dict], check: Optional[dict], ws: str, refs: str,
          out_dir: str, embed_cap: int, vision: Optional[dict], findings: list) -> str:
    cap = cap or {}
    cap_path = cap.get("path")
    cap_abs = os.path.join(ws, cap_path) if cap_path and not os.path.isabs(cap_path) else cap_path
    ref_abs = os.path.join(refs, f"{safe_name(story)}@{size}.png")
    ref_src = img_src(ref_abs, out_dir, embed_cap)
    cap_src = img_src(cap_abs, out_dir, embed_cap) if cap_abs else None
    res = (check or {}).get("result") or ("UNKNOWN" if cap else "MISSING")
    d = cap.get("geometry_defects")
    parts = [f'<div class="cell" id="{esc(story)}@{esc(size)}"><h3><span>{esc(size)}</span>'
             f'<span class="badge {esc(res)}">{esc(res)}</span></h3>']
    pair = [_figure(cap_src, "capture" + (f" {cap['capture_size']}" if cap.get("capture_size") else ""),
                    "no capture" if not cap_path else f"capture missing: {cap_path}")]
    if ref_src or os.path.isdir(refs):
        pair.append(_figure(ref_src, "reference", "no reference render"))
    parts.append(f'<div class="pair{"" if len(pair) > 1 else " solo"}">{"".join(pair)}</div>')
    parts.append(f'<div class="meta">geometry defects: <b>{esc("?" if d is None else d)}</b></div>')
    if check and check.get("result") != "PASS" and check.get("detail"):
        parts.append(f'<div class="meta">{esc(check["detail"])}</div>')
    fl = fail_lines(cap)
    if fl:
        parts.append(f"<pre>{esc(chr(10).join(fl))}</pre>")
    if vision is None:
        parts.append('<div class="vision pending">vision verdict: pending</div>')
    elif findings:
        items = "".join(f"<li><b>#{esc(f.get('n'))}</b> {esc(f.get('text'))}</li>" for f in findings)
        parts.append(f'<div class="vision"><b>vision findings</b><ul class="fails">{items}</ul></div>')
    else:
        parts.append(f'<div class="vision">vision: no findings ({esc(vision.get("verdict") or "?")})</div>')
    parts.append("</div>")
    return "".join(parts)


def render_html(verdict: dict, workspace: str, references_dir: str, out_dir: str, *,
                vision: Optional[dict] = None, embed_cap: int = EMBED_CAP) -> str:
    sprint, head = verdict.get("sprint") or "?", str(verdict.get("head") or "")
    result = verdict.get("result") or "?"
    caps = [c for c in verdict.get("captures") or [] if isinstance(c, dict)]
    checks = {c.get("id"): c for c in verdict.get("checks") or [] if isinstance(c, dict)}
    by = {(c.get("story"), c.get("size")): c for c in caps}
    stories = ordered([c.get("story") for c in caps], [])
    sizes = list(SIZES) + [s for s in ordered([c.get("size") for c in caps], []) if s not in SIZES]
    findings = [f for f in (vision or {}).get("findings") or [] if isinstance(f, dict)]
    failed = [c for c in checks.values() if c.get("result") not in ("PASS", "SKIP")]
    when = verdict.get("finished") or verdict.get("started")
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>Review {esc(sprint)}</title><style>{CSS}</style></head><body><header>",
           f'<h1>{esc(sprint)} <span class="badge {esc(result)}">{esc(result)}</span>']
    if vision is not None:
        vv = vision.get("verdict") or "?"
        out.append(f' <span class="badge {esc(vv)}">vision {esc(vv)}</span>')
    else:
        out.append(' <span class="badge PENDING">vision pending</span>')
    out.append("</h1>")
    out.append(f'<div class="meta">head <code>{esc(head)}</code> · mode {esc(verdict.get("mode"))}'
               + (f" · {esc(time.strftime('%Y-%m-%d %H:%M', time.localtime(when)))}" if isinstance(when, (int, float)) and when else "")
               + f" · {len(stories)} stor{'y' if len(stories) == 1 else 'ies'} × {len(sizes)} sizes</div>")
    if verdict.get("summary_line"):
        out.append(f'<div class="meta">{esc(verdict["summary_line"])}</div>')
    if verdict.get("error"):
        out.append(f'<div class="meta FAIL">error: {esc(verdict["error"])}</div>')
    if failed:
        out.append(f"<div><b>Failed checks ({len(failed)})</b><ul class=\"fails\">")
        out.extend(f'<li><span class="{esc(c.get("result"))}">{esc(c.get("result"))}</span> '
                   f'<code>{esc(c.get("id"))}</code> — {esc(c.get("detail"))}</li>' for c in failed)
        out.append("</ul></div>")
    else:
        out.append('<div class="meta">no failed checks</div>')
    loose = [f for f in findings if not (f.get("story") and f.get("size"))]
    if loose:
        out.append('<div class="vision"><b>vision findings (general)</b><ul class="fails">')
        out.extend(f"<li><b>#{esc(f.get('n'))}</b> {esc(f.get('text'))}</li>" for f in loose)
        out.append("</ul></div>")
    out.append("</header><main>")
    if not stories:
        out.append('<p class="meta">no captures in this verdict</p>')
    for story in stories:
        out.append(f'<h2 id="{esc(story)}">{esc(story)}</h2><div class="row">')
        for size in sizes:
            cap = by.get((story, size))
            if cap is None and size not in SIZES:
                continue
            cell_f = [f for f in findings if f.get("story") == story and f.get("size") == size]
            out.append(_cell(story, size, cap, checks.get(f"story.{story}@{size}"), workspace,
                             references_dir, out_dir, embed_cap, vision, cell_f))
        out.append("</div>")
    out.append("</main></body></html>\n")
    return "".join(out)


def build_sheet(workspace: str, verdict_path: str, references_dir: Optional[str] = None,
                out_dir: Optional[str] = None, *, embed_cap: int = EMBED_CAP) -> str:
    """Write the contact sheet for one verdict JSON; returns the index.html path."""
    workspace = os.path.abspath(workspace)
    verdict = _read_json(verdict_path)
    if not isinstance(verdict, dict):
        raise ValueError(f"verdict unreadable: {verdict_path}")
    sprint = verdict.get("sprint") or "unknown"
    references_dir = os.path.abspath(references_dir or os.path.join(workspace, REF_SUBDIR))
    out_dir = os.path.abspath(out_dir or os.path.join(workspace, ".plan", "tmp", "review", sprint))
    vision = _read_json(vision_path(verdict_path, verdict))
    page = render_html(verdict, workspace, references_dir, out_dir,
                       vision=vision if isinstance(vision, dict) else None, embed_cap=embed_cap)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    os.replace(tmp, path)
    return path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="review_sheet", description="Build the per-sprint UI contact sheet.")
    ap.add_argument("--workspace", required=True, help="AU checkout")
    ap.add_argument("--sprint", required=True)
    ap.add_argument("--verdict", help="verdict JSON (default .plan/tmp/gate/<ID>/latest.json)")
    ap.add_argument("--references", help="reference renders dir")
    ap.add_argument("--out", help="output dir (default .plan/tmp/review/<ID>)")
    ap.add_argument("--embed-cap", type=int, default=EMBED_CAP, help="max bytes to embed per image")
    return ap


def main(argv=None, out=print) -> int:
    a = build_parser().parse_args(argv)
    vp = a.verdict or os.path.join(a.workspace, ".plan", "tmp", "gate", a.sprint, "latest.json")
    if not os.path.isfile(vp):
        out(f"ERROR: no verdict at {vp}")
        return 2
    try:
        path = build_sheet(a.workspace, vp, a.references, a.out or os.path.join(
            a.workspace, ".plan", "tmp", "review", a.sprint), embed_cap=a.embed_cap)
    except ValueError as e:
        out(f"ERROR: {e}")
        return 2
    out(f"SHEET {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

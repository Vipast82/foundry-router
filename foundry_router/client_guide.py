"""Foundry Client downloads and install guide (v0.139.0).

One source of truth: docs/CLIENT_MANAGER.md. This module extracts the
install-relevant sections from that file and renders them with a small,
escaping markdown subset, so the dashboard's Client tab and the operator
chat's read tools always show what the doc says — nothing is hand-copied.

Downloads are LINKS to the GitHub release that matches the running router
version, never proxied: the repository is public, its releases carry the
installers built by .github/workflows/client-release.yml, and linking keeps
binaries (and GitHub auth) out of the router entirely. When the running
version has no release (an untagged dev build) or the release has no assets
yet (CI still building), the page says so honestly and offers the latest
release as a labeled fallback. GitHub answers are cached (1 h, 10 min after
an error); an unreachable GitHub degrades to best-guess links marked
unverified.

Auth posture: the /admin/api/client/* endpoints live with the rest of the
dashboard — open on the internal network by default, and covered by
`client_auth.protect_admin` when that is switched on. Nothing here widens
access: the payloads hold no secrets (public release links, a tracked doc,
and the same heartbeat metadata the dashboard already shows).
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

REPO = "Vipast82/foundry-router"
DOC = Path(__file__).resolve().parents[1] / "docs" / "CLIENT_MANAGER.md"
# Sections of docs/CLIENT_MANAGER.md shown as the install guide, in this order.
# Matching is on the heading text (case-insensitive, prefix), so cosmetic
# heading edits don't silently drop a section.
GUIDE_SECTIONS = ("Install", "First run", "Router key for the relay", "Tray",
                  "Commands", "Troubleshooting", "Migrating this workstation")
CACHE_TTL_S = 3600
ERROR_TTL_S = 600


def parse_version(v: str) -> tuple:
    nums = re.findall(r"\d+", str(v).split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


# --------------------------------------------------------------------------- #
# Guide: extract sections from docs/CLIENT_MANAGER.md and render them         #
# --------------------------------------------------------------------------- #

def _split_sections(text: str) -> list[dict]:
    """Every ##/### section of the doc: {title, level, text} (fenced code can
    hold # lines, so heading detection is fence-aware)."""
    out: list[dict] = []
    cur: Optional[dict] = None
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        m = None if fenced else re.match(r"^(#{2,3})\s+(.+?)\s*$", line)
        if m:
            cur = {"title": m.group(2), "level": len(m.group(1)), "lines": []}
            out.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    return [{"title": s["title"], "level": s["level"],
             "text": "\n".join(s["lines"]).strip()} for s in out]


def _anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "section"


def load_guide() -> dict:
    """The install guide as data: ordered sections with ids, a content hash,
    and where it came from. Missing doc -> missing: True and no sections."""
    try:
        text = DOC.read_text(encoding="utf-8")
    except OSError:
        return {"source": "docs/CLIENT_MANAGER.md", "missing": True, "sections": [],
                "hash": ""}
    picked = []
    sections = _split_sections(text)
    for want in GUIDE_SECTIONS:
        for s in sections:
            if s["title"].lower().startswith(want.lower()):
                picked.append({"id": _anchor(want), "title": s["title"], "text": s["text"]})
                break
    h = hashlib.sha256("\n\n".join(s["text"] for s in picked).encode()).hexdigest()[:12]
    return {"source": "docs/CLIENT_MANAGER.md", "missing": False, "sections": picked,
            "hash": h}


_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(s: str) -> str:
    """Escaped text with the inline subset: code spans, bold, http(s) links."""
    out, i = [], 0
    for m in _INLINE_CODE.finditer(s):
        out.append(_bold_links(s[i:m.start()]))
        out.append(f"<code>{html.escape(m.group(1))}</code>")
        i = m.end()
    out.append(_bold_links(s[i:]))
    return "".join(out)


def _bold_links(s: str) -> str:
    s = html.escape(s, quote=False)
    s = _BOLD.sub(r"<b>\1</b>", s)
    return _LINK.sub(r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)


def _md_html(text: str) -> str:
    """A deterministic subset of markdown -> HTML (everything escaped):
    paragraphs, - and 1. lists, | tables, ``` code blocks, inline code,
    bold and plain http(s) links. Enough for CLIENT_MANAGER.md; anything
    unrecognised renders as a paragraph."""
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
            continue
        if line.lstrip().startswith(("- ", "* ")) or re.match(r"^\s*\d+\.\s", line):
            ordered = bool(re.match(r"^\s*\d+\.\s", line))
            items = []
            while i < len(lines) and (lines[i].lstrip().startswith(("- ", "* "))
                                      or re.match(r"^\s*\d+\.\s", lines[i])
                                      or (items and lines[i].startswith("  ") and lines[i].strip())):
                s = lines[i].strip()
                if s.startswith(("- ", "* ")) or re.match(r"^\d+\.\s", s):
                    items.append(re.sub(r"^(?:[-*]|\d+\.)\s+", "", s))
                else:
                    items[-1] += " " + s          # wrapped continuation line
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?\s*$", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], rows[2:]
            out.append("<table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                                 for r in body) + "</table>")
            continue
        if not line.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("```", "|")) \
                and not lines[i].lstrip().startswith(("- ", "* ")) \
                and not re.match(r"^\s*\d+\.\s", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def guide_html() -> dict:
    """{html, hash, source, missing}: a TOC plus every guide section rendered,
    ready to drop into the dashboard page (ids are stable anchors)."""
    g = load_guide()
    if g["missing"]:
        return {**g, "html": "<p class=\"hint\">docs/CLIENT_MANAGER.md was not found next to this "
                             "checkout, so the install guide cannot be shown. The doc lives in the "
                             "foundry-router repository.</p>"}
    toc = "<ul class=\"guide-toc\">" + "".join(
        f'<li><a href="#cg-{s["id"]}">{html.escape(s["title"])}</a></li>' for s in g["sections"]) + "</ul>"
    parts = [toc]
    for s in g["sections"]:
        parts.append(f'<h3 id="cg-{s["id"]}">{html.escape(s["title"])}</h3>')
        parts.append(_md_html(s["text"]))
    return {"source": g["source"], "missing": False, "hash": g["hash"],
            "html": "\n".join(parts)}


# --------------------------------------------------------------------------- #
# Release availability: does GitHub have installers for this version?         #
# --------------------------------------------------------------------------- #

async def _fetch_github(url: str) -> tuple[int, dict]:
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as c:
        r = await c.get(url, headers={"Accept": "application/vnd.github+json"})
        try:
            body = r.json()
        except Exception:                                        # noqa: BLE001
            body = {}
        return r.status_code, body if isinstance(body, dict) else {}


def _asset_os(name: str) -> Optional[str]:
    if name.lower().endswith(".exe"):
        return "windows"
    if name.lower().endswith(".appimage"):
        return "linux"
    return None


def _guessed_assets(repo: str, version: str) -> list[dict]:
    """The deterministic asset names client-release.yml produces, as unverified
    links — shown when GitHub cannot be asked whether they exist."""
    base = f"https://github.com/{repo}/releases/download/v{version}"
    return [{"name": f"FoundryClientSetup-{version}.exe", "os": "windows", "size": None,
             "url": f"{base}/FoundryClientSetup-{version}.exe", "unverified": True},
            {"name": f"FoundryClient-{version}-x86_64.AppImage", "os": "linux", "size": None,
             "url": f"{base}/FoundryClient-{version}-x86_64.AppImage", "unverified": True}]


def _assets(body: dict) -> list[dict]:
    out = []
    for a in body.get("assets") or []:
        kind = _asset_os(str(a.get("name") or ""))
        if kind:
            out.append({"name": a.get("name"), "os": kind, "size": a.get("size"),
                        "url": a.get("browser_download_url")})
    return sorted(out, key=lambda a: a["os"], reverse=True)    # windows first


class ReleaseSource:
    """Cached availability of the installers for one router version.

    states: available (release exists, installers attached) · no_assets
    (release exists, nothing attached yet — CI may still be building) ·
    untagged (no v<version> release: a dev build) · unreachable (GitHub did
    not answer; best-guess links are given, marked unverified)."""

    def __init__(self, repo: str = REPO,
                 fetch: Callable[[str], Awaitable[tuple[int, dict]]] = _fetch_github,
                 clock: Callable[[], float] = time.time):
        self.repo, self.fetch, self.clock = repo.strip("/"), fetch, clock
        self._cache: dict[str, tuple[float, dict]] = {}

    async def release_info(self, version: str, refresh: bool = False) -> dict:
        version = str(version).lstrip("vV")
        now = self.clock()
        hit = self._cache.get(version)
        if hit and not refresh:
            ts, data = hit
            ttl = ERROR_TTL_S if data["state"] == "unreachable" else CACHE_TTL_S
            if now - ts < ttl:
                return {**data, "cached": True}
        data = await self._lookup(version)
        data["checked_ts"] = self.clock()
        self._cache[version] = (now, data)
        return {**data, "cached": False}

    async def _lookup(self, version: str) -> dict:
        tag = f"v{version}"
        base = {"tag": tag, "repo": self.repo,
                "releases_url": f"https://github.com/{self.repo}/releases",
                "html_url": f"https://github.com/{self.repo}/releases/tag/{tag}",
                "assets": [], "latest": None, "note": ""}
        try:
            code, body = await self.fetch(
                f"https://api.github.com/repos/{self.repo}/releases/tags/{tag}")
        except Exception as e:                                   # noqa: BLE001
            return {**base, "state": "unreachable",
                    "assets": _guessed_assets(self.repo, version),
                    "note": f"GitHub not reachable ({str(e)[:120]}); the links are "
                            "best-guess and unverified."}
        if code == 200:
            assets = _assets(body)
            if assets:
                return {**base, "state": "available", "assets": assets,
                        "html_url": body.get("html_url") or base["html_url"]}
            return {**base, "state": "no_assets",
                    "html_url": body.get("html_url") or base["html_url"],
                    "latest": await self._latest(),
                    "note": "The release exists but no installer is attached yet — the "
                            "packaging workflow may still be building."}
        if code == 404:
            return {**base, "state": "untagged", "latest": await self._latest(),
                    "note": f"This router runs {version} but no {tag} release exists — "
                            "an untagged dev build. The latest released installers are "
                            "offered instead; their version will not match this router."}
        return {**base, "state": "unreachable",
                "assets": _guessed_assets(self.repo, version),
                "note": f"GitHub answered HTTP {code}; the links are best-guess and "
                        "unverified."}

    async def _latest(self) -> Optional[dict]:
        try:
            code, body = await self.fetch(
                f"https://api.github.com/repos/{self.repo}/releases/latest")
        except Exception:                                        # noqa: BLE001
            return None
        if code != 200 or not body.get("tag_name"):
            return None
        return {"tag": body["tag_name"], "html_url": body.get("html_url"),
                "assets": _assets(body)}


_SOURCE = ReleaseSource()


def version_match(client_version: Optional[str], router_version: str) -> str:
    """match · older · newer · unknown — the mismatch badge on the Client tab
    and in the chat's install status."""
    if not client_version:
        return "unknown"
    c, r = parse_version(client_version), parse_version(router_version)
    return "match" if c == r else "older" if c < r else "newer"


async def install_status(svc, router_version: str, refresh: bool = False,
                         source: Optional[ReleaseSource] = None) -> dict:
    """Everything the Client tab's top cards and the chat's
    client_install_status tool show: the running router version, whether its
    installers are downloadable, and each heart-beating Foundry Client with
    its version compared against the router's."""
    src = source or _SOURCE
    release = await src.release_info(router_version, refresh=refresh)
    clients = []
    for c in svc.control.client_sources():
        d = c.get("detail") or {}
        clients.append({"source_id": c.get("source_id"), "host": c.get("host"),
                        "version": c.get("version"), "age_s": c.get("age_s"),
                        "fresh": c.get("fresh"), "overall": d.get("overall"),
                        "match": version_match(c.get("version"), router_version)})
    return {"router_version": router_version, "repo": src.repo, "release": release,
            "clients": clients}

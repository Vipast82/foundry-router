"""Images, audio and video made by persona MCP tools reach the chat user.

Before this, an image or audio result from a persona tool became a text note
("[image: image/png, 512 KB]") that only the model saw; nothing reached the
person chatting. Now, on the brain, worker and direct paths:

1. **Store.** Image/audio blocks, image/audio resource blobs, and media URLs in
   the tool's text (kokoro / stable-audio / acestep return
   ``http://<their host>/file.wav``) are saved in the media store
   (media_jobs rows with op ``tool``, files under <data>/media/<id>/), with a
   sha256, a per-file size cap and a total cap. URLs are fetched only from the
   MCP server's own host (plus ``media.tool_fetch_hosts``) — never from an
   arbitrary host a model or web page wrote.
2. **Signed links.** ``/media/a/<token>`` serves one stored file to anyone
   holding the token until it expires (``media.public_link_ttl_hours``). It is
   the one deliberate unauthenticated read in Foundry: a browser rendering a
   chat sends no Foundry key. HMAC-SHA256 over (job, index, expiry) with a
   secret kept in the DB.
3. **Hand-back.** The reply content gets ``![label](link)`` per image and a
   🔊 link per audio file after the model's text. The model only sees the text
   note (plus the image itself when it has vision) and is told the files are
   already shown, so it doesn't repeat or invent links.
4. **Long jobs.** A tool on a server marked ``long_running`` runs as a
   background job (op ``tool_job``). The request waits up to
   ``media.tool_inline_wait_seconds`` and then hands the model a job id it
   checks with the Foundry-owned ``tool_job_status`` tool. A client's own
   request timeout can't kill a 10-minute generation, and polling never
   resubmits anything.
5. **Video (v0.181.136).** An .mp4 / .m4v / .mov a tool lists is fetched and
   stored like the rest and handed back as a link that plays in the browser,
   with a second link that downloads it. Before, the chat got the tool server's
   own address, which only works on the same network.
6. **One copy per result (v0.181.136).** A tool that lists the same result in
   several formats (Media_Gen: ogg, mp3 and flac of one sound) is shown once,
   in the format a browser handles best; side files a tool labels as such
   ("uncaptioned_mp4:", "narration_flac:") are left as links for the model.
8. **Private files stay where they are (v0.181.137).** A file its server sends
   with ``Cache-Control: no-store`` (Media_Gen: a private library) is not copied
   and gets no signed link, unless ``media.tool_fetch_no_store`` is on. The model
   is told to give the user the server's own link.
7. **Queued jobs (v0.181.136).** A server that answers at once with a job id
   and makes the file later (Media_Gen) is waited for here: Foundry reads the
   job with ``media.job_check_tool`` until it is done or
   ``media.job_wait_seconds`` passed, so the picture is in the same reply.
9. **3D models (v0.181.249).** A .glb a tool lists (Media_Gen generate_3d_model)
   is stored like the rest and shown as a download link plus a "view in 3D" page
   (``/media/a/<token>/view``, Google <model-viewer>, ``media.model_viewer_url``).
   A detailed mesh (label or file name ending ``_detail``, hundreds of MB) is
   never fetched: the chat gets one line with the tool server's own link. A job
   whose tool takes longer than any chat wait (``media.job_no_wait_tools``, or
   the queued reply's own "about 10-25 minutes") is not waited for at all.
10. **Deleted at the source (v0.181.249).** When Media_Gen says files of a job
   were deleted from its library, Foundry deletes its own copies of them too,
   so the old links stop working, and the model is told not to show them again.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger(__name__)

JOBS_SERVER = "foundry-jobs"          # pseudo MCP server for tool_job_status
STATUS_TOOL = "tool_job_status"
_SECRET_KV = "media_link_secret"
_MAX_WAIT = 60.0                      # tool_job_status long-poll cap
_DEDUPE_S = 600.0                     # same long call from the same client -> same job
_URL_RE = re.compile(r"https?://[^\s<>\"'`\])}]+", re.I)
_MEDIA_EXT = {"png", "jpg", "jpeg", "webp", "gif", "wav", "mp3", "ogg", "oga", "flac",
              "m4a", "webm", "opus", "mp4", "m4v", "mov", "glb"}
_VIDEO_EXT = {"mp4", "m4v", "mov"}
_MODEL_EXT = {"glb"}
GLB_MIME = "model/gltf-binary"
# which copy to show when one result is listed in several formats: first wins
_FORMAT_ORDER = ("png", "jpg", "jpeg", "webp", "gif", "mp4", "m4v", "mov", "webm",
                 "mp3", "m4a", "ogg", "oga", "opus", "wav", "flac", "glb")
# a side file that is still shown: the picture a 3D model was built from ("picture_png:")
_SHOWN_SIDE = re.compile(r"^picture_(png|jpe?g|webp)$")
# a model file that is only ever a link (Media_Gen "glb_detail:", <name>_detail.glb)
_DETAIL = re.compile(r"_detail$")
# Media_Gen 5.20.1: files deleted from the library since the job ran
_DELETED_SOME = re.compile(r"Deleted from the library since this job ran[^:]*:\s*([A-Za-z0-9_, ]+)")
_DELETED_ALL = re.compile(r"FILES DELETED")
# "mp4: http://..." — the word a tool put in front of a link
_LABELLED_URL = re.compile(r"([A-Za-z0-9_]+):\s*(https?://[^\s<>\"'`\])}]+)")
_JOB_FINAL = ("done", "failed", "cancelled", "canceled", "error")
_NO_STORE = "no-store"                # _fetch's reason for a file its server marks private
_MAX_JOBS = 4                         # jobs of one reply that are waited for together
_MAX_URLS = 8                         # media URLs fetched per tool result
_FETCH_TIMEOUT = 120.0

STATUS_SPEC = {
    "type": "function",
    "function": {
        "name": STATUS_TOOL,
        "description": (
            "Check a long-running tool job (image/music/sound generation) that an earlier "
            "tool call handed back as 'job img_…'. Returns 'still running' or the finished "
            "result; finished images/audio are shown to the user automatically. Never call "
            "the original tool again for the same request — that would start a second "
            "generation."),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job id, e.g. img_0123abcd4567ef89"},
                "wait_seconds": {"type": "number",
                                 "description": f"Wait up to this long for it to finish "
                                                f"(0–{int(_MAX_WAIT)}, default 30)."}},
            "required": ["job_id"]}}}


@dataclass
class MediaItem:
    """One stored file to show the user."""
    job_id: str
    index: int
    mime: str
    bytes: int
    label: str

    @property
    def kind(self) -> str:
        return ("audio" if self.mime.startswith("audio/")
                else "video" if self.mime.startswith("video/")
                else "model" if self.mime.startswith("model/") else "image")


@dataclass
class LinkOnly:
    """A file shown only as the tool server's own link, never fetched: a
    detailed 3D mesh of hundreds of MB."""
    url: str
    label: str


@dataclass
class PendingJob:
    """A long-running tool job still working when the reply went out."""
    job_id: str
    label: str


def job_ended(sink: Optional[list], job_ids) -> None:
    """v0.181.258: a later check in the same reply found these jobs done, failed or
    cancelled: their PendingJob ("still running") is taken out of the sink. Foundry's own
    wait had run out, the model then checked the job itself and it had finished, and the
    "still running" line stayed under a reply that said it was done."""
    ids = set(job_ids or ())
    if sink is None or not ids:
        return
    sink[:] = [i for i in sink if not (isinstance(i, PendingJob) and i.job_id in ids)]


@dataclass
class _Captured:
    items: list = field(default_factory=list)
    notes: list = field(default_factory=list)     # skipped files, for the model
    links: list = field(default_factory=list)     # LinkOnly: shown as the server's own link


# -- signed links -------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret(db) -> bytes:
    v = db.kv_get(_SECRET_KV)
    if not v:
        v = secrets.token_urlsafe(32)
        db.kv_set(_SECRET_KV, v)
        v = db.kv_get(_SECRET_KV) or v      # another worker may have won the race
    return v.encode()


def sign(db, job_id: str, index: int, ttl_seconds: float, now: Optional[float] = None) -> str:
    exp = int((now if now is not None else time.time()) + max(60.0, float(ttl_seconds)))
    payload = f"{job_id}.{int(index)}.{exp}".encode()
    sig = hmac.new(_secret(db), payload, hashlib.sha256).digest()[:18]
    return f"{_b64(payload)}.{_b64(sig)}"


def verify(db, token: str, now: Optional[float] = None) -> Optional[tuple[str, int]]:
    """(job_id, index) for a valid, unexpired token; None otherwise."""
    try:
        p64, s64 = (token or "").split(".", 1)
        payload, sig = _unb64(p64), _unb64(s64)
        want = hmac.new(_secret(db), payload, hashlib.sha256).digest()[:18]
        if not hmac.compare_digest(sig, want):
            return None
        job_id, index, exp = payload.decode().split(".")
        if int(exp) < (now if now is not None else time.time()):
            return None
        return job_id, int(index)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def public_base(cfg) -> str:
    """The origin the chat user's browser reaches Foundry on."""
    base = (getattr(cfg.media, "public_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    from . import request_context
    return request_context.public_origin() or ""


def link(svc, item: MediaItem) -> str:
    cfg = svc.config_store.config
    token = sign(svc.db, item.job_id, item.index, float(cfg.media.public_link_ttl_hours) * 3600)
    return f"{public_base(cfg)}/media/a/{token}"


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def render(svc, sink: list) -> str:
    """Markdown to append to the reply for everything in the sink ("" = none)."""
    if not sink:
        return ""
    cfg = svc.config_store.config
    lines: list[str] = []
    seen: set = set()
    for it in sink:
        if isinstance(it, MediaItem):
            key = (it.job_id, it.index)
            if key in seen:
                continue
            seen.add(key)
            url = link(svc, it)
            if it.kind == "image":
                lines.append(f"![{_md(it.label)}]({url})")
            elif it.kind == "model":
                line = f"🧊 [{_md(it.label)} (glb, {_size(it.bytes)})]({url}?download=1)"
                if (getattr(cfg.media, "model_viewer_url", "") or "").strip():
                    line += f" · [view in 3D]({url}/view)"
                lines.append(line)
            elif it.kind == "video":
                ext = it.mime.split("/", 1)[1].replace("quicktime", "mov")
                lines.append(f"🎬 [{_md(it.label)} ({ext}, {_size(it.bytes)})]({url}) · "
                             f"[download]({url}?download=1)")
                if getattr(cfg.media, "video_html", False):
                    lines.append(f'<video controls preload="metadata" src="{url}"></video>')
            else:
                ext = it.mime.split("/", 1)[1].replace("mpeg", "mp3")
                lines.append(f"🔊 [{_md(it.label)} ({ext}, {_size(it.bytes)})]({url})")
                if getattr(cfg.media, "audio_html", False):
                    lines.append(f'<audio controls src="{url}"></audio>')
        elif isinstance(it, LinkOnly):
            if ("link", it.url) in seen:
                continue
            seen.add(("link", it.url))
            lines.append(f"🧊 [{_md(it.label)} (large file, opens on your network)]({it.url})")
        elif isinstance(it, PendingJob):
            if ("job", it.job_id) in seen:
                continue
            seen.add(("job", it.job_id))
            lines.append(f"⏳ {it.label} is still running (job `{it.job_id}`) — ask me to "
                         f"check on it.")
    return ("\n\n" + "\n\n".join(lines)) if lines else ""


def _md(text: str) -> str:
    return re.sub(r"[\[\]()\n]", " ", text or "file").strip()[:80] or "file"


def model_note(items: list, notes: list, links: Optional[list] = None) -> str:
    """What the model is told about the files it can't hand back itself."""
    out = []
    if items or links:
        n_img = sum(1 for i in items if i.kind == "image")
        n_vid = sum(1 for i in items if i.kind == "video")
        n_mod = sum(1 for i in items if i.kind == "model")
        n_aud = len(items) - n_img - n_vid - n_mod
        n_lnk = len(links or [])
        what = ", ".join(p for p in (f"{n_img} image(s)" if n_img else "",
                                     f"{n_vid} video(s)" if n_vid else "",
                                     f"{n_mod} 3D model(s) (a download link and a 3D view)"
                                     if n_mod else "",
                                     f"{n_aud} audio file(s)" if n_aud else "",
                                     f"{n_lnk} link(s) to a detailed mesh (large, link only)"
                                     if n_lnk else "") if p)
        out.append(f"[Foundry: {what} from this tool are shown to the user automatically "
                   f"under your reply. Don't repeat or invent links to them; just say what "
                   f"you made.]")
    out.extend(notes)
    return "\n".join(out)


# -- capture --------------------------------------------------------------------

def _allowed_hosts(svc, server: str) -> set[str]:
    hosts = {h.strip().lower() for h in (svc.config_store.config.media.tool_fetch_hosts or [])
             if h and h.strip()}
    cfg = (getattr(svc.mcp, "servers", {}) or {}).get(server)
    if cfg is not None:
        h = urlsplit(getattr(cfg, "url", "") or "").hostname
        if h:
            hosts.add(h.lower())
    return hosts


def _url_ext(url: str) -> str:
    """The media extension a URL names ("" = none): its path, or a file name in
    its query (ComfyUI style: /view?filename=x.png&type=output)."""
    parts = urlsplit(url)
    ext = parts.path.rsplit(".", 1)[-1].lower() if "." in parts.path.rsplit("/", 1)[-1] else ""
    if ext in _MEDIA_EXT:
        return ext
    for k, v in parse_qsl(parts.query):
        if k.lower() in ("filename", "file", "name") and v.rsplit(".", 1)[-1].lower() in _MEDIA_EXT:
            return v.rsplit(".", 1)[-1].lower()
    return ""


def _media_url(url: str) -> bool:
    return bool(_url_ext(url))


def _url_stem(url: str) -> str:
    """A URL without its extension: two formats of one result share it."""
    parts = urlsplit(url)
    path = parts.path
    if "." in path.rsplit("/", 1)[-1]:
        path = path.rsplit(".", 1)[0]
    names = [v.rsplit(".", 1)[0] for k, v in parse_qsl(parts.query)
             if k.lower() in ("filename", "file", "name")]
    return f"{(parts.hostname or '').lower()}{path}?{'&'.join(names)}"


def pick_urls(texts: list, hosts: set) -> list[str]:
    """The media links of one tool result that are fetched and shown, in the
    order the tool listed them.

    * only links to a media file on an allowed host;
    * when the tool labels its links and some labels are the plain file type
      ("mp4:", "png:") while others say more ("uncaptioned_mp4:",
      "narration_flac:"), only the plain ones: the others are side files;
    * one link per result: of "x.ogg", "x.mp3" and "x.flac" the first format in
      _FORMAT_ORDER;
    * never a detailed 3D mesh (detail_links): it is hundreds of MB;
    * the picture a 3D model was built from ("picture_png:") counts as a main file."""
    cands: list[tuple[str, str, bool]] = []          # (url, ext, plain label)
    labels = url_labels(texts)
    for txt in texts:
        for u in _URL_RE.findall(txt or ""):
            u = u.rstrip(".,;:!?")
            ext = _url_ext(u)
            if not ext or any(c[0] == u for c in cands) \
                    or (urlsplit(u).hostname or "").lower() not in hosts \
                    or _is_detail(u, labels.get(u, "")):
                continue
            lab = labels.get(u)
            cands.append((u, ext, lab == ext or bool(_SHOWN_SIDE.match(lab or ""))))
    if any(c[2] for c in cands):
        cands = [c for c in cands if c[2]]
    best: dict[tuple, tuple[str, str, bool]] = {}
    rank = {e: i for i, e in enumerate(_FORMAT_ORDER)}
    for c in cands:
        # v0.181.260: only copies of one kind are one result: animate_3d_model's
        # <name>_animated.glb and its preview <name>_animated.png share a stem, and the
        # picture won, so the animated model was never shown
        stem = (_url_stem(c[0]), _ext_kind(c[1]))
        if stem not in best or rank.get(c[1], 99) < rank.get(best[stem][1], 99):
            best[stem] = c
    return [c[0] for c in best.values()]


def _ext_kind(ext: str) -> str:
    if ext in ("png", "jpg", "jpeg", "webp", "gif"):
        return "image"
    if ext in _VIDEO_EXT or ext == "webm":
        return "video"
    return "model" if ext in _MODEL_EXT else "audio"


def url_labels(texts: list) -> dict:
    """{url: the word the tool put in front of it, lower case} ("glb_detail: http://...")."""
    out: dict = {}
    for txt in texts:
        for m in _LABELLED_URL.finditer(txt or ""):
            out.setdefault(m.group(2).rstrip(".,;:!?"), m.group(1).lower())
    return out


def _is_detail(url: str, label: str) -> bool:
    """A detailed 3D mesh: a model file labelled or named *_detail."""
    if _url_ext(url) not in _MODEL_EXT:
        return False
    name = urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return bool(_DETAIL.search(label or "") or _DETAIL.search(name))


def detail_links(texts: list, hosts: set) -> list[tuple[str, str]]:
    """[(url, label)] of the detailed 3D meshes a result lists: shown as the tool
    server's own link and never fetched."""
    labels = url_labels(texts)
    out: list[tuple[str, str]] = []
    for txt in texts:
        for u in _URL_RE.findall(txt or ""):
            u = u.rstrip(".,;:!?")
            if (urlsplit(u).hostname or "").lower() in hosts and _is_detail(u, labels.get(u, "")) \
                    and all(o[0] != u for o in out):
                out.append((u, labels.get(u) or "glb_detail"))
    return out


def _model_mime(raw: bytes, ext: str) -> Optional[str]:
    """model/gltf-binary for a file a tool NAMED .glb whose bytes start with the
    binary glTF magic; None otherwise."""
    return GLB_MIME if ext in _MODEL_EXT and raw[:4] == b"glTF" else None


def _job_at(text: str, pos: int) -> str:
    """The Media_Gen job id a link at `pos` of a reply belongs to: the last
    "job_id: <id>" line before it ("" = none)."""
    from .media import _MG_JOB
    last = ""
    for m in _MG_JOB.finditer(text or ""):
        if m.start() > pos:
            break
        last = m.group(1)
    return last


def _video_mime(raw: bytes, ext: str) -> Optional[str]:
    """video/mp4 or video/quicktime for a file a tool NAMED as video and whose
    bytes are that container. An .m4a and an .mp4 are the same container, so the
    name decides between sound and video; the bytes decide whether it is one at
    all (a page of text called x.mp4 is nothing)."""
    if ext not in _VIDEO_EXT or len(raw) < 12 or raw[4:8] != b"ftyp":
        return None
    return "video/quicktime" if raw[8:12] == b"qt  " else "video/mp4"


async def _fetch(svc, url: str, cap: int) -> tuple[Optional[bytes], str]:
    """(bytes or None, why-not)."""
    try:
        async with svc.http.stream("GET", url, timeout=_FETCH_TIMEOUT,
                                   follow_redirects=False) as r:
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            # the body is not read: nothing of a private file passes through here
            if _NO_STORE in (r.headers.get("cache-control") or "").lower() \
                    and not getattr(svc.config_store.config.media, "tool_fetch_no_store", False):
                return None, _NO_STORE
            clen = int(r.headers.get("content-length") or 0)
            if clen and clen > cap:
                return None, f"larger than {_size(cap)} (media.tool_output_max_mb)"
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > cap:
                    return None, f"larger than {_size(cap)} (media.tool_output_max_mb)"
            return bytes(buf), ""
    except Exception as e:                                       # noqa: BLE001
        from .errors import describe_exception
        return None, describe_exception(e)[:160]


async def _collect(svc, server: str, tool: str, rich,
                   fetch_urls: bool) -> tuple[list, list, list]:
    """([{raw, mime, w, h, label, source_*}], notes, [LinkOnly]) from one tool result."""
    from .media import sniff_media
    mcfg = svc.config_store.config.media
    cap = int(float(mcfg.tool_output_max_mb or 0) * 1024 * 1024) or (100 * 1024 * 1024)
    found: list[dict] = []
    notes: list[str] = []
    seen: set[str] = set()

    links: list = []

    def add(raw: Optional[bytes], label: str, video: Optional[str] = None,
            src: Optional[dict] = None) -> None:
        if not raw:
            return
        if len(raw) > cap:
            notes.append(f"[Foundry: a {label} of {_size(len(raw))} was not shown to the user "
                         f"— over media.tool_output_max_mb.]")
            return
        mime, w, h = (video, None, None) if video else sniff_media(raw)
        if not mime:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        found.append({"raw": raw, "mime": mime, "w": w, "h": h, "label": label, **(src or {})})

    for b in getattr(rich, "blocks", None) or []:
        t = b.get("type")
        data = None
        if t in ("image", "audio"):
            data = b.get("data")
        elif t == "resource" and b.get("blob") and str(b.get("mimeType") or "").startswith(
                ("image/", "audio/")):
            data = b.get("blob")
        if not data:
            continue
        if len(data) * 3 // 4 > cap:
            notes.append(f"[Foundry: a {t} of {_size(len(data) * 3 // 4)} was not shown to the "
                         f"user — over media.tool_output_max_mb.]")
            continue
        try:
            add(base64.b64decode(data), f"{tool} {t}")
        except (binascii.Error, ValueError):
            continue

    if fetch_urls and mcfg.tool_fetch_urls:
        hosts = _allowed_hosts(svc, server)
        texts = [b.get("text") or "" for b in getattr(rich, "blocks", None) or []
                 if b.get("type") == "text"]
        texts += [b.get("uri") or "" for b in getattr(rich, "blocks", None) or []
                  if b.get("type") == "resource_link"]
        joined = "\n".join(texts)
        labels = url_labels(texts)
        links = [LinkOnly(u, f"{tool} {lab}") for u, lab in detail_links(texts, hosts)]
        for u in pick_urls(texts, hosts)[:_MAX_URLS]:
            raw, why = await _fetch(svc, u, cap)
            if raw is None and why == _NO_STORE:
                notes.append(f"[Foundry: {u} is a private file ({server} marks it as not to be "
                             f"kept elsewhere), so it was not copied and is NOT shown under your "
                             f"reply. Give the user that link as it is; it opens on their own "
                             f"network.]")
                continue
            if raw is None:
                notes.append(f"[Foundry: couldn't fetch {u} to show the user ({why}); give the "
                             f"user that link instead.]")
                continue
            ext = _url_ext(u)
            forced = _video_mime(raw, ext) or _model_mime(raw, ext)
            kind = ("video" if (forced or "").startswith("video/") else
                    "3D model" if forced else
                    "audio" if (sniff_media(raw)[0] or "").startswith("audio/") else "image")
            # where it came from: Media_Gen's delete notice names the job and the file's key
            add(raw, f"{tool} {kind}", forced,
                {"source_url": u, "source_key": labels.get(u, ""),
                 "source_job": _job_at(joined, max(0, joined.find(u)))})
    return found, notes, links


def forget_deleted(svc, text: str) -> list[str]:
    """Media_Gen 5.20.1: a check reply says files of a job were deleted from its
    library ("Deleted from the library since this job ran ...: png, glb." or
    "FILES DELETED: every file of this job ..."). Foundry's copies of those files
    are deleted too, so a /media/a link given earlier stops working (bare 404),
    and the model gets a note not to show earlier links. Returns the notes.
    Training-capture records hold hashes only and are kept."""
    if not text or "job_id" not in text:
        return []
    every = bool(_DELETED_ALL.search(text))
    some = _DELETED_SOME.search(text)
    if not (every or some):
        return []
    from .media import _MG_JOB, _jl
    jid = (_MG_JOB.search(text) or [None, ""])[1]
    if not jid:
        return []
    keys = None if every else {k.strip().lower() for k in some.group(1).split(",") if k.strip()}
    media = getattr(svc, "media", None)
    n = 0
    if media is not None:
        try:
            rows = svc.db.query(
                "SELECT id, artifacts FROM media_jobs WHERE op IN ('tool','tool_job') "
                "AND artifacts LIKE ?", (f'%"source_job": "{jid}"%',))
            for row in rows:
                for a in _jl(row.get("artifacts"), []):
                    if a.get("source_job") != jid or (
                            keys is not None and (a.get("source_key") or "") not in keys):
                        continue
                    p = media.dir / row["id"] / str(a.get("file") or "")
                    if a.get("file") and p.is_file():
                        p.unlink()
                        n += 1
        except Exception:                                        # noqa: BLE001
            log.exception("forgetting the deleted files of job %s failed", jid)
    if n:
        svc.db.log_event("info", "media", f"job {jid}: {n} copied file(s) deleted",
                         "the tool server says they were deleted from its library")
    what = "every file" if every else "the file(s) " + ", ".join(sorted(keys or []))
    return [f"[Foundry: {what} of job {jid} were deleted from the media server's library"
            + (f"; Foundry deleted its {n} copied file(s) too, so links to them no longer work"
               if n else "")
            + ". Don't show or repeat any earlier link to them; tell the user they are gone.]"]


def fetches_files(svc, tool: str) -> bool:
    """v0.181.259: False for a tool in media.no_fetch_tools - a listing (Media_Gen's
    list_assets, search_library ...) whose links stay links for the model; "show me the
    pictures in the game library" copied every picture and 12 MB GLB it listed."""
    names = getattr(svc.config_store.config.media, "no_fetch_tools", None) or []
    return (tool or "").lower() not in {str(t).strip().lower() for t in names if str(t).strip()}


async def store_result(svc, server: str, tool: str, args: Optional[dict], rich, *,
                       persona: Optional[str] = None, job_id: Optional[str] = None,
                       fetch_urls: bool = True) -> _Captured:
    """Save the result's media; return the items and notes for the model.
    job_id: write into this existing tool_job row instead of a new 'tool' row."""
    out = _Captured()
    media = getattr(svc, "media", None)
    if media is None:
        return out
    # before anything else, and also with tool_outputs off: copies made earlier go
    gone = forget_deleted(svc, getattr(rich, "text", "") or "")
    if not svc.config_store.config.media.tool_outputs:
        out.notes = gone
        return out
    try:
        found, out.notes, out.links = await _collect(svc, server, tool, rich, fetch_urls)
        out.notes += gone
        if not found:
            return out
        n_img = n_aud = n_vid = n_mod = 0
        for f in found:
            if f["mime"].startswith("model/"):
                n_mod += 1
                f["label"] = f"{tool} 3D model" + (f" {n_mod}" if n_mod > 1 else "")
            elif f["mime"].startswith("audio/"):
                n_aud += 1
                f["label"] = f"{tool} audio" + (f" {n_aud}" if n_aud > 1 else "")
            elif f["mime"].startswith("video/"):
                n_vid += 1
                f["label"] = f"{tool} video" + (f" {n_vid}" if n_vid > 1 else "")
            else:
                n_img += 1
                f["label"] = f"{tool} image" + (f" {n_img}" if n_img > 1 else "")
        rid = job_id or media.new_tool_row("tool", server, tool, args, persona=persona)
        arts = await asyncio.to_thread(media.write_tool_artifacts, rid, found)
        if job_id is None:
            media.finish_tool_row(rid, status="succeeded", artifacts=arts)
        else:
            media._update(rid, artifacts=json.dumps(arts))
        out.items = [MediaItem(rid, a["index"], a["mime"], a["bytes"], a["label"]) for a in arts]
    except Exception:                                            # noqa: BLE001
        log.exception("storing media from %s/%s failed", server, tool)
    return out


def items_of(row: dict) -> list[MediaItem]:
    from .media import _jl
    return [MediaItem(row["id"], a["index"], a["mime"], int(a.get("bytes") or 0),
                      a.get("label") or f"{(row.get('model') or '').split('/')[-1]} file")
            for a in _jl(row.get("artifacts"), [])]


# -- long-running tool jobs ------------------------------------------------------

class ToolJobs:
    """Background tool calls, one asyncio task each, state in media_jobs."""

    def __init__(self, svc):
        self.svc = svc
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._progress: dict[str, Callable] = {}     # job -> live narrator while inline

    def is_long(self, server: str) -> bool:
        cfg = (getattr(self.svc.mcp, "servers", {}) or {}).get(server)
        return bool(cfg is not None and getattr(cfg, "long_running", False))

    def _key(self, server: str, tool: str, args: Optional[dict]) -> str:
        try:
            a = json.dumps(args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            a = str(args)
        return "t:" + hashlib.sha256(f"{server}/{tool}:{a}".encode()).hexdigest()[:32]

    def _running_duplicate(self, key: str) -> Optional[str]:
        from . import client_auth
        cid = client_auth.current().get("client_id") or "anonymous"
        row = self.svc.db.query_one(
            "SELECT id FROM media_jobs WHERE op='tool_job' AND client_id=? AND idem_key=? "
            "AND status='running' AND created_ts >= ? ORDER BY created_ts DESC LIMIT 1",
            (cid, key, time.time() - _DEDUPE_S))
        return row["id"] if row and row["id"] in self._tasks else None

    def start(self, mcp, server: str, tool: str, args: Optional[dict],
              persona: Optional[str]) -> tuple[str, bool]:
        """(job id, reused an identical running job)."""
        key = self._key(server, tool, args)
        dup = self._running_duplicate(key)
        if dup:
            return dup, True
        media = self.svc.media
        jid = media.new_tool_row("tool_job", server, tool, args, status="running",
                                 persona=persona, idem_key=key)
        self._events[jid] = asyncio.Event()
        self._tasks[jid] = asyncio.create_task(self._run(mcp, jid, server, tool, args, persona))
        return jid, False

    async def _run(self, mcp, jid: str, server: str, tool: str, args, persona) -> None:
        media = self.svc.media

        async def relay(progress, total, message):
            fn = self._progress.get(jid)
            if fn is not None:
                pct = f" {progress:.0f}/{total:.0f}" if total else ""
                try:
                    fn(f"⏳ {server}/{tool}{pct} {message or ''}".rstrip() + "\n")
                except Exception:                                # noqa: BLE001
                    pass

        try:
            from .brain.agent import raw_call_tool_rich
            rich = await raw_call_tool_rich(mcp, server, tool, args or {}, progress_callback=relay)
            cap = await store_result(self.svc, server, tool, args, rich, persona=persona,
                                     job_id=jid, fetch_urls=fetches_files(self.svc, tool))
            text = rich.text or "(empty tool result)"
            media.finish_tool_row(jid, status="succeeded",
                                  artifacts=[a for a in _artifacts(media, jid)],
                                  result_text="\n".join(p for p in (text, "\n".join(cap.notes))
                                                        if p))
        except asyncio.CancelledError:
            media.finish_tool_row(jid, status="cancelled", artifacts=_artifacts(media, jid),
                                  error="cancelled", error_code="cancelled")
            raise
        except Exception as e:                                   # noqa: BLE001
            from .errors import describe_exception
            media.finish_tool_row(jid, status="failed", artifacts=_artifacts(media, jid),
                                  error=describe_exception(e)[:1000], error_code="tool_error")
            self.svc.db.log_event("warning", "media", f"tool job {jid} ({server}/{tool}) failed",
                                  describe_exception(e))
        finally:
            self._tasks.pop(jid, None)
            self._progress.pop(jid, None)
            ev = self._events.pop(jid, None)
            if ev is not None:
                ev.set()

    async def wait(self, jid: str, timeout: float, progress: Optional[Callable] = None,
                   label: str = "") -> dict:
        """Wait up to timeout for the job; narrate progress meanwhile."""
        ev = self._events.get(jid)
        if ev is not None and timeout > 0:
            if progress is not None:
                self._progress[jid] = progress
            t0 = time.monotonic()
            beat = 15.0
            try:
                while not ev.is_set():
                    left = timeout - (time.monotonic() - t0)
                    if left <= 0:
                        break
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=min(beat, left))
                    except asyncio.TimeoutError:
                        if progress is not None and not ev.is_set():
                            from .keepalive import fmt_elapsed
                            try:
                                progress(f"⏳ {label or jid}: still working · "
                                         f"{fmt_elapsed(int(time.monotonic() - t0))}\n")
                            except Exception:                    # noqa: BLE001
                                pass
            finally:
                if self._progress.get(jid) is progress:
                    self._progress.pop(jid, None)
        return self.svc.media.get(jid) or {}


def _artifacts(media, jid: str) -> list:
    from .media import _jl
    return _jl((media.get(jid) or {}).get("artifacts"), [])


def _owned(row: dict) -> bool:
    from . import client_auth
    c = client_auth.current()
    return "admin" in (c.get("scopes") or []) or row.get("client_id") == (
        c.get("client_id") or "anonymous")


def _job_text(row: dict, sink: Optional[list]) -> str:
    st = row.get("status")
    label = row.get("model") or "tool"
    if st == "running":
        el = int(time.time() - (row.get("started_ts") or time.time()))
        return (f"Job {row['id']} ({label}) is still running ({el}s so far). Do NOT call the "
                f"original tool again. Tell the user it's still working; check again later "
                f"with {STATUS_TOOL}.")
    if st == "succeeded":
        items = items_of(row)
        if sink is not None:
            sink.extend(items)
        body = row.get("result_text") or "(empty tool result)"
        return f"Job {row['id']} ({label}) finished.\n{body}" + (
            "\n" + model_note(items, []) if items else "")
    return (f"Job {row['id']} ({label}) {st}: {row.get('error') or 'no detail'}. "
            f"You may start it again only if the user asks.")


async def job_status(svc, args: dict, sink: Optional[list] = None,
                     progress: Optional[Callable] = None) -> str:
    jid = str((args or {}).get("job_id") or "").strip().strip("`")
    row = svc.media.get(jid) if getattr(svc, "media", None) else None
    if not row or row.get("op") != "tool_job" or not _owned(row):
        return (f"No tool job {jid!r} for this client. Job ids look like img_ followed by 16 "
                f"hex digits and come from an earlier tool result.")
    try:
        wait = max(0.0, min(_MAX_WAIT, float((args or {}).get("wait_seconds", 30))))
    except (TypeError, ValueError):
        wait = 30.0
    if row.get("status") == "running":
        row = await svc.tool_jobs.wait(jid, wait, progress, row.get("model") or jid)
    return _job_text(row, sink)


# -- the one entry point the chat paths use --------------------------------------

async def run_tool(svc, mcp, server: str, tool: str, args: Optional[dict], *,
                   sink: list, persona: Optional[str] = None,
                   progress: Optional[Callable] = None):
    """Run a persona tool call for a chat path: long-running servers become a
    tool job, every result's media is stored and queued in `sink` for the
    reply, and the returned ToolResult's text says what the model needs."""
    from .tools.mcp_client import ToolResult
    if server == JOBS_SERVER:
        got: list = []
        res = ToolResult([{"type": "text", "text": await job_status(svc, args, got, progress)}])
        sink.extend(got)
        res.media_items = [i for i in got if isinstance(i, MediaItem)]
        jid = str((args or {}).get("job_id") or "").strip().strip("`")
        row = svc.media.get(jid) if jid and getattr(svc, "media", None) else None
        if row and row.get("status") not in (None, "running"):
            job_ended(sink, [jid])
        return res
    jobs: Optional[ToolJobs] = getattr(svc, "tool_jobs", None)
    if jobs is not None and jobs.is_long(server) and getattr(svc, "media", None) is not None:
        jid, reused = jobs.start(mcp, server, tool, args, persona)
        wait = float(svc.config_store.config.media.tool_inline_wait_seconds or 0)
        if progress is not None:
            try:
                progress(f"⏳ {server}/{tool} running as job {jid}"
                         + (" (same request already running)" if reused else "")
                         + f" — waiting up to {int(wait)}s here\n")
            except Exception:                                    # noqa: BLE001
                pass
        row = await jobs.wait(jid, wait, progress, f"{server}/{tool}")
        if row.get("status") == "running":
            sink.append(PendingJob(jid, f"{server}/{tool}"))
            text = (f"Started as background job {jid} ({server}/{tool}); still running after "
                    f"{int(wait)}s. Do NOT call {tool} again — that would start a second "
                    f"generation. Tell the user it's working and that they can ask you to check; "
                    f"check with {STATUS_TOOL}(job_id=\"{jid}\").")
            return ToolResult([{"type": "text", "text": text}])
        if row.get("status") != "succeeded":
            raise RuntimeError(f"{server}/{tool}: {row.get('error') or row.get('status')}")
        items = items_of(row)
        sink.extend(items)
        res = ToolResult([{"type": "text", "text": row.get("result_text") or ""}])
        res.text = "\n".join(p for p in (res.text, model_note(items, [])) if p)
        res.media_items = items
        return res
    from .brain.agent import raw_call_tool_rich
    rich = await raw_call_tool_rich(mcp, server, tool, args or {})
    rich = await wait_for_queued(svc, mcp, server, tool, rich, sink, progress)
    cap = await store_result(svc, server, tool, args, rich, persona=persona,
                             fetch_urls=fetches_files(svc, tool))
    if cap.items or cap.notes or cap.links:
        sink.extend(cap.items)
        sink.extend(cap.links)
        rich.text = "\n".join(p for p in ((rich.text or ""),
                                          model_note(cap.items, cap.notes, cap.links)) if p)
    rich.media_items = cap.items
    return rich


# -- refusals a chat model must relay, not retry (v0.181.249) ----------------------

# Media_Gen 5.21.1: "Krea 2 Turbo is not installed on the media server: missing ...
# Tell the user to install it in the media-gen console ... Nothing was queued."
# v0.181.252: generate_3d_model words it "3D models are not set up yet: missing model files
# (Models page, or download into the ComfyUI models folder): ..." (found by the live
# checklist; the model then quietly switched pixal3d to trellis2)
_NOT_INSTALLED = re.compile(r"\bis not installed on the media server\b|\bmissing model files\b",
                            re.I)
_REFUSAL_NOTE = ("[Foundry: this is a missing-model refusal from the media server. Nothing was "
                 "queued and nothing in this chat can install it. Tell the user this message as "
                 "it is (which model, and that they install it in the media-gen console: the "
                 "tool's form has a 'Download the missing files' button, or Models). Do NOT call "
                 "the tool again until the user says it is installed, and do not switch to "
                 "another model or setting to get around it unless the user asks for that.]")
# v0.181.260: a limit the server will not go past - a range ("duration_seconds must be between
# 1 and 374 for this model (got 500)"), a library, a licence. Round 2 of the Media_Gen chat
# test: asked for "a 500 second sound effect", the model got that refusal and made 500 s of
# rain with generate_music instead, without telling the user.
_LIMIT = re.compile(r"\bmust be between\b|\blibrary must be one of\b|\bis a commercial library\b"
                    r"|\blicensed for non-commercial use\b", re.I)
_LIMIT_NOTE = ("[Foundry: the media server refused this request on one of its limits and "
               "nothing was queued. Tell the user what it said and what they could change. Do "
               "not use another tool, library or setting to get around the limit unless the "
               "user asks for that.]")
_REFUSED: dict = {}                    # (request, server, tool, args) -> (time, the error)
_REFUSED_TTL = 900.0


def _refusal_key(server: str, tool: str, args) -> Optional[tuple]:
    """None outside a request: nothing is remembered then."""
    from . import request_context
    who = request_context.request_id()
    if not who:
        return None
    try:
        a = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        a = str(args)
    return (who, server, tool, a)


def note_refusal(server: str, tool: str, args, err):
    """The error to raise for a failed tool call: a missing-model refusal, or a refusal
    on one of the server's limits (v0.181.260), gets its relay note (once) and is
    remembered for this request; anything else as it is."""
    from .tools.mcp_client import ToolCallError
    msg = str(err)
    if not isinstance(err, ToolCallError):
        return err
    note = (_REFUSAL_NOTE if _NOT_INSTALLED.search(msg)
            else _LIMIT_NOTE if _LIMIT.search(msg) else "")
    if not note:
        return err
    if note not in msg:
        err = ToolCallError(f"{msg}\n{note}", err.result)
    now = time.time()
    for k in [k for k, v in _REFUSED.items() if now - v[0] > _REFUSED_TTL]:
        _REFUSED.pop(k, None)
    key = _refusal_key(server, tool, args)
    if key is not None:
        _REFUSED[key] = (now, err)
    return err


def refused_before(server: str, tool: str, args):
    """The remembered refusal when this exact call was refused earlier in the same
    request (the model retried it), else None. A different request — the user
    saying it is installed now — calls the server again."""
    key = _refusal_key(server, tool, args)
    hit = _REFUSED.get(key) if key is not None else None
    if hit is None or time.time() - hit[0] > _REFUSED_TTL:
        return None
    from .tools.mcp_client import ToolCallError
    err = hit[1]
    return ToolCallError(f"{err}\n[Foundry: refused again without calling the server — the "
                         f"same call was refused earlier in this reply.]", err.result)


def _server_has_tool(svc, server: str, tool: str) -> bool:
    try:
        return any(t.server == server and (t.mcp_tool or t.name) == tool
                   for t in svc.tool_registry.enabled())
    except Exception:                                            # noqa: BLE001
        return False


# v0.181.258: media-gen 5.27 words animate_3d_model's estimate "roughly 15-40 minutes"
_ESTIMATE = re.compile(r"\b(?:about|roughly|around|approximately)\s+(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*"
                       r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.I)
_UNIT_S = {"s": 1.0, "m": 60.0, "h": 3600.0}


def too_long_to_wait(mcfg, tool: str, text: str, wait: float) -> str:
    """Why a chat should not wait for this queued job at all ("" = wait): its tool
    is listed in media.job_no_wait_tools, or the queued reply's own estimate
    ("usually takes about 10-25 minutes") starts above the wait."""
    listed = {str(t).strip().lower() for t in (getattr(mcfg, "job_no_wait_tools", None) or [])
              if str(t).strip()}
    m = _ESTIMATE.search(text or "")
    est = " ".join(m.group(0).split()) if m else ""          # "about 10-25 minutes"
    if tool.lower() in listed:
        return f"takes {est}" if est else "takes many minutes"
    if m and float(m.group(1)) * _UNIT_S[m.group(2)[0].lower()] > wait:
        return f"takes {est}"
    return ""


async def wait_for_queued(svc, mcp, server: str, tool: str, rich, sink: list,
                          progress: Optional[Callable] = None):
    """v0.181.136. Media_Gen answers every generate tool at once with
    "job_id: <id>" and makes the file later; the chat then showed a job id and
    the picture only came when the user asked again. When a tool's reply names a
    job that is not finished and its server has ``media.job_check_tool``, the job
    is read here every few seconds until it is done, failed, or
    ``media.job_wait_seconds`` passed.

    Done or failed: the check's result is returned in place of the "queued"
    reply, so its files are stored and shown like any other result. Still
    running: the first reply is returned with a note for the model, and the
    reply to the user says the job is still being made. Nothing is ever
    submitted twice: only the check tool is called."""
    from .media import _MG_JOB, _MG_STATUS
    mcfg = svc.config_store.config.media
    check = (getattr(mcfg, "job_check_tool", "") or "").strip()
    wait = float(getattr(mcfg, "job_wait_seconds", 0) or 0)
    text = getattr(rich, "text", "") or ""
    if check and tool == check and not getattr(rich, "is_error", False):
        # the model's own check of a job: a final status ends that job's "still running" line
        st = ((_MG_STATUS.search(text) or [None, ""])[1]).lower()
        if st in _JOB_FINAL:
            job_ended(sink, _MG_JOB.findall(text))
    if not check or wait <= 0 or tool == check or getattr(rich, "is_error", False):
        return rich
    m = _MG_JOB.search(text)
    if not m or ((_MG_STATUS.search(text) or [None, ""])[1]).lower() in _JOB_FINAL:
        return rich
    if not _server_has_tool(svc, server, check):
        return rich
    from .brain.agent import raw_call_tool_rich
    from .tools.mcp_client import ToolResult
    # one reply can queue several jobs (Media_Gen: variations=3 lists three job ids)
    ids = list(dict.fromkeys(_MG_JOB.findall(text)))[:_MAX_JOBS]

    def say(line: str) -> None:
        if progress is not None:
            try:
                progress(line)
            except Exception:                                    # noqa: BLE001
                pass

    names = ", ".join(ids)
    takes = too_long_to_wait(mcfg, tool, text, wait)
    if takes:
        # v0.181.249: a 3D model (10-25 min) or a finished video is never waited for:
        # the turn ends now and the user asks later
        say(f"⏳ {server} queued job {names} for {tool} — {takes}, so not waiting for it\n")
        for jid in ids:
            sink.append(PendingJob(jid, f"{server}/{tool}"))
        rich.text = (f"{text}\n[Foundry: {tool} {takes}, longer than a chat reply can wait, so "
                     f"Foundry did not wait for job {names}. Tell the user the job id and roughly "
                     f"how long it takes, and stop. When they ask later, call {check} once with "
                     f"job_id \"{ids[0]}\". Do NOT call {tool} again — that would start a second "
                     f"job.]")
        return rich
    say(f"⏳ {server} queued job {names} for {tool} — waiting up to {int(wait)}s for it here\n")
    # v0.181.258: media-gen asks for a check at most every 10-15 s (this used the 3 s
    # fallback_poll_seconds of the gpt-image fallback)
    poll = max(0.05, float(getattr(mcfg, "job_check_every_seconds", 10) or 10))
    t0 = said = time.monotonic()
    reports: dict = {}                    # job id -> the check result that ended it
    last: dict = {}
    broken = False
    while len(reports) < len(ids) and not broken:
        left = wait - (time.monotonic() - t0)
        if left <= 0:
            break
        await asyncio.sleep(min(poll, left))
        for jid in ids:
            if jid in reports:
                continue
            try:
                chk = await raw_call_tool_rich(mcp, server, check, {"job_id": jid})
            except Exception as e:                               # noqa: BLE001
                from .errors import describe_exception
                log.warning("%s/%s for job %s failed while waiting: %s", server, check, jid,
                            describe_exception(e))
                broken = True
                break
            ctext = getattr(chk, "text", "") or ""
            st = ((_MG_STATUS.search(ctext) or [None, ""])[1]).lower()
            if st in _JOB_FINAL:
                reports[jid] = chk
                say(f"{server} job {jid}: {st} after {int(time.monotonic() - t0)}s\n")
                continue
            pct = re.search(r"\b(\d{1,3}) ?%", ctext)
            now = f"{st or 'working'}{' ' + pct.group(1) + '%' if pct else ''}"
            # a line when the state changes, and one every 15 s so the stream is never silent
            if now != last.get(jid) or time.monotonic() - said >= 15:
                say(f"⏳ {server} job {jid}: {now} ({int(time.monotonic() - t0)}s)\n")
                last[jid], said = now, time.monotonic()
    secs = int(time.monotonic() - t0)
    waiting = [j for j in ids if j not in reports]
    for jid in waiting:
        sink.append(PendingJob(jid, f"{server}/{tool}"))
    still = ""
    if waiting:
        still = (f"[Foundry: job {', '.join(waiting)} was still "
                 f"{last.get(waiting[0]) or 'running'} after {secs}s of waiting. Tell the user it is "
                 f"being made and that they can ask you to check on it; then call {check} once with "
                 f"job_id \"{waiting[0]}\". Do NOT call {tool} again — that would start a second job.]")
    if not reports:
        rich.text = f"{text}\n{still}"
        return rich
    # the finished jobs' reports stand in for the "queued" reply: their files are stored and shown
    done = [reports[j] for j in ids if j in reports]
    out = ToolResult([blk for r in done for blk in (getattr(r, "blocks", None) or [])])
    out.text = "\n\n".join(p for p in (
        text, f"[Foundry waited {secs}s for job {', '.join(j for j in ids if j in reports)}; "
              f"{check} now reports:]", *[(getattr(r, "text", "") or "") for r in done], still) if p)
    return out


def status_tooldef():
    from .tools.sync import ToolDef
    f = STATUS_SPEC["function"]
    return ToolDef(name=STATUS_TOOL, kind="mcp", description=f["description"],
                   parameters=f["parameters"], server=JOBS_SERVER, mcp_tool=STATUS_TOOL,
                   read_only=True, destructive=False)

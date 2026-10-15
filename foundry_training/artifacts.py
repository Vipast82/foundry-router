"""Image and artifact handling: content-addressed, with hashes, dimensions,
MIME type and provenance. A path or URL in a transcript is only a reference:
an artifact counts as available when its bytes were actually read."""

from __future__ import annotations

import base64
import os
import re
import struct
from typing import Optional

_DATA_URL = re.compile(r"^data:([\w/+.-]+);base64,(.+)$", re.S)


def image_info(data: bytes) -> dict:
    """{mime, width, height} for PNG / JPEG / GIF / WebP headers, else {}."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return {"mime": "image/png", "width": w, "height": h}
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return {"mime": "image/gif", "width": w, "height": h}
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return {"mime": "image/webp", "width": w, "height": h}
        return {"mime": "image/webp"}
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return {"mime": "image/jpeg", "width": w, "height": h}
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
        return {"mime": "image/jpeg"}
    return {}


def resolve(store, project_id: str, event_id: str, ref: str, base_dir: Optional[str],
            allowed_roots: list[str], provenance: dict) -> dict:
    """Read the bytes behind a reference (data: URL or a file inside an
    allowed root) into the artifact store. Anything else is recorded as
    missing / not permitted, never assumed present."""
    from .schema import stable_id
    ref_id = stable_id(project_id, event_id, ref)
    m = _DATA_URL.match(ref or "")
    if m:
        try:
            data = base64.b64decode(m.group(2), validate=False)
        except ValueError:
            store.ref_artifact(ref_id, project_id, event_id, ref[:80], None, "unreadable",
                               "bad base64")
            return {"status": "unreadable"}
        meta = store.put_artifact(data, m.group(1), provenance)
        store.ref_artifact(ref_id, project_id, event_id, "data-url", meta["sha256"], "present")
        return {"status": "present", **meta}
    path = ref[7:] if ref.startswith("file://") else ref
    if base_dir and not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    real = os.path.realpath(path)
    if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + os.sep)
               for r in allowed_roots):
        store.ref_artifact(ref_id, project_id, event_id, ref, None, "not-permitted",
                           "outside the project's artifact roots")
        return {"status": "not-permitted"}
    if not os.path.isfile(real):
        store.ref_artifact(ref_id, project_id, event_id, ref, None, "missing",
                           "referenced file no longer exists")
        return {"status": "missing"}
    with open(real, "rb") as f:
        data = f.read()
    meta = store.put_artifact(data, "", provenance)
    store.ref_artifact(ref_id, project_id, event_id, ref, meta["sha256"], "present")
    return {"status": "present", **meta}

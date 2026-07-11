"""MCP wrapper for a self-hosted ACE-Step-1.5 REST API (music generation).

Reference implementation (untested against a live ACE-Step install — adjust
the env vars below to your version's actual API):

  ACESTEP_URL            base URL of `uv run acestep-api`   (default http://127.0.0.1:8001)
  ACESTEP_GENERATE_PATH  generation endpoint path           (default /generate)
  ARTIFACT_DIR           where to save returned audio       (default ./artifacts)
  MCP_HOST / MCP_PORT    where this wrapper listens         (default 0.0.0.0:8765)

Run: python acestep_mcp.py   →   MCP endpoint at http://<host>:8765/mcp
"""

import base64
import os
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

ACESTEP_URL = os.environ.get("ACESTEP_URL", "http://127.0.0.1:8001").rstrip("/")
GENERATE_PATH = os.environ.get("ACESTEP_GENERATE_PATH", "/generate")
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "./artifacts"))

mcp = FastMCP("acestep", host=os.environ.get("MCP_HOST", "0.0.0.0"),
              port=int(os.environ.get("MCP_PORT", "8765")))


@mcp.tool()
async def generate_music(prompt: str, duration_seconds: int = 60,
                         lyrics: str = "") -> str:
    """Generate music from a text description (optionally with lyrics) using
    the local ACE-Step model. Returns a file path / URL to the audio artifact.
    Generation takes minutes — be patient."""
    payload = {"prompt": prompt, "audio_duration": duration_seconds}
    if lyrics:
        payload["lyrics"] = lyrics
    async with httpx.AsyncClient(timeout=1800) as client:
        r = await client.post(f"{ACESTEP_URL}{GENERATE_PATH}", json=payload)
        r.raise_for_status()
        data = r.json()

    # Version-tolerant result handling: newer builds return a URL/path,
    # some return base64 audio.
    for key in ("url", "audio_url", "file", "path", "output_path"):
        if isinstance(data.get(key), str):
            return f"Music generated: {data[key]}"
    if isinstance(data.get("audio"), str):
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        out = ARTIFACT_DIR / f"acestep_{int(time.time())}.wav"
        out.write_bytes(base64.b64decode(data["audio"]))
        return f"Music generated: {out.resolve()}"
    return f"Generation finished but response shape unrecognized: {str(data)[:500]}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

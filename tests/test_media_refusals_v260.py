"""v0.181.260: a refusal on one of the media server's limits is relayed, not worked around.
Round 2 of the Media_Gen chat test: "a 500 second sound effect" got "duration_seconds must be
between 1 and 374 for this model (got 500)", and the model made 500 s with generate_music."""
import asyncio
from types import SimpleNamespace

import pytest

from foundry_router import request_context, tool_media
from foundry_router.tools.mcp_client import ToolCallError, ToolResult

RANGE = ("Error executing tool generate_sfx: duration_seconds must be between 1 and 374 for "
         "this model (got 500).")
LICENCE = ("Error executing tool animate_3d_model: 'game' is a commercial library; this model is "
           "licensed for non-commercial use only. Use a personal library (personal)")
LIBRARY = "Error executing tool generate_image: library must be one of: game, personal"
WRONG_INPUT = "Error executing tool add_captions: expected a video, got an image"


def _err(text, tool="generate_sfx"):
    return ToolCallError(f"MCP tool Media_Gen/{tool} returned error: {text}",
                         ToolResult([{"type": "text", "text": text}], is_error=True))


@pytest.mark.parametrize("text,tool", [(RANGE, "generate_sfx"), (LICENCE, "animate_3d_model"),
                                       (LIBRARY, "generate_image")])
def test_a_limit_refusal_gets_the_relay_note_once(text, tool):
    out = tool_media.note_refusal("Media_Gen", tool, {"x": 1}, _err(text, tool))
    s = str(out)
    assert text in s and "refused this request on one of its limits" in s
    assert "Do not use another tool, library or setting to get around the limit" in s
    again = tool_media.note_refusal("Media_Gen", tool, {"x": 1}, out)
    assert str(again).count("one of its limits") == 1
    assert "missing-model refusal" not in s


def test_an_input_the_model_can_fix_is_left_as_it_is():
    err = _err(WRONG_INPUT, "add_captions")
    assert tool_media.note_refusal("Media_Gen", "add_captions", {}, err) is err
    assert tool_media.note_refusal("Media_Gen", "x", {}, RuntimeError("boom")).args == ("boom",)


def test_through_the_tool_call_path_and_the_same_call_is_not_sent_again():
    from foundry_router.brain.agent import call_tool_rich
    calls = []

    async def fake(server, tool, args, **kw):
        calls.append(dict(args))
        raise _err(RANGE)
    mcp = SimpleNamespace(call_tool_rich=fake, foundry_svc=None)

    async def go():
        request_context._request_id.set("req-260-1")
        out = []
        for args in ({"prompt": "rain", "duration_seconds": 500},
                     {"prompt": "rain", "duration_seconds": 500}):
            try:
                await call_tool_rich(mcp, "Media_Gen", "generate_sfx", args)
            except ToolCallError as e:
                out.append(str(e))
        return out
    out = asyncio.run(go())
    assert calls == [{"prompt": "rain", "duration_seconds": 500}]        # the repeat never left
    assert "one of its limits" in out[0] and "refused again without calling the server" in out[1]


def test_the_missing_model_note_still_wins():
    text = ("Krea 2 Turbo is not installed on the media server: missing diffusion_models/krea.gguf"
            " (9 GB). Nothing was queued. duration must be between 1 and 4")
    s = str(tool_media.note_refusal("Media_Gen", "generate_image", {}, _err(text, "generate_image")))
    assert "missing-model refusal" in s and "one of its limits" not in s

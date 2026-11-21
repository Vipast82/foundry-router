"""v0.181.220: the finder breaks ties on the query, not on registry order.

The brain lane's FLOW-013 searched "video processing ffmpeg subtitles burned in
srt vtt". add_captions scored 4 - video, burned, srt, vtt, all in its
description - and so did every *_video tool, for "video" in its name; registry
order put eight of those first and the search never showed the tool. Measured
on all 109 searches the two latest profiles made (ties counted worst case), the
wanted tool came up in the top 8 for 83; with this tie-break, 87, and first for
51 rather than 47. Weighting every word by rarity instead also fixed FLOW-013
but moved others out of the eight, because it changes who scores higher.
"""
from types import SimpleNamespace as NS

from foundry_router import tool_finder

# the live Media_Gen catalogue, first line of each description, 2026-11-21
LIVE = {
    "create_explainer_video": "Finished narrated explainer / information video in one call: "
                              "voiceover, animated slides with real",
    "add_audio_to_video": "Put music, a sound effect or a voice track onto a video (ffmpeg, no GPU).",
    "stitch_videos": "Join video clips into one longer video (ffmpeg, no GPU) - for long-form "
                     "video made of",
    "create_tutorial_video": "Narrated tutorial / how-to video from screenshots or screen "
                             "recordings (or slides + speaker notes):",
    "upscale_video": "Rebuild a video at a higher resolution: every frame is cleaned and "
                     "enlarged with the same",
    "generate_video": "Generate a video clip WITH matching sound (LTX: picture and audio are "
                      "generated together,",
    "interpolate_video": "Make a video smoother by generating in-between frames (FILM frame "
                         "interpolation).",
    "create_music_video": "Make a complete music video for a song, with a lead singer who "
                          "looks like the person in a photo.",
    "edit_video": "Cut, trim, crop or speed up a video.",
    "reformat_video": "Change a video's aspect ratio for another platform.",
    "add_captions": "Add captions to a video: word-timed, burned into the picture, plus "
                    ".srt/.vtt files for YouTube's",
}
QUERY = "video processing ffmpeg subtitles burned in srt vtt"


def _tools(order):
    return [NS(name=n, mcp_tool=n, server="Media_Gen",
               description="[MCP:Media_Gen] " + LIVE[n], kind="mcp") for n in order]


def test_the_brain_lanes_search_now_shows_add_captions_whatever_the_registry_order():
    names = list(LIVE)                       # add_captions last, as it lost the tie live
    for order in (names, names[::-1]):
        got = [t.name for t in tool_finder.search(_tools(order), QUERY)]
        assert "add_captions" in got, order
        # it matched four of the query's words; the tools it tied with matched one
        tied = [n for n in got if n not in ("add_audio_to_video", "stitch_videos")]
        assert tied[0] == "add_captions"


def test_a_tie_break_never_lifts_a_tool_over_one_that_scored_higher():
    """stitch_videos and add_audio_to_video say "ffmpeg" too, so they score 5 to
    add_captions' 4 and stay ahead of it."""
    got = [t.name for t in tool_finder.search(_tools(list(LIVE)), QUERY)]
    assert got.index("stitch_videos") < got.index("add_captions")
    assert got.index("add_audio_to_video") < got.index("add_captions")


def test_more_of_the_query_matched_wins_a_tie():
    tools = [NS(name="alpha_video", mcp_tool="alpha_video", server="", kind="mcp",
                description="something else"),                       # video in name: 3
             NS(name="beta", mcp_tool="beta", server="", kind="mcp",
                description="srt vtt burned")]                       # three words: 3
    for order in (tools, tools[::-1]):
        assert [t.name for t in tool_finder.search(order, "video srt vtt burned")] \
            == ["beta", "alpha_video"]


def test_then_the_rarer_words_win():
    common = [NS(name=f"x{i}", mcp_tool=f"x{i}", server="", kind="mcp",
                 description="video") for i in range(5)]
    rare = NS(name="r", mcp_tool="r", server="", kind="mcp", description="vtt")
    for order in (common + [rare], [rare] + common):
        assert tool_finder.search(order, "video vtt", 1)[0].name == "r"


def test_a_full_tie_still_keeps_registry_order():
    a = NS(name="a", mcp_tool="a", server="", kind="mcp", description="video")
    b = NS(name="b", mcp_tool="b", server="", kind="mcp", description="video")
    assert [t.name for t in tool_finder.search([a, b], "video")] == ["a", "b"]
    assert [t.name for t in tool_finder.search([b, a], "video")] == ["b", "a"]

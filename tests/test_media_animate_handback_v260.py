"""v0.181.260: animate_3d_model's animated .glb is shown. Its preview picture has the same file
stem (<name>_animated.glb / <name>_animated.png), and the one-link-per-result rule kept only
the picture (round 2, job c16ba3c7d437)."""
from foundry_router import tool_media

F = "http://192.168.0.206:8190/files/personal"
CHECK = ("job_id: c16ba3c7d437\nSTATUS: done\ntool: animate_3d_model (Puppeteer + LTX-2.5 + Blender)\n"
         "Files (URLs on the remote server; download with curl or open in a browser, do not read them):\n"
         f"  animated_glb: {F}/3d/red_robot_toy_animated_c16ba3c7_animated.glb\n"
         f"  animated_fbx: {F}/3d/red_robot_toy_animated_c16ba3c7_animated.fbx\n"
         f"  compare_mp4: {F}/video/red_robot_toy_animated_c16ba3c7_compare.mp4\n"
         f"  guide_mp4: {F}/video/red_robot_toy_animated_guide_feda09c6.mp4\n"
         f"  first_frame_png: {F}/3d/red_robot_toy_animated_c16ba3c7_front.png\n"
         f"  rig_txt: {F}/3d/red_robot_toy_animated_c16ba3c7_rig.txt\n"
         f"  preview_png: {F}/3d/red_robot_toy_animated_c16ba3c7_animated.png\n"
         f"  report_json: {F}/3d/red_robot_toy_animated_c16ba3c7_animation.json\n")
HOSTS = {"192.168.0.206"}


def test_the_animated_model_and_its_preview_are_both_shown():
    got = tool_media.pick_urls([CHECK], HOSTS)
    assert got[0] == f"{F}/3d/red_robot_toy_animated_c16ba3c7_animated.glb"      # main file first
    assert f"{F}/3d/red_robot_toy_animated_c16ba3c7_animated.png" in got
    assert f"{F}/video/red_robot_toy_animated_c16ba3c7_compare.mp4" in got
    assert not any(u.endswith((".fbx", ".txt", ".json")) for u in got)


def test_one_sound_in_three_formats_is_still_one_link():
    text = (f"ogg: {F}/sfx/door_6faab0cc.ogg\nmp3: {F}/sfx/door_6faab0cc.mp3\n"
            f"flac: {F}/sfx/door_6faab0cc.flac\n")
    assert tool_media.pick_urls([text], HOSTS) == [f"{F}/sfx/door_6faab0cc.mp3"]
    text = f"png: {F}/image/x_1.png\njpg: {F}/image/x_1.jpg\n"
    assert tool_media.pick_urls([text], HOSTS) == [f"{F}/image/x_1.png"]

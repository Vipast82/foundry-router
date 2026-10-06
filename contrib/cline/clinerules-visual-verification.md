# Visual verification (Roblox Studio + Blender, main 1440p monitor)

## Capturing
- Capture with `tools/capture-window.ps1` (copy of Foundry's
  `contrib/cline/capture-window.ps1`), never a full-desktop grab:
  - Roblox Studio: `-Process RobloxStudio` (add `-Title "<place name>"` when
    more than one place is open)
  - Blender: `-Process blender`
  The script brings the window to the foreground, confirms it is in focus,
  and captures only that window at native resolution. If it reports that the
  window could not be focused, stop and say so instead of capturing anyway.
- Both apps live on the main 1440p monitor. Keep the target window maximized
  there; never capture from the ultrawide.
- Choose where each image goes with `-Out` (a folder or a full `.png` path)
  to suit the task, e.g. `docs/evidence/<sprint>/` for evidence, or a
  scratch folder for quick looks; `-Name` sets the file name prefix. With no
  `-Out` it saves to `screenshots/<name>-<timestamp>.png`. Use the path on
  the final `Saved:` line to open the image and in the evidence file.
- Check the script's output line: if it says the capture is above the 5120
  token cap, re-capture with `-Crop x,y,w,h` (relative to the window).

## What to capture
- UI checks: crop to the panel or element being verified (`-Crop`), at native
  resolution. Use a full-window capture only to check overall layout.
- Blender checks: do not screenshot the UI. Render fixed views with Blender's
  Python at 1024x1024 (front, side, top, perspective): plain background, even
  lighting, camera framed on the object. Attach them as separate images, not
  a contact sheet.
- After every change, re-capture the SAME view/crop and compare before vs
  after explicitly (what changed, what should have changed, what did not).

## Numbers before pixels
- Blender: print exact dimensions, bounding box, vertex/face counts,
  modifiers and materials, and compare them to the spec. Images confirm the
  overall look; numbers confirm precision.
- Roblox UI: confirm layout with AbsolutePosition / AbsoluteSize checks
  (e.g. the column-fit tests), not screenshots alone.
- Report a visual check as PASS only when both the numbers and the image
  agree; record the image path and the numbers in the evidence file.

## Context hygiene
- One capture per question. Do not re-attach or re-read the same
  screenshot; refer to its path and to what you already observed in it.
  Every image stays in the conversation until Cline compacts it.

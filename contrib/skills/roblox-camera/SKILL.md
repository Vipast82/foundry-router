---
name: roblox-camera
description: Script or fix the Roblox camera - CameraType and subject, CFrame math, custom follow/shoulder/top-down cameras, screen-to-world raycasts, cutscenes, shake and field of view. Load for any camera behaviour or screen-to-world picking.
---

# Roblox camera

- `workspace.CurrentCamera` is per client; the server cannot drive it.
- Take control with `CameraType = Enum.CameraType.Scriptable`, otherwise the default scripts overwrite your CFrame every frame. `CameraSubject` cannot be nil.
- Update custom cameras in `RunService.PreRender` (visual sync); `Heartbeat` adds a frame of lag. `RenderStepped` still works but `PreRender` supersedes it.
- `CFrame.lookAt(pos, target, up)` for new code (`CFrame.new(pos, lookAt)` is legacy). Roll via `CFrame.Angles(0, 0, roll)`, not `SetRoll`.
- Smooth with `cf:Lerp(goal, math.min(dt * k, 1))` so speed is frame-rate independent.
- Screen picking: `camera:ScreenPointToRay(x, y)` accounts for the GUI inset, `ViewportPointToRay` does not; both return unit rays (multiply the direction by your range).
- `camera.ViewportSize` is the real drawable size: read it rather than assume a resolution (devices, orientation and safe areas change it).
- VR: `Camera.CFrame` lacks head rotation; use `GetRenderCFrame()` for the true view.
- Cutscenes: switch to Scriptable, tween or lerp through authored CFrames, restore the previous type and subject afterwards (also on interruption).
- Shake: offset in local space and decay it; respect a reduced-motion setting if the game has one.

## References
- `references/camera.md` - first/third person, shoulder cam, cutscenes, shake, full API (roblox-brain).

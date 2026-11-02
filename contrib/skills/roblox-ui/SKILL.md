---
name: roblox-ui
description: Build or fix Roblox UI that works on phones, tablets, desktop and console - ScreenGui and safe areas, Scale vs Offset, layouts and flex, constraints, text and fonts, gamepad selection, reactive UI cleanup, HUDs, shops and dialogs, and verifying sizes. Load for any menu, HUD or screen.
---

# Roblox UI

## Layout
- Mobile first: design on the smallest landscape phone with touch controls showing, then grow. Reflow for space; do not just shrink.
- Keep interactive UI inside the safe area (`ScreenGui.ScreenInsets = CoreUISafeInsets`); leave the top bar, thumbstick, jump button and chat clear. Console needs the TV-safe area and has no chat window.
- Scale for containers, Offset for fixed-size touch targets; `UIAspectRatioConstraint`, `UISizeConstraint`, `UITextSizeConstraint` to stop extremes. Touch targets at least ~44 px.
- Layout objects (`UIListLayout` with flex / `UIFlexItem`, `UIGridLayout`, `UIPadding`) own repeated layout; do not hand-position lists.
- Check every pair of elements that can be visible together for overlap, by measured `AbsolutePosition`/`AbsoluteSize`.

## Text
`TextScaled` sparingly (combine with `UITextSizeConstraint`); `TextWrapped` + truncation for user text; RichText only on trusted strings. Every player-facing string goes through the localization path.

## Input and focus
- Gamepad: one owner of `GuiService.SelectedObject`, `SelectionGroup` bounds, focus restored when a screen closes, a layered Back action.
- Block gameplay input while a TextBox is focused or a modal is open (see `roblox-input`).

## Authority and lifecycle
- UI shows server state; a button press is a request, never an authority (validate on the server).
- Each screen owns its connections, tweens and threads and cleans them up on close; open/close repeatedly and check memory.
- Loading screens live in `ReplicatedFirst`.

## Verify sizes
- Test a fixed size set, at minimum: 1920x1080, 1366x768, 1024x768, 844x390 (phone landscape) and 390x844 (phone portrait).
- With Studio's Device Simulator, set the orientation explicitly with every device switch: orientation is kept separately from the device, and a portrait setting left over from a phone check rotates the next desktop device (1920x1080 shows as 1080x1920).
- Read the real size back (`ScreenGui.AbsoluteSize` or `camera.ViewportSize`) before measuring or capturing; a capture whose size differs from the one asked for is a failure, not evidence.

## References
- `references/gui.md` - mobile-first layout, safe areas, gamepad, console, reactive UI, deprecated APIs (roblox-brain).
- `references/ui-quick.md`, `ui-styling-and-frameworks.md` - containers, scale/offset, flex, StyleSheets and tokens, React-lua/Fusion notes (EL4CTEO).

---
name: roblox-input
description: Handle player input across touch, keyboard and mouse, gamepad and VR - UserInputService, ContextActionService, the Input Action System (InputContext, InputAction, InputBinding), on-screen touch buttons, input gating and key hints. Load for controls, bindings or cross-platform input bugs.
---

# Roblox input

- Events give `(input, gameProcessedEvent)`: ignore gameplay input when `gameProcessedEvent` is true. The mouse wheel fires only `InputChanged`.
- Bind actions, not keys: `ContextActionService:BindAction(name, handler, createTouchButton, ...keys)` (check the `state` argument; touch arrives as `KeyCode.Unknown`), `BindActionAtPriority` when bindings stack. Projects on Server Authority use `InputAction`/`InputContext` with `RunService:BindToSimulation()`.
- Pick the control scheme from `UserInputService.PreferredInput` and listen for its change (hybrid devices switch); do not cache `TouchEnabled` once at startup.
- On-screen touch buttons fire only for touches that began on them (a camera drag across must not fire). Position them relative to the measured jump button and keep the thumbstick zone clear.
- Gate gameplay input while a menu is open, a TextBox is focused, `GuiService.MenuIsOpen`, or `GuiService.SelectedObject` is set.
- A cancelled hold is not a release: cancel holds on window open, unequip, focus loss, input-type switch and gamepad disconnect.
- Gamepad: default tools activate on R2 via `BindActivate` (`Tool.ManualActivationOnly` to opt out); aim from the screen centre, not `GetMouseLocation()`.
- Key hints come from the engine (`GetStringForKeyCode`, `GetImageForKeyCode`, `InputActionLabel`); never hard-code a console's button names.
- Input only expresses intent: the server validates the resulting action.
- Test with Studio's Device Simulator and Controller Emulator.

## References
- `references/input.md` - bindings, touch buttons, gating, Input Action System, gamepad (roblox-brain).
- `references/input-quick.md` - UserInputService/ContextActionService/Input Action System and cameras across devices (EL4CTEO).

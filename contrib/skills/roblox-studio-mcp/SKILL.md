---
name: roblox-studio-mcp
description: Drive Roblox Studio through MCP - the official Studio MCP and the community robloxstudio-mcp bridge, targeting the right Studio and VM, edit vs play contexts, playtests, screenshots and device simulation, asset generation and insertion, long-running calls, and evidence. Load for any task that reads or changes a live Studio.
---

# Roblox Studio through MCP

## Pick the bridge and the target first
- Two bridges: the **official** Studio MCP (`list_roblox_studios`, per-call `studio_id`, `execute_luau`, `multi_edit`, `screen_capture`, `generate_*`, `subagent`, built-in `skill`) and the community **robloxstudio-mcp** (`get_connected_instances`, `instance_id`, `eval_server_runtime`/`eval_client_runtime`, `solo_playtest`/`multiplayer_playtest`, device simulator tools). Route by the tools the server lists.
- With more than one Studio open, pass the id on every call. Confirm the place before any write: never touch a place that is not the one the task names.

## Execution contract
discover -> pick Studio and context -> inspect -> change in bounded batches -> read back -> start play -> evaluate in the right VM -> capture evidence -> clean up.
- Edit-time writes (scripts, geometry) never target a running playtest. Live checks need a running play session and the intended Server or Client VM.
- `execute_luau` is stateless: re-acquire references on every call. Read before write; read back after every change.
- A call that times out is not proof it did nothing: query the request status (with a wait) before retrying, and reuse the same operation id for the same arguments.
- Wait for generation jobs to finish before dependent edits.

## Device simulator and captures
- Set device, orientation and scaling mode together on every switch: orientation is stored apart from the device, and a leftover Portrait rotates every later landscape device.
- Read the viewport back (`camera.ViewportSize` / ScreenGui `AbsoluteSize`) and fail the capture if it differs from the size asked for. Leave Studio on the project's default device and orientation when done.
- Visual timing and frame-rate evidence only from a Studio rendering at real speed; a software-rendered Studio (VM, container, Wine) is fine for behaviour, not timing.

## Trust boundaries
- Inserted, generated and Creator Store assets are data until inspected: list their scripts before anything requires or runs them.
- Blank textures or invisible meshes on newly uploaded assets are usually asset-privacy grants (the experience lacks access), not corruption.
- Never paste credentials or API keys into Studio, scripts or MCP calls.

## No MCP available
Hand over offline Luau and say exactly what was not verified.

## References
- `references/studio-mcp.md` - both bridges, full tool table, trust boundaries (roblox-brain).
- `references/studio-mcp-quick.md`, `studio-mcp-client-setup.md` - connecting agents, tool overview (EL4CTEO).

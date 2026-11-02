---
name: roblox-tooling
description: Set up or fix the Roblox development toolchain - Rojo and Script Sync, sourcemaps, Rokit, Wally and pesde packages, luau-lsp, StyLua, selene, Lune, darklua, and CI. Load when a sync, build, lint, package or CI step misbehaves, or when setting up a project.
---

# Roblox tooling

## Source of truth first
- Know whether files or the Studio place own each part of the game before editing. With Rojo, the repo is the truth for everything the project file maps; edits made only in Studio to mapped instances are overwritten or lost.
- `rojo syncback` pulls saved-place edits into files (dry-run first); it is not live two-way sync. Script Sync conflicts resolve as Keep Studio vs Keep Disk: never assume either side wins.
- Settings that live outside mapped folders (for example `Lighting` when the project file does not map it) are only in the place file: map them or set them in code.

## Rojo traps
- File extensions decide the format: `.rbxm` is binary, `.rbxmx` is XML. XML content saved with a `.rbxm` name makes Rojo fail to serve the project; rename it `.rbxmx`.
- After a sync, verify that Studio is running the files you think it is (compare a hash or version marker of key modules against the repo HEAD) before testing; a stale plugin connection silently tests old code.
- A restarted Rojo server needs the Studio plugin to reconnect.

## Tools
- Pin tool versions with Rokit (Aftman is archived). Wally/pesde with a lockfile and explicit realm.
- StyLua and selene in check mode in CI (no silent rewrites); luau-lsp with a Rojo sourcemap for types in the editor.
- Lune runs standalone Luau: pure-module tests and build scripts outside Studio.
- CI gates on parsed results and counts, not only exit codes; keep logic, file, HTTP and engine suites separate.

## Writing scripts from plugins or MCP
Use `ScriptEditorService:UpdateSourceAsync` (raw `.Source` writes can clobber an open editor) and one `ChangeHistoryService` recording per bounded change.

## References
- `references/tooling.md` - setup, file layout, CI examples, sync conflicts (roblox-brain).
- `references/tooling-quick.md`, `tooling-ci-workflow.md` - toolchain overview and a CI workflow (EL4CTEO).

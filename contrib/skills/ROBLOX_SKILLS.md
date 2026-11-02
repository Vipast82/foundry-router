# Foundry Roblox skill library

General Roblox development skills for any Foundry game project (`contrib/skills/roblox-*`). A project
installs the ones it wants through its profile:

```toml
[skills]
install = ["roblox-luau", "roblox-networking", "..."]
```

`skills_install.py` copies each listed folder into the game's untracked `.kilo/skills/` and allows it
in the kilo.jsonc agent blocks. Project skills (for example `au-*` for AnimalUprising) sit on top: load
the project skill first; it names the general skills it builds on, and it wins where they differ.

## How each skill is built
- `SKILL.md` is Foundry's own short entry page (under ~4 KB): when to load it, the rules that matter,
  known traps (including lessons from Foundry's own projects) and a map of its references. Sessions
  read it first.
- `references/` holds the deep material, read on demand. Most of it is copied, with a provenance line,
  from two MIT libraries: TabooHarmony/roblox-brain (primary: sourced and checked against Roblox's
  docs) and EL4CTEO/roblox-skills (compact cheat sheets and topics roblox-brain lacks). Ideas from
  SyphoDev's free Roblox skills are summarised in Foundry's own words with credit. Each folder's
  `references/THIRD_PARTY_NOTICES.md` carries the licences.
- Where sources disagree, the entry page follows the current Roblox API reference and says so (for
  example: `Lighting.LightingStyle` is script-writable, `Lighting.Technology` is not).

## Which skill for which task
| Task | Load |
|---|---|
| Any Luau code | `roblox-luau` |
| Where code lives, ownership, startup, streaming | `roblox-architecture` |
| Remotes, client/server messages | `roblox-networking` |
| Exploit review, rewards, currency, permissions | `roblox-security` |
| Saves, leaderboards, cross-server data | `roblox-data` |
| FPS, lag, memory, budgets | `roblox-performance` |
| Maps, environments, lighting, terrain, props | `roblox-world-building` |
| Physics, constraints, raycasts, projectiles | `roblox-physics` |
| NPCs, enemies, pathfinding | `roblox-npc-ai` |
| Camera behaviour, screen picking | `roblox-camera` |
| Controls and bindings across devices | `roblox-input` |
| Menus, HUD, shops, sizes and safe areas | `roblox-ui` |
| Animation, characters, VFX | `roblox-animation-vfx` |
| Sound, music, audio generation and review | `roblox-audio` |
| Translation, locales | `roblox-localization` |
| Chat, player-written text | `roblox-text-chat` |
| Lobbies, matches, teleports, parties | `roblox-teleport-matchmaking` |
| Currency, XP, rounds, combat, inventory recipes | `roblox-game-systems` |
| Core loop, onboarding, economy, retention | `roblox-game-design` |
| Robux purchases, passes, products | `roblox-monetization` |
| Telemetry, game health, live events | `roblox-analytics-liveops` |
| Tests, gates, evidence, QA | `roblox-testing` |
| Code review | `roblox-code-review` |
| Rojo, packages, linters, CI | `roblox-tooling` |
| Driving Studio through MCP | `roblox-studio-mcp` |
| Open Cloud, HTTP, asset upload, webhooks | `roblox-open-cloud` |
| Release readiness | `roblox-publish-checklist` |

Load only what the task needs: one or two entry pages, then the references they point to.

## Updating
Re-run `contrib/skills/_build/build_roblox_skills.py <upstream_dir> <repo> <el4_sha> <brain_sha>` against new upstream
commits to refresh `references/`; it never touches `SKILL.md` or hand-written references. Review the
upstream diff before shipping, and re-check any API claim that changed against the Roblox API reference.

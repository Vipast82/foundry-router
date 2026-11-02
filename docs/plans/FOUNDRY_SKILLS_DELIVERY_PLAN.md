# Foundry-shipped skills reach AU's Kilo, plus `au-blender-asset` (gap 7)

**Status:** built with the recommended answers (Victor 2026-11-01: "go ahead and plan everything
and then build it"). Ships off: `[skills] install = []`.
**Source:** `reports/ASSET_PIPELINE_REVIEW_2026-11.md` gap 7.

## The question first: how does a Foundry skill reach AU's Kilo without editing the AU repo?
What was measured on 2026-11-01 (read-only):
- Kilo 7.8.8 loads AU skills from `C:/Roblox/AnimalUprising/.kilo/skills/<name>/SKILL.md`. The
  kilo.db tool records show every session loading from there, **including sessions in track
  worktrees** (`.tracks/mv2-2` loaded `C:\Roblox\AnimalUprising\.kilo\skills\au-runtime-gate`).
  The tracks sit inside the main checkout, so Kilo finds the parent's folder.
- That folder is **untracked** in AU's git (`git ls-files .kilo` lists only
  `.kilo/rules/animaluprising-adapter.md`). It holds 15 skills, all copied there by hand:
  `au-fast-lane` "is installed from" Foundry's copy, and `foundry-images` is a hand copy of
  `contrib/media-adapter/skills/foundry-images`.
- AU's `kilo.jsonc` (also untracked; Foundry already rewrites its agent blocks) denies every
  skill (`"skill": {"*": "deny", ...}`) except the ones it lists. A new skill also needs an
  allow there.

So the skill folder and `kilo.jsonc` are the workstation's Kilo install, not the game repo.
Foundry can write both without a sprint, as long as it never touches a tracked file.

## What changes
1. **`foundry_coordinator/skills_install.py`:** installs the skills `[skills] install` lists
   into `<workspace>/<skills.target>` (default `.kilo/skills`).
   - **Sources, searched in order:** `foundry_coordinator/projects/<project>/kilo_skills/`,
     `contrib/media-adapter/skills/`, `contrib/skills/`.
   - **Refuses** when any file under the target is tracked in git (event
     `skills_install_refused`). Installing there would change the game repo.
   - **A folder Foundry installed** carries `.foundry-skill.json` (source, hash, time). It is
     replaced when Foundry's copy changes (a temp folder, then a swap).
   - **A hand copy** (no marker, different content) is first copied to
     `<state_dir>/skills_backup/<project>/<name>-<time>/` (outside the game checkout), then
     replaced. Identical content just gets the marker.
   - **A skill taken off the list** is removed only when it carries the marker. Hand-made
     skills are never touched.
   - **Runs** from the engine tick every `skills.check_seconds` (300), and at once when the
     list changes. Event `skills_installed` when anything changed.
2. **The allow in kilo.jsonc:** every listed skill gets `skill: {<name>: allow}` in the agent
   blocks (main file and track files), through `kilo_config.with_extra`.
   - Generated denies still win: `foundry-images` stays denied for a sprint without `images`.
   - A change to `skills.*` regenerates kilo.jsonc and asks for a Kilo reload, as a role-rules
     change does.
3. **New skill `au-blender-asset`** (Foundry
   `projects/animaluprising/kilo_skills/au-blender-asset/`), drawn from AU's
   `docs/ASSET_PIPELINE.md` section 7 as it stands after AS-P9:
   - the conventions: +Y front, studs, budgets, and sides by axis (+X is screen-left in FRONT);
   - how Blender runs (the pool command, `--bg` per model, exit 75);
   - the A-E one-asset loop, with the reference pack (`ref_align.py` → `ref_setup.py`,
     `side_check.py`, `ref_iou.py`) and the short-note rule (gap 6);
   - the traps.
   - It says the runbook wins where the two differ.
4. **The AS- prompt:** when `au-blender-asset` is installed, an AS- sprint's prompt says to
   load it first.
5. **The Foundry-side skill** is `foundry-images` with the turnaround recipe (v0.171.0, gap 5).
   Listing it in `skills.install` replaces AU's stale hand copy (backed up first).

## Decisions (recommended answers, used for the build)
- **D-K1. Where Foundry puts skills:** AU's untracked `.kilo/skills/`, the folder Kilo already
  reads for main and track sessions. *Recommended.* The alternatives were Kilo's global folder
  (not verified for Kilo 7.8.8, and it would reach every project) or an AU sprint committing
  the skills (it would edit the repo for workstation config and need a sprint per change).
- **D-K2. Never write into tracked files:** the installer checks `git ls-files` first and
  refuses. *Recommended:* the "AU repo is changed only by its sprints" rule holds by
  construction.
- **D-K3. Hand copies:** backed up outside the checkout, then replaced. *Recommended:* AU's
  `foundry-images` hand copy is exactly the stale-copy problem this fixes. The alternative
  (skip hand copies) would leave it stale forever.
- **D-K4. Removal:** only Foundry-marked folders, when they leave the list. *Recommended.*
- **D-K5. No AU sprint needed:** the skill, the allow and the prompt line are all Foundry-side.
  *Recommended.* Pointing AU's ASSET_PIPELINE.md §7 at the skill is optional and can ride along
  in any later AS- sprint's docs step.

## Not in this release
- Committing the 11 AU skills that exist only as untracked files in the OneDrive Foundry
  checkout (housekeeping, noted in the report). They can be listed in `skills.install` once
  they are in Foundry's repo.

## To switch it on (Victor)
1. Rebuild the router, so Settings shows `skills.install`, `skills.target` and
   `skills.check_seconds`.
2. Settings → `skills.install` = `au-blender-asset, foundry-images`, then Review & deploy.
3. Within a tick:
   - the coordinator installs both (AU's old `foundry-images` hand copy goes to
     `~/.foundry-coordinator/skills_backup/animaluprising/`);
   - it rewrites the kilo.jsonc allows and asks the client to reload Kilo.
   - New AS- sessions load the skill.

## Tests
`tests/coordinator/test_skills_install.py`:
- off by default, and the real skills are found;
- install, update, same and remove;
- a hand copy is backed up and replaced;
- a tracked target is refused;
- the timing rule (`due`);
- kilo.jsonc allows the skill in the main and track files, and the `foundry-images` deny still
  wins without `images`;
- a `skills.*` change regenerates kilo.jsonc, and the AS- prompt line appears.

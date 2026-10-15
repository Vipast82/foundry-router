# Foundry media adapter (Windows and Linux)

A small local MCP server that gives Kilo, Cline or Claude Code image tools and
**saves the results into your project**. Foundry (on victor-ai) runs the image
job; this adapter runs on the machine where the project lives, so it can write
to `C:\Roblox\AnimalUprising` (or `~/Roblox/AnimalUprising` on Linux). The
same script works on both systems; only the installer differs.

```
Kilo / Cline ──stdio──> foundry_media_mcp.py ──HTTP──> Foundry /media/*  ──> CLIProxyAPI (ChatGPT images)
                              │
                              └─ writes only inside your authorised folders
```

| File | What it is |
|---|---|
| `foundry_media_mcp.py` | the adapter (Python 3.8+, standard library only) |
| `install.ps1` | Windows installer → `%LOCALAPPDATA%\FoundryMedia` |
| `install.sh` | Linux/macOS installer → `~/.local/share/foundry-media` |
| `skills/foundry-images/SKILL.md` | the workflow skill for Kilo (`.kilo/skills/`) |
| `clinerules-images.md` | the same rules as a short `.clinerules` entry for Cline |

## Tools

| Tool | Does |
|---|---|
| `generate_image` | submits a job, waits up to `wait_seconds` (default 50), saves the image(s), returns paths |
| `edit_image` | same, with input images (and an optional mask) read from the project |
| `image_job_status` | checks a job; when finished, saves it. Never submits anything. |
| `list_image_jobs` | this client's recent jobs |
| `image_settings` | authorised folders, default folder, allowed models, budget use |

Each saved image gets a sidecar `<file>.foundry.json` with `"status":
"unapproved"`, the job id, model, prompt and SHA-256. Generating an image
never puts it into the game; that stays with your approval process.

Safety built in:
- **Folders**: `save_as` must resolve inside an authorised folder (symlinks
  and `..` resolved; case-insensitive on Windows). The destination is checked
  **before** the job is submitted, so a bad path never costs an image.
  Input images for `edit_image` must come from those folders too.
- **No overwrites**: an existing file gets a `-v1`, `-v2` copy unless
  `overwrite: true`.
- **Integrity**: every download is checked against Foundry's SHA-256.
- **No double billing**: a job still running returns its `job_id` and tells
  the agent to poll with `image_job_status`. Asking again for an image already
  saved returns the existing file.
- **No secrets in config files**: the key comes from `FOUNDRY_KEY` (Windows:
  a user environment variable; Linux: a 0600 key file read by the launcher).

## Install: Windows

1. In Foundry, add a client key with the `media` scope (`clients:` in
   config.yaml, see docs/CLIENTS.md), e.g. `kilo` with `scopes: [inference, media]`.
2. In PowerShell, from this folder:
   ```powershell
   .\install.ps1 -ProjectRoot C:\Roblox\AnimalUprising -SetKey
   ```
   Options: `-FoundryUrl http://192.168.0.112:11435` (the default),
   `-DefaultDir assets\generated\_unapproved`, `-ExtraRoots D:\Art\Exports`,
   `-WaitSeconds 50`. If scripts are blocked, run
   `powershell -ExecutionPolicy Bypass -File .\install.ps1 ...`.
3. Restart VS Code (so it sees `FOUNDRY_KEY`), then paste the printed entries:
   - **Kilo**: into the `mcp` section of `kilo.jsonc`. Match the shape of your
     existing `Roblox_Studio` entry if it differs from the printed one.
   - **Cline**: into `cline_mcp_settings.json`. `timeout: 300` lets one call
     wait longer than Cline's 60-second default.
4. Copy `skills\foundry-images` to `C:\Roblox\AnimalUprising\.kilo\skills\`,
   and/or add `clinerules-images.md` to `.clinerules`.

## Install: Linux (or if you switch OS)

```bash
./install.sh --project ~/Roblox/AnimalUprising --set-key
# options: --foundry URL  --default-dir DIR  --extra-root DIR  --wait 50
```

It writes `~/.local/share/foundry-media/foundry-media` (a launcher that loads
the key file) and prints the Kilo and Cline entries. The skill and rules are
the same files on both systems. Moving the project between systems only means
re-running the installer with the new path.

## Check it

```
python foundry_media_mcp.py --check      # Windows: py -3 ...\foundry_media_mcp.py --check
```

This prints the authorised folders, whether Foundry answers with your key,
the image backend, and each model's last successful job.

## Configuration reference

`foundry-media.json` (next to the script; the installers write it):

```json
{"foundry_url": "http://192.168.0.112:11435",
 "roots": ["C:\\Roblox\\AnimalUprising"],
 "default_dir": "assets\\generated\\_unapproved",
 "wait_seconds": 50, "sidecar": true, "allow_read_outside": false}
```

Environment variables override it: `FOUNDRY_URL`, `FOUNDRY_KEY`,
`FOUNDRY_MEDIA_ROOTS` (separated by `;` on Windows, `:` on Linux),
`FOUNDRY_MEDIA_DEFAULT`, `FOUNDRY_MEDIA_WAIT`.

`wait_seconds` is how long one tool call waits. Keep it under your client's
MCP timeout: Cline's default is 60 s, and the printed config raises it to 300
so you can use `wait_seconds` up to about 280. Longer jobs simply come back as
"running" and finish via `image_job_status`.

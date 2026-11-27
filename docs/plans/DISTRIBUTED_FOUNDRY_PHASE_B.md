# Distributed Foundry, phase b: the lane agent, the tunnel and remote sessions

Status: **approved 2026-11-27 with Db1-Db5 as recommended** (Victor: "Approve, build b-1").
**b-1 (B1 + B3 + B4) is built in v0.181.265**, all behind `[slots.<id>] lanes` (off); b-2..b-5
are not started. Nothing creates a lane on a slot yet (that is B6, release b-3).
Design: `docs/DISTRIBUTED_FOUNDRY.md` (§3, §4). Overall plan: `docs/plans/DISTRIBUTED_FOUNDRY_PLAN.md`.
Line references are on `main` at `016931a` (v0.181.264).

## Goal and done

At the end of phase b, a coding session for a **scratch pilot project** runs **on a spare slot**:
- the agent edits and commits in a worktree on the slot;
- the coordinator on the workstation starts, watches, recovers and stops it through one ssh tunnel;
- the junction fetches the commits back.

There is one sprint on Kilo and one on OpenCode. Losing the tunnel mid-session loses no commit.
The same agent then runs on a Windows VM (b2).

**Not in phase b:** Studio gates and Lune on the slot (phase c), live AU lanes, merges of remote
lanes into AU master (phase d). Nothing touches slot-1/2/3, the live coordinator or AU.

## What I found while planning (changes to the design)

1. **A container's 127.0.0.1 cannot be reached through the host.** Design §3 says the agent, Kilo
   and OpenCode listen on 127.0.0.1 "inside the slot". Docker forwards a published port to the
   container's own address, not its loopback. The slot already solves this for noVNC: the port is
   published on the **host's** 127.0.0.1 only and reached with `ssh -L`
   (`contrib/studio-slot/proxmox/docker-compose.yml:11-15`). Phase b does the same: inside the
   container the three servers listen on all addresses, and compose publishes them only on
   `127.0.0.1` of the slot host. The LAN still cannot reach them, and each needs its password or
   token as well.
2. **The slot's bare game copy is mounted read-only** into the container
   (`proxmox/docker-compose.yml:42`), and `git worktree add` writes into the repository. Lane
   worktrees get their own writable store: `/srv/foundry-lanes/<project>.git` on the host (a new
   rw mount). It is made with `git clone --bare --shared` from the existing copy, so it holds only
   new objects (`--shared` uses the copy's objects through alternates). The junction fetches lane
   branches from it over the same ssh.
3. **Training capture.** Kilo and OpenCode keep their session records in their own storage. For a
   remote session that storage is on the slot, and today the training collector and
   `foundry_training.mirror` read it on the workstation (`foundry_training/mirror.py:1-21`).
   Remote sessions must still be captured (memory note *training-data-keep-everything*). The
   slot must not get a broad key for this: the `training` preset has orchestration scope
   (`foundry_router/ui/setup_routes.py:45`). So the **coordinator** pulls the slot's session
   records through the agent (`/api/lane/sessions/export`) and puts them in its own training
   store. They then reach the router the way they do today.
4. **The pilot needs some of phase c's file reads.** To start and close a sprint, the engine reads
   the sprint plan and a few files directly (`engine.py:1687`, `:2489`, `:3905`, `:7287`). The
   generic verify command also runs in the workspace (`adapters/verify_cmd.py:23`). Phase b moves
   these reads, and the generic verify, onto the remote workspace. The Studio and AU-specific
   reads stay in phase c.
5. **The agent's token reaches the coordinator over ssh, not through the router.** The agent makes
   a new token each time it starts and keeps it in the container's tmpfs. The coordinator reads it
   when it opens the tunnel, with the ssh + `docker exec` access it already has
   (`slots.exec_argv`, `slots.py:181-184`). It is never logged or written to its store. A token
   in the router would add a secret to rotate and one more party that holds it.

## Work items

Each item lists its files, what it does, and its tests. Every item ships off by default.

### B1. The lane agent (Foundry Client, `slot` profile)

- **Files:** new `foundry_client/lane.py`; `foundry_client/api.py` (routes);
  `foundry_client/config.py` (profile + defaults); `foundry_client/components.py` (listen
  address).
- **Profile:** `[client] profile = "slot"` in client.toml, or `foundry-client run --headless
  --profile slot`. A slot profile runs kilo and opencode only (no coordinator, supervisor or
  Rojo), takes their listen address from settings (`0.0.0.0` in a container, `127.0.0.1` on a
  Windows host), and turns on the lane routes.
- **Routes**, under the existing `x-client-token` and local-host guard (`api.py:50-58`); through
  the tunnel the Host header is `127.0.0.1:<port>`:

  | Route | Does |
  |---|---|
  | `GET /api/lane/health` | kilo/opencode health, disk % and free GB, load average, free memory, Foundry version + head, lanes held and their HEADs, agent start time |
  | `POST /api/lane/git` `{lane, argv, timeout, env}` | `git -C <lane worktree> argv...`; argv is a list, never a shell; subcommand allow-list = what `Workspace` uses (`adapters/git.py:57-236`), checked by a test against that file; `env` only `GIT_*` author/committer/index names |
  | `POST /api/lane/read` `{lane, path, max_bytes}` / `POST /api/lane/stat` `{lane, paths}` | a file's bytes (base64, capped) / size + mtime + kind for a list; realpath must stay in the worktree; `.git/` refused |
  | `POST /api/lane/write` `{lane, path, data}` | the few files the coordinator itself writes into a worktree (plan ticks, scope grants: committed afterwards through `/git`); same path rules |
  | `POST /api/lane/run` `{lane, job, args}` → `{run_id}`; `GET /api/lane/run/<id>?from=<offset>` | named jobs only: `worktree-add`, `worktree-remove`, `verify` (a profile's `[[verify.checks]]` command, as `verify_cmd.run_check` builds it), `sessions-export`; output kept in a file, re-read from an offset after a network blip; `POST .../cancel` |
  | `GET /api/lane/sessions/export?since=<cursor>` | the session records kilo/opencode stored since a cursor, for the coordinator's training store (finding 3) |

- **Lane registry:** `<state>/lanes.json` lists the lanes this agent holds (`project`, `lane`,
  worktree path, branch). A second `worktree-add` for a held lane is refused (one writer).
- **Tests:** route tests with FastAPI's TestClient on a temp repo: the allow-list, path escapes
  (`..`, absolute paths, links out of the worktree, `.git`), the read/write caps, job re-attach
  from an offset, cancel, a refused second add. A parity test runs the existing Workspace tests
  against B3's `RemoteWorkspace` backed by this agent.
- **Estimate:** 2 days.

### B2. The slot image and compose run the agent

- **Files:** `contrib/studio-slot/entrypoint.sh`, `proxmox/docker-compose.yml`,
  `proxmox/setup-host.sh`, the `Dockerfile` (Python deps for the client: fastapi/uvicorn are
  already in requirements; the image gets a venv at `/opt/foundry-venv`).
- **Changes:**
  - With `SLOT_LANES=1` in the slot's `.env`, the entrypoint starts `foundry-client run
    --headless --profile slot` from `/opt/foundry` as the `studio` user. Without it, nothing new
    starts.
  - The token and the kilo/opencode passwords are made fresh at each start into
    `/run/secrets/lane/` (tmpfs, mode 600).
  - Compose publishes the agent (47811), Kilo (4096) and OpenCode (4097) on
    `127.0.0.1:<port>` of the host only.
  - New rw mount `/srv/foundry-lanes`. `setup-host.sh` creates it (owner 1000) and the per-project
    `--shared` bare clone (finding 2). Re-running it keeps what exists.
  - The model key (B5) arrives in `/run/secrets` through `slot-secrets` and reaches kilo/opencode
    as an environment variable.
- **Tests:** `bash -n`, a compose config check, and a test that the env switch adds nothing when
  off. The real build runs on the spare slot (P1).
- **Estimate:** 1 day + one image build on the spare slot (20-40 min).

### B3. The tunnel and `RemoteWorkspace` in the coordinator

- **Files:** new `foundry_coordinator/lane_tunnel.py`; new `foundry_coordinator/adapters/remote_git.py`;
  `tracks/manager.py` (`track_project`, `:259-276`, picks the workspace class from the track row).
- **The tunnel:** one `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15
  -o ServerAliveCountMax=3 -L <a>:127.0.0.1:47811 -L <k>:127.0.0.1:4096 -L <o>:127.0.0.1:4097
  <host>` per slot with `lanes = true`, with Foundry's key and known_hosts
  (`slot_ssh.ssh_options`). Free local ports are picked at start. It runs on a supervisor thread,
  never the tick thread (the `slotkeep` rule, `slotkeep.py:18-20`), and restarts with backoff
  (5 s → 2 min). Its state goes to the board: up, down since, last error. The token is read once
  per tunnel start (finding 5).
- **`RemoteWorkspace(Workspace)`:** `run()` posts to `/api/lane/git` and returns the same stdout,
  or raises the same `GitError` with the same message shape (`adapters/git.py:40-55`). It adds
  `read`, `stat`, `exists` and `write`. The local `Workspace` gets the same four methods as plain
  file calls, so callers do not care which one they hold.
- **Tests:** a fake ssh (a script that stands in for `ssh -L` and binds the ports) for start, death,
  backoff and token re-read; the B1 parity test; a timeout maps to `GitError("... timed out")`.
- **Estimate:** 1.5 days.

### B4. Remote sessions: one client per slot server

- **Files:** `adapters/__init__.py` (`build_client`), `adapters/clientset.py`, `engine.py` (the
  session start at `:1145-1155` adds the lane host to the metadata).
- **How it works:**
  - A project with lanes on slots gets `ClientSet` members `slot-4/kilo` and `slot-4/opencode`,
    URL = the tunnel's local port, password from the token read.
  - `ClientSet.start_session` picks the member by the session's lane host first, then by role as
    today. Ownership after a restart is found the way it is today: ask each server
    (`clientset.py:16-20`).
  - A tunnel restart gives new local ports: members keep their names and get the new URL, so the
    stored session ids stay valid.
- **Tests:** a scripted server behind a fake tunnel. A session started on `slot-4/kilo` keeps going
  to it after a port change and after a coordinator restart. A lane without a host still goes to
  the local member (today's behaviour).
- **Estimate:** 1.5 days.

### B5. Router: slot model keys at enrollment

- **Files:** `foundry_router/machines.py` (`enroll`, `:914-960`, and the Add-a-slot form), the
  Machines UI, the config POST allow-list (memory note *config-post-allowlist*).
- **The keys:**
  - "Lanes on this slot" (a new checkbox, off) makes enrollment also create `<slot>-kilo`
    (`inference`, `media`) and `<slot>-opencode` (`inference`). It follows the workstation's
    `WS_KEYS` pattern (`machines.py:49-51`).
  - Each key gets the same persona allow-list as the existing `kilo` / `opencode` keys, copied at
    enrollment and shown on Clients & Keys.
  - The keys go into the slot's secrets bundle (names `FOUNDRY_KEY_KILO`,
    `FOUNDRY_KEY_OPENCODE`), so `slot-secrets` puts them in tmpfs. They are never shown.
  - **Revoke** = disable the key; the next pull deletes the files (`slot-secrets.py:24`).
  - For a slot that is already enrolled, a **Lanes: on** action makes the keys without a
    re-enroll.
- **Tests:** enrollment with and without the box; persona lists copied; the bundle names; a disabled
  key removes the files.
- **Estimate:** 1 day. **Victor approves the keys** (P2).

### B6. The pilot path: a worktree on the slot, its sessions, fetch back

- **Files:** `tracks/manager.py` (create/stop for a lane whose slot has `lanes = true` and whose
  project has `[automation] remote_lanes = true`); `engine.py` read sites (finding 4);
  `adapters/project.py` + `adapters/verify_cmd.py` (verify as an agent job); a new
  `foundry_coordinator/lane_fetch.py`.
- **Flow:**
  1. **Create:** push the base to the lanes store as `refs/heads/slot/_base/<lane>` (the
     `slotkeep.keep_fresh` push, `slotkeep.py:128-149`), then the `worktree-add` job, then the
     agent config written through `/api/lane/write` (`services.track_kilo_config`,
     `services.py:353`). The track row records `host` and the slot path.
  2. **Fetch:** after every commit the engine records, at stop and before the end of the sprint:
     `git fetch` into `refs/lanes/<slot>/<lane>`, then the local `track/<lane>` is moved
     **fast-forward only**. A non-ff tip stops the lane with a decision and keeps both tips.
  3. **Stop:** fetch, save the dirty files (`slotkeep.save_dirty`, `slotkeep.py:334-355`), then
     `worktree-remove` only when the junction holds the tip.
  4. **No merge into a real master in b.** The pilot project's branch is a scratch repo.
- **Tests:** the whole flow against a local agent on temp repos (the agent's "host" is this
  machine); a non-ff tip; a stop with dirty files; a stop while the tunnel is down (it waits and
  says why, never removes).
- **Estimate:** 2 days.

### B7. Training records from remote sessions

- **Files:** `foundry_client/lane.py` (export), the coordinator's training hook, the
  kilo/opencode collectors in `foundry_training`.
- **How:** every 10 min, and at each session's close, the coordinator pulls
  `/api/lane/sessions/export?since=<cursor>` per slot and feeds it to the same collector the local
  kilo/opencode records go through, tagged with the slot. Cursors are kept per slot. A slot that is
  away catches up when it is back. Nothing is dropped (*training-data-keep-everything*).
- **Tests:** the cursor resumes; a duplicate is ignored; a session from a slot shows up in the
  training store with its slot.
- **Estimate:** 1 day.

### B8. Settings, board and control center

- **Files:** `foundry_automation/schema.py` + `automation.html` (memory note
  *project-settings-ui-is-schema-driven*); the board/status views; `control_center` session rows.
- **Settings**, each with a UI control and on the POST allow-list (memory notes
  *everything-configurable-in-ui*, *config-post-allowlist*):
  - `[slots.<id>] lanes` (off), `os` (`linux`), `studio` (`container`), `agent_port`
    (47811);
  - `[automation] remote_lanes` (off).
- **Views:** a host column on sessions and tracks (`local` / `slot-4`) and a tunnel pill per slot.
  The same transcript and heartbeat views work for remote sessions, since they read the store.
- **Estimate:** 1 day.

### B9. The pilot on the spare Linux slot (b1)

- A scratch project `distributed-pilot` (a small git repo with a generic `[[verify.checks]]`
  command and two one-file sprints).
- It is run by a **separate pilot coordinator** on the workstation, as the OpenCode pilot was. It
  has its own state, and its SlotSsh registration is a no-op, so it never replaces the live
  coordinator's slot key (memory note *opencode-backend-plan*: that happened once). It uses the
  live key file read-only, through `slot_ssh.ssh_options`.
- Runs:
  1. sprint 1 on Kilo, sprint 2 on OpenCode, both remote → COMPLETE with commits fetched;
  2. stop the tunnel mid-session (kill the ssh) → it comes back and the session goes on;
  3. stop the slot's agent mid-session → the agent restarts kilo/opencode and the dead-session
     rule reruns the step; no commit lost;
  4. reboot the spare slot mid-sprint → the lane shows `slot away`, a decision appears after
     `slot_away_minutes`, nothing is removed;
  5. training records of both sessions appear in the store with `slot-4`.
- **Estimate:** 1 day of running and fixing.

### B10. The Windows agent (b2, after b1 passes)

- **Files:** new `install/slot.ps1`; the platform layer for process and service details.
- **`install/slot.ps1`** (idempotent, ends with `doctor --platform` and `doctor --lane`):
  - Git for Windows, Node 22, Python 3.12, Kilo + OpenCode (the Dockerfile's pins);
  - the OpenSSH server, with the workstation's key from the router, as `setup-host.sh` does;
  - a scheduled task for the client (`autostart.py:8-11`);
  - `C:\Foundry\lanes` for the lane store;
  - enrollment with the same code flow.
- **Listening:** the agent listens on 127.0.0.1 directly (no Docker), and the tunnel targets it.
- **Run:** the B9 runs 1-3 on the Windows VM.
- **Estimate:** 2 days.

## Releases

| Release | Contents | Live effect |
|---|---|---|
| b-1 | B1 + B3 + B4 (agent routes, tunnel, RemoteWorkspace, slot members), all behind `lanes` | none |
| b-2 | B5 (router keys at enrollment, box off) | none until a box is ticked |
| b-3 | B2 + B6 + B7 + B8 (image, pilot path, training, settings/views) | none: `remote_lanes` and `lanes` off |
| b-4 | fixes from the B9 pilot | none |
| b-5 | B10 (Windows) + fixes from its runs | none |

The gate runs in parts for each release (memory note *gate-split-40-min-cap*). The image is built
only on the spare slot. No deploy is part of phase b. Victor deploys a tag only if he wants the
board's new columns live; every switch stays off.

**Estimate:** about 14 working days (Linux 12, Windows 2). The overall plan said 6-8 days. The
difference is findings 1-4: the writable lane store, training capture, the early file reads, and
the pilot path that phase d would otherwise have built first.

## What Victor does

- **P1. A spare Linux slot**, `slot-4`, with its own private place, before release b-3 is tried:
  1. router → Access → Machines → Add a Studio slot (it gives the code);
  2. `bash create-slot-lxc.sh` on Proxmox, or a VM + `setup-host.sh`;
  3. sign Studio in once (phase b does not use Studio, but the installer expects it).
- **P2.** Tick "Lanes on this slot" for slot-4 only, which approves its two keys.
- **P3.** For b2: a Windows VM (D6) and its Studio sign-in.

## Decisions for Victor

- **Db1. Token delivery over ssh** (finding 5). *Recommended:* nothing new in the router.
- **Db2. Ports published on the slot host's 127.0.0.1** (finding 1), like noVNC. *Recommended.*
  The alternative, `network_mode: host`, also exposes the container's other ports to the host.
- **Db3. A separate pilot coordinator** for the scratch project, not the live one. *Recommended:*
  the live coordinator needs no new release to run the pilot, and no live setting changes.
- **Db4. The coordinator pulls training records from the slots** (finding 3). *Recommended:* no
  orchestration-scope key on a slot.
- **Db5. Windows after the Linux pilot passes** (b-5). *Recommended:* one new thing at a time.

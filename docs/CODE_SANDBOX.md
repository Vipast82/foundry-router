# Code execution sandbox

Foundry Router can route code-execution requests to a **Code Sandbox MCP**
server (Philipp Schmid's, MIT, built on `llm-sandbox`) exactly like any other
MCP server. It is the only tool in the stack that runs arbitrary code, so it is
the highest-risk one — treat everything here as security-relevant.

Do **not** use `mcp-run-python` (Pyodide): it has a disclosed sandbox-escape
with no real isolation between the Python runtime and the host JS bridge.

## Where it runs

On `general-ai` (`192.168.0.114`), alongside `crawl4ai` and `searxng-mcp` —
the host that already carries MCP workloads. **Never** co-locate it with
`victor-ai`, which runs Meridian's live Claude OAuth session: arbitrary code
execution and live cloud credentials must not share a host.

## What Foundry enforces vs. what the container enforces

This split is the whole security story — read it before enabling anything.

| Concern | Enforced by | How |
|---|---|---|
| Wall-clock timeout | **Foundry** | `timeout_seconds` on the server entry; `asyncio.wait_for` abandons the call and records a timeout. |
| `network` off, resource args | **Foundry (policy) + container (reality)** | `call_defaults` are force-merged *over* the model's arguments, so a model cannot flip `network` on. The actual block is the container's `--network none`. |
| Fork-bomb / infinite-CPU kill | **Container** | Docker/Podman cgroup `--cpus`, plus the sandbox's own per-exec timeout. |
| Memory cap | **Container** | `--memory`. |
| Filesystem isolation (no host FS outside an ephemeral per-exec workdir) | **Container** | image + mount config; no persistent state between calls unless configured. |

Foundry **cannot** contain code it does not run. The GUI fields
(CPU / memory / network) are the operator's control surface and are passed to
the sandbox as call defaults; the **hard** limits must be set on the sandbox
container itself. Keep the two aligned. Changing a container-level limit may
need a sandbox container restart — that's expected and fine.

## Registering it

Add it on the **MCP tab** (or in `config.yaml`, see `config.example.yaml`):

- `url` → the sandbox on `general-ai`, e.g. `http://192.168.0.114:8975/mcp`
- check **⚠ executes code** → reveals the sandbox controls, turns on the
  full-code audit trail, and shows the ⚠ badge everywhere
- set **timeout**, **CPU**, **memory**, and leave **network access** **off**
- **extra call_defaults (JSON)**: match the exact argument names *your* sandbox
  build expects — nothing is hardcoded in Foundry. The CPU/memory/network
  steppers write `cpus` / `memory_mb` / `network`; override or add keys here.

Enable it **per persona** with the persona editor's existing "preferred MCP
servers" checkbox. It is **off for every persona** until you check it, and any
persona that has it on is flagged **⚠ code exec** in the persona list.

## Audit trail

Every execution lands in `tool_call_log` with the **actual submitted code**
captured (`arguments`, `executed_code=1`). View it under **Usage Log → ⚠
Code-execution audit**. A model that tries to widen its own sandbox (e.g.
request `network: true` when policy forces it off) is recorded as a `warning`
in the Events log.

## Containment verification — run these against the LIVE sandbox

The acceptance bar is *verified, not assumed*. These cannot be unit-tested in
the Foundry repo (they exercise the container on `general-ai`), so run each one
against the deployed sandbox after wiring it up and confirm the expected
outcome. Send them through a persona that has the sandbox enabled.

1. **Runaway / infinite loop** — must be killed, not hang forever:
   ```python
   while True:
       pass
   ```
   Expect: the call ends around `timeout_seconds` with a timeout error in the
   audit row; the container process is gone (verify with `docker ps` /
   `docker stats` on `general-ai` — no lingering pegged container).

2. **Workdir / host-filesystem escape** — must be blocked:
   ```python
   import os
   print(os.listdir("/"))
   open("/etc/hostname").read()
   open("/host/etc/shadow").read()   # or any known host path
   ```
   Expect: only the ephemeral container root is visible; host paths are absent
   or permission-denied. Nothing from the Foundry/host filesystem is readable.

3. **Network reach with the toggle OFF** — must fail:
   ```python
   import urllib.request
   urllib.request.urlopen("https://example.com", timeout=5).read()
   ```
   Expect: a network/DNS error (no route / name resolution fails), not a
   200. Then confirm the model can't self-escalate: have it call the tool with
   `network: true` in its arguments and verify the Events log shows Foundry
   forced it back off and the call still had no network.

4. **No persistent state between calls** — write a file in one call, read it in
   the next; the second call must not see it (fresh ephemeral workdir per exec).

If any of these does **not** behave as described, the container isolation is
misconfigured — fix it on `general-ai` before leaving the sandbox enabled on any
persona.

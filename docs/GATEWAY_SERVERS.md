# MCP > Gateway Servers

Browse the Docker MCP Catalog and attach/detach servers on a live **Docker MCP
Gateway** from Foundry Router's admin UI — no SSH to `general-ai` required.

## What it is

Foundry Router already holds an MCP connection to the gateway. The gateway
injects its own management tools (`mcp-find`, `mcp-add`, `mcp-remove`, …) into
that connection when `dynamic-tools` is on. This panel lets the **operator**
drive those tools from Foundry's backend as a control panel.

The panel appears on the **MCP** tab whenever a connection is detected that
exposes the gateway management tools — detection is by tool presence
(`mcp-find` / `mcp-add`), never by a hardcoded connection name, so it works for
any gateway connection you configure.

## What it does

- **Search catalog** → runs `mcp-find`, lists servers (name, description, tool
  count, whether it requires secrets).
- **Add** a secret-free server → runs `mcp-add`, then re-runs Tool Sync and
  reports how many tools appeared.
- **Remove** a server → runs `mcp-remove` (with a confirm — removal is
  disruptive), then re-runs Tool Sync.
- **Currently attached** → derived from Tool Sync's view of the gateway
  connection's downstream tools, grouped by namespace when the gateway
  namespaces them. A manual *remove by ref* box covers anything grouping can't
  name.

Every action is a **backend-initiated** MCP call from these operator-only
routes (`/admin/api/gateway/*`). It is **never** routed through a persona or a
model tool-loop — same trust tier as the "add MCP connection" form.

## Security

`mcp-add` / `mcp-remove` / `mcp-create-profile` / `mcp-activate-profile` /
`mcp-config-set` are **root-level over the whole gateway** — whoever can add an
arbitrary catalog (or private) server effectively chooses what code runs in a
container on `general-ai`. They are therefore:

- available **only** on this operator page, and
- **excluded from every persona's grantable tool set** — they never appear as a
  checkbox in the persona editor and are filtered out of persona tool
  resolution in the backend, even under a whole-server grant. This holds
  regardless of this page.

## Secrets — out of scope (needs a companion service)

Servers that require secrets (GitHub PAT, Proxmox token, Obsidian/Grafana keys,
…) show **Add disabled** with a tooltip. The gateway's secrets live in a
`chmod 600 ~/.docker/mcp/secrets.env` on the host, read directly by the gateway
process — there is **no MCP tool or API** to write it remotely (Docker
Desktop's secrets engine isn't present on headless Linux).

Closing this gap needs a small **companion HTTP service** on `general-ai`
(bound to localhost or firewalled to Foundry Router's host) that accepts
`{server, key, value}`, updates the matching line in `secrets.env`, and
restarts `mcp-gateway.service`. That is its own small project — not something
the gateway ships. Until it exists, credentialed servers stay a manual
SSH + `secrets.env` edit + `docker mcp profile server add` + service restart.

## Note on gateway response shapes

The gateway's tool argument names and `mcp-find` result shape are the Gateway's,
not Foundry's. Argument names are read from each tool's **discovered input
schema** (not hardcoded), and the catalog parser is tolerant of shape.

Each result row exposes **publisher**, **tools** (count), **config** (non-secret
required config, shown separately from secrets), and **secrets**. The parser
reads these across many likely field names and derives publisher from a
namespaced ref when needed. Every row has an inline **raw** disclosure, and the
whole response has a **show raw mcp-find response** toggle — the untouched
payload is always one click away, so you can confirm which fields the gateway
actually returns before the parser is tuned.

If a column shows `?` (tools) or `—` (config unknown), it means `mcp-find`
didn't include that field for that server. `mcp-find` is a catalog *search* and
may return a summary only; the richer per-server data comes from
`docker mcp catalog server inspect <catalog> <server>` on the host. That is a
host CLI call (no MCP tool exposes it), so wiring it into a per-row "details"
fetch needs the same host-access path as the secrets companion service above —
tracked as a follow-up, not part of the pure-MCP-tool scope.

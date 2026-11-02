---
name: roblox-open-cloud
description: Use Roblox Open Cloud and HttpService from outside or inside a game - API keys and OAuth 2.0, v2 REST patterns, asset upload, DataStore and MemoryStore access, publishing places, messaging, webhooks, and secrets. Load for automation, bots, CI publishing or any external HTTP integration.
---

# Open Cloud and HTTP

## Credentials
- API key for owner automation (CI, bots, webhook workers), scoped to the exact resources and operations; OAuth 2.0 (authorization code + PKCE) when a third-party app acts for a user.
- Keys and tokens never appear in replicated code, logs, chat, commits or agent prompts. Automation reads a key only inside the one tool that uses it (from a protected file or secret store); agents never open, print or copy it.
- In-experience HTTP uses Roblox Secrets (`HttpService:GetSecret`) for keys, HTTPS only.

## REST mechanics
- Most resources are under `https://apis.roblox.com/cloud/v2/...`; confirm each endpoint (some remain v1).
- Paging: send `nextPageToken` back unchanged as `pageToken`. Updates: `updateMask` lists only the fields you change.
- Long operations return an Operation: poll with bounded backoff.
- 429 / `RESOURCE_EXHAUSTED` are quota signals: honour `Retry-After`; retry only transient failures.

## Common jobs
- Asset upload (images, audio, models): upload, poll the operation, record the returned asset id with its source file; then grant the experience access if the asset is restricted.
- Place publishing from CI; DataStore/MemoryStore reads for tools; `publishMessage` to running servers.
- Webhooks: verify the signature, reject stale deliveries, de-duplicate ids, answer 2xx fast, process asynchronously.

## OAuth essentials
Exact redirect URLs and minimum scopes; fresh `state` and PKCE per attempt; verify `state` before the code exchange; refresh through a trusted backend and replace rotated refresh tokens atomically; revoke on disconnect.

## References
- `references/open-cloud.md` - auth, REST, OAuth, webhooks, failure handling (roblox-brain).
- `references/open-cloud-quick.md`, `open-cloud-webhooks.md` - v2 patterns, data stores from outside, publishing, webhooks (EL4CTEO).

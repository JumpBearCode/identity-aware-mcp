# Identity-Aware Azure DataOps MCP

An MCP server that lets an AI agent run Azure DataOps shell commands **as the real
human behind it** — not as a shared robot account. The server verifies the caller's
Entra JWT, checks their AD-group membership via OBO, and routes two tools —
`diagnose_bash` (read-only) and `action_bash` (write) — to an execution backend that
logs in as a **per-group Service Principal**.

The one idea the whole project is built on: **identity is the boundary, not code.**
The MCP server holds *zero* Azure data-plane permission. What a call can touch is
decided entirely by which SP its group maps to (`diagnose` = Reader, `action` =
Contributor). Break out of the sandbox, rewrite the command, jailbreak the agent —
you still only have the identity Entra handed you.

```
   VS Code ───────────────► /mcp        ┐
   Claude Code / opencode ─► /mcpproxy   │  (proxy strips the RFC 8707 `resource`
   Codex ─────────────────► /mcpproxy    │   param Entra v2 rejects — same JWT)
                                         ▼
                        ┌───────────────────────────────────────┐
                        │  mcp-server (FastMCP)                  │
                        │   • validate Entra JWT                 │
                        │   • OBO → AD-group check → tool gating  │
                        │   • derive Session / Conversation      │
                        │   • mask known-format secrets (action) │
                        │   • audit every call (who / IP / cmd)  │
                        └───────────────────────────────────────┘
                            │                         │
          EXECUTOR=local    │                         │   EXECUTOR=aca
                            ▼                         ▼
              diagnose-worker / action-worker   SandboxManager
              (docker, SP + client secret)      → per-Session ACA Sandbox (microVM)
                                                  FIC `az login` as worker SP,
                                                  Blob-backed workspace, stateless
                            │                         │
                            └──── audit row ──────────┴──► Log Analytics `MCPAudit_CL`
                                  + `mcp/<guid>` in User-Agent ──► native Azure logs
```

## What's in the box

Four capabilities, each landed as its own PR and written up in depth under `docs/`.
Read the folder index if you want the *why*; this README is just enough to run it.

| # | Capability | PR | Deep dive |
|---|---|---|---|
| 1 | **Two execution backends** — local docker workers, or per-Session ACA Sandbox microVMs with Redis session-routing + Blob-persisted workspaces. Both behind one `Executor` interface, picked by `EXECUTOR`. | #1 | [`docs/ACA-Redis-Implementation/`](docs/ACA-Redis-Implementation/README.md) |
| 2 | **Any Entra-capable client** — a second `/mcpproxy` endpoint strips the RFC 8707 `resource` param that trips `AADSTS9010010`, so Claude Code / opencode / Codex connect to the same Entra-protected server VS Code already uses — no DCR, no secret. | #2 | [`docs/multi-client-implementation/`](docs/multi-client-implementation/README.md) |
| 3 | **Real-human attribution** — every tool call writes one row to a Log Analytics table (`MCPAudit_CL`) with the real `oid`/`upn`/IP/command, and injects a correlation GUID into the outbound User-Agent so native Azure logs (Storage, Key Vault) `join` back to who actually did it — even for denied calls. | #3 | [`docs/oid-log-tracking/`](docs/oid-log-tracking/README.md) |
| 4 | **Action guardrail** — `action_bash` forces a human approval on every call (server `_meta` + per-client settings), and its output is scanned for known-format secrets before it reaches the client. The real boundary is identity, not this scrubber — see the note below. | #4 | [`docs/action-gate-guardrail/`](docs/action-gate-guardrail/README.md) |

## Two paths, same identity model

* **User identity** (Entra JWT) → authorization, tool visibility, audit.
* **Worker identity** (per-group SP) → the Azure execution boundary.

`EXECUTOR=local` shares one container per group across all users. `EXECUTOR=aca`
isolates execution **per User / per Session**: a Session (30-min sliding TTL) sticks
to one sandbox per group; the sandbox logs in passwordlessly via a Federated Identity
Credential, persists files to Blob, and is deleted when the Session ends — the next
user gets a fresh microVM. Full design in
[`docs/ACA-Redis-Implementation/`](docs/ACA-Redis-Implementation/README.md).

## Quickstart — Local Hosting (Docker)

```bash
# 1. Provision Entra apps, SPs, AD groups (Bicep, Microsoft.Graph extension)
cd provisioning/local
az deployment tenant create -n dataops-mcp-provision -l eastus -f main.bicep
./write-env.sh dataops-mcp-provision      # writes ../../.env (incl. EXECUTOR=local)

# 2. Add users to the AD groups (ids printed by the deployment)
# 3. Grant the worker SPs Azure RBAC at the scope you choose (none is assigned yet)
#    az role assignment create --assignee <DIAGNOSE_SP_CLIENT_ID> --role Reader      --scope <scope>
#    az role assignment create --assignee <ACTION_SP_CLIENT_ID>   --role Contributor --scope <scope>

# 4. Run the local stack (server + two workers + redis)
cd ../.. && docker compose up --build

# 5. Point VS Code at it (.vscode/mcp.json):
#    { "servers": { "azure-dataops": { "url": "http://localhost:8081/mcp" } } }
```

## Quickstart — Azure Hosting (ACA + ACA Sandbox)

Deploys the full cloud footprint and runs the MCP server as a Container App via one
`azd up`. The runbook in [`provisioning/aca/README.md`](provisioning/aca/README.md)
has the phase-by-phase detail (prereqs, roles, what each phase does); a full
walkthrough is in the azd-migration docs
([`docs/en/azd-migration/`](docs/en/azd-migration/deployment-after-azd.md) ·
中文 [`docs/zh/azd-migration/`](docs/zh/azd-migration/azd迁移后-部署说明.md)).

```bash
azd auth login && az login
azd env new dataops-mcp-aca               # pick subscription + region (e.g. westus2)
azd up                                    # provision + hooks + build/deploy the image
# then: add users to the AD groups, and point your client at
#       https://<MCP_FQDN>/mcp  (or /mcpproxy — see below)
az ad group member add --group "$(azd env get-value DIAGNOSE_GROUP_ID)" --member-id <oid>
```

### Tuning knobs — `azd env set`

Every behavior/tuning variable below is controlled through the azd environment: set it
and re-provision; leave it unset and the server uses its **in-code default** (Bicep
never re-states the default, so there's a single source of truth). Mechanically each
maps `${NAME=}` in `main.parameters.json` → a Bicep param → an `envOverrides` object →
injected into the Container App only when non-empty.

```bash
azd env set MCP_EXEC_TIMEOUT 300 && azd provision     # example: raise the tool timeout
azd env set MCP_EXEC_TIMEOUT "" && azd provision       # unset -> back to the code default
```

> Identity/wiring vars (`AZURE_TENANT_ID`, `MCP_APP_ID`, `REDIS_URL`, `STORAGE_ACCOUNT`,
> `*_GROUP_ID`, …) are provisioned outputs and must **not** be set here — doing so
> desyncs the app from the real resources. Full taxonomy in the azd-migration docs §6.

| `azd env set` name | What it controls | Default |
|---|---|---|
| `SANDBOX_DISK_IMAGE` | ACR image the sandbox microVM boots from | `<acr>/mcp-sandbox:latest` |
| `SANDBOX_DISK_ID` | Prebuilt sandbox disk resource id (overrides the image) | *(unset)* |
| `SANDBOX_DISK` | Public disk name used as a fallback when no image/id is set | `ubuntu` |
| `SANDBOX_CPU` | vCPU allocated per sandbox microVM | `1000m` |
| `SANDBOX_MEMORY` | Memory allocated per sandbox microVM | `2048Mi` |
| `SANDBOX_CREATE_TIMEOUT` | Max seconds to wait for a new sandbox to become ready | `30` |
| `SANDBOX_AUTO_SUSPEND_SECONDS` | Idle seconds before a sandbox auto-suspends | `300` |
| `SANDBOX_AUTO_DELETE_SECONDS` | Idle seconds before a sandbox is deleted | `3600` |
| `SANDBOX_REAPER_INTERVAL` | How often (s) the reaper sweeps for idle/expired sandboxes | `300` |
| `SANDBOX_REAPER_LEASE` | Reaper leader-election lease TTL (s) — only one replica reaps | `90` |
| `SANDBOX_DISTRIBUTED_LOCK` | `1`/`0` — Redis lock so only one replica creates a Session's sandbox | `0` |
| `SANDBOX_LOCK_TTL` | TTL (s) of that per-Session create lock | `60` |
| `SANDBOX_LOCK_WAIT` | Max seconds to wait to acquire the create lock before giving up | `45` |
| `MCP_SESSION_TTL` | Session sliding-window TTL (s) = how long a sandbox stays sticky | `1800` |
| `MCP_EXEC_TIMEOUT` | Seconds the server waits for a tool command before timing out | `120` |
| `MAX_OUTPUT_BYTES` | Cap on stdout/stderr bytes returned to the agent | `65536` |
| `BLOB_MOUNTPOINT` | Path inside the sandbox where the Blob workspace is mounted | `/workspace` |
| `MCPPROXY_ENABLED` | `true`/`false` — expose the `/mcpproxy` resource-stripping endpoint | `true` |
| `AUDIT_TIMEOUT` | HTTP timeout (s) for shipping an audit row to the DCR endpoint | `5` |
| `AUDIT_UA_INCLUDE_OID` | `1`/`0` — include the user `oid` in the User-Agent correlation tag | `0` |

`MCP_CLIENT_SECRET` is also an azd env var, but it's set once by the postprovision hook;
only touch it to rotate: `azd env set MCP_CLIENT_SECRET "" && azd provision`.

## Connect a client

Which endpoint a client uses depends on its OAuth behaviour — the server exposes both
on the same app (toggle the proxy with `MCPPROXY_ENABLED`, default on):

| Client | Endpoint | Why |
|---|---|---|
| **VS Code** | `/mcp` | Talks to Entra directly; doesn't send `resource`. |
| **Claude Code**, **opencode**, **Codex** | `/mcpproxy` | Follow RFC 8707 and attach `resource=`, which Entra v2 rejects (`AADSTS9010010`). `/mcpproxy` deletes that one param and forwards to Entra — the client still ends up with a **real** Entra token, verified by the same `AzureJWTVerifier`. |

The deep-dive docs are Chinese-only, so here's the English per-client setup. This repo
already ships working config for all three (`.vscode/mcp.json`, `.mcp.json`,
`opencode.json`) — the snippets below are those files with the deployment-specific bits
as placeholders. Replace:

* `<MCP_FQDN>` — your server host (local: `localhost:8081`; ACA: the app FQDN).
* `<OAUTH_CLIENT_ID>` / `<MCP_APP_ID>` — printed by provisioning. `/mcpproxy` clients
  need them; VS Code needs neither (its built-in client is pre-authorized).
* `<MCP_IDENTIFIER_URI>` — the API's Application ID URI (`api://<name>-mcp-server`),
  printed by provisioning as `MCP_IDENTIFIER_URI`. This is the scope prefix clients
  request; it must match what the server advertises (NOT the `api://<appId>` form).

**Before you connect:** sign in as a user who is a member of `mcp-diagnose-users`
(sees `diagnose_bash`) and/or `mcp-action-admins` (sees `action_bash`). Tool
visibility is group-gated by the OBO check — a non-member sees no tools.

### VS Code — `/mcp`

Put this in `.vscode/mcp.json`, then sign in when VS Code prompts:

```jsonc
{ "servers": {
    "azure-dataops-aca": { "type": "http", "url": "https://<MCP_FQDN>/mcp" }
} }
```

No `clientId`: VS Code hits `/mcp`, gets a 401 with `WWW-Authenticate`, discovers the
Entra tenant + scope, and signs in with its pre-authorized built-in client via PKCE
(no consent screen). Approval lives in `.vscode/settings.json` —
`"chat.tools.autoApprove": false` so every tool prompts; the per-tool auto-approve lock
is VS Code-version-sensitive (see the comment in that file) and should be pushed via MDM
for a fleet.

### Claude Code — `/mcpproxy`

Put this in `.mcp.json` at the project root, then run `claude` and approve the browser
sign-in:

```json
{ "mcpServers": {
    "azure-dataops-aca": {
      "type": "http",
      "url": "https://<MCP_FQDN>/mcpproxy",
      "oauth": { "clientId": "<OAUTH_CLIENT_ID>", "callbackPort": 8080 }
} } }
```

PKCE sign-in runs against `/mcpproxy`, which strips `resource` before forwarding to
Entra. Approval is enforced two ways: the server `_meta` already forces a prompt on
`action_bash`, and `.claude/settings.json` adds a belt-and-suspenders rule (`allow` on
`mcp__azure-dataops-aca__diagnose_bash`, `ask` on `..._action_bash`).

### opencode — `/mcpproxy`

Put this in `opencode.json`:

```json
{ "mcp": {
    "azure-dataops-aca": {
      "type": "remote",
      "url": "https://<MCP_FQDN>/mcpproxy",
      "enabled": true,
      "oauth": {
        "clientId": "<OAUTH_CLIENT_ID>",
        "scope": "<MCP_IDENTIFIER_URI>/user_impersonation"
      }
    }
  },
  "permission": {
    "azure-dataops-aca_diagnose_bash": "allow",
    "azure-dataops-aca_action_bash": "ask"
} }
```

This is the weakest link for approval: opencode ignores the server `_meta` and has no
org lock, so the `permission` block is the *only* gate — confirm the tool id matches
your build (`<server-name>_action_bash`).

### Codex — `/mcpproxy`

Same `/mcpproxy` pattern (Codex is an RFC 8707 client); this repo doesn't ship a Codex
config, but point it at `https://<MCP_FQDN>/mcpproxy` with the same `clientId` / `scope`.

Background (Chinese): [`docs/multi-client-implementation/`](docs/multi-client-implementation/README.md).

## Action guardrail

`action_bash` writes to Azure, so it carries two extra controls (PR #4, merged). The
important framing first: **identity is the boundary.** `diagnose` runs as Reader with
zero data-plane, so it can't even read a secret; any privileged read goes through the
elevated `action` SP *plus* a human approval. Everything below is defense-in-depth on
top of that, not a substitute for it.

* **Forced human approval** — `action_bash` is tagged
  `_meta["anthropic/requiresUserInteraction"]=true` in `main.py`, and each client
  ships an approval setting (`.claude/settings.json`, `.vscode/settings.json`,
  `opencode.json`). Claude Code honors the server `_meta` and forces approval even if
  a `--permission-prompt-tool` says "allow". For fleet lockdown (so an operator can't
  turn it off), see [`docs/action-gate-guardrail/` #2](docs/action-gate-guardrail/README.md).

* **Output redaction** — `redact.py` masks known-format secrets (JWTs, PEM keys, SAS
  sigs, storage keys, connection strings, Azure function/host keys + `?code=` URLs,
  Entra client secrets, and common AWS/GitHub/Slack/GCP/OpenAI token shapes) before
  output leaves the server. Two deliberate scoping decisions, both from the teardown
  in [`docs/action-gate-guardrail/` #4](docs/action-gate-guardrail/README.md): it runs
  **on `action_bash` only** (diagnose has nothing to mask), and it keeps **only the
  known-format regex layer** — the earlier JSON-field and entropy layers were dropped
  because `jq` field-renaming and non-pure-JSON output evade them. It's a
  gitleaks-style hygiene net, not a boundary.

## Layout

| Path | What |
|---|---|
| `src/mcp-server/` | FastMCP server, `Executor` abstraction, `SandboxManager`, `/mcpproxy`, Redis caches, `audit.py`, `redact.py`, session/blob helpers |
| `src/worker/` | local docker bash worker (FastAPI), shared by both groups |
| `src/sandbox-image/` | ACA sandbox disk image + FIC bootstrap |
| `provisioning/local/` | Entra-only Bicep for the local path |
| `provisioning/aca/` | full cloud Bicep (sandbox groups, storage, redis, MCP app, RBAC, FIC, audit table + DCR) |
| `docs/` | four deep-dive folders — one per capability above, each with its own README index |

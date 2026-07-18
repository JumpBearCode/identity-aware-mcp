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

## Quickstart — local docker

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

## Quickstart — ACA sandboxes (cloud)

Deploys the full cloud footprint and runs the MCP server as a Container App. The
runbook in [`provisioning/aca/README.md`](provisioning/aca/README.md) has the whole
step-by-step (image build/push, secret injection, RBAC propagation, the 6 param
traps). There's also a convergence runbook in the attribution docs
([`docs/oid-log-tracking/` #5](docs/oid-log-tracking/README.md)).

```bash
cd provisioning/aca
az deployment sub create -n dataops-mcp-aca -l westus2 -f main.bicep
./write-env.sh dataops-mcp-aca            # writes ../../.env.aca
# then: build/push the MCP + sandbox images to ACR, set the MCP OBO client secret,
#       point your client at https://<MCP_FQDN>/mcp  (or /mcpproxy — see below)
```

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
        "scope": "api://<MCP_APP_ID>/user_impersonation"
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

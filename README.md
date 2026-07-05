# Identity-aware Azure DataOps MCP

An identity-aware MCP server for Azure DataOps. The MCP server verifies the
caller's Entra JWT, checks group membership via OBO, and routes `diagnose_bash`
(read-only) / `action_bash` (write) to an **execution backend**. Two backends
sit behind one `Executor` interface (`src/mcp-server/executor.py`):

| Backend | `EXECUTOR` | Execution substrate | Worker identity |
|---|---|---|---|
| **Local** | `local` | two long-running docker workers (shared `bash-worker` image) | fixed SP + client secret |
| **ACA** | `aca` | per-Session **Azure Container Apps Sandboxes** (microVMs) | passwordless **Federated Identity Credential** → SP |

The MCP server itself holds **no Azure data-plane permissions** in either path —
identity is the boundary, not code.

```
VS Code / Claude Code
   │  Entra OAuth (PKCE) + JWT
   ▼
mcp-server  ── JWT validate · OBO group check · derive Session/Conversation · route
   │
   ├─ EXECUTOR=local ──► diagnose-worker / action-worker  (SP + secret, az cli)
   │
   └─ EXECUTOR=aca   ──► SandboxManager
                           │  Session-sticky route (Redis: oid+session+group)
                           ▼
                         ACA Sandbox (microVM)  ── FIC `az login` as worker SP,
                           per-Session, stateless, Blob-backed workspace
```

## Two paths, same identity model

* **User identity** (Entra JWT) → authorization, audit, tool visibility.
* **Worker identity** (per-group SP) → the Azure execution boundary
  (diagnose = Reader, action = Contributor).

The local path shares one container per group across all users. The ACA path
isolates execution **per User / per Session**: a Session (30-min sliding TTL)
sticks to one sandbox per group; the sandbox logs in passwordlessly via a
Federated Identity Credential, persists files to Blob, and is deleted when the
Session ends — the next user gets a fresh microVM. See
[`docs/ACA-Sandbox-迁移方案.md`](docs/ACA-Sandbox-迁移方案.md).

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

# 4. Run the local stack (now includes redis)
cd ../.. && docker compose up --build

# 5. Point VS Code at it (.vscode/mcp.json):
#    { "servers": { "azure-dataops": { "url": "http://localhost:8081/mcp" } } }
```

## Quickstart — ACA sandboxes (cloud)

Deploys the full cloud footprint and runs the MCP server as a Container App.
See [`provisioning/aca/README.md`](provisioning/aca/README.md) for the complete
step-by-step (image build/push, secret injection, RBAC propagation).

```bash
cd provisioning/aca
az deployment sub create -n dataops-mcp-aca -l westus2 -f main.bicep
./write-env.sh dataops-mcp-aca            # writes ../../.env.aca
# then: build/push the MCP + sandbox images to ACR, set the MCP client secret,
#       point your client at https://<MCP_FQDN>/mcp
```

## Layout

| Path | What |
|---|---|
| `src/mcp-server/` | FastMCP server, `Executor` abstraction, `SandboxManager`, Redis caches, session/blob helpers |
| `src/worker/` | local docker bash worker (FastAPI), shared by both groups |
| `src/sandbox-image/` | ACA sandbox disk image + FIC bootstrap |
| `provisioning/local/` | Entra-only Bicep for the local path |
| `provisioning/aca/` | full cloud Bicep (sandbox groups, storage, redis, MCP app, RBAC, FIC) |

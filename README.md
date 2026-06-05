# Identity-aware Azure DataOps MCP (local Docker)

A minimal reference implementation of the architecture described in `docs/mcp_discussion.md`.

Three containers:

| Container | Identity | Permissions |
|---|---|---|
| `mcp-server` | Entra App (delegated `user_impersonation` scope), client secret for OBO | None on Azure data plane |
| `diagnose-worker` | `diagnose-sp` Service Principal | Read-only RBAC (Reader) |
| `action-worker` | `action-sp` Service Principal | Scoped write RBAC |

```
VS Code / Claude Code
   │  Entra OAuth (PKCE)
   ▼
http://localhost:8080  ── mcp-server (JWT validate + OBO group lookup + route)
                              ├──► diagnose-worker  (read-only az cli)
                              └──► action-worker    (write az cli, bounded by SP RBAC; HITL by client)
```

## Quickstart

```bash
# 1. Provision Entra apps, SPs, AD groups (also grants OBO admin consent)
cd provisioning/python
uv sync
uv run python provision.py     # writes ../../.env

# 2. Add yourself / users to AD groups (see provisioning output for group IDs)

# 3. Grant the worker SPs Azure RBAC (NOT done by provisioning — pick your scope)
#    az role assignment create --assignee <DIAGNOSE_SP_CLIENT_ID> --role Reader      --scope <scope>
#    az role assignment create --assignee <ACTION_SP_CLIENT_ID>   --role Contributor --scope <scope>

# 4. Run local stack
cd ../..
docker compose up --build

# 5. Wire VS Code (.vscode/mcp.json):
#    { "servers": { "azure-dataops": { "url": "http://localhost:8080/mcp" } } }
```

Full step-by-step for the Python route: [`provisioning/python/README.md`](provisioning/python/README.md).

## Identity model (recap)

* **User identity** (Entra JWT) → authorization, audit, tool visibility
* **Worker identity** (fixed SP) → Azure execution boundary

The MCP server has **no Azure data-plane permissions**. It only verifies JWTs and routes commands. Workers each carry their own SP credential and run `az cli` inside their container.

## Two provisioning routes

* **`provisioning/python/`** — `msgraph-sdk`. Recommended for first-time setup; easier to debug, prints all IDs/secrets. Worker RBAC is assigned by you afterwards (see its README).
* **`provisioning/bicep/`** — uses the `Microsoft.Graph` Bicep extension (public preview). Declarative, idempotent. Secret rotation is harder.

Pick one. Both produce the same `.env` consumed by `docker-compose.yml`.

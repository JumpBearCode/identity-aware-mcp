# ACA provisioning (cloud path, `EXECUTOR=aca`)

Stands up the whole cloud footprint for the Azure Container Apps Sandboxes path
and runs the MCP server as a Container App. One subscription-scoped Bicep
deployment fans out to modules; principal ids flow between them.

## What gets created

| Module | Resources |
|---|---|
| `identity.bicep` (Graph) | MCP server app (+ OBO admin consent), 2 AD groups, `diagnose-sp` / `action-sp` apps |
| `sandbox-groups.bicep` | 2 × `Microsoft.App/sandboxGroups` with SystemAssigned MI |
| `storage.bicep` | Storage account + `mcp-workspaces` blob container |
| `registry.bicep` | ACR (MCP image + sandbox image) |
| `environment.bicep` | Log Analytics + Container Apps environment |
| `redis.bicep` | self-hosted `redis:7-alpine` Container App (internal TCP, `minReplicas=1`) |
| `mcp-app.bicep` | MCP server Container App, SystemAssigned MI, full env |
| `rbac.bicep` | MCP MI → SandboxGroup Data Owner (×2) + Blob Data Contributor + AcrPull; group MIs → Blob Data Contributor + AcrPull |
| `worker-rbac.bicep` | diagnose-sp → Reader, action-sp → Contributor (subscription scope) |
| `fic.bicep` (Graph) | worker SP federated credentials trusting their sandbox-group MI |

## Identity & the passwordless chain

```
sandbox group (SystemAssigned MI)  ──seen inside the microVM (inception)──►
  ManagedIdentityCredential() token  (aud api://AzureADTokenExchange)
     │  az login --service-principal --federated-token
     ▼
  worker SP (diagnose-sp / action-sp)   ← FIC trusts the group MI (subject = MI object id)
     │  Reader / Contributor RBAC
     ▼
  az commands run as the worker SP — no secret anywhere in the cloud
```

The MCP app's own MI drives the ACA data plane (**SandboxGroup Data Owner** =
`c24cf47c-5077-412d-a19c-45202126392c`) and reads/writes the workspace blobs.

## Deploy (`azd up`)

Prereqs: [`azd`](https://aka.ms/azd-install) + Azure CLI ≥ 2.62. `docker` is **not**
needed — images build in ACR. Sign in with **both** `azd auth login` and `az login`
(the postprovision hook uses `az`), as a principal with subscription **Owner** /
**User Access Administrator** (role assignments) + Entra **Application/Group
Administrator** and **Privileged Role Administrator** (OBO consent + FIC). The region
must support `Microsoft.App/sandboxGroups` (e.g. `westus2`, `eastus2`).

```bash
azd auth login && az login

# 1. Create an azd environment (prompts for subscription + region).
azd env new dataops-mcp-aca

# 2. One command: provision (Bicep) + postprovision hook + build & deploy the image.
azd up
```

`azd up` runs, in order:

| Phase | What |
|---|---|
| **provision** (`main.bicep`) | RG, identity, sandbox groups, storage, ACR, env, redis, the MCP Container App (on a public placeholder image), RBAC, FIC. Outputs are captured into the azd env (`.azure/<env>/.env`) — the single source of truth (no `.env.aca`). |
| **postprovision** (`hooks/postprovision.sh`) | `az acr build` the sandbox image into ACR; reset + inject the MCP OBO client secret **once** (guarded — not on every deploy); print the group-membership reminder. |
| **deploy** (azd) | build `src/mcp-server` in ACR (`remoteBuild`) and swap it into the Container App, located via its `azd-service-name: mcp` tag. |

Then the one manual step — authorize users (per-user, intentionally not automated):

```bash
az ad group member add --group "$(azd env get-value DIAGNOSE_GROUP_ID)" --member-id <user-object-id>
az ad group member add --group "$(azd env get-value ACTION_GROUP_ID)"   --member-id <user-object-id>
echo "Point your MCP client at: https://$(azd env get-value MCP_FQDN)/mcp"
```

Iterate: `azd deploy` for code changes, `azd provision` for infra, `azd up` for both.
Rotate the OBO secret deliberately: `azd env set MCP_CLIENT_SECRET "" && azd provision`.
Full walkthrough (local vs ACA, the deploy steps, and how to wire a behavior param to
azd): [`../../docs/en/azd-migration/deployment-after-azd.md`](../../docs/en/azd-migration/deployment-after-azd.md)
· 中文 [`../../docs/zh/azd-migration/azd迁移后-部署说明.md`](../../docs/zh/azd-migration/azd迁移后-部署说明.md).

## Container env vars — and which are azd-settable

`mcp-app.bicep` injects **23 fixed** env vars into the MCP container, **plus whatever
tuning knobs you `azd env set`** (spliced in via `optionalEnv`). The server reads only
`os.environ`. Grouped by whether you can/should change one with `azd env set`:

| Group | env var | Source | azd-settable? |
|---|---|---|---|
| **① Don't touch — provision output / identity / wiring** | `EXECUTOR`, `MCP_SERVER_BASE_URL`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACA_RESOURCE_GROUP`, `ACA_REGION`, `MCP_APP_ID`, `MCP_IDENTIFIER_URI`, `DIAGNOSE_GROUP_ID`, `ACTION_GROUP_ID`, `DIAGNOSE_SANDBOX_GROUP`, `ACTION_SANDBOX_GROUP`, `DIAGNOSE_SP_APP_ID`, `ACTION_SP_APP_ID`, `REDIS_URL`, `STORAGE_ACCOUNT`, `BLOB_CONTAINER`, `BLOB_CONTAINER_RESOURCE_ID`, `AUDIT_DCR_ENDPOINT`, `AUDIT_DCR_RULE_ID`, `AUDIT_STREAM_NAME` | hardcoded (`'aca'`) / derived var / Azure context (`tenant()`, `subscription()`) / module `output`s | ❌ No — provisioned resource attributes & identity |
| **② Secret** | `MCP_CLIENT_SECRET` | `parameters.json` `${MCP_CLIENT_SECRET}`, set by the postprovision hook | ✅ Yes; normally only to rotate |
| **③ Behavior / tuning knobs — all `azd env set`, unset → code default** | `SANDBOX_DISK_IMAGE`, `SANDBOX_DISTRIBUTED_LOCK`, `MCP_EXEC_TIMEOUT`, `MAX_OUTPUT_BYTES`, `MCP_SESSION_TTL`, `MCPPROXY_ENABLED`, `SANDBOX_AUTO_SUSPEND_SECONDS`, `SANDBOX_AUTO_DELETE_SECONDS`, `SANDBOX_REAPER_INTERVAL`, `SANDBOX_REAPER_LEASE`, `SANDBOX_LOCK_TTL`, `SANDBOX_LOCK_WAIT`, `SANDBOX_CREATE_TIMEOUT`, `SANDBOX_CPU`, `SANDBOX_MEMORY`, `SANDBOX_DISK`, `SANDBOX_DISK_ID`, `BLOB_MOUNTPOINT`, `AUDIT_TIMEOUT`, `AUDIT_UA_INCLUDE_OID` | `parameters.json` `${…=}` → main.bicep param → `envOverrides` → empty = not injected (code default) | ✅ **all wired** — `azd env set <NAME> <value> && azd provision` |

Group ③ (20 knobs) is wired via `envOverrides` → `optionalEnv`; unset ones fall back to
the app's `os.environ.get` default (single source of truth = the code). Mechanism &
how to add a new knob: [deployment-after-azd.md](../../docs/en/azd-migration/deployment-after-azd.md)
§5.3–6 (中文 [azd迁移后-部署说明.md](../../docs/zh/azd-migration/azd迁移后-部署说明.md)).
`SANDBOX_DISK_IMAGE` unset uses the deterministic `<acr>/mcp-sandbox:latest`, not "no
injection". `DIAGNOSE_WORKER_URL` / `ACTION_WORKER_URL` are local-docker only.

## Notes / open items

- **FIC issuer/subject** (`fic.bicep`): trust uses
  `issuer = https://login.microsoftonline.com/{tenant}/v2.0`,
  `subject = <group MI object id>`, `audience = api://AzureADTokenExchange`
  ("configure an app to trust a managed identity", preview). If `az login
  --federated-token` is rejected, reconcile the issuer with the MI token's `iss`.
- **Redis** is the self-hosted container by design (cheap; lost state self-heals
  via the 1-hour idle auto-delete) — not Azure Cache for Redis. See §4.1 of the
  migration doc.
- **Blob volume** mounts the whole `mcp-workspaces` container per group;
  per-User/Session/Conversation separation is by directory. Tighten to a
  per-Session prefix or switch to explicit read/write sync if needed (§7.3).
- The worker RBAC scope (subscription Reader/Contributor) is a demo default —
  tighten for production.

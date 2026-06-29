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

## Deploy

Prereqs: Azure CLI ≥ 2.62, `az login` with subscription **Owner**/**User Access
Administrator** (role assignments) + Entra **Application/Group Administrator**
and **Privileged Role Administrator** (OBO consent + FIC), `jq`, `docker`. The
region must support `Microsoft.App/sandboxGroups` (e.g. `westus2`, `eastus2`).

```bash
# 1. Deploy infrastructure (RG, identity, sandbox groups, storage, ACR,
#    env, redis, MCP app on a placeholder image, RBAC, FIC).
az deployment sub create -n dataops-mcp-aca -l westus2 -f main.bicep

# 2. Capture outputs into ../../.env.aca
./write-env.sh dataops-mcp-aca
set -a && source ../../.env.aca && set +a

# 3. Build + push the MCP server image and the sandbox image to ACR.
az acr login -n "$REGISTRY_NAME"
docker build -t "$REGISTRY_LOGIN_SERVER/mcp-server:latest" ../../src/mcp-server
docker push "$REGISTRY_LOGIN_SERVER/mcp-server:latest"
docker build -t "$REGISTRY_LOGIN_SERVER/mcp-sandbox:latest" ../../src/sandbox-image
docker push "$REGISTRY_LOGIN_SERVER/mcp-sandbox:latest"

# 4. Reset the MCP app's OBO client secret and set it on the Container App.
MCP_SECRET=$(az ad app credential reset --id "$MCP_APP_ID" --display-name aca --query password -o tsv)
az containerapp secret set -n "$MCP_APP_NAME" -g "$ACA_RESOURCE_GROUP" \
  --secrets mcp-client-secret="$MCP_SECRET"

# 5. Let the app pull from ACR via its managed identity (AcrPull was granted in
#    rbac.bicep), then point it at the real image + the sandbox disk source.
az containerapp registry set -n "$MCP_APP_NAME" -g "$ACA_RESOURCE_GROUP" \
  --server "$REGISTRY_LOGIN_SERVER" --identity system
az containerapp update -n "$MCP_APP_NAME" -g "$ACA_RESOURCE_GROUP" \
  --image "$REGISTRY_LOGIN_SERVER/mcp-server:latest" \
  --set-env-vars SANDBOX_DISK_IMAGE="$REGISTRY_LOGIN_SERVER/mcp-sandbox:latest"

# 6. Add users to the AD groups, then point your MCP client at:
#    https://<MCP_FQDN>/mcp
```

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

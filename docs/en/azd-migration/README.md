# azd Migration Plan: Converging the ACA Path into `azd up`

> Target audience: Maintainers of this repository's deployment workflow.
> Bottom line upfront: **Only azd-ify the ACA (cloud) path; keep the local path as-is.**
> `azd up` replaces the current ACA workflow of "`az deployment sub create` + `write-env.sh` + 4 manual commands" with a single command.

---

## 0. TL;DR

| | Current | After azd |
|---|---|---|
| ACA Deployment | 6 manual steps (see §1.2) | `azd up` single command + 1 still-manual group assignment |
| `write-env.sh` (the half that reads outputs) | Run manually, writes `.env.aca` | **Deleted** — azd automatically injects Bicep outputs into `.azure/<env>/.env` |
| Image build/push + image swap | Manual `docker build/push` + `az containerapp update --image` | Built into `azd deploy` |
| OBO secret reset + injection | Two manual commands | `preprovision` hook + secure param |
| Sandbox second image | Manual build/push + `--set-env-vars` | `postprovision` hook (`az acr build`) + deterministic reference in Bicep |
| Adding users to AD group | Manual | **Still manual** (azd cannot and should not manage this) |

The local path **remains unchanged**; reasons are in §2.

---

## 1. Current State Assessment

### 1.1 Local Path Current State (`EXECUTOR=local`)

`provisioning/local/` + `docker-compose.yml`. 5 steps:

1. `az deployment tenant create -f provisioning/local/main.bicep` — **tenant scope**, only creates Entra: MCP app (delegated `user_impersonation` + OBO admin consent), 2 AD groups, `diagnose-sp` / `action-sp` two worker apps.
2. `write-env.sh` — reads outputs **+ runs `az ad app credential reset` for each of the 3 apps to generate keys on the fly**, writes `.env`.
3. `az ad group member add` adds users to the two groups (manual).
4. `az role assignment create` grants Reader / Contributor to the two worker SPs (manual, intentionally not in Bicep).
5. `docker compose up --build` — starts mcp-server + diagnose-worker + action-worker + redis. Workers log in using **SP + client secret** (see `AZURE_CLIENT_SECRET` in `docker-compose.yml`).

Key point: local worker identities **have passwords**; secrets must be materialized in `write-env.sh`, and `.env` is consumed by compose.

### 1.2 ACA Path Current State (`EXECUTOR=aca`)

`provisioning/aca/` has a `main.bicep` with `targetScope = 'subscription'` fanning out to 11 modules. The 6 steps from `provisioning/aca/README.md`:

| Step | Command | Nature |
|---|---|---|
| 1 | `az deployment sub create -f main.bicep` — creates RG, identity, sandbox groups, storage, ACR, env, redis, **MCP app (placeholder public image)**, RBAC, FIC | Declarative ✅ |
| 2 | `write-env.sh` reads outputs → `.env.aca`, `source` it | Glue (**azd can absorb**) |
| 3 | `docker build/push` **two** images (mcp-server + sandbox) to ACR | Imperative |
| 4 | `az ad app credential reset` for MCP's OBO secret + `az containerapp secret set` | Imperative |
| 5 | `az containerapp registry set --identity system` + `az containerapp update --image <real image> --set-env-vars SANDBOX_DISK_IMAGE=...` | Imperative (image swap) |
| 6 | `az ad group member add` to add users; client points to `https://<MCP_FQDN>/mcp` | Manual |

Worker identities in the cloud are **passwordless** (sandbox-group MI exchanges FIC for worker SP token, `az login --federated-token`), so there is **no worker secret** here; the only secret is the MCP server's own OBO client secret (step 4).

**Already azd-friendly aspects** (advantages):
- `main.bicep` already has `targetScope = 'subscription'` and creates its own RG — this is azd's native shape; no structural changes needed.
- Images already use a **placeholder public image** (`mcr.microsoft.com/k8se/quickstart:latest`) + later replacement with the real image — this is the standard azd ACA template pattern.
- All resource IDs are already exposed via `output` — azd can take over directly.

---

## 2. Why Not azd-ify the Local Path

- **Scope mismatch**: local uses `az deployment tenant create` (tenant scope), purely creating Entra/Graph resources. azd `provision` uses the subscription + RG model and does not deploy tenant-scope Graph resources.
- **Target mismatch**: the local deployment target is local docker-compose, not Azure. azd is a "deploy to cloud" tool and will not run compose for you.
- Therefore, the local `write-env.sh` (reading outputs **+ resetting 3 SP secrets**) remains untouched.

> In short: the benefits of azd-ification are entirely on the ACA path. Forcing azd onto the local path would only add complexity with no benefit.

---

## 3. What ACA Looks Like After `azd up`

### 3.1 Single Command

```bash
azd auth login
azd env new dataops-mcp-aca      # First time; select subscription, region=westus2
azd up                           # = provision(Bicep) + deploy(build/push/image swap)
# Only manual step after: add users to AD group
az ad group member add --group "$(azd env get-value DIAGNOSE_GROUP_ID)" --member-id <oid>
```

After code changes, redeploy with `azd deploy` (no infra re-run); for infra changes, use `azd provision`.

### 3.2 Lifecycle Mapping (Current Steps → azd Phases)

```
azd up
├─ preprovision  (hook)   → reset MCP OBO secret, store in azd env (first time only)         ← Current step 4 (first half)
├─ provision     (bicep)  → Current step 1 full infrastructure + seed secret via secure param    ← Current step 1 + step 4 (second half)
│                           outputs automatically injected into .azure/<env>/.env                    ← Current step 2 (write-env.sh) entirely removed
├─ deploy        (azd)    → build/push mcp-server image, swap image (via azd-service-name tag) ← Current step 3 (first half) + step 5
└─ postprovision (hook)   → az acr build to build sandbox image                                   ← Current step 3 (second half)
```

### 3.3 `azure.yaml` (Repository Root)

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Azure/azure-dev/main/schemas/v1.0/azure.yaml.json
name: identity-aware-mcp
metadata:
  template: identity-aware-mcp@0.1
infra:
  provider: bicep
  path: provisioning/aca
  module: main
services:
  mcp:
    project: src/mcp-server
    language: docker
    host: containerapp
    docker:
      path: Dockerfile
      remoteBuild: true          # Use ACR remote build; no docker daemon needed in hooks or locally
hooks:
  preprovision:
    windows: { shell: pwsh, run: provisioning/aca/hooks/preprovision.ps1 }
    posix:   { shell: sh,   run: provisioning/aca/hooks/preprovision.sh }
  postprovision:
    windows: { shell: pwsh, run: provisioning/aca/hooks/postprovision.ps1 }
    posix:   { shell: sh,   run: provisioning/aca/hooks/postprovision.sh }
```

---

## 4. What Needs to Change (Change List)

### 4.1 Add `azure.yaml` (see §3.3)
At the repository root, declare the infra path + the `mcp` containerapp service + two hooks.

### 4.2 Modify `mcp-app.bicep`: Let azd Claim the Container App

azd `deploy` finds the container app to swap images via the **`azd-service-name` tag** and finds where to push via the **ACR endpoint output**. Three changes needed:

1. **Add the tag**, value = the service name `mcp` from `azure.yaml`:
   ```bicep
   resource app 'Microsoft.App/containerApps@2024-03-01' = {
     name: '${name}-mcp'
     location: location
     tags: { 'azd-service-name': 'mcp' }   // ← New
     ...
   ```
2. **Image read-back** (the most critical pitfall, see §5.1): Re-running `provision` must not revert the real image swapped by azd back to the placeholder. Use the azd ACA template's `exists` pattern — pass a `mcpAppExists` bool, and if it exists, read back the current image from the existing app:
   ```bicep
   param mcpAppExists bool = false
   resource existing 'Microsoft.App/containerApps@2024-03-01' existing = if (mcpAppExists) {
     name: '${name}-mcp'
   }
   var effectiveImage = mcpAppExists
     ? existing.properties.template.containers[0].image
     : mcpImage
   ```
   `azd` automatically computes and passes `<service>Exists` (`mcpExists` → mapped to `mcpAppExists`); no manual maintenance needed.
3. **Deterministic `SANDBOX_DISK_IMAGE`** (eliminate current step 5's `--set-env-vars`): The ACR login server is known at provision time, and the image tag is fixed; compose it directly in Bicep:
   ```bicep
   // Change param sandboxImage default to a deterministic reference (main.bicep passes registry.outputs.loginServer)
   { name: 'SANDBOX_DISK_IMAGE', value: '${acrLoginServer}/mcp-sandbox:latest' }
   ```
   The env variable points to "where the image will be"; as long as `postprovision` pushes the image before SandboxManager uses it for the first time, this works.

### 4.3 `main.bicep`: Add azd-Convention Output + Pass New Params

azd's containerapp target needs to know the ACR endpoint. Add a conventionally named output (reusing an existing value):

```bicep
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
// All existing outputs like REGISTRY_LOGIN_SERVER / MCP_FQDN remain; azd injects them all into the env
```

And pass `mcpAppExists`, `mcpClientSecret` (secure) through to the `mcp-app` module.

### 4.4 `write-env.sh`: Retire (ACA Version)

`provisioning/aca/write-env.sh` **delete entirely**. Both things it does are taken over:
- Reading outputs → azd automatically writes `.azure/<env>/.env` (accessible via `azd env get-values`).
- The comment history about "deleted `az ad app update --identifier-uris`" (AADSTS500011 drift) is archived along with it — azd does not introduce any identifier-uri overwrites, consistent with the `fix-identify-uri-overwrite` direction.

> If runtime/scripts still depend on the filename `.env.aca`, keep a 3-line compatibility script: `azd env get-values > ../../.env.aca`.

### 4.5 OBO Secret → `preprovision` Hook

```sh
# provisioning/aca/hooks/preprovision.sh  (excerpt)
set -euo pipefail
# Idempotent: skip reset if already exists (credential reset invalidates old secrets; don't rotate on every azd up)
if [ -z "$(azd env get-value MCP_CLIENT_SECRET 2>/dev/null || true)" ]; then
  APP_ID="$(azd env get-value MCP_APP_ID 2>/dev/null || true)"
  if [ -n "$APP_ID" ]; then
    SECRET="$(az ad app credential reset --id "$APP_ID" --display-name aca --query password -o tsv)"
    azd env set MCP_CLIENT_SECRET "$SECRET"
  fi
fi
```

- azd maps `MCP_CLIENT_SECRET` (in azd env) to the secure bicep param `mcpClientSecret`; Bicep directly seeds the container app secret, **eliminating current step 4's `az containerapp secret set`**.
- On the first `azd up`, `MCP_APP_ID` doesn't exist yet (identity module hasn't run) → the hook skips, and a placeholder secret is used; a second provision is needed after the app is created to get the appId and seed the secret. **A more robust approach**: move secret injection to `postprovision` (where outputs already contain `MCP_APP_ID`), reverting to `az containerapp secret set`, but changing it from "manual" to "automatic". §6 implements the robust version.
- Note: azd env's `.env` is **stored in plaintext**, equivalent risk to today's `.env.aca`; this is not a regression.

### 4.6 Sandbox Second Image → `postprovision` Hook

The sandbox image is not a "deployed as a container app" service; SandboxManager pulls it at runtime. Therefore, it **cannot** be a regular azd service. Use a hook to build it server-side in ACR (no local docker needed):

```sh
# provisioning/aca/hooks/postprovision.sh  (excerpt)
set -euo pipefail
REG="$(azd env get-value REGISTRY_NAME)"
az acr build -r "$REG" -t mcp-sandbox:latest ../../src/sandbox-image
```

### 4.7 Still Manual Parts

Adding real users to the AD group (`az ad group member add`) — who to authorize is a human decision and should not be automated. The `postprovision` hook can `echo` the group ID and command template at the end (copying the closing hint from the current `write-env.sh`).

---

## 5. Risks and Pitfalls

### 5.1 Container App Image Ownership Conflict (Biggest Pitfall)
azd `deploy` updates the container app's image **out-of-band** from provision. If Bicep still hardcodes `image: placeholder-image`, the next `azd provision`/`azd up` will revert the real image back to the placeholder. **Must** use the `exists` read-back pattern from §4.2. This is the most common rework point in azd + ACA templates; get it right first.

### 5.2 AcrPull Timing
Current state: first cold start uses a **public placeholder image** (at this point the app MI doesn't have AcrPull), `rbac.bicep` grants AcrPull later, then the image is swapped to a private ACR image. Under azd, the order naturally holds: `provision` (public placeholder + grant AcrPull) → `deploy` (push private image + swap image, MI already has AcrPull). The registry mounting logic in `mcp-app.bicep` using `useAcrRegistry = contains(mcpImage,'azurecr.io')` must align with the `exists` read-back `effectiveImage` and not reference the placeholder param anymore.

### 5.3 Graph Extension Still Preview
`identity.bicep` / `fic.bicep` use the `Microsoft.Graph` Bicep extension (public preview). azd only changes the trigger; the preview's roughness (especially `preAuthorizedApplications`, FIC issuer/subject) remains unchanged. The migration **does not** alter Graph logic, reducing variables.

### 5.4 Permission Surface Unchanged
The executor of `azd up` still needs subscription **Owner/UAA** (role assignment) + Entra **Application/Group Admin** + **Privileged Role Admin** (OBO consent + FIC). azd does not lower the prerequisite permission requirements.

### 5.5 Secret Stored in Plaintext
azd env's `.azure/<env>/.env` stores `MCP_CLIENT_SECRET` in plaintext, equivalent risk to today's `.env.aca`. Already covered by `.gitignore` (`.azure/` needs to be added to gitignore).

---

## 6. Phased Implementation Recommendations

| Phase | Content | Verifiable Outcome |
|---|---|---|
| **P0** | Add `azure.yaml` + add `AZURE_CONTAINER_REGISTRY_ENDPOINT` output to `main.bicep`; add `azd-service-name` tag to container app. **Don't touch image read-back yet**; `azd provision` runs successfully, outputs go into azd env | `azd provision` succeeds, `azd env get-values` shows all variables |
| **P1** | Add `exists` read-back to `mcp-app.bicep` (§4.2) + `azd deploy` swaps mcp-server image; `postprovision` builds sandbox image + deterministic `SANDBOX_DISK_IMAGE` | `azd up` end-to-end; re-running `azd up` does not revert the image to placeholder |
| **P2** | Secret injection into `postprovision` (robust version, §4.5); delete `aca/write-env.sh` (or keep a 3-line compatibility script); update `provisioning/aca/README.md` and root `README.md` Quickstart | On a fresh subscription, `azd up` works end-to-end, with only "add users to group" remaining manual |

> The local path remains untouched in all three phases.

---

## Appendix: File Change Overview

| File | Action |
|---|---|
| `azure.yaml` (new) | Declare infra + `mcp` service + hooks |
| `provisioning/aca/main.bicep` | Add `AZURE_CONTAINER_REGISTRY_ENDPOINT` output; pass through `mcpAppExists` / secure secret |
| `provisioning/aca/modules/mcp-app.bicep` | `azd-service-name` tag; `exists` image read-back; deterministic `SANDBOX_DISK_IMAGE` |
| `provisioning/aca/hooks/{pre,post}provision.sh` (new) | Secret injection + `az acr build` for sandbox image + group authorization hint |
| `provisioning/aca/write-env.sh` | Delete (or downgrade to a 3-line `.env.aca` compatibility export) |
| `provisioning/aca/README.md` / root `README.md` | Change Quickstart to `azd up` |
| `.gitignore` | Add `.azure/` |
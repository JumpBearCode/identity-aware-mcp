# First-Time azd Deployment: Gotchas Post-Mortem

> Context: 2026-07-19, first `azd` run (`azd provision` → `azd deploy`) of the ACA
> stack on a **brand-new subscription**, verified end-to-end with the MCP dual
> channels (`diagnose_bash` / `action_bash`).
> Bottom line: **the azd migration works — end-to-end was verified live**; but a
> first deployment on a clean environment hits 5 classes of gotcha — Microsoft.Graph
> extension, ARM dependency graph, azd hooks, Entra pre-authorization, and sandbox
> cold-start. This doc explains **why** each happens, the on-the-spot fix, and how to
> root-cause-fix it.

---

## Overview

| # | Problem | Layer | Root cause in one line | Status |
|---|---|---|---|---|
| 1 | `fic` module can't find the worker SP | Microsoft.Graph ext + ARM preflight | The `existing` reference is validated at **preflight, before** the SP is created; `dependsOn` doesn't govern preflight | Rerun to bypass · **not yet root-fixed** |
| 2 | `mcp-app` circular dependency | ARM dependency graph (static) | Same-name `existing` + the main resource = a self-cycle; the `if` condition doesn't affect the static cycle check | ✅ Fixed (`fetch-container-image.bicep`) |
| 3 | OBO secret not injected into the container | azd hook re-entrancy | Calling `azd env set` from **inside** a postprovision hook is unreliable | Manual patch · **not yet root-fixed** |
| 4 | Client token rejected by the server | Entra pre-authorization | The MCP API's `preAuthorizedApplications` omits the self-built CLI client | Manual patch · **not yet root-fixed** |
| 5 | First tool call errors out | sandbox cold-start | Cold image pull + microVM boot > default `SANDBOX_CREATE_TIMEOUT=30s` | Retry succeeds · raise the timeout |

**Deployment timeline** (how they chain):
`provision#1` hits #1 (SP partly created) → `provision#2` hits #2 (first 6 resources created) → fix #2 → `provision#3` **succeeds** but #3 (secret not applied) → manually patch secret → `deploy` succeeds → repoint `.mcp.json` + add group members → client reconnect hits #4 (token rejected) → fix #4 → reconnect works → first `diagnose_bash` hits #5 (timeout) → retry succeeds, new instance verified on both channels.

---

## Gotcha 1 · `fic` module's Graph preflight race

**Symptom**
`azd provision` fails on the first run:
```
InvalidTemplateDeployment: The template deployment 'fic' is not valid ...
NotFound: Request_ResourceNotFound: Resource 'dataops-mcp-diagnose-sp' does not exist ...
```
Yet checking afterwards, `dataops-mcp-diagnose-sp` **had in fact been created**.

**Why (root cause)**
- `fic.bicep` references the worker SP app via `existing`:
  ```bicep
  resource diagnoseSpApp 'Microsoft.Graph/applications@v1.0' existing = {
    uniqueName: '${name}-diagnose-sp'
  }
  ```
- For a Microsoft.Graph extension resource, resolving an `existing` reference means
  **querying Graph for that object by `uniqueName`**.
- ARM runs a **preflight validation** on every module (= a `Microsoft.Resources/deployments`),
  and that validation resolves the `existing` references inside it. This happens during
  the **whole-template preflight**, *before* the `identity` module actually **executes**
  (creates the SP).
- `fic` has `dependsOn: [identity]` in `main.bicep`, but `dependsOn` only guarantees
  **execution order** (fic's resources apply after identity's) — it does **not** govern
  **when preflight validation runs**. So at preflight the SP doesn't exist yet →
  `fic` validation fails → the whole template is rejected as `InvalidTemplateDeployment`.
- **Why did the SP get created anyway?** Microsoft.Graph extension resources aren't
  standard ARM resources; their writes are submitted to the Graph API early in the
  deployment. The overall template was failed by `fic`'s validation, but the
  already-submitted Graph changes **are not rolled back** (Graph has no transaction).
  So the SPs/groups persisted.
- **Why does a rerun pass?** The SP is a tenant-level, durable object. On the second
  run, `fic`'s `existing` resolves the **already-present** SP at preflight → passes →
  deployment proceeds.

In one line: **an `existing` cross-module reference to a Graph object that is created
in the *same* deployment hits the preflight-before-execution ordering.** This is a
known pattern issue of the Microsoft.Graph Bicep extension (public preview), exposed
only on a clean environment's very first deploy (object never existed).

**On-the-spot bypass**: rerun `azd provision` (the SP now exists).

**Root fix** (pick one):
- **A. Structural (recommended)**: move the FIC definitions **into `identity.bicep`**,
  in the same compilation unit as the worker SP apps — a same-module reference doesn't
  use `existing`, so there's no preflight race. Cost: the FIC needs the sandbox-group
  MI `principalId`, so `identity` must take it as a param (`main.bicep` passes
  `sandboxGroups.outputs` into identity; identity depends on sandboxGroups).
- **B. Two-phase**: deploy identity alone first (create SPs), then the full stack.
- **C. Auto-rerun**: a preprovision/wrapper script that detects the first failure and
  reruns. A band-aid, not a cure.

---

## Gotcha 2 · `mcp-app` circular dependency (a bug this migration introduced)

**Symptom**
After fixing #1, `provision` fails building the 7th resource:
```
InvalidTemplate: Circular dependency detected on resource:
'.../containerApps/dataops-mcp-mcp'
```

**Why (root cause)**
- To keep the real image (that `azd deploy` swapped in) from being reset to the
  placeholder on a re-`provision`, I inlined an image read-back in `mcp-app.bicep`:
  ```bicep
  resource existingApp '...containerApps@2024-03-01' existing = if (mcpAppExists) {
    name: '${name}-mcp'          // ← SAME name as the app below!
  }
  var effectiveImage = mcpAppExists ? existingApp!.properties.template.containers[0].image : mcpImage
  resource app '...containerApps@2024-03-01' = {
    name: '${name}-mcp'          // ← same name
    ... image: effectiveImage
  }
  ```
- `existingApp` and `app` share the name `${name}-mcp`. `app.image` depends on
  `effectiveImage`, which depends on `existingApp`. In ARM's dependency graph
  `app` → `existingApp`, and both point at the **same resource name** → **`app`
  depends on itself** → a cycle.
- Key point: ARM's circular-dependency check is **static** (compile/validation time,
  purely graph topology) and **ignores the runtime `if (mcpAppExists)` value**. So even
  when `mcpAppExists=false` on the first run, the self-edge in the static graph is still
  flagged as circular.
- The official azd ACA templates break this cycle precisely by putting the image
  read-back in its **own module** (`fetch-container-image.bicep`): the child module is
  an independent compilation/deployment unit, `existingApp` lives inside it, and the
  main `app` gets the image via the module `output`. ARM treats the module as one graph
  node, so no self-cycle. I inlined it to save effort and hit the trap.

**Fix (already in the code)**: added `provisioning/aca/modules/fetch-container-image.bicep`;
`mcp-app.bicep` now calls it to get `effectiveImage`. `bicep build` and `provision` pass.

---

## Gotcha 3 · postprovision hook's secret injection didn't take

**Symptom**
After `provision` succeeds:
- the ACR **has** the `mcp-sandbox` image (hook **step 1** `az acr build` ran);
- but the container app's `mcp-client-secret` is **still the placeholder**
  `placeholder-set-via-secret-set`, and azd env has **no** `MCP_CLIENT_SECRET` (hook
  **step 2** didn't take effect).

**Why (root cause, best inference)**
Hook `postprovision.sh` step 2 is:
```sh
if [ -z "$(azd env get-value MCP_CLIENT_SECRET ...)" ]; then
  secret=$(az ad app credential reset --id "$MCP_APP_ID" ...)
  azd env set MCP_CLIENT_SECRET "$secret"          # ← suspect
  az containerapp secret set ... mcp-client-secret="$secret"
fi
```
- Step 1 running (image is there) proves the hook **did execute** and azd's injected
  env vars (e.g. `REGISTRY_NAME`) were present.
- Most likely root cause: **calling `azd` (`azd env get-value` / `azd env set`) from
  inside an azd hook is unreliable.** While azd runs the postprovision hook, the parent
  azd process holds the current environment's context; a child `azd` spawned from the
  hook to read/write the **same** env can fail to read, have its write clobbered by the
  parent's later state, or not resolve the environment at all (azd offers no reliable
  guarantee about re-entering itself from a hook).
- Result: `azd env set` doesn't persist, and the `az containerapp secret set` step may
  be skipped too if `set -eu` bails on the preceding `azd env set` (or the block never
  finished). The secret stays at the placeholder.
- Corroboration: `provision` reported `SUCCESS`; if the hook had exited non-zero azd
  should have reported a hook failure — so the hook "looked successful" but the secret
  side-effect didn't land, consistent with a silent azd re-entrancy failure.

**On-the-spot bypass**: run what step 2 should have done — `az ad app credential reset`
+ `az containerapp secret set` + `azd env set` — from **outside** the hook, in a
separate shell (no azd re-entrancy). Verified the container secret became a real value.

**Root fix**: make secret injection **not depend on `azd env` re-entrancy inside the hook**:
- change the idempotency guard from "query azd env" to "is the container app secret still
  the placeholder?" (`az containerapp secret show`);
- or drop the `azd env set` step and inject the secret solely via the hook's direct
  `az containerapp secret set` (accepting the trade-off that the value never enters azd
  env and a re-provision seeds the placeholder before the hook overwrites it);
- validate which is reliable against the azd version (this run didn't verify azd's
  exact hook re-entrancy behavior).

---

## Gotcha 4 · The MCP API's `preAuthorizedApplications` omits the self-built CLI client

**Symptom**
After repointing `.mcp.json` at the new deployment and re-authenticating: the client
**gets a token (login succeeds), but the server rejects it on reconnect**
(`POST /mcpproxy 401`), and the tools won't connect.

**Why (root cause)**
- Inspecting the MCP API app's auth config:
  ```
  identifierUris:   ["api://dataops-mcp-mcp-server"]   ✓
  preAuthApps:      ["aebc6443-..."]                    ← VS Code built-in client only
  scopes:           ["user_impersonation"]              ✓
  ```
  **Only VS Code (`aebc6443`) was pre-authorized — not the self-built CLI client
  (`32b136f2`).**
- OAuth has **two independent gates**:
  1. **Get a token**: the client uses CLI client `32b136f2` via PKCE; Entra
     authenticates the user and issues a token. This is largely independent of whether
     that client is pre-authorized on the API — if you can log in, a token is issued (so
     the client reports "authentication successful").
  2. **Is the token valid *for this API*?**: the server checks the token's `aud`/`scope`
     (requires `aud = MCP API`, includes `user_impersonation`).
- `preAuthorizedApplications` governs **gate 2**: it lets a named client obtain the
  correct scope for this API **without extra consent**, with the token's `aud` pointing
  at this API. `32b136f2` isn't on the list → the token Entra issues is invalid for the
  MCP API (`aud`/`scope` unsatisfied) → server 401.
- Root cause is in `identity.bicep`: it creates the CLI client but never adds it to the
  MCP API's `preAuthorizedApplications` (only VS Code is listed explicitly). This is the
  same family as the README's note that the Graph extension is flaky at updating
  `preAuthorizedApplications`.
- **Why "authentication succeeded" yet it won't connect**: because "got a token" ≠
  "token accepted by the API". The client only knows it received credentials, not that
  they're invalid for the server — the final arbiter is always the server's JWT check.

**On-the-spot fix**: `az rest PATCH` to add CLI client `32b136f2` to the MCP API's
`preAuthorizedApplications` (keeping VS Code), with the `user_impersonation` scope id.
After Entra propagates (1–2 min), re-authenticating succeeds.

**Root fix**: add `cliClientAppId` to `preAuthorizedApplications` in `identity.bicep`
(alongside VS Code).

---

## Gotcha 5 · sandbox cold-create timeout (the timeout record)

**Symptom**
On the new deployment, the **first** `diagnose_bash` call errors out (empty error);
**retrying immediately** succeeds and returns the correct SP identity.

**Why (root cause)**
- The first call makes `SandboxManager` **cold-create** an ACA sandbox (microVM) for the
  current Session/group: call the sandboxGroups API → **pull the image** from
  `SANDBOX_DISK_IMAGE` (ACR `mcp-sandbox:latest`) → boot the microVM → passwordless FIC
  `az login` bootstrap → mount the Blob workspace.
- **First time**: the image isn't cached in that ACA environment yet, so "pull image +
  cold-boot microVM" **exceeds the default `SANDBOX_CREATE_TIMEOUT = 30s`** → create
  times out → the tool errors.
- **Retry**: the image/microVM is ready or cached, so creation is fast → success.
- This isn't a logic bug — it's a **cold-start latency vs. timeout threshold** config
  issue: 30s is tight for a first cold image pull.

**On-the-spot bypass**: just retry once (fast once the image is cached).

**Root fix / tuning**:
- Raise `SANDBOX_CREATE_TIMEOUT` (e.g. 60–90s). **This value is exactly one of the
  `azd env set`-controllable knobs added by this migration**:
  ```bash
  azd env set SANDBOX_CREATE_TIMEOUT 90 && azd provision
  ```
- Or pre-warm: after deploy, do one throwaway call to trigger the image pull so later
  users take the warm path.

---

## Appendix: how to run a clean first deployment (workaround checklist)

**Before** #1/#3/#4 are root-fixed, a clean environment runs reliably in this order:

1. `azd env new <name> --subscription <sub> --location westus2`
2. `azd provision` — **expected to fail on the first run** due to #1 (creates SPs/groups).
3. `azd provision` — second run (SP now exists) passes provision (#2 is fixed in code, won't recur).
4. **Manually patch the secret** (#3):
   `az ad app credential reset` → `az containerapp secret set mcp-client-secret=...` → `azd env set MCP_CLIENT_SECRET ...`
5. `azd deploy` — build + swap the mcp-server image.
6. **Manually pre-authorize the CLI client** (#4): add `CLI_CLIENT_APP_ID` to the MCP API's `preAuthorizedApplications`.
7. Add users to the diagnose / action AD groups; point the client at `https://<MCP_FQDN>/mcpproxy`.
8. If the first tool call times out (#5), **retry once**; or set `azd env set SANDBOX_CREATE_TIMEOUT 90` beforehand.

Once #1/#3/#4 are root-fixed, the goal is: `azd up` + add group members + point the
client — with none of the manual steps 2–4 or 6.

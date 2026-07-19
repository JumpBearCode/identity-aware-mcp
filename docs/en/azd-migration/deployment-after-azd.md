# Deployment After the azd Migration (local vs. ACA)

> Companion to [`README.md`](README.md) (the migration *plan*). This doc describes
> what deployment actually looks like **after** the implementation landed, and
> answers four concrete questions.
>
> **Implementation note:** the shipped version differs slightly from the plan —
> it uses a **single `postprovision` hook** (not `pre` + `post`), injects the OBO
> secret through an azd-env round-trip (the plan's "robust P2" variant), and
> **deletes** `provisioning/aca/write-env.sh` outright — the azd env
> (`.azure/<env>/.env`) is the single source of truth, and `e2e_deployed.py`
> loads it directly (see §4).

---

## 1. What does the **local** deploy look like? (unchanged)

The local path (`EXECUTOR=local`) is **not** azd-managed and did not change. It is
still Bicep (tenant scope) + `write-env.sh` + docker-compose:

```bash
cd provisioning/local
az deployment tenant create -n dataops-mcp-provision -l eastus -f main.bicep
./write-env.sh dataops-mcp-provision      # resets 3 app secrets -> ../../.env
# add users to the AD groups; grant the worker SPs RBAC (both manual)
cd ../.. && docker compose up --build
```

Why local stays as-is:

- **Scope**: it is `az deployment tenant create` (tenant scope), only creating
  Entra apps / AD groups / worker SPs. azd provisions at subscription + RG scope.
- **Target**: it deploys to local docker-compose, not Azure. azd deploys to cloud.
- **Secrets**: local workers log in with **SP + client secret**, so the local
  `write-env.sh` must still materialize those secrets into `.env` (consumed by
  compose). That is its real job here and it stays. (Contrast with §4.)

---

## 2. What does the **ACA** deploy look like now? (`azd up`)

The whole cloud footprint is now one `azd up`. azd runs this lifecycle:

```
azd up
├─ provision      (Bicep)  provisioning/aca/main.bicep — RG, identity, sandbox
│                          groups, storage, ACR, env, redis, the MCP Container App
│                          (on a public placeholder image), RBAC, FIC. Bicep
│                          outputs are captured into the azd env automatically.
├─ postprovision  (hook)   provisioning/aca/hooks/postprovision.sh —
│                            1. az acr build → mcp-sandbox:latest into ACR
│                            2. reset + inject the MCP OBO client secret (once)
│                            3. print the "add users to AD groups" reminder
└─ deploy         (azd)    builds src/mcp-server in ACR (remoteBuild) and swaps
                           it into the Container App, located via its
                           `azd-service-name: mcp` tag.
```

What replaced the old manual steps:

| Old ACA step | Now |
|---|---|
| `az deployment sub create` | `azd provision` (same Bicep, unchanged logic) |
| `write-env.sh` reads outputs → `.env.aca` | azd captures outputs into `.azure/<env>/.env` automatically |
| `docker build/push` mcp-server + swap image | `azd deploy` (build in ACR + swap via tag) |
| `docker build/push` sandbox image | `postprovision` hook (`az acr build`) |
| `credential reset` + `containerapp secret set` | `postprovision` hook (once, guarded) |
| `--set-env-vars SANDBOX_DISK_IMAGE=...` | deterministic ref in `mcp-app.bicep` |
| add users to AD groups | **still manual** |

Two design points worth knowing:

- **The OBO secret is a one-time bootstrap, not a per-deploy action.** On the
  first `azd up`, `postprovision` runs `az ad app credential reset`, stashes the
  value with `azd env set MCP_CLIENT_SECRET`, and applies it with
  `az containerapp secret set`. On every later provision that value is passed
  back as the secure `mcpClientSecret` Bicep param, so re-provisioning never
  clobbers it with the placeholder — and the hook skips the reset. `azd deploy`
  never touches the secret. Rotate deliberately with
  `azd env set MCP_CLIENT_SECRET "" && azd provision`.
- **Image read-back** (`mcpAppExists`): azd swaps the real image out-of-band, so
  `mcp-app.bicep` reads the currently-deployed image back on re-provision instead
  of resetting to the placeholder (azd feeds `SERVICE_MCP_RESOURCE_EXISTS`).

Iterate afterwards: `azd deploy` for code changes, `azd provision` for infra
changes, `azd up` for both.

---

## 3. What are the ACA deployment steps?

**Prereqs**: `azd` (`https://aka.ms/azd-install`), Azure CLI, `az login` as a
principal with subscription **Owner / User Access Administrator** + Entra
**Application/Group Administrator** + **Privileged Role Administrator** (for OBO
consent and FIC), and a region that supports `Microsoft.App/sandboxGroups`
(e.g. `westus2`, `eastus2`).

```bash
# 0. Sign in (azd for provisioning/deploy; az for the hook's az acr build / secret set)
azd auth login
az login

# 1. Create an azd environment (prompts for subscription + region)
azd env new dataops-mcp-aca

# 2. One command: provision + postprovision hook + deploy
azd up

# 3. Manual: authorize real users (per-user, intentionally not automated)
az ad group member add --group "$(azd env get-value DIAGNOSE_GROUP_ID)" --member-id <user-object-id>
az ad group member add --group "$(azd env get-value ACTION_GROUP_ID)"   --member-id <user-object-id>

# 4. Point your MCP client at the server
echo "https://$(azd env get-value MCP_FQDN)/mcp"
```

That is the whole flow. The single still-manual step is group membership (step 3),
because who gets access is a human decision.

---

## 4. Is `write-env.sh` (ACA) now deletable?

**Done — it is deleted.** Both `provisioning/aca/write-env.sh` and the repo-root
`.env.aca` are gone. `.azure/<env>/.env` (the azd environment) is now the single
source of truth for a deployed stack.

Why nothing breaks:

- The running Container App **never** read `.env.aca`: the server reads
  `os.environ`, and in the cloud those variables are injected by `mcp-app.bicep`.
  Deployment never depended on it.
- The only local consumer, `src/mcp-server/tests/e2e_deployed.py`, now **loads the
  azd env itself**: a small `_load_azd_env()` helper reads `.azure/config.json` →
  `defaultEnvironment` → `.azure/<env>/.env` into `os.environ` at startup, and
  derives `MCP_SERVER_URL` from the `MCP_FQDN` output. So `python e2e_deployed.py`
  just works against whatever `azd env select` points at — nothing to `source`,
  no `.env.aca`. Explicit shell vars still win
  (`MCP_SERVER_URL=... python e2e_deployed.py`).

**Do not** confuse this with the **local** `provisioning/local/write-env.sh`, which
is **not** deleted — it still materializes the worker SP client secrets into `.env`
for docker-compose.

---

## 5. Deeper Q&A

### 5.1 Why can't `azd deploy` build the sandbox image?

Because `azd deploy` deploys a **service — something that runs** — and the sandbox
image isn't one.

For a `containerapp` service, `azd deploy` does exactly: build image → push to ACR →
**update that container app's `image`**. It needs a running target to point the
image at. `mcp-server` has one (the MCP Container App), so it fits.

The **sandbox image is a base disk image**, not a hosted app. Nothing runs it at
deploy time — `SandboxManager` pulls it **at runtime** to boot a per-Session microVM
when a user first calls a tool. No container app's `image` is the sandbox image, so
`azd deploy` has nothing to target. It's a **dependency artifact**, more like data
than a service. azd has no clean "build + push, don't deploy" service type, so a hook
(`az acr build`) that just puts the image into ACR for runtime pull is the honest fit.

### 5.2 What is `containerapp secret set`? (and: reset runs once)

First, a correction to a common misconception: it does **not** mint a new client
secret on every deploy. `reset` runs **once** per azd environment (the hook is
guarded — skip if it's already in the azd env), and `azd deploy` never touches it.
So: first `azd up` resets once; every deploy after does nothing. (Why the op is
called "reset": Azure only returns a secret's value at creation, you can't read an
existing one back, so obtaining a value to inject means creating one — a one-time
bootstrap, not a per-deploy act.)

Container Apps separates **storing** a secret from **exposing** it:

1. `az containerapp secret set` writes the value into the app's **encrypted secret
   store** (`configuration.secrets`) under the name `mcp-client-secret` — encrypted
   at rest, never in the resource JSON as plaintext.
2. **The env var is a separate reference.** The container env in `mcp-app.bicep`
   already has:
   ```bicep
   { name: 'MCP_CLIENT_SECRET', secretRef: 'mcp-client-secret' }
   ```
   `secretRef` (not `value`) means "populate this env var *from* the secret named
   `mcp-client-secret`."

So the chain is: `secret set` fills the encrypted store → `secretRef` surfaces it as
the `MCP_CLIENT_SECRET` env var inside the container → the Python server reads
`os.environ["MCP_CLIENT_SECRET"]` for OBO. `secret set` without `secretRef` never
reaches the app; `secretRef` without `secret set` is empty. You need both.

### 5.3 What are the other env vars? Can azd feed params that change app behavior?

Yes. The container's env block **is** the app's entire runtime config; the Python
server reads it via `os.environ` to change behavior. `SANDBOX_DISK_IMAGE` is one of
~23 lines in `mcp-app.bicep`, in three buckets:

| Bucket | Examples | Effect |
|---|---|---|
| **Behavior / logic** | `EXECUTOR` (local vs aca backend), `SANDBOX_DISTRIBUTED_LOCK` (Redis lock on/off), `SANDBOX_DISK_IMAGE`, audit knobs (`AUDIT_DCR_*`) | change what the app does |
| **Wiring / connection** | `REDIS_URL`, `STORAGE_ACCOUNT`, `BLOB_CONTAINER`, `MCP_SERVER_BASE_URL` | point the app at resources |
| **Identity / auth** | `AZURE_TENANT_ID`, `MCP_APP_ID`, `MCP_IDENTIFIER_URI`, `MCP_CLIENT_SECRET`, `*_GROUP_ID` | drive JWT validation + OBO |

Note the "multiple Redis replicas" example is **not** an app env var — replica count
is **infra scaling**, in `redis.bicep`'s `scale { minReplicas / maxReplicas }` block.
Different layer: app behavior = container env var; how many replicas of a service run
= Bicep infra.

The azd-to-behavior chain:

```
azd env var ──► main.parameters.json (${VAR}) ──► main.bicep param
   ──► mcp-app.bicep env block ──► container env ──► os.environ in Python
```

**These behavior knobs are now all wired to azd** (full list in §6). To change one,
just two steps:

```bash
azd env set SANDBOX_DISTRIBUTED_LOCK 1   # unset -> the app's code default (here "0")
azd provision                            # or azd up
```

The chain behind it (already wired — you don't touch it): `main.parameters.json`'s
`"sandboxDistributedLock": { "value": "${SANDBOX_DISTRIBUTED_LOCK=}" }` → main.bicep's
`param sandboxDistributedLock` → collected into an `envOverrides` object →
`mcp-app.bicep`'s `optionalEnv` injects only the **non-empty** ones (empties are
dropped so the server falls back to its `os.environ.get(..., default)`). So "unset →
default" is inherent, and the default lives in exactly one place — the code.

**To add a knob that isn't in the list yet** (the code must already
`os.environ.get("X")` it) takes just two steps:
① `main.bicep`: add `param x string = ''` and one line `X: x` in the `envOverrides` object;
② `main.parameters.json`: add `"x": { "value": "${X=}" }`.
**No `mcp-app.bicep` change** — `optionalEnv` is generic and injects any non-empty override.

In one line: **behavior knobs → app env vars, now all `azd env set`-controllable (unset
→ code default); scaling like Redis replicas → Bicep infra params. Both reachable from
azd, at different layers.**

---

## 6. Which env vars Bicep injects, and which are azd-settable

`mcp-app.bicep` injects **23 fixed** env vars into the MCP container, **plus whatever
tuning knobs you `azd env set`** (spliced in via `optionalEnv`). The Python server
reads only `os.environ`. Grouped by "can/should you change it with `azd env set`":

| Group | env var | Source | azd-settable? |
|---|---|---|---|
| **① Don't touch — provision output / identity / wiring** | `EXECUTOR`, `MCP_SERVER_BASE_URL`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACA_RESOURCE_GROUP`, `ACA_REGION`, `MCP_APP_ID`, `MCP_IDENTIFIER_URI`, `DIAGNOSE_GROUP_ID`, `ACTION_GROUP_ID`, `DIAGNOSE_SANDBOX_GROUP`, `ACTION_SANDBOX_GROUP`, `DIAGNOSE_SP_APP_ID`, `ACTION_SP_APP_ID`, `REDIS_URL`, `STORAGE_ACCOUNT`, `BLOB_CONTAINER`, `BLOB_CONTAINER_RESOURCE_ID`, `AUDIT_DCR_ENDPOINT`, `AUDIT_DCR_RULE_ID`, `AUDIT_STREAM_NAME` | hardcoded (`'aca'`) / derived var / Azure context (`tenant()`, `subscription()`) / module `output`s (identity, redis, storage, sandboxGroups, observability) | ❌ No — provisioned resource attributes & identity; changing them desyncs from the real resources |
| **② Secret** | `MCP_CLIENT_SECRET` | `parameters.json` `${MCP_CLIENT_SECRET}`, set by the postprovision hook | ✅ Yes; normally only to rotate (`azd env set MCP_CLIENT_SECRET "" && azd provision`) |
| **③ Behavior / tuning knobs — all `azd env set`, unset → code default** | `SANDBOX_DISK_IMAGE`, `SANDBOX_DISTRIBUTED_LOCK`, `MCP_EXEC_TIMEOUT`, `MAX_OUTPUT_BYTES`, `MCP_SESSION_TTL`, `MCPPROXY_ENABLED`, `SANDBOX_AUTO_SUSPEND_SECONDS`, `SANDBOX_AUTO_DELETE_SECONDS`, `SANDBOX_REAPER_INTERVAL`, `SANDBOX_REAPER_LEASE`, `SANDBOX_LOCK_TTL`, `SANDBOX_LOCK_WAIT`, `SANDBOX_CREATE_TIMEOUT`, `SANDBOX_CPU`, `SANDBOX_MEMORY`, `SANDBOX_DISK`, `SANDBOX_DISK_ID`, `BLOB_MOUNTPOINT`, `AUDIT_TIMEOUT`, `AUDIT_UA_INCLUDE_OID` | `main.parameters.json` `${…=}` → main.bicep param → `envOverrides` → empty means not injected (code default) | ✅ **all wired now** |

**These 20 (group ③) can now all be:**

```bash
azd env set <ENV_NAME> <value>   # e.g. azd env set MCP_EXEC_TIMEOUT 300
azd provision                    # or azd up; unset -> server uses its os.environ.get default
```

- Mechanism in §5.3 (`envOverrides` → `optionalEnv`, empties dropped). The default
  lives in one place: the code.
- Special case `SANDBOX_DISK_IMAGE`: when unset it is **not** "not injected" — it uses
  main.bicep's deterministic ref `<acr-login-server>/mcp-sandbox:latest`; set it only to
  override (point at a different sandbox image).
- ① don't touch (provision-decided); ② is hook-managed.

> `DIAGNOSE_WORKER_URL` / `ACTION_WORKER_URL` are **local docker** only (set in
> `docker-compose.yml`); they don't apply to ACA and aren't listed above.

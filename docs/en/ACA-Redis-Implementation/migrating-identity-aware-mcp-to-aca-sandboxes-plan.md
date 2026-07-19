# Migrating Identity-Aware MCP to ACA Sandboxes (Session-Level Stickiness + Stateless) Plan

> This document is the implementation plan, to be delivered incrementally over the next several requests. Audience: project maintainers.

---

## Terminology Alignment (Important)

Granularity from coarse to fine, four layers:

```
User (Entra oid)
  └─ Session            One work cycle; 30-minute sliding TTL; stickiness / lifecycle at this layer
       └─ Conversation  A single dialogue (one user question = one conversation); multiple conversations per Session
            └─ Tool Call  A single diagnose_bash / action_bash invocation
```

- **Routing / stickiness / sandbox teardown all happen at the Session level**. Route key = `(user_oid, session_id, group)`.
- **Multiple Conversations within a Session all hit the same sandbox** (one per group); `conversation_id` **does not participate in routing**.
- `conversation_id` is only used for directory partitioning in Blob (`.../sessionid_ts/conversationid/`), keeping files from each conversation separate.
- Use the English terms **Session / Conversation** throughout to avoid ambiguity.

### Refer to Azure Container App Sandbox Docs
  - https://techcommunity.microsoft.com/blog/appsonazureblog/introducing-azure-container-apps-sandboxes-secure-infrastructure-for-agentic-wor/4524131
  - https://sandboxes.azure.com/docs/sandboxes/

Refer to these two links if needed, and read the existing project as needed.

---

## I. Context and Goals

### Current State
The current stack (see `README.md`, `docker-compose.yml`) runs **two long-running worker containers**: `diagnose-worker`, `action-worker`, both built from the same `bash-worker` image.

- Each worker logs in at startup using a **fixed Service Principal + client secret** (`src/worker/entrypoint.sh`), then executes `az` commands via HTTP (`src/worker/worker.py`).
- The MCP server (`src/mcp-server/main.py`) validates Entra JWTs, queries user group membership via OBO, and routes `diagnose_bash` / `action_bash` to the corresponding worker URL.
- **Problem**: Workers are long-running containers; the `az` login state, `/tmp`, and files written by the Agent are **shared across all users**, causing cross-contamination at the execution environment level and preventing true per-user isolation.

### Goals
Replace the **cloud execution foundation** with **Azure Container Apps Sandboxes** (`Microsoft.App/sandboxGroups@2026-02-01-preview`), driven by the `azure-containerapps-sandbox` SDK. Achieve:

1. **Per-User / Per-Session isolation + true statelessness**: Kill the sandbox when the Session ends; the next user gets a fresh microVM, with identity and files restored from Redis / Blob.
2. **Passwordless login**: `az login` inside the sandbox is done via **Federated Identity Credential (FIC)**, with no secrets stored in the cloud.
3. **Session-level sticky routing**: All tool calls within a Session (30-minute sliding TTL) reuse the same sandbox (one per group). Multiple Conversations within a Session also route to the same sandbox; a new sandbox is only created when **another group** is triggered. **Not** a new sandbox per tool call.
4. **Preserve the local Docker path**: Local development still uses the original two workers, and the code layer uses a unified interface; changes are additive.

### Two Key Decisions Confirmed with the User
1. **Session → sandbox cardinality**: **One sandbox per (Session, group)**. A Session sticks to at most two sandboxes (one diagnose, one action), thus **preserving the read/write identity boundary**.
   - Multiple diagnose calls reuse the diagnose sandbox; multiple action calls reuse the action sandbox.
2. **Sandbox identity mechanism**: **Federated SP (FIC)**.
   - Each sandbox group has a SystemAssigned managed identity (MI).
   - Each worker app registration (`diagnose-sp` / `action-sp`) has a **federatedIdentityCredential** trusting the MI of its respective group.
   - Inside the sandbox: Obtain a token from the group MI (audience `api://AzureADTokenExchange`) and execute `az login --service-principal --federated-token` to log in as that SP.
   - Local Docker still uses SP + secret (the two paths do not interfere).

---

## II. Verified ACA SDK / Bicep Facts (from Official Docs + Azure-Samples)

- **Resource type**: `Microsoft.App/sandboxGroups@2026-02-01-preview`, `identity: { type: 'SystemAssigned' }`, `properties: {}`.
  - Egress policy / lifecycle / single sandbox login identity are all set at **runtime via the data plane**, not in the group's Bicep.
- **Two planes**:
  - **ARM control plane** (`SandboxGroupManagementClient`, targeting `management.azure.com`): Create / delete sandbox groups.
  - **ADC data plane** (`SandboxGroupClient`, via `endpoint_for_region(region)` targeting `management.<region>.azuredevcompute.io`): Manage sandboxes, exec, files.
- **RBAC required for the driver**: Role **Container Apps SandboxGroup Data Owner** = `c24cf47c-5077-412d-a19c-45202126392c`, granted on the group to the identity driving it (i.e., our MCP server's identity).
- **SDK surface area** (package `azure-containerapps-sandbox`, async in `.aio`):
  - `SandboxGroupManagementClient(cred, subscription_id=, resource_group=)`
    → `begin_create_group(name, region, identity={'type':'SystemAssigned'}).result()`, `get_group(name).identity['principalId']`, `delete_group(name)`
  - `SandboxGroupClient(endpoint_for_region(region), cred, subscription_id=, resource_group=, sandbox_group=)`
  - `begin_create_sandbox(disk=, labels=, cpu=, memory=, ports=, volumes=, snapshot_id=).result()` → `SandboxClient`
  - **Sticky reconnect**: `get_sandbox_client(sandbox_id)` + `ensure_running()`
  - `SandboxClient`: `.sandbox_id`, `.exec(cmd_str)`→`(stdout, stderr, exit_code)`, `.read_file`, `.write_file(path, bytes, create_dirs=True)`, `.add_port`, `.delete()` / group-level `begin_delete_sandbox(id)`
  - Label-based lookup: `list_sandboxes(labels={...})` (for crash recovery / warm pool)
- **Passwordless auth inside sandbox** (official "inception" pattern): `ManagedIdentityCredential()` directly obtains the group MI's token; no secrets inside the sandbox. We extend this with FIC→SP secondary exchange.
- **Volumes**: **Azure Blob** volumes can be mounted into the sandbox → perfect for our "per-User / per-Session" file persistence.

---

## III. Runtime Architecture and Program Flow

### 3.1 What is a Sandbox? (Key Clarification)

**A sandbox is not a server; it does not run our service.**

| | Local Worker (Current) | ACA Sandbox (Target) |
|---|---|---|
| Form | Long-running container running FastAPI (`worker.py`) | On-demand microVM running our disk image |
| How we make it run commands | Custom HTTP, `POST /exec` | SDK `SandboxClient.exec("az ...")` |
| Who provides the exec channel | Our FastAPI | **ACA data plane** (platform-provided RPC) |
| What's in the image | az + python + uvicorn + our server | Just az + python + jq + bootstrap script |

In other words: a tool call on the ACA path = **one `SandboxClient.exec(...)` call**; the SDK sends HTTPS to `management.<region>.azuredevcompute.io` behind the scenes, and the platform runs the command inside that microVM and returns stdout/stderr. We **do not** need to run any listening process inside the sandbox.

### 3.2 Component Responsibilities ("Which class does what")

| Component | What it is | Responsibility | Lifecycle |
|---|---|---|---|
| **MCP Server** (`main.py`'s FastMCP `app`) | Long-running HTTP service (ACA Container App) | OAuth/JWT validation, OBO group check, derive Session/Conversation, hand tool call to Executor | Long-running |
| **Executor** (protocol, `executor.py`) | An interface `exec(ctx, command)` | Abstract "where to execute", decouple local from cloud | — |
| `LocalDockerExecutor` | Local implementation of Executor | POST to existing worker containers (behavior unchanged) | Long-running |
| **SandboxManager** (`sandbox_manager.py`) | ACA implementation of Executor + orchestration brain | See 3.4; **this is the new core class** | In-process singleton + background reaper |
| `SandboxGroupClient` / `SandboxClient` | Handles from Azure SDK | Data plane RPC (create/exec/file/delete) | Created on demand / cached |
| **Sandbox** | microVM + our image | Actually runs `az`; already FIC-logged in as the corresponding SP | Created/deleted per Session |

### 3.3 Program Sequence Diagram (client → mcp server → sandbox)

```
Client(VS Code / Claude)
  │  diagnose_bash("az datafactory ... show")   + Entra JWT
  ▼
┌──────────────────────────────────────────────────────────────┐
│ MCP Server (FastMCP app, long-running ACA Container App, with its own MI) │
│  1 Validate JWT (AzureJWTVerifier)                                 │
│  2 OBO group check (require_diagnose / require_action)              │
│  3 Derive (user_oid, session_id, conversation_id)  [session.py]      │
│  4 executor.exec(ctx{group=diagnose}, "az ...")                │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ SandboxManager (implements Executor; in-process singleton)   │
│  5 Redis lookup (oid, session_id, "diagnose") → sandbox_id ?   │
│     ├─ Hit → get_sandbox_client(id) + ensure_running()        │
│     └─ Miss → begin_create_sandbox(disk=our image, labels)    │
│                 → Write back to Redis (30min TTL) → Run bootstrap once: │
│                    a) FIC passwordless az login as diagnose-sp        │
│                    b) Restore user az profile from Redis (set sub)    │
│                    c) Mount Blob (userid/sessionid_ts/...)           │
│  6 sandbox_client.exec("az ...")  ──ADC data plane HTTPS──►        │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ Sandbox D (microVM + our image; not a server)                │
│  7 az + python + jq; already logged in as diagnose-sp (read-only RBAC) │
│  8 Execute `az ...`, return stdout/stderr/exit_code                 │
└───────────────┬──────────────────────────────────────────────┘
                ▼   (Result returned as ExecResult to client)

Another diagnose_bash in the same Session → Step 5 hits → Reuses Sandbox D (skips bootstrap)
An action_bash in the same Session → Route key group=action → Lands on Sandbox A (different group)
Session idle for 30min → reaper deletes Sandbox D / A          1h completely idle → platform auto-delete fallback
```

### 3.4 What Does SandboxManager Actually Do? (Answering Point 4)

It is the "control plane" for the ACA path, translating "one tool call" into "run the command in the correct, alive, already-logged-in sandbox" and managing the entire lifecycle of these sandboxes. Specifically five things:

1. **Holds SDK clients**: One `SandboxGroupClient` (data plane) per group + a shared `SandboxGroupManagementClient` (control plane), built with `DefaultAzureCredential` (on the cloud, this is the MCP app's MI).
2. **Session sticky routing**: Uses Redis to map `(oid, session_id, group)` to a `sandbox_id`; on hit, `get_sandbox_client + ensure_running` to reuse; on miss, `begin_create_sandbox`.
3. **First-time bootstrap** (done once per sandbox): FIC passwordless `az login` → restore user profile → mount Blob.
4. **Execution**: `SandboxClient.exec(command)`, refresh Session TTL, map result to `ExecResult`.
5. **Lifecycle**: `begin_delete_sandbox` when Session ends / expires; background reaper for fallback cleanup; (optional) warm pool maintenance.

> Note: A previous draft had a `SandboxExecutor` delegating to `SandboxManager`, which was an extra layer. **Now merged**: `SandboxManager` directly implements the `Executor` protocol, one less layer of indirection.

---

## IV. Phased Implementation

### Phase 1 — Code Refactoring: Executor Abstraction (Preserve Local Interface)
Goal: `main.py` no longer directly calls worker URLs; both backends sit behind the same interface. The local Docker path behavior remains unchanged; the ACA path is additive.

- **Add `src/mcp-server/executor.py`**:
  - `ExecResult` (aligned with current worker JSON: `exit_code, stdout, stderr, truncated`).
  - `SessionCtx` (`user_oid, session_id, conversation_id, group: 'diagnose'|'action'`).
  - `Executor(Protocol): async def exec(self, ctx: SessionCtx, command: str) -> ExecResult`.
  - `LocalDockerExecutor`: Encapsulates the current `main.py:127` `_exec_on_worker(worker_url, command)` logic (selects diagnose / action URL based on `ctx.group`). No Session concept (single shared container) — **completely preserves today's behavior**.
- **`SandboxManager` (Phase 3) directly implements `Executor`**, as the ACA backend.
- **Modify `src/mcp-server/main.py`**:
  - Use environment variable `EXECUTOR=local|aca` to select the backend (default `local`).
  - `diagnose_bash` / `action_bash` assemble `SessionCtx` then call `executor.exec(...)`; truncation hints / timeout contracts remain in place.
  - Extend `UserAuthMiddleware` (`main.py:114`) to stash `session_id` and `conversation_id` alongside `user_oid` (derivation in Phase 4).

### Phase 2 — Provisioning Refactoring (Bicep Only, Two Folders)
Per requirements: Rewrite Bicep, remove Python provisioning, split into **local** and **ACA** folders; preserve the `.env` interface consumed by `docker-compose.yml`.

- **`provisioning/local/`** (move today's `provisioning/bicep/main.bicep` here): `targetScope='tenant'`, `extension microsoftGraphV1`. Keep MCP server app (with `user_impersonation`, VS Code pre-authorization, OBO admin consent), two AD groups, two worker SP app registrations. Output + `write-env.sh` → root `.env` (append `EXECUTOR=local`). **This is the preserved local interface**. Then delete `provisioning/python/` and the old `provisioning/bicep/`.
- **`provisioning/aca/`** (written from scratch, can borrow shape from local): Full cloud infrastructure, `targetScope='subscription'` to allow creating a Resource Group, using nested modules internally (see Phase 2b).

### Phase 2b — ACA Infrastructure Modules (`provisioning/aca/`)
- `main.bicep` (subscription scope): Create **Resource Group**, then call each module.
- `modules/identity.bicep` (tenant / Graph): MCP server app (same as local) + AD groups + `diagnose-sp` / `action-sp` app registrations. **FIC is in `rbac.bicep`, added after the group MI exists**.
- `modules/sandbox-groups.bicep`: Two `Microsoft.App/sandboxGroups@2026-02-01-preview` (`...-diagnose`, `...-action`), each with `identity: { type: 'SystemAssigned' }`. Output their respective `identity.principalId`.
- `modules/storage.bicep`: **Storage Account** + one Blob **container** (`mcp-workspaces`), hosting the `userid/sessionid_ts/conversationid/` structure.
- `modules/redis.bicep`: **Self-hosted Redis container within ACA** (see §4.1 decision below), not Azure Cache for Redis.
- `modules/mcp-app.bicep`: **Azure Container App** hosting the MCP server image, **SystemAssigned MI**, environment variables wired up (`EXECUTOR=aca`, subscription / RG / region, group names, Redis host, Storage account / container).
- `modules/rbac.bicep` (requires principals to exist first):
  - MCP app MI → **Container Apps SandboxGroup Data Owner** (`c24cf47c-…`), on **both** sandbox groups (refer to example `namespace-sandbox-rbac.bicep`).
  - `diagnose-sp` → **Reader**, `action-sp` → **Contributor** (or tighter write permissions), scope driven by parameters (preserving today's "you choose the scope" approach).
  - MCP app MI → **Storage Blob Data Contributor** on the Storage Account.
  - On each worker app, add **`Microsoft.Graph/applications/federatedIdentityCredentials@v1.0`**, trusting its group MI (audience `api://AzureADTokenExchange`). ⚠️ *The exact `issuer`/`subject` for app-trusting-managed-identity (preview feature) needs to be verified against the Entra "configure an app to trust a managed identity" documentation + the inception lab example before implementation; `subject` = the group MI's object id.*
- `write-env.sh`: Produce the cloud `.env` (subscription, RG, region, group names, app ID, Redis, Storage, `EXECUTOR=aca`). Secrets are only needed for the local fallback SP path; the cloud path is passwordless.

#### §4.1 Decision: Redis as a Container in ACA, Not Azure Cache for Redis
**Conclusion: Running a `redis:7-alpine` container inside ACA is recommended; it's more cost-effective.**

- We only store two types of data, and **loss is self-healing**:
  - `session→sandbox` mapping (TTL 30min): Redis restart loses it → old sandboxes become orphans → reclaimed by the **1-hour idle auto-delete fallback**; the user gets a new sandbox on the next call.
  - User profile cache: Lost → re-derived on next login.
  - Therefore **no persistence / HA is needed**, and Azure Cache's lowest tier (Basic C0 ~$16/mo) charges for exactly this.
- Two deployment options:
  - **A (Recommended)**: A standalone internal-only Container App (internal ingress, TCP 6379), `minReplicas = 1`, MCP app accesses it via internal DNS.
  - **B**: As a **sidecar container** in the MCP Container App (multi-container app, `localhost:6379`). Saves one app, but Redis scales with MCP.
- ⚠️ Constraint: Redis **cannot scale-to-0** (connections would break, data lost), `minReplicas` must be = 1 → it's always on, not saving to zero, but still cheaper than Azure Cache. For persistence, optionally mount an Azure Files volume (usually not needed).

### Phase 3 — SandboxManager (SDK Integration, Sticky Routing, Bootstrap, Lifecycle)
> Responsibility overview in §3.4. Implementation details here.

- **Add `src/mcp-server/sandbox_manager.py`**, implementing `Executor`:
  - `get_or_create(ctx) -> SandboxClient`:
    1. Look up `session_sandbox[(oid, session_id, group)]` in Redis (Phase 4).
    2. Hit → `get_sandbox_client(id)` + `ensure_running()`; if `NotFound`, continue.
    3. Miss → `begin_create_sandbox(disk=<our image>, labels={user,session,group})`, write the id back to Redis with TTL, then **bootstrap** (done once per sandbox; mark `bootstrapped` in Redis):
       - **Passwordless login**: Obtain the group MI's token for `api://AzureADTokenExchange`, execute
         `az login --service-principal -u <sp-app-id> --tenant <tid> --federated-token <token> --allow-no-subscriptions`.
       - **Restore user profile** (Phase 4): `az account set --subscription <sub>` (+ default items).
       - **Mount Blob** (Phase 5): Mount blob volume, or `cd` into the synchronized workspace directory.
  - `exec(ctx, command)`: `get_or_create` then `.exec(command)`; on success, refresh Session TTL; map result to `ExecResult` (reuse truncation logic from `worker.py:45`).
  - `end_session(oid, session_id)`: `begin_delete_sandbox` for both groups; clear Redis keys.
- Specific sources to finalize in this phase: How to obtain the MI→`AzureADTokenExchange` token inside the sandbox (a bootstrap script using `ManagedIdentityCredential`); and the source of the user's subscription on first login (a subscription visible to the worker, or a configured default value).

### Phase 4 — Redis Layer (Session Routing + User Azure Profile)
- **Extend `src/mcp-server/cache.py`** (don't extend the backend interface — add typed views and `RedisBackend` alongside the existing sketch at `cache.py:42`):
  - `RedisBackend`: Implements `get/set` (the existing sketch), shared by all views.
  - `SessionSandboxCache`: `(oid, session_id, group) → sandbox_id`, **30-minute sliding TTL** (refreshed on each call) — this is both the source of Session stickiness and the signal for Session end.
  - `UserProfileCache`: `oid → {subscription_id, tenant_id, default_rg?}` — **strip the token**, store only durable profile metadata. Written on first login, restored for each new sandbox.
  - `GroupCache` remains unchanged (`cache.py:58`).
- **Session / Conversation Derivation** (add `src/mcp-server/session.py`, used by middleware):
  - `user_oid` from JWT (already exists).
  - `session_id`: Sliding window per user in Redis — if last activity < 30 minutes, **reuse the current Session** (even across multiple Conversations); otherwise, forge `sessionid + '_' + timestamp` as a new Session. (TBD: If the FastMCP transport layer's session id is stable enough, prefer it; otherwise, use the user-specified TTL heuristic as a fallback.)
  - `conversation_id`: Associated with the FastMCP request / session id in `Context`, **only used for Blob directory partitioning, not in the route key**. This is an identifier not natively carried by the MCP protocol; its source needs to be confirmed during implementation.

### Phase 5 — Blob Persistence (Per User / Session / Conversation, Stateless)
- **Add `src/mcp-server/blob.py`**: `azure-storage-blob` helper, writing to the `mcp-workspaces/{userid}/{sessionid_timestamp}/{conversationid}/` structure (session id suffixed with timestamp for easy time-based lookup).
- Primary approach: **Mount an Azure Blob volume** into the sandbox at creation time (`volumes=[...]`), scoped to that User / Session prefix, so files written by bash commands (python files, JSON profiles) are automatically persisted, keeping the sandbox stateless.
- Fallback: If volume prefix granularity is too coarse, use explicit `SandboxClient.read_file/write_file` ↔ Blob synchronization per Conversation.

### Phase 6 — Lifecycle Management (All at Session Level)
- **Session-level teardown (primary path, meets requirements)**: When a Session expires or ends, delete its two sandboxes (`SandboxManager.end_session`). Triggered by a lightweight reaper task in the MCP app that captures Redis Session key TTL expiry (scanning / expiry notifications), plus an explicit end hook on client disconnection.
- **1-hour idle fallback**: Set auto-delete / auto-suspend on each sandbox at creation time, so orphans missed by the reaper are still reclaimed after 1 hour without tool calls.
- **Warm pool (optional, cost-saving)**: Keep ~2 pre-built idle sandboxes (one per group) so the first call of a Session is sub-second; claim-then-replenish, true idle savings via scale-to-zero. Marked as a follow-up item after correctness is validated.

### Phase 7 — Image and Local Compose
- **Add `src/sandbox-image/Dockerfile`**: Disk image for the ACA sandbox — `az` CLI + python3 + jq + bootstrap script (FIC login + profile restore). **No FastAPI service** (the data plane is the transport layer, see §3.1). Push to an image registry, referenced by `begin_create_sandbox(disk=…)`.
- **Keep `src/worker/`** for the local path (still a FastAPI bash executor).
- **Modify `docker-compose.yml`**: Add a **redis** service for the local path; keep the two workers; add `EXECUTOR=local` + `REDIS_URL` to `mcp-server`.
- **Modify `src/mcp-server/requirements.txt`**: Add `azure-containerapps-sandbox`, `azure-identity`, `azure-mgmt-resource`, `azure-mgmt-authorization`, `azure-storage-blob`, `redis`.
- **Modify `.env.example`**: Add `EXECUTOR`, `AZURE_SUBSCRIPTION_ID`, `ACA_RESOURCE_GROUP`, `ACA_REGION`, `DIAGNOSE_SANDBOX_GROUP`, `ACTION_SANDBOX_GROUP`, `REDIS_URL`, `STORAGE_ACCOUNT`, `BLOB_CONTAINER`, and Session / idle TTL knobs.

### Phase 8 — Documentation
- Update root `README.md` (local vs ACA paths, new identity diagram), add `provisioning/aca/README.md`. Refresh `docs/` notes referencing the "two-worker model".

---

## V. Key File List

| Area | Path | Action |
|---|---|---|
| Backend interface | `src/mcp-server/executor.py` | Add |
| ACA SDK + stickiness + bootstrap + lifecycle | `src/mcp-server/sandbox_manager.py` | Add (implements `Executor`) |
| Session / Conversation keys | `src/mcp-server/session.py` | Add |
| Redis views + RedisBackend | `src/mcp-server/cache.py` | Extend |
| Blob persistence | `src/mcp-server/blob.py` | Add |
| Tool wiring | `src/mcp-server/main.py` | Modify |
| Dependencies | `src/mcp-server/requirements.txt` | Modify |
| Local provisioning (Bicep) | `provisioning/local/` | Add (move `provisioning/bicep/`) |
| ACA provisioning (Bicep) | `provisioning/aca/main.bicep` + `modules/*` | Add from scratch |
| Sandbox disk image | `src/sandbox-image/Dockerfile` | Add |
| Local stack | `docker-compose.yml`, `.env.example` | Modify |
| Delete | `provisioning/python/`, `provisioning/bicep/` | Delete after moving |

---

## VI. Verification

- **Local path unchanged**: `docker compose up --build` (now includes redis), log in as a diagnose-group user in VS Code, run a `diagnose_bash` → behavior identical to today (regression test for Executor refactoring).
- **ACA deployment**: `az deployment sub create -f provisioning/aca/main.bicep` deploys RG, two sandbox groups (with MIs), worker apps (with FICs), storage, redis container, MCP Container App (with MI and Data Owner on both groups). Confirm role propagation.
- **Session stickiness**: Send multiple `diagnose_bash` calls in **the same Session** (can span multiple Conversations), assert they all resolve to the **same** `sandbox_id` (check labels / MCP logs), while `action_bash` resolves to the **second** sandbox. A new Conversation within 30 minutes → still these two sandboxes; after 30 minutes (new Session) → new ones.
- **Passwordless**: `az account show` succeeds inside the sandbox with **no secrets** (FIC→SP).
- **Stateless**: End Session → both sandboxes are deleted; files written by the previous Session are gone from the sandbox but present in Blob (`userid/sessionid_ts/...`); a new Session restores the profile from Redis and files from Blob.
- **Idle reclamation**: Sandboxes with no calls for 1 hour are auto-deleted.

---

## VII. Open Items to Confirm During Implementation (Noted, Not Blocking the Plan)
1. Exact FIC `issuer`/`subject` for *app-trusting-managed-identity* (preview) — verify against Entra docs + inception lab.
2. Reliable source of `session_id` / `conversation_id` in the MCP / FastMCP layer vs TTL heuristic fallback.
3. Whether Blob volume prefix granularity is fine enough for per-Session isolation; otherwise, switch to read/write synchronization.
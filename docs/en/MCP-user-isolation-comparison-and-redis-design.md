# MCP User Isolation Comparison and Redis Design

This document answers three questions:

1. What are the channels of interference caused by multiple users sharing a worker (where `~/.azure` contamination is just one type), and what can each isolation scheme block — including a comparison table.
2. "Can different requests be hosted at the pod level?" — Yes, how, and at what cost.
3. How should Redis be designed: beyond the existing `groups:{oid}` cache (see `src/mcp-server/cache.py` and [MCP-Authentication-Cache and Credential Evolution.md](MCP-authentication-group-check-caching-and-credential-evolution.md)), what should and **should not** be stored; and several mandatory issues with the current worker container.

---

## 1. Interference Channel Panorama: Contamination is Not Just `~/.azure`

When sharing a resident worker pod and using bare `create_subprocess_shell` to execute arbitrary bash, interference between users falls into two categories:

**Category A: Correctness Crosstalk (benign concurrency can also collide)**

| Channel | Manifestation |
|---|---|
| `azureProfile.json` (active subscription) | After user A runs `az account set`, user B's commands land in A's subscription |
| `az configure --defaults` | A sets `group=rg-A location=eastus`, B silently inherits |
| `az cloud set` / active tenant | Switching cloud/tenant affects everyone |
| `az extension add` | Installed into the shared config dir `cliextensions/`, affects everyone |
| Fixed-path temporary files | The worker's TRUNCATE_HINT teaches the agent to write `az ... > /tmp/out.json` (`worker.py`'s `TRUNCATE_HINT`) — fixed path, concurrent execution causes mutual overwrite/misreading |

**Category B: Security Crosstalk (when the threat model includes a prompt-injection-compromised agent)**

| Channel | Manifestation |
|---|---|
| `/proc` snooping | `ps aux` sees SAS tokens/passwords in others' command lines; `cat /proc/<pid>/environ` reads others' process env (including `AZURE_CLIENT_SECRET`) |
| Shared filesystem | Reading others' output files, modifying `$HOME`, poisoning shared scripts |
| Background process residue | `nohup ... &` escapes `proc.communicate()`, persists across requests |
| Shared network namespace | Reverse shells, local port access, tunnels |
| IMDS / SA token | After enabling workload identity, any bash inside the pod can directly mint a token, bypassing all server-side logic |
| Resource exhaustion | Filling `/tmp`, fork bomb saturating the shared cgroup |

**Key judgment: Techniques like `AZURE_CONFIG_DIR` only cover Category A; Category B requires OS namespaces or pod boundaries.**

---

## 2. Candidate Isolation Schemes

### Scheme A: Current State — Shared Resident Pod + Bare Subprocess

No isolation. Both Category A and B are fully open. Only barely usable when "users are completely trustworthy + serial usage."

### Scheme B: Per-request `AZURE_CONFIG_DIR` (+ Redis Persistence of User Context)

Set `AZURE_CONFIG_DIR=/tmp/cfg/{request_id}` for each exec; the user's subscription/defaults land in a private directory; the binding relationship is stored in Redis (see §5.2), reconstructable across pods.

- Solves: All of Category A (config, defaults, cloud, extension; temporary files require a private cwd).
- Does not solve: All of Category B.
- Two implementation points:
  1. **The new config dir has no login state** — identity must be injected per-request: copy the SP login state (profile + token cache, millisecond-level) from a base dir, or use `az login --federated-token` under workload identity. Otherwise, az will directly report "Please run az login."
  2. Isolating the config dir = abandoning the shared token cache (the same SP could have reused tokens). Copying the base dir also brings the token cache along, which can mitigate this.

### Scheme C: B + Namespace Sandbox (bwrap / unshare), Resident Pod

The worker remains resident, but each exec uses `bubblewrap` (or `unshare`) to enter a completely new PID / mount / IPC namespace: private `/proc`, private tmpfs `/tmp`, read-only rootfs, non-root, cgroup limits, `--die-with-parent` ensures the entire process tree is killed on timeout. Startup overhead **~10–30ms**.

```bash
bwrap \
  --unshare-pid --unshare-ipc --unshare-uts \
  --proc /proc --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /lib /lib \
  --bind /tmp/cfg/$REQ /home/worker/.azure \
  --die-with-parent --new-session --uid 1000 \
  bash -c "$command"
```

- Solves: All of Category A + Category B's `/proc` snooping, file crosstalk, background residue, fork bomb.
- Does not solve: Shared kernel (kernel 0-days can escape), shared network ns (can add `--unshare-net`, but az needs outbound access, requiring slirp or keeping shared + NetworkPolicy to lock egress), IMDS/SA token still reachable from within the sandbox (az needs the token to work; this is essentially not a user isolation issue, see end of §4).
- **Deployment caveat**: Creating namespaces in a non-privileged container is blocked by Docker/containerd's default seccomp profile (`unshare`/`clone` restricted). On K8s, the worker pod needs a securityContext / custom seccompProfile to allow this; this must be verified before deployment — it's not guaranteed to work just by writing bwrap.

### Scheme D: Pod-per-request (Ephemeral Job/Pod) — "Hosting Different Requests at the Pod Level" Option 1

**Direct answer: Yes.** The MCP server, upon receiving a tool call, creates a one-time Pod via the K8s API (bound to the corresponding SP's workload identity), executes, and destroys it. This is "one pod per request."

- Solves: All of Category A and B **completely** (completely new kernel namespace family each time, new filesystem, independent network ns, independent cgroup); audit is naturally one-to-one; with gVisor/Kata RuntimeClass, even the kernel surface is isolated.
- Cost: **Cold start 2–10s** (scheduling + container startup + login); K8s API permissions granted to mcp-server (it goes from "only sends HTTP" to "can create pods," increasing control plane attack surface, requires a dedicated namespace + minimal RBAC to contain); purely pay-per-use, no idle cost.

### Scheme E: Warm Pool — Low-Latency Variant of D

Pre-warm 2–3 already-logged-in pods in a pool. On request, **grab a ready hot pod (milliseconds) → execute → remove from rotation, destroy asynchronously → replenish one in the background**. Cold starts still happen, but **not on the user's critical path**. During idle times (after work/weekends), it can scale-to-zero, with only the first request after idle experiencing a cold start.

- Isolation strength = D (each request gets a pod no one has used before).
- Cost: Idle cost of 2–3 small pods + a pool manager (write your own control loop or use an existing operator); this is the main complexity increase over D.

### Scheme F: Pod-per-user (Resident Pod per User, Sticky Routing) — "Hosting at the Pod Level" Option 2

Start a resident pod per user (oid), use session affinity to route the same user back to the same pod.

- Isolation **between** users = pod level, clean; the user's az state naturally persists, no need to rebuild.
- But: ① Cost scales linearly with the number of users, large idle waste (you mentioned "lean team" is not feasible); ② **Concurrent requests from the same user still share the pod**, Category A crosstalk still exists in single-user multi-agent scenarios; ③ Permission revocation/reclamation logic must be managed manually.

### Modifier G: gVisor / Kata RuntimeClass

Not an independent scheme, but a kernel-surface hardening added to D/E/F: eliminates the residual risk of "shared kernel." Only worthwhile when multi-tenants do not trust each other; not necessary for this team at the current stage.

---

## 3. Comparison Table

### 3.1 Capability Matrix (Blocks ✅ / Partial ⚠️ / Does Not Block ❌)

| Interference Channel | A Current | B config-dir | C +bwrap | D pod/request | E warm pool | F pod/user |
|---|---|---|---|---|---|---|
| subscription / defaults / cloud / extension contamination | ❌ | ✅ | ✅ | ✅ | ✅ | ⚠️ Single-user concurrency still collides |
| `/tmp` etc. shared file crosstalk | ❌ | ⚠️ Only config dir | ✅ Private tmpfs | ✅ | ✅ | ⚠️ Between users ✅, within user ❌ |
| `/proc` snooping (steal argv/env) | ❌ | ❌ | ✅ New PID ns | ✅ | ✅ | ⚠️ Same as above |
| Background process residue / timeout not fully killed | ❌ | ❌ | ✅ die-with-parent | ✅ Pod destroyed | ✅ | ❌ Pod resident |
| Fork bomb / resource exhaustion | ❌ | ❌ | ✅ cgroup | ✅ pod limit | ✅ | ⚠️ Impact limited to that user |
| Network ns isolation | ❌ | ❌ | ⚠️ Optional but tricky | ✅ | ✅ | ✅ Between users |
| Kernel 0-day escape | ❌ | ❌ | ❌ Shared kernel | ⚠️ +gVisor→✅ | ⚠️ Same left | ⚠️ Same left |
| IMDS / SA token reachable | ❌ | ❌ | ❌ | ❌* | ❌* | ❌* |

\* All users share the same worker SP; "the SP token is reachable inside the sandbox" is **by design** (az needs it to work), not a user isolation problem — it belongs to the credential lifecycle problem, solved by workload identity + short TTL tokens + auditing (see auth doc §4). Do not expect any isolation scheme to "conveniently" solve it.

### 3.2 Cost / Latency / Complexity

| Dimension | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| Additional latency per call | 0 | ~0 (copy config millisecond-level) | +10–30ms | **+2–10s** | ~ms (pool hit) | First cold, subsequent 0 |
| Idle cost | 1 pod/type | Same as A | Same as A | **≈0** (pure pay-per-use) | 2–3 hot standby pods | **∝ number of users** |
| Operational complexity | Low | Low (+Redis read/write) | Medium (seccomp/securityContext verification) | Medium (K8s API + RBAC) | Medium-High (pool manager) | Medium (sticky + reclamation) |
| Multi-replica consistency | — | Requires Redis reconstruction (§5.2) | Same as B | Natural (stateless) | Natural | Sticky routing solves |
| Audit correspondence | Weak | Weak | Medium (per-exec) | **Strong (pod=request)** | Strong | Medium |

### 3.3 Recommended Combination (Split by Worker Type, Not a Single Global Choice)

| Worker | Frequency/Risk | Recommendation | Reason |
|---|---|---|---|
| **diagnose** | High frequency, read-only, low risk | **C (resident + bwrap) + B (per-request config dir)** | Latency ~0, cost of one pod; namespace isolation is more than sufficient for read operations |
| **action** | Low frequency, write, high risk | **D (ephemeral pod) or E (1 hot standby)** | Write operations don't mind waiting a few extra seconds, in exchange for full isolation + pod=request auditing; lean version starts with D, upgrade to E with 1 hot standby if latency becomes an issue |

> Evolution order: Now (compose/single pod) implement B+C first; after moving to K8s, switch action to D; only when multi-tenancy is truly needed, consider G.
> F (pod-per-user) has no optimal position in this scenario: expensive to keep resident, yet cannot solve single-user concurrency; not recommended.

---

## 4. Two Points to Note for "Hosting Different Requests at the Pod Level"

1. **Scheme B is a prerequisite for D/E, not an adversary.** Pod-per-request means no local state can survive a request — the user's subscription binding **must** be externalized (Redis), otherwise every new pod is amnesic. Therefore, the `usercfg` design in §5.2 must be done regardless of which route is chosen.
2. **Identity binding uses oid, not the client-provided request_id.** There is a pitfall with "request ID is carried into the HTTP request, first check Redis": if the request_id is generated/passed by the client, it means the client is self-identifying — it can be forged, or collide with another user's context. Correct approach: **The MCP server takes `oid` from the verified JWT as the binding key** (the server already has this, see `UserAuthMiddleware` in `main.py`); the request_id is only used as a server-generated trace/audit correlation ID, and does not participate in any lookup authorization.

---

## 5. Redis Design

### 5.1 Current State Review (Already Implemented, see cache.py)

- Two-layer structure (`CacheBackend` protocol + `GroupCache` typed view), key prefix isolation, JSON-safe values — **this skeleton is correct; adding new caches = adding new views, without changing the backend interface.**
- Already stored: `groups:{oid}` → subset of KNOWN_GROUPS the user belongs to, TTL 300s.

### 5.2 What Else Should Be Stored (Ordered by Value)

**① `usercfg:{oid}` — User Execution Context (This is State, Not Cache)**

Solves the cross-request/cross-pod persistence of `az account set` — i.e., the scheme you proposed, but with the key changed to oid (§4).

```
usercfg:{oid} = { "subscription": "...", "defaults": {"group": "...", "location": "..."} }
TTL: 7d sliding (or none, LRU eviction fallback)
```

Read/Write protocol (**Only mcp-server touches Redis; the worker remains without Redis credentials** — keep the data plane "dumb"; do not give another set of infrastructure credentials to a container that executes arbitrary bash):

```
mcp-server (tools/call):
  usercfg = redis.get(usercfg:{oid})
  POST worker/exec { command, timeout, request_id, usercfg }

worker:
  cfg = /tmp/cfg/{request_id}        # private directory
  Copy profile + token cache from base login state (millisecond-level)
  If usercfg.subscription exists: az account set --subscription ...   # pure local file write, no network
  Run command with AZURE_CONFIG_DIR=cfg, minimal env whitelist
  After execution, read cfg/azureProfile.json to get active subscription
  Return { exit_code, stdout, stderr, context: {subscription} }

mcp-server:
  If context.subscription changed → redis.set(usercfg:{oid})
```

> Use "read azureProfile.json after execution" to capture state, rather than the server-side regex parsing of `az account set` — the file is the source of truth; parsing the command string is fragile (`az account set` could be hidden in a loop, variable, or subshell).

**② `audit` — Audit Event Stream (Redis Stream as Buffer, Not Final Destination)**

Currently, auditing is only `logger.info` to stdout (`main.py:120,188`), lost on pod restart, and `action_bash` only records **before** execution, not the result — the audit chain is incomplete.

```
XADD audit MAXLEN ~ 100000 * \
  ts <server time> oid <oid> tool action_bash request_id <id> \
  command <...> explanation <...> exit_code <rc> duration_ms <ms> truncated <bool>
```

- Record **two entries** per tool call (received / completed) or one complete record containing the result;
- A consumer asynchronously moves data to Log Analytics / blob (append-only). **Redis is not a reliable audit store** (non-persistent by default); it is only a decoupling buffer here — the true system of record is external.

**③ `ratelimit:{oid}:{tool}:{window}` — Rate Limiting/Quota (Stop-loss Valve for Runaway Agents)**

Agent infinite retry loops are a real risk: saturating workers, hitting Graph/ARM rate limits, rapid-fire high-risk write operations. `INCR` + `EX` is sufficient:

```
diagnose_bash: 60/min/user (lenient, only prevents loss of control)
action_bash:   10/min/user (write operations have no legitimate reason to be this frequent)
Exceeded → return structured error, prompt agent to back off
```

**④ `idem:{oid}:{sha256(command)[:16]}` — Action Write Operation Idempotency Guard (Optional)**

Under agent retry semantics, running the same `az datafactory pipeline create-run` twice = data duplication. `SET NX EX 60`: if the same user issues the same command within 60s, **do not execute**, return "identical write executed Ns ago; rerun intentionally? change command or wait". Note this is an **advisory guardrail** (legitimate "intentional reruns" will be blocked once), not strong-consistency deduplication — do not treat it as a correctness guarantee.

**⑤ `revoked:{oid}` — Instant Revocation Switch + Global Circuit Breaker**

Fills the TTL revocation delay gap (already documented in auth doc §3): when kicking a user, an admin operation does `DEL groups:{oid}` + `SET revoked:{oid} 1`; `require_action` checks this flag **before** checking the group cache. Additionally, add a global `pause:action` flag to freeze all write operations during an incident. Cost is nearly zero, providing an emergency handle "beyond the 5-minute TTL."

**⑥ MCP Session State (Directional Record for Multi-replica Control Plane)**

Streamable HTTP sessions are stateful; with multiple mcp-server replicas, either use session affinity or externalize the session. FastMCP's support for external session stores **must be verified against the version documentation before implementation**; this is only a directional note.

### 5.3 What Should NOT Go into Redis (Equally Important)

| Item | Why Not |
|---|---|
| OBO-exchanged Graph token / user JWT | Expands credential attack surface; MSAL's in-process `TokenCache` is sufficient. If cross-replica token sharing is truly needed, it must be encrypted + very short TTL — not done by default |
| SP client secret | Never, in any form |
| Command stdout/stderr | Large, contains data-plane sensitive content, low reuse rate; large output goes to worker local files (existing TRUNCATE_HINT approach), not Redis |
| Data-plane query results (e.g., factory list) | Agent re-querying is cheap; caching introduces staleness judgment overhead |

### 5.4 Failure Semantics: What Happens When Redis is Down (Decide fail-open / fail-closed per Key Type)

| Key | When Redis is Unavailable |
|---|---|
| `groups:*` | **fail-open to source**: treat as miss, query Graph directly (this is a cache, the source still exists) |
| `usercfg:*` | Treat as no binding: execute in a fresh context, and prompt the agent "defaults not restored" in the return |
| `ratelimit:*` | diagnose **fail-open** (availability first); action **fail-closed** (prefer to deny high-risk operations) |
| `revoked:*` / `pause:*` | action **fail-closed**; diagnose fail-open |

### 5.5 TTL / Revocation Supplement

For the issue that a single `groups` cache TTL=300s is too long for actions, **prioritize using the revoked flag from §5.2-⑤ for emergency revocation**, rather than creating a separate shorter TTL cache for actions (dual TTLs would require writing the same Graph query twice, not worth the mental overhead). If the organization requires "regular revocation must also take effect within <5min," then globally reduce the TTL.

---

## 6. Worker Container Review: Four Mandatory Issues

Combined with the design above, `src/worker/` currently has four specific issues, independent of the isolation scheme, that should be fixed separately:

1. **`/exec` has no authentication at all** (`worker.py:51`). Under compose, the worker port is not published to the host, which is acceptable; on K8s, any pod that can reach the ClusterIP can directly call the worker to execute arbitrary commands, **bypassing all identity/group checks of the MCP server**. Must: NetworkPolicy to restrict access to only-from mcp-server, plus a shared secret header or mTLS (defense in depth).
2. **Timeout only kills the shell, not the process tree** (`worker.py:62`'s `proc.kill()`). `create_subprocess_shell` starts `sh -c`; `kill()` only kills `sh`, leaving the `az` (python process) and `&` background child processes **alive and orphaned**. Fix: `start_new_session=True` + `os.killpg(os.getpgid(proc.pid), SIGKILL)`; with bwrap, `--die-with-parent` naturally solves this.
3. **Child process inherits the full environment, including `AZURE_CLIENT_SECRET`** (the secret remains in the environment after entrypoint login, and `create_subprocess_shell` inherits by default). Fix: pass a minimal env whitelist explicitly during exec: `env={"PATH":..., "HOME":..., "AZURE_CONFIG_DIR":...}` — more thorough than `unset` in the entrypoint (unset still requires worrying about the uvicorn process tree inheritance chain; a whitelist handles it in one place). The ultimate solution is still workload identity, making the secret non-existent.
4. **TRUNCATE_HINT teaches the agent to write to a fixed path** (`worker.py:32-37`'s `/tmp/out.json` example). Concurrent users overwrite/misread each other's files. Fix: change the prompt to use `mktemp` style (`az ... > $(mktemp /tmp/out.XXXX.json)`); after implementing per-request private tmpfs (Scheme C/D), this problem naturally disappears, but fix the prompt first, at zero cost.

Two additional points connecting to the §5 design:

- `ExecRequest` needs additional fields: `request_id` (server-generated, for audit correlation) + `usercfg` (server reads from Redis and passes down). The worker itself **does not** connect to Redis.
- The worker response should include `context: {subscription}` (read from `azureProfile.json` after execution), for the server to write back to `usercfg:{oid}`.

---

## 7. Roadmap (From Lean to Rich)

| Phase | Action | What You Get |
|---|---|---|
| **Now (compose)** | Fix §6 items 2/3/4 (process group kill, env whitelist, mktemp prompt); implement per-request `AZURE_CONFIG_DIR` in worker (Scheme B, binding key uses oid) | Category A crosstalk eliminated; secret no longer lies in child process env |
| **+1 week** | Wrap diagnose worker with bwrap (Scheme C); bring up Redis pod, implement `RedisBackend`, deploy `usercfg` / `ratelimit` / `revoked` key types | Category B's /proc, file, background residue blocked; emergency revocation handle available; runaway agent stop-loss in place |
| **When moving to K8s** | Switch action to ephemeral pod (Scheme D) + workload identity to eliminate both types of secrets; add NetworkPolicy + mTLS to worker (§6-1); audit stream → Log Analytics | Full isolation for write operations + pod=request auditing; credential leakage surface reduced to zero; auditable traceability |
| **When multi-tenancy arrives** | Add warm pool for action (Scheme E) + gVisor RuntimeClass (G) | Kernel-level isolation between untrusted tenants, without users suffering cold starts |

---

## 8. One-Sentence Summary

> **The config-dir technique handles "correctness," namespace/pod handles "security"; diagnose uses zero-latency bwrap sandbox, action uses latency-tolerant ephemeral pods. In Redis: `groups` is cache, `usercfg` is state, `audit` is buffer, `ratelimit`/`revoked` are valves — four different semantics, each with a distinct failure strategy; tokens and command output never enter Redis. The binding key is always the verified oid, never the client-reported request_id.**

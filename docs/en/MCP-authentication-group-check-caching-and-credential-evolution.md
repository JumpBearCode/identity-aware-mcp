# MCP Authentication: Group Check, Caching, and Credential Evolution

This document records the current state of OAuth + OBO + group authentication in `src/mcp-server/main.py`, the rationale behind several changes, and the direction for credential evolution that has not yet been implemented. The authentication model references Pamela Fox's
[Building MCP servers with Entra ID and OBO](https://blog.pamelafox.org/2026/04/building-mcp-servers-with-entra-id-and.html).

---

## 1. Authentication Model Overview

```
Client ──(Entra user token, HTTP Bearer)──► MCP server
                                              │ 1) AzureJWTVerifier validates JWT (using Entra public key)
                                              │ 2) OBO exchanges for Graph token (MSAL)
                                              │ 3) checkMemberGroups checks if user is in group
                                              ▼
                                 In group → expose/execute tools
                                 Not in group → tools hidden + direct call returns not-found
              MCP server itself has no data-plane permissions; forwards to worker container for execution
```

- **JWT Validation**: `AzureJWTVerifier` + `RemoteAuthProvider`, only verifies signature, no app secret required.
- **Tool-level Authentication**: `@mcp.tool(auth=require_xxx)` is a **FastMCP 3.0** feature. When the check returns `False`, the tool is **both hidden from `tools/list` and returns not-found on direct call** — this is the ideal semantics of "not in group = not exposed / no authorization".
  - ⚠️ Therefore `requirements.txt` must have `fastmcp>=3.0` (`AuthContext`, `auth=` are only available from 3.0 onwards).
  - ⚠️ Tool-level `auth=` only works under **HTTP transport**. stdio mode has no OAuth layer, `get_access_token()` returns `None`, and **all checks are skipped**. Production must use `mcp.http_app()`.

---

## 2. Group Check: Why checkMemberGroups

**Before**: `GET /me/transitiveMemberOf/...` fetched **all groups** the user belongs to and then evaluated. Issues:

- Payload grows with the number of user groups;
- Pagination exists (`@odata.nextLink`), which can **miss groups** when there are many.

**Now**: `POST /me/checkMemberGroups`, only asks "is the user in these specific groups I pass in".

- Fixed-size payload, **no pagination**;
- Membership is computed server-side by Graph (still transitive);
- Maximum of 20 groups per call (we only have 2, more than enough).

```python
POST https://graph.microsoft.com/v1.0/me/checkMemberGroups
{ "groupIds": ["<DIAGNOSE_GROUP_ID>", "<ACTION_GROUP_ID>"] }
→ Returns only the IDs the user is actually in
```

---

## 3. Group Caching: Interface + In-Process TTL (Redis Reserved)

### Why Caching is Needed

FastMCP's `auth=` check **runs at both list and call time**, and `tools/list` runs **once for each tool**. Without caching:

- One `tools/list` (2 tools) = 2 OBO/Graph calls;
- Each `tools/call` = 1 call.

User group membership rarely changes; these repetitions are pure waste and can hit Graph rate limits under high volume.

> Note: The Graph token obtained via OBO is already cached by MSAL's `TokenCache`. The repeated call is the `checkMemberGroups` HTTP request. We are caching the **group result set**, not the token.

### Design: Standalone `cache.py`, Two Layers

Caching is extracted into a standalone file `src/mcp-server/cache.py`, with two layers (intentionally designed for easy extension + Redis migration):

1. **Generic Backend `CacheBackend`**: A TTL key-value store with `get/set` methods, TTL set at construction time. Currently `InMemoryBackend` (wraps `cachetools.TTLCache`, **TTL=300s**); future Redis implementation will use a `RedisBackend` (same interface), **callers remain unchanged**.
2. **Typed View `GroupCache`**: Built on top of the backend, `oid -> set[str]`. Keys are prefixed with `groups:`, values stored as lists (JSON-safe, symmetric behavior across both backends).

```python
# main.py
group_cache = GroupCache(InMemoryBackend(ttl=GROUP_CACHE_TTL))
```

- `_user_groups(ctx)` on a miss resolves **all KNOWN_GROUPS in one checkMemberGroups call** and writes to cache; so the second tool in a single `tools/list` call hits the cache directly → **2 Graph calls reduced to 1**.

### Storing More Data (Extensibility)

**Do not extend the backend interface**. Instead, **add another typed view** in `cache.py`, sharing the same backend / same Redis. For example, to cache other per-user data in the future:

```python
class SomethingCache:
    def __init__(self, backend): self._b = backend
    async def get(self, k): ...   # Use a different key prefix, e.g., "something:{k}"
    async def set(self, k, v): ...
```

After spinning up a Redis pod in K8s, all views share that single `RedisBackend` instance, isolated by key prefix.

### "Request" vs "Session" Scope (Common Confusion)

- FastMCP's `Context` (`fastmcp_context`) is **per-request**: one context per `tools/list`, another per `tools/call`.
- If results are only stored in `fastmcp_context.state`, they are **reused only within a single request** (no cross-request savings) — this is "Option A".
- The current `group_cache` is **process-level + TTL**, **saving across requests too** (Option B), covering Option A as well.

### TTL Trade-off: Revocation Delay

TTL=300s means that after removing someone from a group, they can still use it for up to 5 minutes.

- Read-only `diagnose` is not critical;
- Destructive `action` can have a **shorter TTL, no caching, or active invalidation** if faster revocation is required.

### Upgrading to Redis (Future, Multi-Pod K8s)

In-process caching means **each pod caches independently**: each pod queries on first encounter with a user, and expiration times differ across pods after revocation. When running multiple replicas, implement a `RedisBackend` (same `CacheBackend` interface), change one line in `main.py` to `group_cache = GroupCache(RedisBackend(...))`, and **business code stays unchanged** (skeleton comments already in `cache.py`):

```python
class RedisBackend:
    async def get(self, key): ...   # GET mcp:{key}
    async def set(self, key, value): ...  # SET mcp:{key} ... EX ttl
```

- Redis solves "**multi-pod sharing + reduced calls + consistent revocation**";
- But **revocation real-time behavior is still determined by TTL**, Redis does not make it instant.
- Stores `oid -> [group_ids]`, no sensitive data; still recommended to set Redis key TTL as a safety net.

**Conclusion: No need for aggressive caching before hitting rate limits. The interface is ready; upgrade cost is zero.**

---

## 4. Credential Issue: Client Secret Leakage (Not Yet Implemented, Direction Only)

### Problem

Currently OBO uses `MCP_CLIENT_SECRET` (client secret). **As long as the entire stack runs on the user's own machine** (user runs `docker run`):

- The secret is in the user's container → the user can obtain it and issue arbitrary OBO calls themselves;
- The worker's Azure SP credential is also local → they can bypass the MCP server and call Azure directly;
- They can even modify the code to disable the check.

→ **In this scenario, group authentication is effectively meaningless for this user. Whoever holds the secret is the true trust boundary.**

### Direction

| Environment | Credential Strategy |
|---|---|
| **local (test)** | Continue using `MCP_CLIENT_SECRET`, stored in local `.env` (not committed). It is acceptable for dev to hold their own secret — within the trust boundary. |
| **prod (centrally hosted in Azure)** | **Managed Identity / Workload Identity Federation**: Configure the app registration with a federated credential trusting the service's Managed Identity. MSAL uses the token issued by MI as a client assertion, **no secret stored in the container**. Similarly, replace the worker's SP with MI. |

Key point: **"Centrally hosted + users connect only via HTTP" is the prerequisite for group authentication to be truly effective**; once centrally hosted, switching the secret to Managed Identity completely eliminates the leakage surface (no secret to leak, no rotation needed).

### Implementation Notes (To-Do)

- MSAL Python **1.29.0+** supports using Managed Identity as a Federated Identity Credential (FIC); the exact way to pass `client_credential` should follow the official documentation at implementation time (class names/signatures vary by version, verify before implementation).
- Code can be made **dual-mode**: if `MCP_CLIENT_SECRET` is present, use secret (local); otherwise, use MI (Azure). The same code can run in both environments.

> Current Status: **Not implemented**, `MCP_CLIENT_SECRET` is still a required environment variable. Using a secret for local testing is acceptable.

---

## 5. Change Summary

| Item | Status |
|---|---|
| `requirements.txt` locked to `fastmcp>=3.0` | ✅ Done |
| Group check switched to `checkMemberGroups` (removed full fetch/pagination risk) | ✅ Done |
| Group caching: `GroupCache` interface + `InMemoryGroupCache` (TTL=300) | ✅ Done |
| Redis cache implementation | ⏳ Interface reserved, implement when multi-pod |
| Managed Identity replacing client secret | ⏳ Not implemented, direction only recorded |
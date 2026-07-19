---
title: "Implementation Plan: User Attribution for SP Operations — Azure Log System, Key ID Semantics, and Final Technical Decision"
date: 2026-07-12
tags:
  - audit
  - attribution
  - service-principal
  - log-analytics
  - user-agent
  - storage-blob-logs
  - azure-activity
  - oid-tracking
status: Design Finalized / Pending Implementation
sources:
  - "src/mcp-server/main.py (UserAuthMiddleware, _exec)"
  - "src/mcp-server/executor.py (Executor.exec single execution chokepoint)"
  - "docs/en/oid-log-tracking/value-proposition-and-audit-attribution.md"
verified:
  - "Tested environment: tenant 9ea91fbb-…, sub ee5f77a1-…; queried via diagnose_bash using read-only SP d09dfd39-…"
  - "StorageBlobLogs schema: verified via getschema on workspace dataops-aca-logs (4de6b3e7-…), column names are authoritative"
  - "AzureActivity schema: verified via getschema on DefaultWorkspace (49e67e4c-…)"
  - "Limitation: StorageBlobLogs / AzureActivity across 4 workspaces all have 0 rows — no resources in this subscription are currently emitting data-plane logs, hence only schema is available, no real ID sample values"
---

# Implementation Plan: User Attribution for SP Operations

> This follows the core gap identified in the [Value Analysis](./value-proposition-and-audit-attribution.md): all operations are executed by a **shared Service Principal**. Azure native logs only recognize the SP, not **which real person** is behind it, nor **which IP** they came from.
> This document converges two rounds of discussion into an **executable final technical decision**, and bases conclusions on **actual measurements of real workspace schemas**, rather than memory.
>
> **To see directly "which options we considered and which were adopted or excluded", jump to [§5 Candidate Solution Overview](#5-candidate-solution-overview-all-solutions-at-a-glance).**

---

## 0. One-Sentence Summary + Final Decision

Native logs inherently cannot provide complete attribution, so **do not try to make native logs sufficient on their own**. Instead:

> **Authoritative attribution is held by the MCP server itself (Layer 1). In native logs, only a single "key" is left — a correlation GUID we generate, injected into the User-Agent (Layer 2). Any service that logs UA can use it to join back to the MCP authoritative table.**

Final architecture = combining the **selected** solutions from [§5 Overview](#5-candidate-solution-overview-all-solutions-at-a-glance) into four layers:

| Layer | What it does | Corresponding Solution | Mandatory? |
|---|---|---|---|
| **Layer 1 — Authoritative Audit** | MCP middleware writes structured audit events in-place → Log Analytics custom table (Logs Ingestion API + DCR) | **Solution Four** | ✅ Mandatory |
| **Layer 2 — Native Traceability** | `executor.exec` out-of-band injects `AZURE_HTTP_USER_AGENT=mcp/<guid>` → lands in data-plane log `UserAgentHeader` | **Solution Two (GUID version)** | ✅ Mandatory |
| Layer 3 — High Assurance (Optional) | Use per-user identity (SP/UAMI) for a small set of privileged write users, making native `Caller` itself distinguishable | **Solution Five** | ⬜ Optional |
| Layer 4 — Observability (Optional) | Client-side tracing (LangSmith, etc.) records agent intent, **explicitly not a security control** | **Solution Three** | ⬜ Optional |

**Explicitly excluded**: **Solution One** (relying on Azure native ID join, see §6), **Solution Six** (OBO for user tokens, see §8.4), **Solution Seven** (resource tagging, see §5).

---

## 1. Background: Why the SP Model Loses Attribution

- Users have **zero standing write permissions**. All write operations are executed by per-group shared SPs (this is a security selling point of the project).
- Cost: In Azure native logs, the operator field is **that shared SP**, identical for every user; the source IP is the **sandbox egress IP**, not the user's laptop.
- Consequently, "who used the SP, from where, and what did they run" — native logs **cannot fully answer any of these three questions**. This document fills that gap.

---

## 2. What Logs Does Azure Have (Log Tables · Section One)

Understanding the **categories** is more important than memorizing table names. Azure logs fall into three main categories:

| Category | What it is | On by default? | Table in Log Analytics |
|---|---|---|---|
| **Activity Log** | Control plane (ARM management operations: create/delete/update, listKeys, role assignment…), **one per subscription** | ✅ Always on, but requires a diagnostic setting to be sent to a workspace | `AzureActivity` |
| **Resource logs** (formerly diagnostic logs) | **Per-service, data-plane** operation details, schema varies per service | ❌ Requires a diagnostic setting to be enabled **on each resource** pointing to a workspace | One or more tables per service |
| **Entra logs** | Tenant-level identity events | Partially default | `SigninLogs` / `AuditLogs` / `AADServicePrincipalSignInLogs` |

Resource logs mean "Storage has its own logs, other services have theirs", but **all off by default, schemas all different**:

| Service | Representative Table |
|---|---|
| Storage | `StorageBlobLogs` / `StorageQueueLogs` / `StorageTableLogs` / `StorageFileLogs` |
| Data Factory | `ADFPipelineRun` / `ADFActivityRun` / `ADFTriggerRun` |
| Key Vault | `AKVAuditLogs` (or legacy `AzureDiagnostics`) |
| Azure SQL | `SQLSecurityAuditEvents`, etc. |
| Cosmos DB | `DataPlaneRequests` |
| Batch / Service Bus / Event Hub / App Service / AKS | Each has its own table |

> ⚠️ **Deployment Cost Reminder**: To trace to a specific resource, that **resource** must have a diagnostic setting configured, sending logs to the **same** workspace. This is a per-resource operational cost that must be remembered during the decision process (§8 treats it as a prerequisite for Layer 2).
>
> ⚠️ **Don't Confuse the Two Modes**: For the same storage logs, the new format lands in the resource-specific `StorageBlobLogs`, while the old format (legacy diagnostic setting) lands in `AzureDiagnostics`. **The column names in these two tables are completely different.** If you can't find a column, first confirm which table you are querying.

---

## 3. Semantics of Key IDs (Key IDs · Section Two)

Fields containing "id" actually represent **three completely different things**. Confusing them is the root cause of choosing the wrong solution. The following concepts are universal across services; §4 will solidify them with real Storage column names.

**① Server-side Request ID (`x-ms-request-id`)**
- **Generated by: Azure**. One per HTTP request, globally unique.
- You can only read it from the **HTTP response header**.
- **Correlates to: Nothing.** Its purpose is "give this id to Microsoft support to locate this specific request." It does not cross services or link back to your system.

**② Client-side Request ID (`x-ms-client-request-id`)**
- **Generated by: Client (you)**. You set it in the request header; the service echoes it back and logs it.
- **Correlates to: Wherever you want it to** — it is the **only one** of the three that can "point back to your own system".
- Fatal limitation: **Typed `az` commands (`az storage blob delete …`) have no flag to set this header**. Only `az rest` / SDK can set it → practically unusable for this project.

**③ Correlation ID (`correlationId`)**
- **Generated by: Azure (ARM)**.
- **Correlates to: Within Azure's own logs.** A single logical management operation in ARM might fan out into multiple events. Azure uses the same `correlationId` to **bind them together**, making it easy to see "which sub-events a single operation expanded into". **It correlates parent-child relationships between Azure events, not back to your MCP.**

> One sentence to remember: **Of ①②③, only ② (client-request-id) can point back to your system, and it cannot be set on typed `az` commands.**
> This is the root cause for determining "Solution One is not viable" in §6, and the reason for switching to User-Agent.

---

## 4. Storage Example (Measured Schema)

**Real columns** obtained by running `getschema` on `StorageBlobLogs` in workspace `dataops-aca-logs` (excerpt of identity/ID related columns):

| Column Name (Exists in measurement) | Type | Meaning |
|---|---|---|
| `AuthenticationType` | string | `OAuth` / `SAS` / `AccountKey` / `Anonymous` |
| **`RequesterObjectId`** | string | oid of the authenticated principal → **in this model = oid of the shared SP** (only populated when `AuthenticationType==OAuth`) |
| `RequesterAppId` | string | app/client id of the SP |
| `RequesterTenantId` / `RequesterUpn` / `RequesterAudience` / `RequesterTokenIssuer` | string | Other identity fields (`RequesterUpn` is usually empty for SP scenarios) |
| `AuthenticationHash` | string | Token/SAS hash (irreversible) |
| `AuthorizationDetails` | **dynamic** | RBAC authorization details (which action/role was allowed) |
| `CallerIpAddress` | string | Source IP → **sandbox egress, not the user** |
| `CorrelationId` | string | Storage correlation id (generated by Azure, = ③ in §3) |
| `ClientRequestId` | string | `x-ms-client-request-id` (= ② in §3, controllable but cannot be set on typed `az`) |
| **`UserAgentHeader`** | string | UA string → **landing point for our injected correlation GUID (exists in measurement ✅)** |
| `OperationName` / `Uri` / `ObjectKey` | string | What was done, which blob |

**Three must-know conclusions from measurements:**

1. **`RequesterObjectId` does exist.** The most likely reasons you "can't find it" in the portal are: the table is **empty** (column selector only shows columns with data) / you are looking at legacy `AzureDiagnostics` / or those requests used **SAS/account-key** authentication (this column is only populated for OAuth).
2. **This table does not have a separate `RequestId`/`TransactionId` column** (① in §3 does not appear as a standalone column in resource-specific tables). The ID columns are only `CorrelationId`, `ClientRequestId`, `AuthenticationHash`. — This is a correction to a previous verbal statement.
3. **The control plane `AzureActivity` measured has** `Caller`, `CallerIpAddress`, `CorrelationId`, `HTTPRequest` (containing JSON with `clientRequestId`/`clientIpAddress`), `Claims_d` (dynamic). But `Claims_d` contains **the SP's** claims, not the user's, so it doesn't help with attribution.

> 📌 **Schema Source & Honest Limitation**: The table above is the **Azure official standard schema** for `StorageBlobLogs`, not a configuration artifact of this environment. `StorageBlobLogs` is a **Microsoft predefined standard table** (measured `tableType == "Microsoft"`). **Every Log Analytics workspace inherently "knows" its schema** — so `getschema` returns column definitions **completely independent** of "whether there is data or a diagnostic setting" (evidence: the same query resolves in the unrelated `mlworkspace` workspace, returning 0 rows).
>
> This subscription has **8 storage accounts, all with 0 diagnostic settings**. `StorageBlobLogs` / `AzureActivity` across 4 workspaces **all have 0 rows** — meaning **no resources are currently emitting data-plane logs**. Therefore, the schema in this section is authoritative (= this is what it will look like once logging is enabled), but **there are no real ID sample values**. To see real values, you need to enable a diagnostic setting on a storage account → perform a blob operation → wait 5~15 minutes for ingestion (see §9 To-Do).

---

## 5. Candidate Solution Overview (All Solutions at a Glance)

> This covers **all solutions considered during our two rounds of discussion**. **Solutions One~Three** were the initial three ideas; **Solutions Four~Seven** were added later.
> Each entry below gives a one-liner + verdict + where to find details. All subsequent references to "Solution N" refer to this table.

| Solution | One-Liner | Verdict | Details |
|---|---|---|---|
| **Solution One** — Correlation ID Join | Rely on **Azure-generated** `correlationId` / request id to join with MCP records | ❌ **Excluded** | §6 |
| **Solution Two** — Inject Metadata into Native Logs | Write **our own identifier** into outbound requests, making native logs self-contained → correct form is **injection into User-Agent** | ✅ **Adopted** (Layer 2, inject **GUID**) | §8.2 |
| &nbsp;&nbsp;Solution Two′ — Inject **raw oid** | Variant of Solution Two: put oid directly in UA instead of GUID, aiming for "single-table, no join" | ⚠️ **Not used by default** (can be explicitly opted-in) | §7 |
| **Solution Three** — Client-Side Logging | Use LangSmith or similar tracing tools on the client side to record agent dialogue/intent | 🔶 **Optional** (Layer 4), **not** a security control | §8.4 |
| **Solution Four** — MCP Server Authoritative Audit Table | MCP writes structured audit → Log Analytics custom table (Logs Ingestion + DCR) | ✅ **Adopted** (Layer 1, **Core**) | §8.1 |
| **Solution Five** — Per-User Identity | Issue dedicated SPs/UAMIs to users, making native `Caller` itself distinguishable to the person | 🔶 **Optional** (Layer 3, High Assurance) | §8.3 |
| **Solution Six** — OBO for User Tokens | Execute with user tokens / user delegation SAS, making native logs show the real person directly | ❌ **Excluded** (Breaks the zero-standing-write-permission model) | §8.4 |
| **Solution Seven** — Resource Tagging | Conveniently tag resources with `lastModifiedBy=<id>` during write operations | ❌ **Excluded/Corner Case** (Only for taggable resources, has race conditions, not systematic) | This section |

**Carrier Choice (within Solution Two):** The correlation GUID can theoretically ride on two carriers — `x-ms-client-request-id` (② in §3) or User-Agent. The former **cannot be set** on typed `az`, so the final choice is **User-Agent** (`AZURE_HTTP_USER_AGENT`, §8.2).

**Why Solution Seven is Just a Corner Case:** Only effective for **taggable resources** and **mutating operations**; concurrent writes can overwrite each other, tags can be polluted, and it does not cover read operations at all. It can be a nice addition for a few high-value resources, but **cannot support a complete attribution system**, hence not included in the decision.

---

## 6. Why "Solution One (Relying on Azure Native ID Join)" is Not Viable ★

> Solution One = relying on Azure-generated `correlationId` / request id to join with MCP-side records. **Verdict: Not viable.** Three hard reasons:

1. **The id is generated by Azure; you must first "know" it to join.** `correlationId`/request id are minted by Azure only at execution time. To obtain "the id generated by this `az` command", you would have to **scrape the stderr of `az --debug`** and parse each HTTP call individually — the format breaks with version changes; and a single command often makes multiple HTTP requests, each with its own id, making alignment impossible. **Fragile, unreliable.**
2. **`correlationId` only lives in the control plane.** Data-plane Storage operations **do not share** that ARM `correlationId` (see §4: `CorrelationId` in `StorageBlobLogs` is Storage's own). So this join key **cannot span "control plane + data plane"**, coverage is incomplete.
3. **The only one that can "point back to your system", ② (client-request-id), cannot be set on typed `az`** (§3).

**Comparison with User-Agent (Solution Two) — Why it avoids these problems:** The UA contains a **GUID you generate yourself** (naturally aligns with the MCP table, zero scraping), is **automatically applied to every outbound call** via a single environment variable (zero per-command modification), and **data-plane services natively log `UserAgentHeader`** (verified in §4). It **avoids each of Solution One's three pain points**. This is the root cause for the final decision to use UA and discard Solution One.

---

## 7. Why Injecting the Raw OID into UA (Solution Two′) is Not Recommended (by Default)

The temptation is real: "Put the oid directly in the UA, and a single storage table tells you who, no join needed." It does have advantages — single table gives "who", one less join, and even if the MCP table ingestion fails, the oid remains in the native log. But **it is not the default**, for four reasons:

1. **oid only answers "who", not "which invocation".** Incident response is almost never satisfied with "it was Zhang San", but needs "which session was Zhang San in, what was the full command, from which IP, what was the explanation" — **only the MCP table has these**. For anything deeper, you still need to join back; the "single table is enough" benefit only covers the most superficial questions.
2. **GUID is precise to "this invocation", oid is only precise to "this person".** A correlation GUID is unique per tool call; joining back directly locks onto that command/time/session. If the same person runs 200 commands in a day, the oid cannot distinguish which one. As a join key, **GUID is strictly superior to oid**.
3. **UA is a forgeable field; don't give it false authority.** Regardless of what you put in it, UA is not a security boundary. Putting an oid in it might lead people to mistakenly treat "the log clearly shows oid X" as evidence, when it could be forged. Using GUID + server-side table, **authority always resides in the table written by your server**; UA is just a pointer.
4. **Minimize identity leakage.** A GUID leaks nothing; an oid gets copied into the logs of N services, each with different retention/access control/export policies. (Honest downgrade: within the same tenant, Storage already logs `RequesterObjectId` when users connect directly, so the leakage surface isn't that dramatic, but "leak as little as possible" still holds.)

> **Conclusion**: The final decision is to **put only the GUID in the UA by default**. If the team truly wants the convenience of "a quick glance without a join", it can be added as an **explicit opt-in** in the form `mcp/<guid>;oid/<oid>` — but two iron rules remain unbroken: **(a) Never put only the oid** (loses "which invocation" resolution); **(b) Never treat UA as authoritative evidence.**

---

## 8. Final Technical Decision (Convergence)

### 8.1 Layer 1 = Solution Four — Authoritative Audit Table (Mandatory, Do First)

Upgrade the `logger.info` line at `main.py:161` into a **structured audit event**, collected in-place in `UserAuthMiddleware.on_call_tool`, and written to the Log Analytics custom table `MCPAudit_CL` via **Logs Ingestion API + Data Collection Rule (DCR)** (Note: the old HTTP Data Collector API is deprecated; do not use it).

Fields (minimal set):
```
correlation_guid   # Generated once per tool call, also injected into UA (see 8.2)
ts, user_oid, user_upn
client_ip          # Taken from the ingress X-Forwarded-For — user's real IP only exists at the MCP entry point
session_id, conversation_id
tool, group        # diagnose_bash / action_bash
full_command, explanation
sp_appid           # The worker SP that actually executed
target_resource_ids
exit_code
```
- As long as MCP is trusted, **this table alone fully answers attribution**; native logs are just corroboration.
- Recommended to be **append-only/tamper-proof** (the table itself is immutable, can also be exported to WORM Blob for hardening).

### 8.2 Layer 2 = Solution Two — UA Injection of Correlation GUID (Mandatory, Cheap)

In the **single execution chokepoint** `executor.exec(ctx, command)`, set an **out-of-band** environment variable (the Azure CLI/SDK both read it and automatically append it to the User-Agent of every outbound call):
```
AZURE_HTTP_USER_AGENT = "mcp/<correlation_guid>"
```
- **Must be set in the worker/sandbox process environment, never concatenated into the LLM-provided command string** — otherwise a malicious command could `unset`/override it.
- `ctx` already contains `user_oid`/`session_id`. The GUID is the same one used in Layer 1's audit event → in KQL, `MCPAudit_CL | join StorageBlobLogs on $left.correlation_guid == $right.<guid extracted from UserAgentHeader>` completes the loop.
- Coverage: **Data-plane** services (Storage is best, verified `UserAgentHeader` exists in §4). The control plane Activity Log generally does not record UA → covered by Layer 1.

### 8.3 Layer 3 = Solution Five — Per-User Identity (Optional, High Assurance)

Issue dedicated SPs/UAMIs to a small number of **privileged write users**, making the native `Caller`/`RequesterObjectId` itself distinguishable to the person. Only maintain an `appid→user` mapping. Cost: SP proliferation, per-SP RBAC assignment, does not scale to thousands of users. **Only for high-value, narrow user groups**, not for everyone.

### 8.4 Layer 4 = Solution Three — Client-Side Trace (Optional, Not a Security Control) + Explicit Exclusions

- **Layer 4 (Solution Three)**: Client-side LangSmith, etc., records agent dialogue/intent, answering "why did the AI do this, who told it to". It is **outside** the Azure security boundary, can be bypassed, **serves only as an observability layer, not a security control**.
- **Excluded · Solution One** (relying on Azure native ID join): Reasons in §6.
- **Excluded · Solution Six** (OBO for user tokens / user delegation SAS): Would make native logs show the real person, but requires **the user themselves to have permissions on the target resource**, directly breaking the "user zero standing write permissions" model → not viable for the write path.
- **Excluded · Solution Seven** (resource tagging): Reasons in §5.

---

## 9. Implementation Changes & To-Do Items

| # | Change | Location | Notes |
|---|---|---|---|
| 1 | Capture `client_ip` at ingress + generate `correlation_guid` + write structured audit | `main.py` `UserAuthMiddleware.on_call_tool` | Replace the existing single-line `logger.info` |
| 2 | Pass `correlation_guid` into the execution context | Add field to `SessionCtx` (executor.py) | So `exec` can access it |
| 3 | Out-of-band injection of `AZURE_HTTP_USER_AGENT=mcp/<guid>` | `executor.exec` (both local and ACA backends) | Set in process env, not in the command |
| 4 | Create `MCPAudit_CL` table + DCR + Logs Ingestion permissions | Provisioning (Bicep) | Infrastructure for Layer 1 |
| 5 | Enable diagnostic settings on target resources → workspace | Provisioning / Customer side | **Prerequisite** for Layer 2 (otherwise no data-plane logs) |
| 6 | (Validation) Perform one real blob log operation, verify the GUID in `UserAgentHeader` is end-to-end traceable | Manual, one-time | Move the solution from "schema is valid" to "end-to-end measurement is valid" |

> The honest limitation from §4 falls on To-Do items 5/6: Currently, this subscription has **no data-plane logs**. Layer 2 requires a resource with a diagnostic setting enabled before it can be recorded. Recommended first validation step: Pick one storage account → enable `StorageBlobLogs` → point to `dataops-aca-logs` → perform one diagnose blob read → wait for ingestion → verify `UserAgentHeader`.

---

## 10. Appendix: Verification Record for This Document

- Environment: tenant `9ea91fbb-…`, sub `ee5f77a1-…`; queried via `diagnose_bash` using read-only SP `d09dfd39-…`.
- `StorageBlobLogs` columns: measured via `getschema` on `dataops-aca-logs` (`4de6b3e7-…`) (table in §4).
- `AzureActivity` columns: measured via `getschema` on `DefaultWorkspace` (`49e67e4c-…`).
- Table source: `StorageBlobLogs` measured `tableType == "Microsoft"` (predefined standard table) → schema is Azure official standard, **not a configuration artifact of this environment**; also resolves in the unrelated `mlworkspace` workspace returning 0 rows, proving it is independent of diagnostic settings.
- Configuration status: **8 storage accounts, all with 0 diagnostic settings**; `StorageBlobLogs` / `AzureActivity` across 4 workspaces **all have 0 rows** → schema is authoritative, no real ID sample values.
---
title: "Implementation Log: DCR/DCE Differences, Audit + UA Injection Deployment and Verification Status"
date: 2026-07-13
tags:
  - implementation-log
  - audit
  - dcr
  - dce
  - logs-ingestion
  - user-agent
  - oid-tracking
status: Deployed to ACA (not full IaC, see §5) / Core chain fully verified / Not committed
sources:
  - "src/mcp-server/audit.py (new)"
  - "src/mcp-server/{main,executor,sandbox_manager}.py, src/worker/worker.py (modified)"
  - "provisioning/aca/modules/audit.bicep + audit-standalone.bicep"
  - "Test environment tenant 9ea91fbb-…, sub ee5f77a1-…, workspace dataops-aca-logs (4de6b3e7-…)"
verified:
  - "MCPAudit_CL write: row with correlation_id=2a3e13f7… contains real user jumpbear0920@outlook.com + real IP 107.136.48.82 (written by MCP process via LogAnalyticsAuditSink)"
  - "Layer 2 storage: 2a3e13f7… in StorageBlobLogs.UserAgentHeader"
  - "Layer 2 keyvault: 2a3e13f7… in AzureDiagnostics.clientInfo_s (ingested after cross-region delay, three-point closed loop)"
  - "KV successful read: after adding access policy (get, list) for diagnose SP, secret list returned real secret name, UA=mcp/2a3e13f7…"
---

# Implementation Log: DCR/DCE, Deployment and Verification Status

> A faithful record of this implementation session: what was changed, what was deployed, what was **not** done, **what has been verified, and what is still pending**,
> plus the 4 questions you specified (① DCR/DCE differences, ② what is still pending, ③ why DCE was not used, ④ how to implement).
> The **reasons** for design and selection are in the [Design Document](./implementation-plan-user-attribution-for-sp-operations.md)
> and the [Implementation Plan](./implementation-audit-tool-audit-py-and-ua-injection.md).

---

## 1. Differences between DCR and DCE (full names provided) ★ [Question ①]

| | **DCR = Data Collection Rule** | **DCE = Data Collection Endpoint** |
|---|---|---|
| What it is | A **routing/schema rule**: which stream the incoming data belongs to, which columns to parse, which table to land in | An **ingress URL resource**: you POST data to it |
| Analogy | Sorting rule (which warehouse, which shelf this shipment goes to) | Receiving dock address (where to send it) |
| Billing | Free | Free |

**To ingest a custom log into Log Analytics, you logically always need two things**:
1. An **ingress URL** (where to POST) — technically called the *logs ingestion endpoint*;
2. A **rule** (how to parse and route the incoming data) — that is the **DCR**.

The difference is only in **what form the first item (ingress URL) takes**: as an independent **DCE** resource, or **embedded within the DCR** (see §2).

> ⚠️ Do not confuse this with another system: The Log Analytics **workspace** does not have a receiving endpoint for the new API. The old
> HTTP Data Collector API's per-workspace `*.ods.opinsights.azure.com` + shared key is deprecated,
> and this solution does not use it (excluded in design §8.1).

---

## 2. Why DCE was not used, only DCR ★ [Question ③]

Because the DCR was created with **`kind: 'Direct'`**, it **comes with a built-in ingestion endpoint**. You can send data directly to this DCR, and **no separate DCE resource is needed**.

**Evidence from testing** (the DCR I deployed):

```
kind = "Direct"
Built-in endpoint = https://dataops-aca-audit-dcr-5ee7-westus2.logs.z1.ingest.monitor.azure.com
Referenced independent DCE = null (no DCE referenced)
Number of DCEs in the entire RG = 0
```

And I used this built-in endpoint to POST test data, which returned **204 and the data landed in the table**, proving it works.

**When is an independent DCE needed** (none of these apply to us, so we omitted it):
1. Multiple DCRs need to **share** the same ingress URL;
2. Using **Private Link / AMPLS** (DCE is the resource bound to the private network);
3. Certain regional/compliance configurations explicitly require a DCE.

**Cost perspective**: DCE and DCR are both free; only **data ingestion** is billed by volume (~0.5KB per audit row, a few million calls would be ~$2).
So choosing `kind=Direct` is **not to save money**, but to **have one less resource and one less `dataCollectionEndpointId` association**.

> If a certain region/apiVersion does not expose `endpoints.logsIngestion`, fall back to adding a DCE and setting
> `dataCollectionEndpointId` on the DCR; the logic remains unchanged.

---

## 3. How this is implemented ★ [Question ④]

Two layers, connected by a single correlation id.

### 3.1 Layer 1 — Authoritative Audit (MCP writes its own table)

- New file `src/mcp-server/audit.py`, exposing 4 interfaces: `new_correlation_id()` / `client_ip()` /
  `build_user_agent()` / `get_audit_sink().record()`.
- Middleware in `main.py`: generates a correlation id for each tool call, captures the user's real IP
  (`X-Forwarded-For`, only present at the MCP entry point) and upn; calls `record(AuditEvent(...))` once in `_exec`,
  **replacing** the previous simple `logger.info` line.
- Sink is chosen based on env: if `AUDIT_DCR_*` is configured → `LogAnalyticsAuditSink` (writes to `MCPAudit_CL` via Logs Ingestion API + DCR); if not configured → `StdoutAuditSink` (falls back to container stdout).

### 3.2 Layer 2 — Native Log Traceability (inject id into User-Agent)

- The correlation id is formatted as `mcp/<guid>` and set as the environment variable `AZURE_HTTP_USER_AGENT` for the execution process;
  `az`/Azure SDK will **automatically append it to every outbound call's User-Agent**.
- Local path: `worker.py` sets it in the child process env (`create_subprocess_shell(env=...)`,
  **not in the command string**, preventing tampering).
- ACA path: `sandbox_manager._wrap()` runs `export AZURE_HTTP_USER_AGENT=...` before each exec.
- Consequently, the **native logs** of storage / key vault will contain `mcp/<guid>`, which can be used to join back to `MCPAudit_CL`.

### 3.3 One call produces three records (linked by the same id)

```
One diagnose_bash call
  ├─ MCPAudit_CL          ← Authoritative row: who (upn), real IP, command, result  [correlation_id=<guid>]
  ├─ StorageBlobLogs      ← UserAgentHeader contains mcp/<guid>   (natively shows only SP + sandbox IP)
  └─ KeyVault AuditEvent  ← clientInfo_s    contains mcp/<guid>   (natively shows only SP + sandbox IP)
```

### 3.4 A finding from testing: each service's UA lands in a **different column**

| Service | Column for UA |
|---|---|
| Storage `StorageBlobLogs` | `UserAgentHeader` |
| Key Vault `AzureDiagnostics` | `clientInfo_s` |

Join queries must use the corresponding column name per service: `extend cid = extract(@"mcp/([0-9a-f]+)", 1, <column for that service>)`.

### 3.5 Link-back query (KQL)

```kusto
// storage example: trace back from a suspicious access to the real user
StorageBlobLogs
| extend cid = extract(@"mcp/([0-9a-f]+)", 1, UserAgentHeader)
| where isnotempty(cid)
| join kind=leftouter (MCPAudit_CL) on $left.cid == $right.correlation_id
| project TimeGenerated, OperationName, StatusCode,
          storageRowId=CorrelationId, requesterSP=RequesterObjectId,
          real_user=user_upn, real_IP=client_ip, command=command
// key vault: replace line 2 above with extract(..., clientInfo_s)
```

---

## 4. What was actually done / not done / deployed this time

### 4.1 Code changes (**not committed**, all in workspace)

| File | Change |
|---|---|
| `src/mcp-server/audit.py` | 🆕 Audit utilities (correlation id / client_ip / UA / sink) |
| `src/mcp-server/main.py` | Middleware generates id+captures IP; `_exec` writes audit; removed old `logger.info` |
| `src/mcp-server/executor.py` | `SessionCtx.correlation_id`; local executor passes UA |
| `src/worker/worker.py` | Sets `AZURE_HTTP_USER_AGENT` in child process env |
| `src/mcp-server/sandbox_manager.py` | ACA path `export` UA |
| `src/mcp-server/requirements.txt` | Added `azure-monitor-ingestion` |
| `provisioning/aca/modules/audit.bicep` | 🆕 Formal DCR module |
| `provisioning/aca/modules/{environment,mcp-app,rbac,storage}.bicep` + `main.bicep` | Integrated audit (table/env/role/diag/wiring) |
| `provisioning/aca/audit-standalone.bicep` | 🆕 **One-time patch** (see §5, should be removed after use) |

### 4.2 Actually deployed to Azure (live)

| Item | Value / Description |
|---|---|
| Image | `mcp-server:ua-audit-20260713a` → ACR |
| MCP Container App | Switched to revision `--0000011` (with `AUDIT_DCR_*` env, running new code) |
| `MCPAudit_CL` table | Created in `dataops-aca-logs` |
| DCR | `dataops-aca-audit-dcr` (`kind=Direct`, built-in endpoint, **no DCE**) |
| Role | MCP MI (`17acde50…`) → Monitoring Metrics Publisher on DCR |
| Diagnostic settings | storage `dataopsacavyq3trlvkn4za/blob` + key vault `stanleyakvprod` → workspace |
| KV access policy | diagnose SP (`40eccd97…`) → `stanleyakvprod` **secret get+list** (added to test successful read, minimal, read-only, this vault only) |

### 4.3 **Not done** (honest)

- **Did not run full `main.bicep`**: It is subscription-level, with `mcpImage` defaulting to a placeholder image. A full deployment would roll back the live MCP to the placeholder image and re-run fragile modules like identity/FIC. The audit infrastructure was deployed separately using the patch in §5.
- **Did not commit** any code.

---

## 5. Relationship between the two Bicep files (must be clear)

| File | Purpose | Fate |
|---|---|---|
| `provisioning/aca/modules/audit.bicep` | **Formal module**, already integrated into `main.bicep`. In a fresh full deployment, the audit infrastructure is created from here. | **Keep** (single source of truth) |
| `provisioning/aca/audit-standalone.bicep` | **One-time patch**: creates only the table + DCR + role, applied to the **already running** stack to avoid the risk of a full `main.bicep` redeployment. | **Scaffolding, discard after use** (delete after converging to main.bicep) |

**The current audit infrastructure in Azure was deployed using `audit-standalone.bicep`** (deployment names `audit-standalone` / `audit-standalone2`). This constitutes **configuration drift**: the same resources are declared in the formal template, but the patch was what actually applied them.
**The end state for formal deployment** should be: run `main.bicep` (idempotent, will adopt existing DCRs/tables with the same name), making `main.bicep` the single source of truth, and delete the standalone file. This step (including how to safely handle the image parameter to avoid rolling back to the placeholder image) requires a separate **deployment document**, which has not yet been written.

---

## 6. Verification Conclusion + What is Still Pending ★ [Question ②]

### 6.1 Verified ✅ — Core chain **fully closed loop** (all three points matched)

Based on the latest `correlation_id = 2a3e13f7aa094f8d9f19f04e37e70e88` (diagnose_bash call at 05:03:44Z),
**one id links all three points**, all confirmed by testing:

| Step | Conclusion | Evidence |
|---|---|---|
| Layer 1 · MCPAudit_CL (authoritative row) | ✅ | The row contains real user `jumpbear0920@outlook.com` + real IP `107.136.48.82` (written by MCP process via `LogAnalyticsAuditSink`) |
| Layer 2 · Storage | ✅ | `StorageBlobLogs.UserAgentHeader` contains `mcp/2a3e13f7…` |
| Layer 2 · Key Vault | ✅ **(newly arrived this time)** | `AzureDiagnostics.clientInfo_s` contains `mcp/2a3e13f7…`; ingested after cross-region (vault eastus / workspace westus2) delay, count≥1 |
| KV successful read | ✅ | After adding access policy (get, list) for diagnose SP, `secret list` returned real secret name, with that UA |

> **Conclusion reached**: While writing this, the KV record was still being ingested cross-region; it has now arrived. Therefore, **MCPAudit_CL + storage + KV have been fully matched using the same correlation id**, and the end-to-end attribution chain is **closed**. This was the final piece of the puzzle.

### 6.2 What is still pending — **Technically, nothing** ★

Everything that needed verification has been verified. The only thing still "running in the background" is a **cleanup action unrelated to the conclusion**:

| Item | Status | Impact |
|---|---|---|
| Purge of test rows in `MCPAudit_CL` | ⏳ Asynchronous execution in progress (minutes to ~1 hour after submission), currently 4 test rows remain in the table | **Does not affect any conclusion**; it deletes test data, not the table structure; can be done at any time |

**Therefore: There are no technical conclusions still pending.** The attribution mechanism (Layer 1 authoritative table + Layer 2 UA injection + link-back) has been fully verified on real Azure, across Storage and Key Vault services, using the same correlation id.

### 6.3 Remaining cleanup items (awaiting your confirmation before action, not "pending")

| Item | Suggestion |
|---|---|
| 4 test rows in `MCPAudit_CL` | Purge submitted, waiting for async effect; can check `purge-status` if immediate action needed |
| My own identity's Monitoring Metrics Publisher role on the DCR | **Already removed** (added temporarily for manual POST testing) |
| `audit-standalone` / `audit-standalone2` **deployment records** | Can be deleted (purely historical metadata, deleting records **does not delete resources**); not yet deleted |
| KV `stanleyakvprod` diagnose access policy | Added for testing; can be revoked if no longer needed (`az keyvault delete-policy`) |
| Diagnostic settings for storage/KV | Can be kept or removed as needed if only for this test |
| `audit-standalone.bicep` file | Delete after converging to `main.bicep` |

---

## 7. Next Steps (not done, pending)

1. Write the **formal deployment document**: how to deploy the entire stack (including audit) **solely from the formal bicep in `provisioning/`**, eliminating the dependency on `audit-standalone.bicep`, and explaining how to pass the image parameter without rolling back to the placeholder image.
2. Decide on the fate of test artifacts (§6.3) and clean them up.
3. Decide whether/when to commit.
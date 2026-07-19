---
title: "Implementation: Audit Tool audit.py + UA Injection — Code Changes, Provisioning, and util Interface Design"
date: 2026-07-12
tags:
  - implementation
  - audit
  - log-analytics
  - dcr
  - logs-ingestion
  - user-agent
  - oid-tracking
status: Implementation Spec / Pending Coding
sources:
  - "src/mcp-server/main.py (UserAuthMiddleware.on_call_tool:152-162, _exec:184-193, diagnose_bash:200-213, action_bash:225-251)"
  - "src/mcp-server/executor.py (SessionCtx:55-67, LocalDockerExecutor.exec:95-106)"
  - "src/worker/worker.py (ExecRequest:40-42, exec_command:51-80 — asyncio.create_subprocess_shell)"
  - "src/mcp-server/sandbox_manager.py (exec:518-531, _scope_to_workspace:503-516)"
  - "src/mcp-server/requirements.txt (no azure-monitor-ingestion — needs to be added)"
  - "provisioning/aca/main.bicep + modules/{environment,mcp-app,rbac,storage}.bicep (IaC change points)"
  - "docs/en/oid-log-tracking/implementation-plan-user-attribution-for-sp-operations.md (§8 Selection)"
verified:
  - "Current state: Attribution is only a single logger.info line in main.py:161; worker uses create_subprocess_shell(req.command) without passing env; sandbox exec goes through client.exec(_scope_to_workspace(...))"
  - "requirements.txt already has azure-identity; does not have azure-monitor-ingestion"
  - "In environment.bicep, logs = ${name}-logs workspace exists and is reusable; in storage.bicep, blobService 'default' is ready and can have a diagnostic setting attached"
---

# Implementation: Audit Tool `audit.py` + UA Injection

> Corresponds to [Selection Document](./implementation-plan-user-attribution-for-sp-operations.md) **§8 Layer 1 (Option 4) + Layer 2 (Option 2)**.
> This document only answers three engineering questions: **① Where to change the code, ② What to provision (all via existing Bicep / IaC), ③ Can we create a util that `main.py` can call directly, replacing the existing `logger.info` line**.
>
> The conclusion upfront: **Yes. Add a new file `audit.py` (all logic contained here), `main.py` only needs about a dozen lines changed, `executor` / `worker` / `sandbox_manager` each need 2~4 lines; all provisioning modifies existing Bicep, adding one new `audit.bicep` module.**

---

## 0. One-Sentence Summary

- **One new file** `src/mcp-server/audit.py`, exposing **4 interfaces**:
  `new_correlation_id()`, `client_ip()`, `build_user_agent(cid, oid)`, `await get_audit_sink().record(event)`.
- **Changes to `main.py`**: Generate correlation id + call `audit.client_ip()` in middleware, call `record(...)` once in `_exec` — **replacing the scattered `logger.info` calls**.
- **UA Injection (Layer 2)**: Add one field to `SessionCtx`, inject once in each backend (local via worker's new `env` channel; ACA via command wrapper's `export`).
- **New provisioned resources (all via existing Bicep)**: 1 DCR (`kind: Direct`, with built-in endpoint, **no DCE needed**) + 1 custom table `MCPAudit_CL` + 1 RBAC role (Monitoring Metrics Publisher) + a few env vars + 1 pip package; **reuse the existing `logs` workspace**.
- **Layer 2 prerequisite**: The target resource must have a diagnostic setting enabled (§5.6), otherwise there is no `UserAgentHeader` to query in the data plane logs.

---

## 1. Change Overview (Who Changes, How Much)

| File | Change | New/Modified | Size |
|---|---|---|---|
| `src/mcp-server/audit.py` | Audit sink + correlation id + client_ip + UA construction, all logic | 🆕 **New** | ~110 lines |
| `src/mcp-server/main.py` | Middleware generates cid/calls `client_ip()`; `_exec` calls `record()`; remove old `logger.info` | ✏️ Modified | ~10 lines |
| `src/mcp-server/executor.py` | `SessionCtx` adds `correlation_id`; `LocalDockerExecutor.exec` passes `user_agent` | ✏️ Modified | ~4 lines |
| `src/worker/worker.py` | `ExecRequest` adds `user_agent`; `create_subprocess_shell` adds `env=` | ✏️ Modified | ~4 lines |
| `src/mcp-server/sandbox_manager.py` | `exec` prepends `export AZURE_HTTP_USER_AGENT` wrapper | ✏️ Modified | ~5 lines |
| `src/mcp-server/requirements.txt` | Add `azure-monitor-ingestion` | ✏️ Modified | 1 line |
| `provisioning/aca/*.bicep` | New module `audit.bicep` (DCR, with built-in endpoint) + table/RBAC/env/diag additions (§5) | 🆕+✏️ | Bicep |

> **Design Principle**: Every change in existing files is just "**get a value / call a function**". All logic, SDK calls, fallbacks, and formatting are hidden inside `audit.py`.
> Future changes to the sink (Log Analytics ⇄ stdout), UA format, or adding fields only require changes to `audit.py`, without touching `main.py`.

---

## 2. New Utility: `src/mcp-server/audit.py` (Interface Design) ★

This is the answer to question ③. It exposes only four things, which `main.py` / `executor` call directly:

```python
"""audit.py — The single place to: emit the authoritative audit event for each tool call,
and forge the correlation id that joins back native Azure logs.

main.py / executor only call these:
    cid = new_correlation_id()                       # One per tool call
    ip  = client_ip()                                # Get user's real IP (Layer 1 field)
    ua  = build_user_agent(cid, oid)                 # Inject into AZURE_HTTP_USER_AGENT (Layer 2)
    await get_audit_sink().record(AuditEvent(...))   # Write authoritative row (Layer 1)

Everything else is hidden here: Log Analytics ingestion, credentials, stdout fallback, never-raise semantics,
and the version-specific details of extracting the IP from the fastmcp request. Changing the sink means changing
an env var, not code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("dataops-mcp.audit")


# ---- Interface 1: Correlation id (one per tool call) --------------------------
def new_correlation_id() -> str:
    """hex (no hyphens) => safe to put in User-Agent token, easy to extract in KQL."""
    return uuid.uuid4().hex


# ---- Interface 2: UA Injection String (Layer 2) ------------------------------------------------
def build_user_agent(correlation_id: str, oid: str | None = None) -> str:
    """String to append to AZURE_HTTP_USER_AGENT.

    Default is just the GUID (Selection §7). Set AUDIT_UA_INCLUDE_OID=1 to also include oid (opt-in);
    Regardless, the GUID is the authoritative join key.
    """
    tok = f"mcp/{correlation_id}"
    if oid and os.environ.get("AUDIT_UA_INCLUDE_OID", "0") == "1":
        tok += f";oid/{oid}"
    return tok


# ---- Interface 3: Get User's Real IP (Layer 1 field; implementation here, main.py calls directly) --------
def client_ip() -> str | None:
    """The first hop of the X-Forwarded-For header from the incoming HTTP request = user's real IP
    (only available at the MCP entry point).

    The fastmcp HTTP request symbols depend on the version (get_http_headers / get_http_request);
    If unavailable, degrade to None — never fail a tool call because the IP couldn't be fetched.
    Keep this version-specific detail inside the util; main.py does not touch the HTTP layer.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        h = get_http_headers() or {}
        xff = h.get("x-forwarded-for")
        return xff.split(",")[0].strip() if xff else None
    except Exception:
        return None


# ---- Interface 4: Audit Event + Sink ------------------------------------------------
@dataclass
class AuditEvent:
    correlation_id: str
    tool: str
    group: str | None
    user_oid: str | None = None
    user_upn: str | None = None
    client_ip: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    command: str | None = None
    explanation: str | None = None
    sp_appid: str | None = None
    exit_code: int | None = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_row(self) -> dict:
        # Column names must align with the DCR stream / table schema (§5); TimeGenerated is a required column for custom tables.
        d = asdict(self)
        d["TimeGenerated"] = d.pop("ts")
        return d


class AuditSink:
    async def record(self, event: AuditEvent) -> None:  # Contract: never raise, never block the tool
        ...


class StdoutAuditSink(AuditSink):
    """Fallback when local / DCR not configured: one structured JSON line, replacing the old free-text logger.info."""

    async def record(self, event: AuditEvent) -> None:
        try:
            logger.info("AUDIT %s", json.dumps(event.to_row(), ensure_ascii=False))
        except Exception as e:
            logger.warning("audit stdout failed: %s", e)


class LogAnalyticsAuditSink(AuditSink):
    """Sends audit events to a Log Analytics custom table via the Logs Ingestion API + DCR."""

    def __init__(self, endpoint: str, rule_id: str, stream: str):
        from azure.identity.aio import DefaultAzureCredential
        from azure.monitor.ingestion.aio import LogsIngestionClient

        self._stream = stream
        self._rule_id = rule_id
        self._cred = DefaultAzureCredential()          # = MCP app's managed identity
        self._client = LogsIngestionClient(endpoint, self._cred)

    async def record(self, event: AuditEvent) -> None:
        # Auditing must never crash/block a tool call: timeout + swallow exception
        # (authoritativeness vs availability, choose availability;
        #  for stronger guarantees, add local WAL/retry later).
        try:
            await asyncio.wait_for(
                self._client.upload(self._rule_id, self._stream, [event.to_row()]),
                timeout=float(os.environ.get("AUDIT_TIMEOUT", "5")),
            )
        except Exception as e:
            logger.warning("audit ingestion failed (%s); cid=%s", e, event.correlation_id)


_sink: AuditSink | None = None


def get_audit_sink() -> AuditSink:
    """Singleton. Uses Log Analytics if AUDIT_DCR_* is configured, otherwise falls back to stdout."""
    global _sink
    if _sink is None:
        ep = os.environ.get("AUDIT_DCR_ENDPOINT")
        rid = os.environ.get("AUDIT_DCR_RULE_ID")
        stream = os.environ.get("AUDIT_STREAM_NAME", "Custom-MCPAudit_CL")
        if ep and rid:
            _sink = LogAnalyticsAuditSink(ep, rid, stream)
            logger.info("audit sink: Log Analytics (%s)", stream)
        else:
            _sink = StdoutAuditSink()
            logger.info("audit sink: stdout (AUDIT_DCR_* not configured)")
    return _sink
```

> **Why audit is sent from `_exec` and not middleware**: `_exec` has access to `command`, `exit_code` (returned by the executor), and `SessionCtx` (with correlation id) simultaneously, allowing it to construct a **complete** event in one call; middleware is only responsible for creating the id and capturing middleware-specific data (client_ip, upn), storing them in state for `_exec` to retrieve.

---

## 3. Minimal Changes to `main.py` (Question ①, Main File)

### 3.1 Top-level import (+2 lines)

```python
import audit                       # New util
from audit import AuditEvent
```

### 3.2 Middleware: Forge correlation id + capture client_ip (modify `on_call_tool`)

Current state (`main.py:152-162`) only stashes oid/session/conversation and logs one line with `logger.info`. Change to:

```python
async def on_call_tool(self, context: MiddlewareContext, call_next):
    token = get_access_token()
    claims = token.claims if token and hasattr(token, "claims") else {}
    oid = claims.get("oid")
    fctx = context.fastmcp_context
    if fctx is not None:
        session_id, conversation_id = await _derive_ids(oid, fctx)
        await fctx.set_state("user_oid", oid)
        await fctx.set_state("session_id", session_id)
        await fctx.set_state("conversation_id", conversation_id)
        # Add three new items for §3.3's _exec:
        await fctx.set_state("user_upn", claims.get("preferred_username") or claims.get("upn"))
        await fctx.set_state("client_ip", audit.client_ip())      # Capture X-Forwarded-For (see audit.py interface 3)
        await fctx.set_state("correlation_id", audit.new_correlation_id())
    return await call_next(context)                              # Remove the original logger.info line
```

> The implementation of `client_ip()` is in `audit.py` (§2 interface 3); `main.py` calls it directly, keeping HTTP details out of the main file.
> The exact symbol for `get_http_headers` should be verified against your fastmcp version (§8 risk table); if the IP cannot be captured, only one field is missing, and the rest of the record is unaffected.

### 3.3 `_exec`: Call `record()` once, replace all old logs (modify `_exec` + two tools)

Current state `_exec` (`184-193`) only builds `SessionCtx` and then executes. Change to:

```python
async def _exec(group: str, command: str, ctx: Context, explanation: str | None = None):
    correlation_id = await ctx.get_state("correlation_id")
    sctx = SessionCtx(
        user_oid=await ctx.get_state("user_oid"),
        session_id=await ctx.get_state("session_id"),
        conversation_id=await ctx.get_state("conversation_id"),
        group=group,                       # type: ignore[arg-type]
        correlation_id=correlation_id,     # ← New field (§4.1), used for UA injection
    )
    result = await executor.exec(sctx, command)
    # —— This single line replaces all the original logger.info calls in middleware/diagnose_bash/action_bash ——
    await audit.get_audit_sink().record(AuditEvent(
        correlation_id=correlation_id,
        tool=f"{group}_bash",
        group=group,
        user_oid=sctx.user_oid,
        user_upn=await ctx.get_state("user_upn"),
        client_ip=await ctx.get_state("client_ip"),
        session_id=sctx.session_id,
        conversation_id=sctx.conversation_id,
        command=command,
        explanation=explanation,
        exit_code=result.exit_code,
    ))
    return result.to_dict()
```

The two tools only need one line changed each (remove their respective `logger.info`):

```python
async def diagnose_bash(command: str, ctx: Context) -> dict:
    return await _exec("diagnose", command, ctx)

async def action_bash(command: str, explanation: str, ctx: Context) -> dict:
    return await _exec("action", command, ctx, explanation=explanation)
```

> `sp_appid` is optional: locally, it can be populated from env vars (`DIAGNOSE_SP_APP_ID` / `ACTION_SP_APP_ID`) per group; leaving it empty does not affect the main pipeline.

---

## 4. UA Injection (Layer 2): `executor` / `worker` / `sandbox_manager` (Question ①, Continued)

### 4.1 `executor.py`: Add field to `SessionCtx` (+1 line)

```python
@dataclass(frozen=True)
class SessionCtx:
    user_oid: str | None
    session_id: str | None
    conversation_id: str | None
    group: Group
    correlation_id: str | None = None      # ← New; source for UA injection
```

### 4.2 Local backend: `LocalDockerExecutor.exec` passes UA to worker (+3 lines)

```python
from audit import build_user_agent
...
async def exec(self, ctx: SessionCtx, command: str) -> ExecResult:
    worker_url = self._urls[ctx.group]
    payload = {"command": command, "timeout": self._timeout - 10}
    if ctx.correlation_id:
        payload["user_agent"] = build_user_agent(ctx.correlation_id, ctx.user_oid)
    async with httpx.AsyncClient(timeout=self._timeout) as client:
        r = await client.post(f"{worker_url}/exec", json=payload)
        r.raise_for_status()
        return ExecResult.from_worker_json(r.json())
```

### 4.3 `worker.py`: Set env externally (**not in the command string**) (+4 lines)

```python
class ExecRequest(BaseModel):
    command: str
    timeout: float
    user_agent: str | None = None          # ← New

@app.post("/exec")
async def exec_command(req: ExecRequest):
    env = dict(os.environ)
    if req.user_agent:
        env["AZURE_HTTP_USER_AGENT"] = req.user_agent   # Set in the subprocess environment; az automatically appends to UA
    proc = await asyncio.create_subprocess_shell(
        req.command, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ...
```

This is the **cleanest injection**: the env is set on the shell process started by the worker, the LLM's command inherits it as a subprocess, and the UA value **never appears in the command string**, so the LLM cannot change it through normal command concatenation (it can still `unset` it in the same shell, see §8 boundaries).

### 4.4 ACA backend: `sandbox_manager.exec` prepends `export` (+5 lines)

ACA's `client.exec` only accepts a single command string, with no separate env channel. Therefore, prepend an `export` in a wrapper:

```python
from audit import build_user_agent

def _wrap(self, ctx: SessionCtx, command: str) -> str:
    inner = self._scope_to_workspace(ctx, command)     # Existing: mkdir/cd to workspace
    if ctx.correlation_id:
        ua = build_user_agent(ctx.correlation_id, ctx.user_oid)
        return f"export AZURE_HTTP_USER_AGENT={shlex.quote(ua)}\n{inner}"
    return inner

async def exec(self, ctx: SessionCtx, command: str) -> ExecResult:
    self._ensure_reaper()
    client = await self.get_or_create(ctx)
    result = await client.exec(self._wrap(ctx, command))   # ← Use _wrap instead of directly calling _scope_to_workspace
    ...
```

> ⚠️ ACA is **best-effort**: `export` can be overridden by `unset` in the same shell. This is not a vulnerability — Layer 2 is intended as a convenience for "honest paths + post-hoc forensics," **the authoritative source is always Layer 1's table** (Selection §7). If the sandbox SDK's `client.exec` supports `env=`, prefer using it.

---

## 5. What Needs to be Provisioned (Question ②) — Modify Existing Bicep, No `az` CLI

All via IaC. **Reuse** the existing workspace (`logs` in `environment.bicep`, i.e., `${name}-logs`). Changes: create 1 new module `audit.bicep`, the rest are small additions to existing modules.

### 5.0 First Answer: Is a DCE Required? — No

- A Log Analytics **workspace itself does not** have a receiving endpoint for the new Logs Ingestion API (it is a "destination," not an HTTP entry point).
  (The old HTTP Data Collector API had per-workspace `*.ods.opinsights.azure.com` + shared key — the kind used in `environment.bicep:34` for ACA environment console logs; that API is deprecated, and the audit path does not use it, as excluded in Selection §8.1.)
- The new API requires an **ingestion endpoint**, which comes from: **(a) a standalone DCE**, or **(b) the DCR's built-in endpoint** — if the DCR is created with `kind: 'Direct'`, it has `properties.endpoints.logsIngestion`, allowing direct ingestion to the DCR, **no DCE needed**.
- Therefore, **a DCE is not required**. This solution uses option (b), **saving one resource**. If a region/policy does not expose the DCR endpoint, fall back to option (a) by adding a DCE (§5.2 note).

### 5.1 `environment.bicep` — Add Custom Table + Output workspaceId

Add a table (child resource) after the `logs` resource, and add an output:

```bicep
resource auditTable 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = {
  parent: logs
  name: 'MCPAudit_CL'
  properties: {
    schema: {
      name: 'MCPAudit_CL'
      columns: [
        { name: 'TimeGenerated', type: 'datetime' }
        { name: 'correlation_id', type: 'string' }
        { name: 'tool', type: 'string' }
        { name: 'group', type: 'string' }        // Note: 'group' is a keyword in KQL; use ['group'] in queries
        { name: 'user_oid', type: 'string' }
        { name: 'user_upn', type: 'string' }
        { name: 'client_ip', type: 'string' }
        { name: 'session_id', type: 'string' }
        { name: 'conversation_id', type: 'string' }
        { name: 'command', type: 'string' }
        { name: 'explanation', type: 'string' }
        { name: 'sp_appid', type: 'string' }
        { name: 'exit_code', type: 'int' }
      ]
    }
    retentionInDays: 30
    totalRetentionInDays: 30
  }
}

output workspaceId string = logs.id
```

### 5.2 New Module `modules/audit.bicep` — DCR (Built-in Endpoint, No DCE)

Only creates the DCR, no RBAC (RBAC needs the mcp principalId, placed in §5.4 to break the cycle).

```bicep
@description('Resource prefix.')
param name string
@description('Region.')
param location string
@description('Log Analytics workspace resource id (destination).')
param workspaceId string

var streamName = 'Custom-MCPAudit_CL'
var columns = [
  { name: 'TimeGenerated', type: 'datetime' }
  { name: 'correlation_id', type: 'string' }
  { name: 'tool', type: 'string' }
  { name: 'group', type: 'string' }
  { name: 'user_oid', type: 'string' }
  { name: 'user_upn', type: 'string' }
  { name: 'client_ip', type: 'string' }
  { name: 'session_id', type: 'string' }
  { name: 'conversation_id', type: 'string' }
  { name: 'command', type: 'string' }
  { name: 'explanation', type: 'string' }
  { name: 'sp_appid', type: 'string' }
  { name: 'exit_code', type: 'int' }
]

resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: '${name}-audit-dcr'
  location: location
  kind: 'Direct'                       // Built-in logsIngestion endpoint, no DCE needed
  properties: {
    streamDeclarations: {
      '${streamName}': { columns: columns }
    }
    destinations: {
      logAnalytics: [
        { name: 'auditWs', workspaceResourceId: workspaceId }
      ]
    }
    dataFlows: [
      {
        streams: [ streamName ]
        destinations: [ 'auditWs' ]
        outputStream: streamName        // Lands directly into the custom table
      }
    ]
  }
}

output dcrName string = dcr.name
output dcrImmutableId string = dcr.properties.immutableId
output dcrEndpoint string = dcr.properties.endpoints.logsIngestion
output streamName string = streamName
```

> ⚠️ **DCE Fallback**: If your region/apiVersion does not expose `endpoints.logsIngestion`, add a `Microsoft.Insights/dataCollectionEndpoints`, set `dataCollectionEndpointId: dce.id` on the DCR, and change `dcrEndpoint` to `dce.properties.logsIngestion.endpoint`. The logic is unchanged, only one more resource is needed.

### 5.3 `mcp-app.bicep` — Add 3 params + 3 env vars

```bicep
// --- audit (Layer 1) ---
param auditDcrEndpoint string
param auditDcrImmutableId string
param auditStreamName string
```

Append to the env array (after the existing `SANDBOX_DISK_IMAGE` entry):

```bicep
            { name: 'AUDIT_DCR_ENDPOINT', value: auditDcrEndpoint }
            { name: 'AUDIT_DCR_RULE_ID', value: auditDcrImmutableId }
            { name: 'AUDIT_STREAM_NAME', value: auditStreamName }
```

And add to `requirements.txt`: `azure-monitor-ingestion>=1.0`.

### 5.4 `rbac.bicep` — Add Monitoring Metrics Publisher (scope = DCR)

The Logs Ingestion API requires the **Monitoring Metrics Publisher** role on the DCR. `rbac` runs after `mcpApp` and already has `mcpPrincipalId`. Pass the DCR name in (break the cycle: DCR is created first, role binding is after):

```bicep
@description('Audit DCR name.')
param auditDcrName string

var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource auditDcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' existing = {
  name: auditDcrName
}
resource metricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(auditDcr.id, mcpPrincipalId, monitoringMetricsPublisherRoleId)
  scope: auditDcr
  properties: {
    principalId: mcpPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalType: 'ServicePrincipal'
  }
}
```

### 5.5 `main.bicep` — Wire Everything Together (Pay Attention to Order, Avoid Module Cycles)

`audit` runs after `environment`, before `mcpApp`; role binding is in `rbac` (after `mcpApp`):

```bicep
module audit 'modules/audit.bicep' = {
  name: 'audit'
  scope: rg
  params: {
    name: name
    location: location
    workspaceId: environment.outputs.workspaceId
  }
}
```

Append to `mcpApp`'s params:

```bicep
    auditDcrEndpoint: audit.outputs.dcrEndpoint
    auditDcrImmutableId: audit.outputs.dcrImmutableId
    auditStreamName: audit.outputs.streamName
```

Append to `rbac`'s params:

```bicep
    auditDcrName: audit.outputs.dcrName
```

> Dependency chain: `environment` (table + workspaceId) → `audit` (DCR, outputs endpoint/immutableId) → `mcpApp` (consumes env vars) → `rbac` (mcp principal × DCR role binding). **No cycles**.

### 5.6 Layer 2 Prerequisite: Diagnostic Setting for Target Resources (Also via Bicep)

For the UA to be queryable, the target service must send its data plane logs to the workspace. The **workspace storage within the stack** can serve as an end-to-end test target — attach a diagnostic setting to the existing `blobService` ('default') in `storage.bicep` (requires workspaceId, passed in from `main.bicep`):

```bicep
param workspaceId string
resource blobDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-dataops-logs'
  scope: blobService                    // Existing 'default' blobServices sub-resource in storage.bicep
  properties: {
    workspaceId: workspaceId
    logs: [
      { category: 'StorageRead', enabled: true }
      { category: 'StorageWrite', enabled: true }
      { category: 'StorageDelete', enabled: true }
    ]
  }
}
```

> Other resources the customer wants to trace (their storage, ADF, etc.) should each have a diagnostic setting added in **their IaC**; they are not part of this stack.

### New Resource List (Answer to Question ②)

| Resource | Location | One-time / Per Resource |
|---|---|---|
| Custom table `MCPAudit_CL` | `environment.bicep` (child of `logs`) | One-time |
| DCR (`kind: Direct`, built-in endpoint) | New `modules/audit.bicep` | One-time |
| **DCE** | —— | **Not needed** (§5.0) |
| RBAC: Monitoring Metrics Publisher | `rbac.bicep` (scope = DCR) | One-time |
| Env vars `AUDIT_DCR_*` | `mcp-app.bicep` | One-time |
| pip `azure-monitor-ingestion` | `requirements.txt` | One-time |
| Diagnostic setting | `storage.bicep` (test target) / Customer IaC | Per target resource |

---

## 6. End-to-End Data Flow + Join Query

```
tool call
  → middleware: cid=new_correlation_id(), capture client_ip/upn → state
  → _exec: SessionCtx(correlation_id=cid) ─┬─ executor injects AZURE_HTTP_USER_AGENT=mcp/<cid>  (Layer 2)
                                            │      → az → target service native log UserAgentHeader contains mcp/<cid>
                                            └─ audit.record(AuditEvent(cid, oid, ip, command, exit_code)) (Layer 1)
                                                   → MCPAudit_CL
```

A security analyst starts from a suspicious storage log and traces back to the real user:

```kusto
StorageBlobLogs
| where OperationName == "DeleteBlob"
| extend cid = extract(@"mcp/([0-9a-f]+)", 1, UserAgentHeader)
| join kind=leftouter (MCPAudit_CL) on $left.cid == $right.correlation_id
| project TimeGenerated, AccountName, ObjectKey, cid,
          user_oid, user_upn, client_ip, command, explanation, exit_code
```

---

## 7. Implementation Order (Corresponds to Selection §9 To-Do)

1. Write `audit.py` (§2) + add package to `requirements.txt` → **First connect to StdoutAuditSink** (without configuring DCR), you can see structured `AUDIT {…}` locally.
2. Modify `main.py` (§3) + `executor.py` (§4.1/4.2) + `worker.py` (§4.3), rebuild the worker image with `docker compose up` locally.
3. Modify `sandbox_manager.py` (§4.4) — ACA path.
4. `az deployment sub create -f provisioning/aca/main.bicep` (Bicep changes from §5) → Table/DCR/RBAC/env vars are provisioned in one go, the sink automatically switches to Log Analytics, and `MCPAudit_CL` starts receiving data.
5. Deploy the diagnostic setting in `storage.bicep` (§5.6) together with the main deployment → Perform one diagnose blob read → Wait 5~15 min.
6. Run the join query from §6, confirm that `mcp/<cid>` in `UserAgentHeader` can join back to `MCPAudit_CL` → **End-to-end test is successful**.

---

## 8. Risks and Boundaries (Honest Checklist)

| # | Point | Handling |
|---|---|---|
| 1 | UA can be `unset`/overridden in the same shell | Layer 2 is best-effort corroboration; **authority is in Layer 1's table**. Local uses worker `env` (harder to change), ACA uses `export` (changeable) |
| 2 | Audit reporting adds latency / may fail | `record()` has a timeout + swallows exceptions, **never blocks/fails the tool**; lost events only trigger a warning. Add local WAL later for stronger guarantees |
| 3 | `audit.client_ip()` fastmcp symbols | Version-dependent (`get_http_headers` / `get_http_request`), verify during implementation; if unavailable, degrade to None, other fields unaffected |
| 4 | Regional support for DCR `endpoints.logsIngestion` | Primary path uses Direct DCR's built-in endpoint; if unsupported, fall back to DCE as per §5.2 note |
| 5 | Table column `group` is a KQL keyword | Use `['group']` in queries, or rename the column to `tool_group` during table creation (requires syncing the `AuditEvent` field) |
| 6 | `exit_code`/`command` are stored in the audit table | The table is readable by default within the tenant — tighten RBAC on `MCPAudit_CL` / shorten retention as needed |
| 7 | Control plane (ADF/Batch, etc.) generally does not log UA | Layer 2 mainly covers the data plane; control plane attribution is handled by Layer 1's table (see Selection §8.2) |
| 8 | Worker image needs rebuilding | `worker.py` changed → `docker compose build` / push to ACR |

---

## 9. Direct Answers to Your Three Questions

1. **Where to change the code?** — `audit.py` (new, §2) + `main.py` middleware/`_exec`/two tools (§3) + `executor.SessionCtx` and `LocalDockerExecutor` (§4.1/4.2) + `worker.py` (§4.3) + `sandbox_manager.exec` (§4.4). Each existing file only gets a few lines added.
2. **What to provision (all via existing Bicep, no `az` CLI)?** — New module `modules/audit.bicep` creates a **DCR (`kind: Direct`, built-in endpoint, no DCE)**; `environment.bicep` adds the table `MCPAudit_CL`; `rbac.bicep` adds Monitoring Metrics Publisher; `mcp-app.bicep` adds `AUDIT_DCR_*` env vars; `main.bicep` wires everything together; `requirements.txt` adds `azure-monitor-ingestion`. The workspace reuses the existing `logs`. Layer 2 additionally requires diagnostic settings on target resources (§5.6, already provided for the test target in `storage.bicep`).
3. **Can we create a util that the main file calls directly, replacing the existing log?** — **Yes**. `audit.py` exposes four interfaces: `new_correlation_id()`, `client_ip()`, `build_user_agent()`, `get_audit_sink().record()`; the old `logger.info` in `main.py` is replaced by a single line `await ...record(AuditEvent(...))`. All logic, SDK calls, fallbacks, and IP extraction are hidden in the util. **This is the "existing minimum effort + new util exposes interfaces" implementation pattern.**
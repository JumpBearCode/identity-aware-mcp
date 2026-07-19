---
title: "落地实现：审计工具 audit.py + UA 注入 —— 代码改哪里、Provision 什么、util 接口设计"
date: 2026-07-12
tags:
  - implementation
  - audit
  - log-analytics
  - dcr
  - logs-ingestion
  - user-agent
  - oid-tracking
status: 实现规格 / 待编码
sources:
  - "src/mcp-server/main.py (UserAuthMiddleware.on_call_tool:152-162, _exec:184-193, diagnose_bash:200-213, action_bash:225-251)"
  - "src/mcp-server/executor.py (SessionCtx:55-67, LocalDockerExecutor.exec:95-106)"
  - "src/worker/worker.py (ExecRequest:40-42, exec_command:51-80 — asyncio.create_subprocess_shell)"
  - "src/mcp-server/sandbox_manager.py (exec:518-531, _scope_to_workspace:503-516)"
  - "src/mcp-server/requirements.txt (无 azure-monitor-ingestion —— 需新增)"
  - "provisioning/aca/main.bicep + modules/{environment,mcp-app,rbac,storage}.bicep (IaC 改动点)"
  - "docs/oid-log-tracking/实现方案-SP操作的用户归因-Azure日志体系与最终技术选型.md (§8 选型)"
verified:
  - "现状：归因只有 main.py:161 一行 logger.info；worker 用 create_subprocess_shell(req.command) 未传 env；sandbox exec 走 client.exec(_scope_to_workspace(...))"
  - "requirements.txt 已装 azure-identity；未装 azure-monitor-ingestion"
  - "environment.bicep 里 logs = ${name}-logs 工作区已存在，可复用；storage.bicep 里 blobService 'default' 现成，可挂 diagnostic setting"
---

# 落地实现：审计工具 `audit.py` + UA 注入

> 对应 [选型文档](./实现方案-SP操作的用户归因-Azure日志体系与最终技术选型.md) 的 **§8 层 1(方案四)+ 层 2(方案二)**。
> 本文只回答三个工程问题:**① 代码改哪里、② 要 provision 什么(全走 existing Bicep / IaC)、③ 能不能做一个 util
> 让 `main.py` 直接 call、把现有那行 `logger.info` 换掉**。
>
> 结论先给:**能。新增一个文件 `audit.py`(全部逻辑收在这里),`main.py` 只改十来行,`executor` / `worker` /
> `sandbox_manager` 各加 2~4 行;provision 全部改现有 Bicep,新增一个 `audit.bicep` 模块。**

---

## 0. 一句话总结

- **一个新文件** `src/mcp-server/audit.py`,对外暴露 **4 个接口**:
  `new_correlation_id()`、`client_ip()`、`build_user_agent(cid, oid)`、`await get_audit_sink().record(event)`。
- **`main.py` 的改动**:middleware 里生成 correlation id + 调 `audit.client_ip()`,`_exec` 里调一次 `record(...)`——
  **替换掉现有散落的 `logger.info`**。
- **UA 注入(层 2)**:`SessionCtx` 加一个字段,两个 backend 各注入一次(local 走 worker 的新 `env` 通道;
  ACA 走命令 wrapper 的 `export`)。
- **新 provision 资源(全走 existing Bicep)**:1 个 DCR(`kind: Direct`,自带 endpoint,**免 DCE**)+ 1 张自定义表
  `MCPAudit_CL` + 1 条 RBAC(Monitoring Metrics Publisher)+ 几个 env + 1 个 pip 包;**工作区复用现有 `logs`**。
- **层 2 前置条件**:目标资源要开 diagnostic setting(§5.6),否则数据面日志里根本没有 `UserAgentHeader` 可查。

---

## 1. 改动全景图(谁动、动多少)

| 文件 | 改动 | 新建/改动 | 规模 |
|---|---|---|---|
| `src/mcp-server/audit.py` | 审计 sink + correlation id + client_ip + UA 构造,全部逻辑 | 🆕 **新建** | ~110 行 |
| `src/mcp-server/main.py` | middleware 生成 cid/调 `client_ip()`;`_exec` 调 `record()`;删掉旧 `logger.info` | ✏️ 改 | ~10 行 |
| `src/mcp-server/executor.py` | `SessionCtx` 加 `correlation_id`;`LocalDockerExecutor.exec` 传 `user_agent` | ✏️ 改 | ~4 行 |
| `src/worker/worker.py` | `ExecRequest` 加 `user_agent`;`create_subprocess_shell` 加 `env=` | ✏️ 改 | ~4 行 |
| `src/mcp-server/sandbox_manager.py` | `exec` 前置 `export AZURE_HTTP_USER_AGENT` wrapper | ✏️ 改 | ~5 行 |
| `src/mcp-server/requirements.txt` | 加 `azure-monitor-ingestion` | ✏️ 改 | 1 行 |
| `provisioning/aca/*.bicep` | 新模块 `audit.bicep`(DCR,自带 endpoint)+ 表/RBAC/env/diag 增补(§5) | 🆕+✏️ | Bicep |

> **设计原则**:existing 文件里每处改动都只是"**取一个值 / 调一个函数**",判断、SDK、降级、格式全部藏在 `audit.py`。
> 以后换 sink(Log Analytics ⇄ stdout)、改 UA 格式、加字段,都只动 `audit.py`,不回头碰 `main.py`。

---

## 2. 新建 utility:`src/mcp-server/audit.py`(接口设计)★

这是问题 ③ 的答案。对外只暴露四样东西,`main.py` / `executor` 直接 call:

```python
"""audit.py — 唯一一处:发"每次 tool call 的权威审计事件",并铸造把原生 Azure 日志
行 join 回来的 correlation id。

main.py / executor 只 call 这些:
    cid = new_correlation_id()                       # 每次 tool call 一个
    ip  = client_ip()                                # 抓用户真实 IP(层 1 字段)
    ua  = build_user_agent(cid, oid)                 # 注入 AZURE_HTTP_USER_AGENT(层 2)
    await get_audit_sink().record(AuditEvent(...))   # 写权威行(层 1)

其余全部藏在这里:Log Analytics ingestion、凭据、stdout 兜底、never-raise 语义、
以及从 fastmcp 请求里抓 IP 的版本细节。换 sink 是改 env、不是改代码。
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


# ---- 接口 1:correlation id(每次 tool call 一个) --------------------------
def new_correlation_id() -> str:
    """hex(无短横)=> 放进 User-Agent token 安全,也好在 KQL 里 extract。"""
    return uuid.uuid4().hex


# ---- 接口 2:UA 注入串(层 2) ------------------------------------------------
def build_user_agent(correlation_id: str, oid: str | None = None) -> str:
    """追加到 AZURE_HTTP_USER_AGENT 的串。

    默认只放 GUID(选型 §7)。置 AUDIT_UA_INCLUDE_OID=1 才额外盖 oid(opt-in);
    无论如何 GUID 都是权威 join key。
    """
    tok = f"mcp/{correlation_id}"
    if oid and os.environ.get("AUDIT_UA_INCLUDE_OID", "0") == "1":
        tok += f";oid/{oid}"
    return tok


# ---- 接口 3:抓用户真实 IP(层 1 字段;实现放这里,main.py 直接 call) --------
def client_ip() -> str | None:
    """入口 HTTP 请求的 X-Forwarded-For 首跳 = 用户真实 IP(只在 MCP 入口存在)。

    fastmcp 的 HTTP 请求依赖符号按版本核对(get_http_headers / get_http_request);
    取不到就降级 None——绝不因为抓不到 IP 而挂掉 tool。把这个版本细节关在 util 里,
    main.py 不碰 HTTP 层。
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        h = get_http_headers() or {}
        xff = h.get("x-forwarded-for")
        return xff.split(",")[0].strip() if xff else None
    except Exception:
        return None


# ---- 接口 4:审计事件 + sink ------------------------------------------------
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
        # 列名要和 DCR stream / 表 schema(§5)对齐;TimeGenerated 是自定义表必填列。
        d = asdict(self)
        d["TimeGenerated"] = d.pop("ts")
        return d


class AuditSink:
    async def record(self, event: AuditEvent) -> None:  # 约定:永不抛、不阻塞 tool
        ...


class StdoutAuditSink(AuditSink):
    """本地/未配 DCR 时的兜底:结构化 JSON 一行,替代原来的自由文本 logger.info。"""

    async def record(self, event: AuditEvent) -> None:
        try:
            logger.info("AUDIT %s", json.dumps(event.to_row(), ensure_ascii=False))
        except Exception as e:
            logger.warning("audit stdout failed: %s", e)


class LogAnalyticsAuditSink(AuditSink):
    """经 Logs Ingestion API + DCR 送进 Log Analytics 自定义表。"""

    def __init__(self, endpoint: str, rule_id: str, stream: str):
        from azure.identity.aio import DefaultAzureCredential
        from azure.monitor.ingestion.aio import LogsIngestionClient

        self._stream = stream
        self._rule_id = rule_id
        self._cred = DefaultAzureCredential()          # = MCP app 的 managed identity
        self._client = LogsIngestionClient(endpoint, self._cred)

    async def record(self, event: AuditEvent) -> None:
        # 审计绝不能拖垮/失败 tool call:限时 + 吞异常(权威性 vs 可用性,选可用性;
        # 真要更强保证,后面再做本地 WAL/重试)。
        try:
            await asyncio.wait_for(
                self._client.upload(self._rule_id, self._stream, [event.to_row()]),
                timeout=float(os.environ.get("AUDIT_TIMEOUT", "5")),
            )
        except Exception as e:
            logger.warning("audit ingestion failed (%s); cid=%s", e, event.correlation_id)


_sink: AuditSink | None = None


def get_audit_sink() -> AuditSink:
    """单例。配了 AUDIT_DCR_* 就用 Log Analytics,否则 stdout 兜底。"""
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
            logger.info("audit sink: stdout (未配 AUDIT_DCR_*)")
    return _sink
```

> **为什么审计从 `_exec` 发、而不是 middleware**:`_exec` 手里同时有 `command`、`exit_code`(executor 返回)、
> 以及 `SessionCtx`(带 correlation id),一次就能凑齐**完整**事件;middleware 只负责铸 id 和抓 middleware 才拿得到
> 的东西(client_ip、upn),塞进 state 让 `_exec` 取。

---

## 3. `main.py` 的最小改动(问题 ①,主文件)

### 3.1 顶部 import(+2 行)

```python
import audit                       # 新 util
from audit import AuditEvent
```

### 3.2 middleware:铸 correlation id + 抓 client_ip(改 `on_call_tool`)

现状(`main.py:152-162`)只 stash 了 oid/session/conversation 并打一行 `logger.info`。改成:

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
        # 新增三样,给 §3.3 的 _exec 用:
        await fctx.set_state("user_upn", claims.get("preferred_username") or claims.get("upn"))
        await fctx.set_state("client_ip", audit.client_ip())      # 抓 X-Forwarded-For(见 audit.py 接口 3)
        await fctx.set_state("correlation_id", audit.new_correlation_id())
    return await call_next(context)                              # 删掉原来那行 logger.info
```

> `client_ip()` 的实现放在 `audit.py`(§2 接口 3),`main.py` 直接 call,不在主文件里塞 HTTP 细节。
> `get_http_headers` 的确切符号按你的 fastmcp 版本核对(§8 风险表);抓不到 IP 只少一个字段,不影响其余记录。

### 3.3 `_exec`:调一次 `record()`,替换所有旧日志(改 `_exec` + 两个 tool)

现状 `_exec`(`184-193`)只 build `SessionCtx` 然后 exec。改成:

```python
async def _exec(group: str, command: str, ctx: Context, explanation: str | None = None):
    correlation_id = await ctx.get_state("correlation_id")
    sctx = SessionCtx(
        user_oid=await ctx.get_state("user_oid"),
        session_id=await ctx.get_state("session_id"),
        conversation_id=await ctx.get_state("conversation_id"),
        group=group,                       # type: ignore[arg-type]
        correlation_id=correlation_id,     # ← 新字段(§4.1),给 UA 注入用
    )
    result = await executor.exec(sctx, command)
    # —— 这一句取代 middleware/diagnose_bash/action_bash 里原来所有的 logger.info ——
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

两个 tool 只改一行(删掉各自的 `logger.info`):

```python
async def diagnose_bash(command: str, ctx: Context) -> dict:
    return await _exec("diagnose", command, ctx)

async def action_bash(command: str, explanation: str, ctx: Context) -> dict:
    return await _exec("action", command, ctx, explanation=explanation)
```

> `sp_appid` 可选:本地路径能从 env(`DIAGNOSE_SP_APP_ID` / `ACTION_SP_APP_ID`)按 group 填;不填也不影响主链路。

---

## 4. UA 注入(层 2):`executor` / `worker` / `sandbox_manager`(问题 ①,续)

### 4.1 `executor.py`:`SessionCtx` 加字段(+1 行)

```python
@dataclass(frozen=True)
class SessionCtx:
    user_oid: str | None
    session_id: str | None
    conversation_id: str | None
    group: Group
    correlation_id: str | None = None      # ← 新增;UA 注入的来源
```

### 4.2 本地 backend:`LocalDockerExecutor.exec` 传 UA 给 worker(+3 行)

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

### 4.3 `worker.py`:带外设 env(**不进 command 字符串**)(+4 行)

```python
class ExecRequest(BaseModel):
    command: str
    timeout: float
    user_agent: str | None = None          # ← 新增

@app.post("/exec")
async def exec_command(req: ExecRequest):
    env = dict(os.environ)
    if req.user_agent:
        env["AZURE_HTTP_USER_AGENT"] = req.user_agent   # 设在子进程环境,az 自动追加到 UA
    proc = await asyncio.create_subprocess_shell(
        req.command, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ...
```

这是**最干净的注入**:env 设在 worker 起的 shell 进程上,LLM 的 command 作为子进程继承它,而 UA 值**从没出现在
command 字符串里**,所以 LLM 无法通过普通命令拼接改掉它(仍能在同一 shell 里 `unset`,见 §8 边界)。

### 4.4 ACA backend:`sandbox_manager.exec` 前置 `export`(+5 行)

ACA 的 `client.exec` 只吃一个命令串、没有独立 env 通道,所以在 wrapper 里前置一个 `export`:

```python
from audit import build_user_agent

def _wrap(self, ctx: SessionCtx, command: str) -> str:
    inner = self._scope_to_workspace(ctx, command)     # 既有:mkdir/cd 到工作区
    if ctx.correlation_id:
        ua = build_user_agent(ctx.correlation_id, ctx.user_oid)
        return f"export AZURE_HTTP_USER_AGENT={shlex.quote(ua)}\n{inner}"
    return inner

async def exec(self, ctx: SessionCtx, command: str) -> ExecResult:
    self._ensure_reaper()
    client = await self.get_or_create(ctx)
    result = await client.exec(self._wrap(ctx, command))   # ← 用 _wrap 取代直接 _scope_to_workspace
    ...
```

> ⚠️ ACA 是 **best-effort**:`export` 在同一 shell 里可被 `unset` 覆盖。这不是漏洞——层 2 本就是"给诚实路径 +
> 事后取证"的便利,**权威永远是层 1 那张表**(选型 §7)。若 sandbox SDK 的 `client.exec` 支持 `env=`,优先用它。

---

## 5. 需要 provision 什么(问题 ②)—— 改 existing Bicep,不用 az

全部走 IaC。**复用**现有工作区(`environment.bicep` 里的 `logs`,即 `${name}-logs`)。改动:新建 1 个模块
`audit.bicep`,其余是对 existing 模块的小增补。

### 5.0 先回答:DCE 是必须的吗?—— 不是

- Log Analytics **工作区本身没有**给新版 Logs Ingestion API 用的接收 endpoint(它是"目的地",不是 HTTP 入口)。
  (旧的 HTTP Data Collector API 才有 per-workspace 的 `*.ods.opinsights.azure.com` + shared key —— 就是
  `environment.bicep:34` 给 ACA 环境 console 日志用的那种;那套已弃用,审计不走它,选型 §8.1 已排除。)
- 新版必须有一个 **ingestion endpoint**,它来自:**(a) 一个独立 DCE**,或 **(b) DCR 自带的 endpoint** —— 把 DCR
  建成 `kind: 'Direct'`,它就带 `properties.endpoints.logsIngestion`,直接往 DCR 发,**不需要 DCE**。
- 所以 **DCE 不是必须的**。本方案走 (b),**少一个资源**。若某区域/策略不吐 DCR endpoint,再退回 (a) 加一个
  DCE(§5.2 备注)。

### 5.1 `environment.bicep` — 加自定义表 + 输出 workspaceId

在 `logs` 资源后面加表(child resource),并加一条 output:

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
        { name: 'group', type: 'string' }        // 注:KQL 里 group 是关键字,查询时写 ['group']
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

### 5.2 新模块 `modules/audit.bicep` — DCR(自带 endpoint,免 DCE)

只建 DCR,不含 RBAC(RBAC 需要 mcp principalId,放 §5.4 破环)。

```bicep
@description('Resource prefix.')
param name string
@description('Region.')
param location string
@description('Log Analytics workspace resource id(目的地).')
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
  kind: 'Direct'                       // 自带 logsIngestion endpoint,免 DCE
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
        outputStream: streamName        // 原样落进自定义表
      }
    ]
  }
}

output dcrName string = dcr.name
output dcrImmutableId string = dcr.properties.immutableId
output dcrEndpoint string = dcr.properties.endpoints.logsIngestion
output streamName string = streamName
```

> ⚠️ **DCE 回退**:若你的区域/apiVersion 不吐 `endpoints.logsIngestion`,加一个
> `Microsoft.Insights/dataCollectionEndpoints`,DCR 上设 `dataCollectionEndpointId: dce.id`,并把 `dcrEndpoint`
> 改成 `dce.properties.logsIngestion.endpoint`。逻辑不变,只多一个资源。

### 5.3 `mcp-app.bicep` — 加 3 个 param + 3 个 env

```bicep
// --- audit (层 1) ---
param auditDcrEndpoint string
param auditDcrImmutableId string
param auditStreamName string
```

env 数组里追加(接在现有 `SANDBOX_DISK_IMAGE` 那条后面):

```bicep
            { name: 'AUDIT_DCR_ENDPOINT', value: auditDcrEndpoint }
            { name: 'AUDIT_DCR_RULE_ID', value: auditDcrImmutableId }
            { name: 'AUDIT_STREAM_NAME', value: auditStreamName }
```

并在 `requirements.txt` 加:`azure-monitor-ingestion>=1.0`。

### 5.4 `rbac.bicep` — 加 Monitoring Metrics Publisher(scope = DCR)

Logs Ingestion API 需要的就是 DCR 上的 **Monitoring Metrics Publisher**。`rbac` 已在 `mcpApp` 之后跑、已有
`mcpPrincipalId`,把 DCR 名字传进来即可(破环:DCR 建在前、role 绑在后):

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

### 5.5 `main.bicep` — 串起来(注意顺序,避免 module 循环)

`audit` 在 `environment` 之后、`mcpApp` 之前;role 绑定在 `rbac`(`mcpApp` 之后):

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

`mcpApp` 的 params 追加:

```bicep
    auditDcrEndpoint: audit.outputs.dcrEndpoint
    auditDcrImmutableId: audit.outputs.dcrImmutableId
    auditStreamName: audit.outputs.streamName
```

`rbac` 的 params 追加:

```bicep
    auditDcrName: audit.outputs.dcrName
```

> 依赖链:`environment`(表 + workspaceId)→ `audit`(DCR,出 endpoint/immutableId)→ `mcpApp`(吃 env)
> → `rbac`(mcp principal × DCR 绑 role)。**无环**。

### 5.6 层 2 前置:目标资源的 diagnostic setting(也走 Bicep)

UA 要能被查到,目标服务得把数据面日志送进工作区。**stack 内的 workspace storage** 正好可当端到端测试目标——
在 `storage.bicep` 给现成的 `blobService`('default')挂一条(需要 workspaceId,从 `main.bicep` 传入):

```bicep
param workspaceId string
resource blobDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-dataops-logs'
  scope: blobService                    // storage.bicep 里现成的 'default' blobServices 子资源
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

> 客户自己要 trace 的其它资源(他们的 storage/ADF/…)在**他们的 IaC** 里各开一条,不属于本 stack。

### 新资源清单(问题 ② 的答案)

| 资源 | 在哪 | 一次性/每资源 |
|---|---|---|
| 自定义表 `MCPAudit_CL` | `environment.bicep`(child of `logs`) | 一次性 |
| DCR(`kind: Direct`,自带 endpoint) | 新 `modules/audit.bicep` | 一次性 |
| **DCE** | —— | **不需要**(§5.0) |
| RBAC:Monitoring Metrics Publisher | `rbac.bicep`(scope = DCR) | 一次性 |
| env `AUDIT_DCR_*` | `mcp-app.bicep` | 一次性 |
| pip `azure-monitor-ingestion` | `requirements.txt` | 一次性 |
| diagnostic setting | `storage.bicep`(测试目标)/ 客户 IaC | 每目标资源 |

---

## 6. 端到端数据流 + join 查询

```
tool call
  → middleware: cid=new_correlation_id(), 抓 client_ip/upn → state
  → _exec: SessionCtx(correlation_id=cid) ─┬─ executor 注入 AZURE_HTTP_USER_AGENT=mcp/<cid>  (层 2)
                                            │      → az → 目标服务原生日志 UserAgentHeader 带 mcp/<cid>
                                            └─ audit.record(AuditEvent(cid, oid, ip, command, exit_code)) (层 1)
                                                   → MCPAudit_CL
```

安全分析师从一条可疑 storage 日志出发,回连真人:

```kusto
StorageBlobLogs
| where OperationName == "DeleteBlob"
| extend cid = extract(@"mcp/([0-9a-f]+)", 1, UserAgentHeader)
| join kind=leftouter (MCPAudit_CL) on $left.cid == $right.correlation_id
| project TimeGenerated, AccountName, ObjectKey, cid,
          user_oid, user_upn, client_ip, command, explanation, exit_code
```

---

## 7. 落地顺序(对应选型 §9 待办)

1. 写 `audit.py`(§2)+ `requirements.txt` 加包 → **先接 StdoutAuditSink**(不配 DCR),本地就能看到结构化 `AUDIT {…}`。
2. 改 `main.py`(§3)+ `executor.py`(§4.1/4.2)+ `worker.py`(§4.3),本地 `docker compose up` 重建 worker 镜像。
3. 改 `sandbox_manager.py`(§4.4)—— ACA 路径。
4. `az deployment sub create -f provisioning/aca/main.bicep`(§5 的 Bicep 改动)→ 表/DCR/RBAC/env 一次性到位,
   sink 自动切到 Log Analytics,`MCPAudit_CL` 开始进数据。
5. Bicep 里 `storage.bicep` 那条 diagnostic setting(§5.6)一起部署 → 走一次 diagnose 的 blob 读 → 等 5~15 min。
6. 跑 §6 的 join 查询,确认 `UserAgentHeader` 里的 `mcp/<cid>` 能 join 回 `MCPAudit_CL` → **端到端实测成立**。

---

## 8. 风险与边界(诚实清单)

| # | 点 | 处理 |
|---|---|---|
| 1 | UA 可被同 shell `unset`/覆盖 | 层 2 是 best-effort 佐证;**权威在层 1 表**。local 走 worker `env`(更难改),ACA 走 `export`(可改) |
| 2 | 审计上报增加时延/可能失败 | `record()` 限时 + 吞异常,**永不阻塞/失败 tool**;丢事件只告警。要更强再加本地 WAL |
| 3 | `audit.client_ip()` 的 fastmcp 符号 | 版本相关(get_http_headers / get_http_request),落地核对;取不到降级 None,不影响其余字段 |
| 4 | DCR `endpoints.logsIngestion` 区域支持 | 主路径用 Direct DCR 自带 endpoint;不支持时按 §5.2 备注退回 DCE |
| 5 | 表列 `group` 是 KQL 关键字 | 查询里写 `['group']`,或建表时改名 `tool_group`(需同步 `AuditEvent` 字段) |
| 6 | `exit_code`/`command` 落进审计表 | 表默认租户内可读——按需对 `MCPAudit_CL` 收紧 RBAC / 缩短 retention |
| 7 | 控制面(ADF/Batch 等)基本不记 UA | 层 2 主覆盖数据面;控制面归因由层 1 表兜底(见选型 §8.2) |
| 8 | worker 镜像需重建 | `worker.py` 改了 → `docker compose build` / 重推 ACR |

---

## 9. 直接回答你的三个问题

1. **代码改哪里?** —— `audit.py`(新建,§2)+ `main.py` middleware/`_exec`/两个 tool(§3)+ `executor.SessionCtx`
   与 `LocalDockerExecutor`(§4.1/4.2)+ `worker.py`(§4.3)+ `sandbox_manager.exec`(§4.4)。existing 每处只加几行。
2. **要 provision 什么(全走 existing Bicep,不用 az)?** —— 新模块 `modules/audit.bicep` 建 **DCR(`kind: Direct`,
   自带 endpoint,免 DCE)**;`environment.bicep` 加表 `MCPAudit_CL`;`rbac.bicep` 加 Monitoring Metrics Publisher;
   `mcp-app.bicep` 加 `AUDIT_DCR_*` env;`main.bicep` 串起来;`requirements.txt` 加 `azure-monitor-ingestion`。
   工作区复用现有 `logs`。层 2 另需目标资源开 diagnostic setting(§5.6,`storage.bicep` 里已给测试目标)。
3. **能不能做个 util 让主文件直接 call、替换现有 log?** —— **能**。`audit.py` 暴露
   `new_correlation_id()` / `client_ip()` / `build_user_agent()` / `get_audit_sink().record()` 四个接口;`main.py` 的旧
   `logger.info` 用一行 `await ...record(AuditEvent(...))` 取代,判断/SDK/降级/抓 IP 全沉在 util 里。**这就是
   "existing minimum effort + 新 util 暴露接口"的落地形态。**

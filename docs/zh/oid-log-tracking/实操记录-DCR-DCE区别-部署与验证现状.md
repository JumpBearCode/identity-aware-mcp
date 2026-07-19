---
title: "实操记录：DCR/DCE 区别、审计+UA 注入的部署与验证现状"
date: 2026-07-13
tags:
  - implementation-log
  - audit
  - dcr
  - dce
  - logs-ingestion
  - user-agent
  - oid-tracking
status: 已部署到 ACA（非 IaC 全量，见 §5）/ 核心链路已全部验证 / 未 commit
sources:
  - "src/mcp-server/audit.py（新建）"
  - "src/mcp-server/{main,executor,sandbox_manager}.py, src/worker/worker.py（改动）"
  - "provisioning/aca/modules/audit.bicep + audit-standalone.bicep"
  - "实测环境 tenant 9ea91fbb-…, sub ee5f77a1-…, workspace dataops-aca-logs (4de6b3e7-…)"
verified:
  - "MCPAudit_CL 写入：correlation_id=2a3e13f7… 的行含真人 jumpbear0920@outlook.com + 真实 IP 107.136.48.82（MCP 进程经 LogAnalyticsAuditSink 写入）"
  - "层2 storage：2a3e13f7… 在 StorageBlobLogs.UserAgentHeader"
  - "层2 keyvault：2a3e13f7… 在 AzureDiagnostics.clientInfo_s（跨区延迟后已 ingest，三处闭环）"
  - "KV 成功读：给 diagnose SP 加 access policy(get,list) 后，secret list 返回真实 secret 名，UA=mcp/2a3e13f7…"
---

# 实操记录：DCR/DCE、部署与验证现状

> 本次实操 session 的如实记录：改了什么、部署了什么、**没**做什么、**已验证什么、还在等什么**，
> 外加你点名的 4 个问题（① DCR/DCE 区别、② 还等什么、③ 为何不用 DCE、④ 怎么 implement）。
> 设计与选型的**为什么**见 [选型文档](./实现方案-SP操作的用户归因-Azure日志体系与最终技术选型.md)
> 与 [落地方案](./落地实现-审计工具audit-py与UA注入-代码改动与Provision清单.md)。

---

## 1. DCR 与 DCE 的区别（full name 标出）★【问题①】

| | **DCR = Data Collection Rule（数据收集规则）** | **DCE = Data Collection Endpoint（数据收集端点）** |
|---|---|---|
| 是什么 | 一条**路由/schema 规则**：进来的数据属于哪个 stream、按什么列解析、落到哪张表 | 一个**入口 URL 资源**：你把数据 POST 到它 |
| 类比 | 分拣规则（这批货送哪个仓库、哪个货架） | 收货口地址（往哪送） |
| 计费 | 免费 | 免费 |

**摄入一条自定义日志到 Log Analytics，逻辑上永远需要两件事**：
1. 一个**入口 URL**（往哪 POST）——术语叫 *logs ingestion endpoint*；
2. 一条**规则**（进来后怎么解析、路由）——就是 **DCR**。

区别只在于**第 1 件（入口 URL）以什么形式存在**：作为独立的 **DCE** 资源，还是**内嵌进 DCR**（见 §2）。

> ⚠️ 别和另一套东西混：Log Analytics **工作区**没有给新版 API 用的接收 endpoint。旧版
> HTTP Data Collector API 那种 per-workspace `*.ods.opinsights.azure.com` + shared key 已弃用，
> 本方案不用它（选型 §8.1 已排除）。

---

## 2. 为什么没用 DCE，只用了 DCR ★【问题③】

因为把 DCR 建成 **`kind: 'Direct'`**，它就**自带一个内置的 ingestion endpoint**，直接往这个 DCR 发数据即可，**不需要单独的 DCE 资源**。

**实测证据**（我部署的那个 DCR）：

```
kind = "Direct"
自带 endpoint = https://dataops-aca-audit-dcr-5ee7-westus2.logs.z1.ingest.monitor.azure.com
引用的独立 DCE = null（没有引用任何 DCE）
整个 RG 里 DCE 数量 = 0
```

而且我用这个自带 endpoint POST 数据实测返回 **204、行进表**，证明它真能用。

**什么时候才需要独立 DCE**（我们三条都不沾，所以省掉）：
1. 多个 DCR 要**共享**同一个入口 URL；
2. 走 **Private Link / AMPLS**（DCE 是绑私网的那个资源）；
3. 某些区域/合规配置显式要求 DCE。

**成本层面**：DCE、DCR 都免费，只有**数据摄入**按量计费（审计行 ~0.5KB，几百万次调用才 ~$2）。
所以选 `kind=Direct` **不是为了省钱**，而是**少一个资源、少一层 `dataCollectionEndpointId` 关联**。

> 若某区域/apiVersion 不吐 `endpoints.logsIngestion`，退回加一个 DCE、DCR 设
> `dataCollectionEndpointId` 即可，逻辑不变。

---

## 3. 这个东西是怎么 implement 的 ★【问题④】

两层，一条 correlation id 串起来。

### 3.1 层 1 — 权威审计（MCP 自己写一张表）

- 新建 `src/mcp-server/audit.py`，对外 4 个接口：`new_correlation_id()` / `client_ip()` /
  `build_user_agent()` / `get_audit_sink().record()`。
- `main.py` 的 middleware：每次 tool call 生成一个 correlation id，抓用户真实 IP
  （`X-Forwarded-For`，只在 MCP 入口存在）和 upn；`_exec` 里调一次 `record(AuditEvent(...))`，
  **取代**原来那行简陋的 `logger.info`。
- sink 二选一（看 env）：配了 `AUDIT_DCR_*` → `LogAnalyticsAuditSink`（经 Logs Ingestion API +
  DCR 写入 `MCPAudit_CL`）；没配 → `StdoutAuditSink`（落容器 stdout 兜底）。

### 3.2 层 2 — 原生日志可回溯（把 id 注入 User-Agent）

- correlation id 拼成 `mcp/<guid>`，设成执行进程的环境变量 `AZURE_HTTP_USER_AGENT`；
  `az`/Azure SDK 会把它**自动追加到每个出站调用的 User-Agent**。
- 本地路径：`worker.py` 把它设在子进程 env（`create_subprocess_shell(env=...)`，
  **不进命令字符串**，防篡改）。
- ACA 路径：`sandbox_manager._wrap()` 每次 exec 前 `export AZURE_HTTP_USER_AGENT=...`。
- 于是 storage / key vault 的**原生日志**里就带着 `mcp/<guid>`，拿它 join 回 `MCPAudit_CL`。

### 3.3 一次调用产生三处记录（用同一个 id 串起来）

```
一次 diagnose_bash 调用
  ├─ MCPAudit_CL          ← 权威行：谁(upn)、真实IP、命令、结果  [correlation_id=<guid>]
  ├─ StorageBlobLogs      ← UserAgentHeader 含 mcp/<guid>   （原生只显示 SP + sandbox IP）
  └─ KeyVault AuditEvent  ← clientInfo_s   含 mcp/<guid>   （原生只显示 SP + sandbox IP）
```

### 3.4 一个实测发现：每个服务 UA 落在**不同列**

| 服务 | 记 UA 的列 |
|---|---|
| Storage `StorageBlobLogs` | `UserAgentHeader` |
| Key Vault `AzureDiagnostics` | `clientInfo_s` |

join 查询要按服务用对应列名 `extend cid = extract(@"mcp/([0-9a-f]+)", 1, <该服务的列>)`。

### 3.5 link-back 查询（KQL）

```kusto
// storage 例：从一条可疑访问回连真人
StorageBlobLogs
| extend cid = extract(@"mcp/([0-9a-f]+)", 1, UserAgentHeader)
| where isnotempty(cid)
| join kind=leftouter (MCPAudit_CL) on $left.cid == $right.correlation_id
| project TimeGenerated, OperationName, StatusCode,
          storageRowId=CorrelationId, requesterSP=RequesterObjectId,
          真人=user_upn, 真实IP=client_ip, 命令=command
// key vault：把上面第 2 行换成 extract(..., clientInfo_s)
```

---

## 4. 这次到底做了什么 / 没做什么 / 部署了什么

### 4.1 改了代码（**未 commit**，都在工作区）

| 文件 | 改动 |
|---|---|
| `src/mcp-server/audit.py` | 🆕 审计工具（correlation id / client_ip / UA / sink） |
| `src/mcp-server/main.py` | middleware 生成 id+抓 IP；`_exec` 写审计；删旧 `logger.info` |
| `src/mcp-server/executor.py` | `SessionCtx.correlation_id`；本地 executor 传 UA |
| `src/worker/worker.py` | 子进程 env 设 `AZURE_HTTP_USER_AGENT` |
| `src/mcp-server/sandbox_manager.py` | ACA 路径 `export` UA |
| `src/mcp-server/requirements.txt` | 加 `azure-monitor-ingestion` |
| `provisioning/aca/modules/audit.bicep` | 🆕 正式 DCR 模块 |
| `provisioning/aca/modules/{environment,mcp-app,rbac,storage}.bicep` + `main.bicep` | 接入审计（表/env/角色/diag/串联） |
| `provisioning/aca/audit-standalone.bicep` | 🆕 **一次性补丁**（见 §5，用完应弃） |

### 4.2 真部署到 Azure 的（live）

| 项 | 值 / 说明 |
|---|---|
| 镜像 | `mcp-server:ua-audit-20260713a` → ACR |
| MCP 容器 App | 切到 revision `--0000011`（带 `AUDIT_DCR_*` env，跑新代码） |
| `MCPAudit_CL` 表 | 建在 `dataops-aca-logs` |
| DCR | `dataops-aca-audit-dcr`（`kind=Direct`，自带 endpoint，**无 DCE**） |
| 角色 | MCP MI（`17acde50…`）→ DCR 上 Monitoring Metrics Publisher |
| diagnostic settings | storage `dataopsacavyq3trlvkn4za/blob` + key vault `stanleyakvprod` → 工作区 |
| KV access policy | diagnose SP（`40eccd97…`）→ `stanleyakvprod` **secret get+list**（为测成功读而加，最小、只读、只此 vault） |

### 4.3 **没做**的（诚实）

- **没跑全量 `main.bicep`**：它是订阅级、`mcpImage` 默认占位镜像，一把全量部署会把线上 MCP
  打回占位镜像并重跑 identity/FIC 等脆弱模块。审计设施是用 §5 的补丁单独上的。
- **没 commit** 任何代码。

---

## 5. 两个 Bicep 的关系（务必理清）

| 文件 | 定位 | 命运 |
|---|---|---|
| `provisioning/aca/modules/audit.bicep` | **正式模块**，已接进 `main.bicep`。全新完整部署时审计设施从这里长出来 | **保留**（唯一真相） |
| `provisioning/aca/audit-standalone.bicep` | **一次性补丁**：只建 表+DCR+角色，装到**已在跑**的栈上，避免全量重跑 `main.bicep` 的风险 | **脚手架，用完即弃**（收敛到 main.bicep 后删） |

**当前 Azure 里的审计设施，是用 `audit-standalone.bicep` 上的**（部署名 `audit-standalone` /
`audit-standalone2`）。这属于**配置漂移**：同一批资源，正式模板声明了、但真正 apply 它的是补丁。
**正式 deploy 的终局**应是：跑 `main.bicep`（幂等，会认领同名的现有 DCR/表），从此 `main.bicep`
是唯一来源，删掉 standalone。这一步（含如何安全处理镜像参数、避免打回占位镜像）需要一篇独立的
**deploy 文档**，尚未写。

---

## 6. 验证结论 + 还在等什么 ★【问题②】

### 6.1 已验证 ✅ —— 核心链路**全部闭环**（三处对上了）

以最新一次 `correlation_id = 2a3e13f7aa094f8d9f19f04e37e70e88`（05:03:44Z 的 diagnose_bash 调用）为准，
**一个 id 串起三处**，全部实测命中：

| 环节 | 结论 | 证据 |
|---|---|---|
| 层1 · MCPAudit_CL（权威行） | ✅ | 该行含真人 `jumpbear0920@outlook.com` + 真实 IP `107.136.48.82`（MCP 进程经 `LogAnalyticsAuditSink` 写入） |
| 层2 · Storage | ✅ | `StorageBlobLogs.UserAgentHeader` 含 `mcp/2a3e13f7…` |
| 层2 · Key Vault | ✅ **（本次新到）** | `AzureDiagnostics.clientInfo_s` 含 `mcp/2a3e13f7…`；跨区（vault eastus / 工作区 westus2）延迟后已 ingest，count≥1 |
| KV 成功读 | ✅ | 给 diagnose SP 加 access policy(get,list) 后，`secret list` 返回真实 secret 名，带该 UA |

> **等到的结论**：写这篇时 KV 那条还在跨区 ingest；现已到达。所以 **MCPAudit_CL + storage + KV
> 三处已用同一个 correlation id 完全对上**，端到端归因链**闭环**。这是最后一块拼图。

### 6.2 还在等什么 —— **技术结论上：没有了** ★

该验证的都验证完了。唯一还"在后台跑"的是一个**与结论无关**的清理动作：

| 项 | 状态 | 影响 |
|---|---|---|
| `MCPAudit_CL` 测试行的 purge | ⏳ 异步执行中（提交后数分钟~1 小时），当前表内仍有 4 行测试数据 | **不影响任何结论**；删的是测试行、不动表结构，何时抹掉都行 |

**所以：没有任何还在等的技术结论。** 归因机制（层1 权威表 + 层2 UA 注入 + link-back）已在真实
Azure 上、跨 Storage 与 Key Vault 两个服务、用同一 correlation id 完整验证。

### 6.3 遗留清理项（待你确认再动，非"在等"）

| 项 | 建议 |
|---|---|
| `MCPAudit_CL` 4 行测试数据 | purge 已提交，等异步生效；如需立刻可再查 `purge-status` |
| 我自己身份在 DCR 上的 Monitoring Metrics Publisher | **已删**（当时为手动 POST 测试而加） |
| `audit-standalone` / `audit-standalone2` **部署记录** | 可删（纯历史元数据，删记录**不删资源**）；尚未删 |
| KV `stanleyakvprod` 的 diagnose access policy | 为测试而加；如不再需要可撤（`az keyvault delete-policy`） |
| storage/KV 的 diagnostic settings | 若只为本次测试，可按需保留或移除 |
| `audit-standalone.bicep` 文件 | 收敛到 `main.bicep` 后删 |

---

## 7. 下一步（未做，待定）

1. 写**正式 deploy 文档**：如何**只从 `provisioning/` 的正式 bicep** 部署整套（含审计），
   消除对 `audit-standalone.bicep` 的依赖，并讲清镜像参数怎么传才不会打回占位镜像。
2. 决定测试痕迹（§6.3）的去留并清理。
3. 决定是否/何时 commit。

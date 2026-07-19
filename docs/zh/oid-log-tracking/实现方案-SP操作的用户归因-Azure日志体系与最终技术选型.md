---
title: "实现方案：SP 操作的用户归因 —— Azure 日志体系、关键 ID 语义与最终技术选型"
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
status: 设计定稿 / 待实现
sources:
  - "src/mcp-server/main.py (UserAuthMiddleware, _exec)"
  - "src/mcp-server/executor.py (Executor.exec 唯一执行 chokepoint)"
  - "docs/oid-log-tracking/价值分析以及oid-tracking-start.md"
verified:
  - "实测环境：tenant 9ea91fbb-…, sub ee5f77a1-…；用只读 SP d09dfd39-… 经 diagnose_bash 查询"
  - "StorageBlobLogs schema：对工作区 dataops-aca-logs (4de6b3e7-…) getschema 实测，列名权威"
  - "AzureActivity schema：对 DefaultWorkspace (49e67e4c-…) getschema 实测"
  - "局限：4 个工作区的 StorageBlobLogs / AzureActivity 均 0 行——本订阅当前没有任何资源在打数据面日志，故只有 schema、无真实 ID 样例值"
---

# 实现方案：SP 操作的用户归因

> 承接 [价值分析](./价值分析以及oid-tracking-start.md) 里点出的核心缺口：所有操作都由**共享
> Service Principal** 执行,Azure 原生日志只认得 SP,认不出背后是**哪个真人**、来自**哪个 IP**。
> 本文把两轮讨论收敛成**可执行的最终技术选型**,并把结论建立在**对真实工作区 schema 的实测**之上,
> 而不是凭记忆。
>
> **想直接看"我们一共考虑了哪几个方案、各自采用还是排除",跳到 [§5 候选方案总览](#5-候选方案总览全部方案一览)。**

---

## 0. 一句话总结 + 最终选型

原生日志天生给不了完整归因,所以**不要试图让原生日志自己就够用**,而是:

> **权威归因由 MCP 服务端自己持有(层 1),在原生日志里只留一把"钥匙"——一个我们生成的
> correlation GUID,注入到 User-Agent(层 2)——任何记录 UA 的服务都能靠它 join 回 MCP 的权威表。**

最终架构 = 把 [§5 总览](#5-候选方案总览全部方案一览) 里**选中**的几条拼成四层:

| 层 | 做什么 | 对应方案 | 必做? |
|---|---|---|---|
| **层 1 — 权威审计** | MCP middleware 就地写结构化审计事件 → Log Analytics 自定义表(Logs Ingestion API + DCR) | **方案四** | ✅ 必做 |
| **层 2 — 原生可回溯** | `executor.exec` 带外注入 `AZURE_HTTP_USER_AGENT=mcp/<guid>` → 落到数据面日志 `UserAgentHeader` | **方案二(GUID 版)** | ✅ 必做 |
| 层 3 — 高保证(可选) | 对少量特权写用户上 per-user 身份(SP/UAMI),让原生 `Caller` 本身可区分 | **方案五** | ⬜ 可选 |
| 层 4 — 观测(可选) | client 侧 trace(LangSmith 等)记 agent 意图,**明确非安全控制** | **方案三** | ⬜ 可选 |

**明确排除**:**方案一**(靠 Azure 原生 ID join,见 §6)、**方案六**(OBO 换用户令牌,见 §8.4)、**方案七**
(resource tagging,见 §5)。

---

## 1. 背景:SP 模型为什么丢归因

- 人**零常驻写权限**,写操作全部由 per-group 的共享 SP 执行(这是本项目的安全卖点)。
- 代价:在 Azure 原生日志里,操作者字段是**那个共享 SP**,对每个用户都一样;源 IP 是 **sandbox 的
  egress IP**,不是用户笔记本。
- 于是"谁用了 SP、从哪来、跑了什么"这三件事,原生日志**一件都答不全**。本文就是补这个洞。

---

## 2. Azure 有哪些 log(日志表 · Section 一)

理解**分类**比记表名重要。Azure 的日志分三大类:

| 类别 | 是什么 | 默认开? | 进 Log Analytics 的表 |
|---|---|---|---|
| **Activity Log** | 控制面(ARM 管理操作:create/delete/update、listKeys、role assignment……),**每订阅一份** | ✅ 一直在,但要配 diagnostic setting 才进工作区 | `AzureActivity` |
| **Resource logs**(旧称 diagnostic logs) | **每服务、数据面**的操作明细,schema 各服务不同 | ❌ 要在**每个资源**上单独开 diagnostic setting 指向工作区 | 每服务一/多张表 |
| **Entra logs** | 租户级身份事件 | 部分默认 | `SigninLogs` / `AuditLogs` / `AADServicePrincipalSignInLogs` |

Resource logs 就是"Storage 有 Storage 的日志,别的服务也各有各的",但**默认全关、schema 各不相同**:

| 服务 | 代表性表 |
|---|---|
| Storage | `StorageBlobLogs` / `StorageQueueLogs` / `StorageTableLogs` / `StorageFileLogs` |
| Data Factory | `ADFPipelineRun` / `ADFActivityRun` / `ADFTriggerRun` |
| Key Vault | `AKVAuditLogs`(或 legacy `AzureDiagnostics`) |
| Azure SQL | `SQLSecurityAuditEvents` 等 |
| Cosmos DB | `DataPlaneRequests` |
| Batch / Service Bus / Event Hub / App Service / AKS | 各有各的表 |

> ⚠️ **落地成本提醒**:要 trace 到某个资源,前提是**那个资源**配了 diagnostic setting、把日志送进**同一个**
> 工作区。这是逐资源的运维成本,选型时必须记住(§8 会把它作为层 2 的前置条件)。
>
> ⚠️ **两种模式别搞混**:同一份 storage 日志,新式落到 resource-specific 的 `StorageBlobLogs`,老式(旧
> diagnostic setting)落到 `AzureDiagnostics`,**两张表列名完全不同**。查不到某列时,先确认自己在哪张表。

---

## 3. 关键 ID 的语义(关键 ID · Section 二)

带 "id" 的字段其实是**三种完全不同的东西**,混淆它们是选错方案的根源。以下概念对所有服务通用,
§4 会用 Storage 的真实列名坐实。

**① 服务端 Request ID(`x-ms-request-id`)**
- **谁生成:Azure**。每个 HTTP 请求一个,全局唯一。
- 你只能从 **HTTP 响应头**读到它。
- **correlate 到哪:什么都不 correlate。** 用途是"把这个 id 给微软支持,定位到这一条请求"。不跨服务、不回连你的系统。

**② 客户端 Request ID(`x-ms-client-request-id`)**
- **谁生成:客户端(你)**。你在请求头里设,服务原样回显并记进日志。
- **correlate 到哪:你想到哪就到哪** —— 三者里**唯一能"指回你自己系统"**的 id。
- 致命限制:**typed 的 `az` 命令(`az storage blob delete …`)没有 flag 设这个 header**,只有 `az rest` /
  SDK 能设 → 对本项目几乎不可用。

**③ Correlation ID(`correlationId`)**
- **谁生成:Azure(ARM)**。
- **correlate 到哪:Azure 自己日志内部。** 一个逻辑管理操作在 ARM 里可能 fan-out 成多条事件,Azure 用同一个
  `correlationId` 把它们**绑在一起**,方便你看"一个操作展开成了哪些子事件"。**它 correlate 的是 Azure 事件之间
  的父子关系,不回连到你的 MCP。**

> 一句话记牢:**①②③ 里只有 ②(client-request-id)能指回你系统,而它偏偏在 typed az 上设不了。**
> 这正是 §6 判定"方案一不可取"、转而用 User-Agent 的根因。

---

## 4. Storage 实例(实测 schema)

对工作区 `dataops-aca-logs` 的 `StorageBlobLogs` 跑 `getschema` 得到的**真实列**(节选身份/ID 相关列):

| 列名(实测存在) | 类型 | 含义 |
|---|---|---|
| `AuthenticationType` | string | `OAuth` / `SAS` / `AccountKey` / `Anonymous` |
| **`RequesterObjectId`** | string | 认证主体的 oid → **本模型里 = 共享 SP 的 oid**(仅 `AuthenticationType==OAuth` 时填) |
| `RequesterAppId` | string | SP 的 app/client id |
| `RequesterTenantId` / `RequesterUpn` / `RequesterAudience` / `RequesterTokenIssuer` | string | 其余身份字段(SP 场景 `RequesterUpn` 一般空) |
| `AuthenticationHash` | string | 令牌/SAS 哈希(不可逆) |
| `AuthorizationDetails` | **dynamic** | RBAC 授权明细(哪个 action/role 放行) |
| `CallerIpAddress` | string | 源 IP → **sandbox egress,不是用户** |
| `CorrelationId` | string | storage correlation id(Azure 生成,= §3 的 ③) |
| `ClientRequestId` | string | `x-ms-client-request-id`(= §3 的 ②,可控但 typed az 设不了) |
| **`UserAgentHeader`** | string | UA 字符串 → **我们注入 correlation GUID 的落点(实测存在 ✅)** |
| `OperationName` / `Uri` / `ObjectKey` | string | 干了啥、哪个 blob |

**三个必须知道的实测结论:**

1. **`RequesterObjectId` 确实存在**。之所以你在门户里"找不到",最可能是:表是**空的**(列选择器只显示有数据
   的列)/ 你看的是 legacy `AzureDiagnostics` / 或那些请求是 **SAS/account-key** 认证(此列仅 OAuth 时填)。
2. **这张表里没有独立的 `RequestId`/`TransactionId` 列**(§3 的 ① 在 resource-specific 表里不单独成列)。ID 列只有
   `CorrelationId`、`ClientRequestId`、`AuthenticationHash`。—— 这是对上一轮口述的一处更正。
3. **控制面 `AzureActivity` 实测有** `Caller`、`CallerIpAddress`、`CorrelationId`、`HTTPRequest`(内含
   `clientRequestId`/`clientIpAddress` 的 JSON)、`Claims_d`(dynamic)。但 `Claims_d` 装的是**SP 的** claims,
   不是用户的,帮不到归因。

> 📌 **schema 从哪来 & 诚实局限**:上表是 `StorageBlobLogs` 的 **Azure 官方标准 schema**,不是本环境的配置产物。
> `StorageBlobLogs` 是 **Microsoft 预定义标准表**(实测 `tableType == "Microsoft"`),**每个 Log Analytics 工作区天生
> 就"认识"它的 schema**——所以 `getschema` 返回列定义跟"有没有数据、有没有 diagnostic setting"**完全无关**(佐证:
> 同一查询在与 storage 无关的 `mlworkspace` 里也 resolve、返回 0)。
>
> 本订阅**8 个 storage account 全部 0 个 diagnostic setting**,4 个工作区的 `StorageBlobLogs` / `AzureActivity`
> **均 0 行**——即**当前没有任何资源在打数据面日志**。故本节 schema 权威(=将来开了 logging 就长这样),但
> **没有真实 ID 样例值**。要看真实值,需在某 storage account 上开 diagnostic setting → 打一条 blob 操作 → 等
> 5~15 分钟 ingestion(见 §9 待办)。

---

## 5. 候选方案总览(全部方案一览)

> 这是**我们前后两轮讨论里考虑过的所有方案**。**方案一~三**是最初提出的三条思路,**方案四~七**是后续补充。
> 下面每条给一句话 + 判定 + 详情去哪看。后文所有"方案N"都指这张表。

| 方案 | 一句话 | 判定 | 详见 |
|---|---|---|---|
| **方案一** — Correlation ID 关联 | 靠 **Azure 自己生成**的 `correlationId` / request id,去和 MCP 记录做 join | ❌ **排除** | §6 |
| **方案二** — 原生日志注入 metadata | 把**我们自己的标识**写进出站请求,让原生日志自带 → 正确形态是**注入到 User-Agent** | ✅ **采用**(层 2,注入 **GUID**) | §8.2 |
| &nbsp;&nbsp;方案二′ — 注入**原始 oid** | 方案二的变体:UA 里直接放 oid 而非 GUID,图"单表免 join" | ⚠️ **默认不用**(可显式 opt-in) | §7 |
| **方案三** — 客户端自管日志 | 在 client 侧用 LangSmith 等 trace 工具记 agent 的对话/意图 | 🔶 **可选**(层 4),**非**安全控制 | §8.4 |
| **方案四** — MCP 服务端权威审计表 | MCP 自己写结构化审计 → Log Analytics 自定义表(Logs Ingestion + DCR) | ✅ **采用**(层 1,**核心**) | §8.1 |
| **方案五** — per-user 身份 | 给用户发专属 SP/UAMI,让原生 `Caller` 本身就区分到人 | 🔶 **可选**(层 3,高保证) | §8.3 |
| **方案六** — OBO 换用户令牌 | 用用户令牌 / user delegation SAS 执行,让原生日志直接显示真人 | ❌ **排除**(破坏零常驻写权限模型) | §8.4 |
| **方案七** — resource tagging | 写操作顺手给资源打 `lastModifiedBy=<id>` 之类的 tag | ❌ **排除/边角**(仅可 tag 的资源、有竞态、不成体系) | 本节 |

**注入载体的取舍(方案二内部):** correlation GUID 理论上可搭两种载体——`x-ms-client-request-id`(§3 ②)或
User-Agent。前者在 typed `az` 上**设不了**,所以最终选 **User-Agent**(`AZURE_HTTP_USER_AGENT`,§8.2)。

**方案七为什么只是边角:** 只对**可打 tag 的资源**、且是 **mutating 操作**有效;并发写会互相覆盖、tag 会被污染,
且完全不覆盖读操作。个别高价值资源可锦上添花,但**撑不起一套归因体系**,故不纳入选型。

---

## 6. 为什么"方案一(靠 Azure 原生 ID 去 join)"不可取 ★

> 方案一 = 依赖 Azure 自己生成的 `correlationId` / request id,去和 MCP 侧记录做 join。**判定:不可取。** 三条硬理由:

1. **id 是 Azure 生成的,你得先"知道"它才能 join。** `correlationId`/request id 都是执行时才由 Azure mint 的。
   要拿到"我这条 `az` 命令产生的那个 id",只能去 **scrape `az --debug` 的 stderr** 逐个 HTTP 调用解析——版本一
   变格式就崩;且一条命令往往打多个 HTTP 请求、各有各的 id,对不齐。**脆、不可靠。**
2. **`correlationId` 只活在控制面。** 数据面的 Storage 操作**不共享**那个 ARM `correlationId`(见 §4:StorageBlobLogs
   的 `CorrelationId` 是 storage 自己的)。所以这个 join key **跨不了"控制面 + 数据面"**,覆盖不全。
3. **唯一能"指回你系统"的 ②(client-request-id),typed `az` 又设不了**(§3)。

**对比 User-Agent(方案二)为什么没这些毛病:** UA 里放的是**你自己生成的 GUID**(天然和 MCP 表对得上,零
scrape)、通过一个环境变量**自动打到每一个出站调用**(零逐命令改造)、而且**数据面服务原生就记 `UserAgentHeader`**
(§4 实测)。方案一的三个痛点它**逐条避开**。这就是最终选型用 UA、弃用方案一的根因。

---

## 7. 为什么不建议(默认)把原始 OID 也注入 UA(方案二′)

诱惑很实在:"UA 里直接塞 oid,一张 storage 表就知道是谁,连 join 都省了。" 它确有优点——单表出"谁"、少一次 join、
就算 MCP 表 ingestion 挂了原生日志里还留着 oid。但**默认不这么做**,四条理由:

1. **oid 只答"谁",不答"哪一次调用"。** 事故响应几乎从不满足于"是张三",而要"张三在哪个 session、跑了哪条完整
   命令、从哪个 IP、带的 explanation 是什么"——这些**只有 MCP 表有**。稍深一点还是得 join 回去,"单表就够"的红利
   只覆盖最浅的问题。
2. **GUID 精确到"这一次调用",oid 只精确到"这个人"。** correlation GUID 每次 tool call 一个,join 回去直接锁定到
   那条命令/时间/session;同一人一天 200 条命令,oid 分不清是哪条。作为 join key,**GUID 严格优于 oid**。
3. **UA 是可伪造字段,别给它虚假权威。** 无论塞什么,UA 都不是安全边界。塞 oid 会让人误把"日志白纸黑字写了
   oid X"当证据,而它其实能被伪造。用 GUID + 服务端表,**权威始终在你服务端写的那张表里**,UA 只是指针。
4. **少撒身份标识。** GUID 不泄漏任何东西;oid 会被复制进 N 个服务的日志,各自 retention/访问控制/导出策略不一。
   (诚实降权:同租户内 storage 本就会在用户直连时记 `RequesterObjectId`,泄漏面没那么夸张,但"能少撒就少撒"仍成立。)

> **结论**:最终选型 UA 里**默认只放 GUID**。若团队确实想要"扫一眼免 join"的便利,可作为**显式 opt-in** 追加成
> `mcp/<guid>;oid/<oid>`——但两条铁律不破:**(a) 绝不只放 oid(会丢"哪一次"的分辨率);(b) 绝不把 UA 当权威证据。**

---

## 8. 最终技术选型(收敛)

### 8.1 层 1 = 方案四 — 权威审计表(必做,先做)

把 `main.py:161` 那行 `logger.info` 升级成一条**结构化审计事件**,在 `UserAuthMiddleware.on_call_tool` 就地采集,
写入 Log Analytics 自定义表 `MCPAudit_CL`,经 **Logs Ingestion API + Data Collection Rule (DCR)**(注意:老的 HTTP
Data Collector API 已弃用,别用)。

字段(最小集):
```
correlation_guid   # 每次 tool call 生成一个,同时注入 UA(见 8.2)
ts, user_oid, user_upn
client_ip          # 从入口 X-Forwarded-For 取——用户真实 IP 只在 MCP 入口存在
session_id, conversation_id
tool, group        # diagnose_bash / action_bash
full_command, explanation
sp_appid           # 实际执行的 worker SP
target_resource_ids
exit_code
```
- 只要信任 MCP,**这张表本身就完整回答归因**;原生日志只是佐证。
- 建议做成**只追加/防篡改**(表本身不可改,可再导出到 WORM Blob 加固)。

### 8.2 层 2 = 方案二 — UA 注入 correlation GUID(必做,便宜)

在**唯一执行 chokepoint** `executor.exec(ctx, command)` 里,**带外**设置环境变量(Azure CLI/SDK 都读它、自动追加到
每个出站调用的 User-Agent):
```
AZURE_HTTP_USER_AGENT = "mcp/<correlation_guid>"
```
- **必须在 worker/sandbox 进程环境里设,绝不能拼进 LLM 给的 command 字符串**——否则恶意命令能 `unset`/覆盖它。
- `ctx` 里已有 `user_oid`/`session_id`,GUID 与层 1 的审计事件用同一个 → KQL 里
  `MCPAudit_CL | join StorageBlobLogs on $left.correlation_guid == $right.UserAgentHeader-解析出的guid` 即闭环。
- 覆盖面:**数据面**服务(Storage 最佳,§4 实测有 `UserAgentHeader`)。控制面 Activity Log 基本不记 UA → 由层 1 兜底。

### 8.3 层 3 = 方案五 — per-user 身份(可选,高保证)

给少量**特权写用户**发专属 SP/UAMI,让原生 `Caller`/`RequesterObjectId` 本身可区分到人,只维护 `appid→user` 映射。
代价:SP 泛滥、逐 SP 授 RBAC、上千用户不 scale。**只对高价值窄人群开**,不做全员。

### 8.4 层 4 = 方案三 — 客户端 trace(可选,非安全控制)+ 明确排除项

- **层 4(方案三)**:client 侧 LangSmith 等记 agent 对话/意图,回答"AI 为什么这么干、谁让它干的"。它在 Azure
  安全边界**之外**、可被绕过,**只作观测层,不作安全控制**。
- **排除 · 方案一**(靠 Azure 原生 ID join):理由见 §6。
- **排除 · 方案六**(OBO 换用户令牌 / user delegation SAS):能让原生日志显示真人,但要求**用户本人对目标资源
  有权限**,直接打破"用户零常驻写权限"模型 → 写路径不可用。
- **排除 · 方案七**(resource tagging):理由见 §5。

---

## 9. 落地改造点与待办

| # | 改造点 | 位置 | 备注 |
|---|---|---|---|
| 1 | 入口抓 `client_ip` + 生成 `correlation_guid` + 写结构化审计 | `main.py` `UserAuthMiddleware.on_call_tool` | 替换现有单行 `logger.info` |
| 2 | 把 `correlation_guid` 透传进执行上下文 | `SessionCtx`(executor.py)加字段 | 让 exec 拿得到 |
| 3 | 带外注入 `AZURE_HTTP_USER_AGENT=mcp/<guid>` | `executor.exec`(local + ACA 两个 backend) | 设进进程 env,不进 command |
| 4 | 建 `MCPAudit_CL` 表 + DCR + Logs Ingestion 权限 | provisioning(Bicep) | 层 1 的基础设施 |
| 5 | 目标资源开 diagnostic setting → 工作区 | provisioning / 客户侧 | 层 2 的**前置条件**(否则数据面无日志) |
| 6 | (验证)打一条真实 blob 日志,核对 `UserAgentHeader` 里 GUID 端到端可查 | 手动一次 | 把方案从"schema 成立"推进到"端到端实测成立" |

> §4 的诚实局限就落在待办 5/6:当前本订阅**没有任何数据面日志**,层 2 必须先有资源开了 diagnostic setting 才谈得上
> 被记录。第一步实测建议:挑一个 storage account 开 `StorageBlobLogs` → 指向 `dataops-aca-logs` → 走一次 diagnose 的
> blob 读 → 等 ingestion → 核对 `UserAgentHeader`。

---

## 10. 附:本文的核查记录

- 环境:tenant `9ea91fbb-…`,sub `ee5f77a1-…`;经 `diagnose_bash` 用只读 SP `d09dfd39-…` 查询。
- `StorageBlobLogs` 列:对 `dataops-aca-logs`(`4de6b3e7-…`)`getschema` 实测(§4 表)。
- `AzureActivity` 列:对 `DefaultWorkspace`(`49e67e4c-…`)`getschema` 实测。
- 表来源:`StorageBlobLogs` 实测 `tableType == "Microsoft"`(预定义标准表)→ schema 是 Azure 官方标准,**非本环境配置产物**;在无关工作区 `mlworkspace` 也 resolve 返回 0,证明与 diagnostic setting 无关。
- 配置现状:**8 个 storage account 全部 0 个 diagnostic setting**;4 个工作区 `StorageBlobLogs` / `AzureActivity` **均 0 行** → schema 权威、无真实 ID 样例值。

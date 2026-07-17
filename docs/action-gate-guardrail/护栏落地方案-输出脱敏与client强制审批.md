---
title: "action_bash 护栏落地方案 —— 输出侧脱敏 + client 端强制审批（pre-exec / LLM judge 为后续）"
date: 2026-07-17
tags:
  - mcp
  - guardrail
  - post-exec
  - redaction
  - secret-scanning
  - human-in-the-loop
  - pre-exec
  - roadmap
sources:
  - "src/mcp-server/main.py"
  - "src/mcp-server/executor.py"
  - "src/mcp-server/audit.py"
  - "docs/action-gate-guardrail/实现方案-action_bash-策略网关与人工审批.md"
  - "docs/oid-log-tracking/"
  - "docs/multi-client-implementation/MCP-自定义Client接入-Entra与各Agent客户端支持对比.md"
  - "https://code.claude.com/docs/en/mcp"
  - "https://code.visualstudio.com/docs/agents/approvals"
  - "https://opencode.ai/docs/permissions/"
  - "https://gofastmcp.com/servers/elicitation"
  - "https://github.com/Yelp/detect-secrets"
  - "https://github.com/gitleaks/gitleaks"
  - "https://microsoft.github.io/presidio/"
---

# action_bash 护栏落地方案 —— 输出脱敏 + client 强制审批

> 本文是多轮讨论的收敛版，取代 [`实现方案-action_bash-策略网关与人工审批`](实现方案-action_bash-策略网关与人工审批.md) 里已被 oid-log PR 落地或已过时的部分（见 Part 1）。
>
> **这一步（本迭代）只做两件事并落实：① 输出侧脱敏（post-exec gate）；② client 端强制人工审批。**
> Part 3（pre-exec 确定性网关）是紧接着的下一步设计，Part 4（pre-exec 引入 LLM judge）是更远的规划。先把 ①② 做扎实。

---

## 0. 一页纸（TL;DR）

- **可靠性的地板不是 gate，是已经 ship 的两层**：L0 = worker SP 的 RBAC（diagnose=Reader / action=Contributor，封顶爆炸半径）；L3 = oid-log 身份归因审计（correlation-id 把每条命令追到真人+真 IP）。对外讲可靠性先讲这两层。
- **本迭代补的是纵深**：① 输出侧脱敏在 **MCP server 进程内**做（确定性，不外包）；② 每次 `action_bash` 在 **client 端强制弹人工审批**（Claude Code 靠 server 端一行 `_meta`，VS Code 靠 MDM 下发一个设置）。
- **pre-exec 确定性网关（Part 3）**：不枚举命令，而是**扫输出形状 + 匹配 az 动词语法**；命中 secret 动词 → 二次确认（elicitation），命中红线 → 直接拦。
- **LLM judge（Part 4）**：意图一致性 + 绝对危险性，做成**可插拔、外置到托管模型端点**的一条 pipeline 规则，是下一步、不是这一步。

---

## Part 1 — 现状（Current Status）

### 1.1 已经落地的（oid-log PR #3 之后）

| 能力 | 状态 | 代码 |
|---|---|---|
| L0 RBAC 地板（diagnose=Reader / action=Contributor） | ✅ 已有 | Azure 侧 |
| L3 身份归因审计（`AuditEvent` 权威行 → Log Analytics `MCPAudit_CL`；correlation-id 注入 UA 关联原生 Azure 日志；never-raise/never-block） | ✅ **已落地** | `audit.py`、`main.py:_exec` |
| `SessionCtx` 携带 `correlation_id` | ✅ | `executor.py` |
| `explanation` 到达 `_exec` 并写进审计行 | ✅ **半通** | `main.py:_exec(..., explanation)` |

> 注意 `audit.py` 的审计行**不含 stdout**（只有 command/explanation/exit_code），所以审计链本身不泄露输出里的 secret。若将来往审计里塞输出，**先脱敏再写**。

### 1.2 还没有的（本方案要补）

- **`explanation` 还没进 `SessionCtx`、没到 `executor.exec`**：`_exec` 收到了 `explanation`（给审计用），但 `SessionCtx` 里没有该字段、`exec(ctx, command)` 也拿不到。pre-exec 要做「意图 vs 命令」比对，需要补这最后一跳。
- **`action_bash` 还没设 `requiresUserInteraction`**：目前只有 `annotations={readOnlyHint:false, destructiveHint:true, ...}`——而 annotations 是 hint 不是闸门，client 没义务因它弹窗。强制审批的那行 `_meta` 还没加。
- **没有任何 gate**：`GatedExecutor` / pipeline / 脱敏规则都还不存在。`_exec` 目前是 `build SessionCtx → executor.exec → 写审计`，中间没有 pre/post 处理。

### 1.3 本迭代的收敛边界

- **做**：Part 2 的 ①（post-exec 脱敏）+ ②（client 强制审批）。
- **设计好、下一步做**：Part 3（pre-exec 确定性网关）。
- **规划、更后面**：Part 4（pre-exec LLM judge）。

---

## Part 2 — 统一认知：本迭代的两个部分

### 2.1 第一部分：输出侧脱敏（post-exec gate）

**结论先行：在 MCP server 进程内做，确定性规则，不外包、不用 LLM、不上 APIM。**

为什么进程内（回顾讨论的四条硬理由）：

1. **egress 悖论**：外包就得把带 secret 的输出先发出去——为保护 secret 先把它多送一跳，安全控制自打脸。
2. **上下文**：最强检测靠「命令 + az JSON schema + key-ish 位置」，这些只有 server 进程里有；网关只看到一坨不透明 body。
3. **流式**：MCP 是 SSE，网关改写流式 body 要先 buffer、secret 会跨 chunk；进程内手里是完整 `ExecResult`，整段扫。
4. **故障耦合**：外部服务挂了 → fail-open 漏 secret / fail-closed 停服；进程内确定性扫描无此依赖，亚毫秒级。

**脱敏落点**：`_exec` 里、`executor.exec` 返回之后、回 client 之前。也可包成 `GatedExecutor` 透明地套在 inner executor 外（只需 `ExecResult → ExecResult`，无需 FastMCP Context）。二选一，推荐先直接放 `_exec`（改动最小，`_exec` 已是唯一收口）。

**四层确定性检测器**（从精确到兜底）：

```mermaid
flowchart TD
  R["ExecResult.stdout / stderr"] --> A["1. 已知格式正则<br/>JWT eyJ... / SAS sig= / PEM / 连接串 AccountKey= / storage key 88char=="]
  A --> B["2. JSON 按 key 脱值<br/>value / key / password / secret / connectionString / primaryKey ..."]
  B --> C["3. entropy 兜底<br/>仅 key-ish 位置触发, 抓 arbitrary password"]
  C --> D["4. Presidio 可选<br/>PII 实体: email / person / credit card"]
  D --> V{"命中?"}
  V -- "是" --> RED["REDACT: 掩码后放行"]
  V -- "否" --> PASS["原样放行"]
```

要点：

- **① 已知格式正则**：JWT、SAS、PEM 私钥、连接串 key、storage 88 字符 key——高精度、近零误报。可直接嵌 **`detect-secrets`（Yelp，纯 Python）** 或搬 **gitleaks** 规则集，成熟工具就是这些库，不是某个托管 SKU。
- **② JSON 按 key 脱值**：`az` 输出多为 JSON（可强制 `-o json`）。**按字段名脱值**（key 命中敏感名 → mask value），这样**任意值的 password 也抓得到**（key 的是字段名不是值）。`az keyvault secret show` 的 `.value`、`az storage account keys list` 的 `[].value` 是 100% 召回的确定项。
- **③ entropy 兜底**：只在 key-ish 位置（敏感变量赋值、连接串 `=` 后、敏感 JSON key 的 value）触发，压误报。
- **④ Presidio（可选）**：PII 才用，secret 别指望它。依赖重就放 **同 ACA app 的 sidecar 容器**（localhost 调用、依赖隔离）。

**verdict**：`REDACT`（掩码后放行，默认）或 `BLOCK`（validation 模式，检出即拦）。verdict + 命中的规则名写进 `AuditEvent`（见 2.3）。

**范围提示**：privileged secret 读走 `action_bash`；`diagnose`（Reader）本就读不到多数 secret（KV secret 值是数据面、`listKeys` 家族是控制面 action，Reader 都没有）。所以脱敏对 **action 路径**最关键，diagnose 路径挂个轻量兜底即可。

### 2.2 第二部分：client 端强制人工审批（Human-in-the-Loop）

**核心区分**：Claude Code 的强制信号是 **server 端声明**（一行 `_meta`，你的代码里）；VS Code 的强制信号是 **client 端设置**（MDM 下发，你的代码管不了，只能给 hint）。

| Client | 强制审批的参数 | 配在哪 | server 要传什么 | 能否锁到用户/operator 关不掉 |
|---|---|---|---|---|
| **Claude Code** | `_meta["anthropic/requiresUserInteraction"]=true`（v2.1.199+） | **server 的 tool 定义** | **就是这行 `_meta`**（+ 稳定 tool 名 `mcp__dataops__action_bash`、annotations） | ✅ managed settings + `disableBypassPermissionsMode:"disable"` |
| **VS Code** | `chat.tools.eligibleForAutoApproval:{"<toolId>":false}` | **client 设置（MDM/组织策略下发）** | server **传不了强制信号**，只能传 annotations 当 hint（`readOnlyHint:false`/`destructiveHint:true`）+ 稳定 toolId | ✅ 仅靠 MDM/组织策略 |
| **opencode** | 仅 `permission:{tool:"ask"}` | client 配置 | 无 server 端强制、无 elicitation | ❌ 无锁、无 server 强制 —— **弱链** |

#### Claude Code —— server 端一行 + fleet 锁

server（FastMCP）在 `action_bash` 的 tool 定义上加：

```python
@mcp.tool(
    auth=require_action,
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    meta={"anthropic/requiresUserInteraction": True},   # ← 这一行是给 Claude Code 的强制信号
)
async def action_bash(command: str, explanation: str, ctx: Context) -> dict:
    ...
```

设了之后 Claude Code **每次** `action_bash` 都强制真人交互，连 `--permission-prompt-tool` 的 `allow` 都会被转成 `deny`（「prompt 必须到达一个人」）。再叠 managed settings 锁死不让 operator 开 YOLO：

```jsonc
// Claude Code managed settings（团队机器下发）
{
  "permissions": {
    "allow": ["mcp__dataops__diagnose_bash"],
    "ask":   ["mcp__dataops__action_bash"],
    "disableBypassPermissionsMode": "disable"
  }
}
```

#### VS Code —— server 只给 hint，锁靠 MDM

server 端**没有**等价的「强制交互」meta；`annotations` 只是 hint。真正的强制靠 client 侧设置，由 MDM/组织策略下发、用户改不了：

```jsonc
// VS Code —— 组织级 / MDM 下发
{ "chat.tools.eligibleForAutoApproval": { "<action_bash tool id>": false } }
```

所以对 VS Code，server 的职责是：**稳定的 tool id + 诚实的 annotations**；强制那一环交给管设备的人。

#### opencode 是怎么回事

opencode 目前：**无 server 端强制交互、无 elicitation（FR #8251 / #23066 仍未支持）、无企业级锁**，只有 client 本地的 `permission:{tool:"ask"}`，operator 能自行改掉。

```jsonc
// opencode —— 弱链，尽力而为
{ "permission": { "dataops_diagnose_bash": "allow", "dataops_action_bash": "ask" } }
```

**取舍**：opencode 是弱链。高危操作要么**限制只允许 Claude Code / VS Code 接入**，要么把「opencode 仅走 `ask` + 组织约定」作为**残余风险显式接受**（RBAC 地板仍在，爆炸半径有上限）。别把它当可靠的强制点。

### 2.3 两部分如何进同一条审计链

`_exec` 已经每次写一条 `AuditEvent`。给它加 gate 字段，让「谁、哪条命令、gate 判了什么」在一行里可查：

```python
# audit.py: AuditEvent 增补字段
gate_verdict: str | None = None     # ALLOW / BLOCK / NEEDS_APPROVAL / REDACT
gate_rule:    str | None = None     # 命中的规则名
risk_note:    str | None = None     # 给人看的风险标注
approved_by_human: bool | None = None   # elicitation/审批结果（如可得）
```

> 一次 tool call 仍是**一条权威行**：gate 把 verdict 塞回 `_exec`，由 `_exec` 那一行带出，而不是 gate 自己另写一行（避免每次两行）。

---

## Part 3 — 怎么实现 pre-execution（确定性网关）

> 直接说做法。这一层是**确定性**的（无 LLM），紧接本迭代之后做。目标不是拦住 100% 的坏命令，而是**自动拦明显红线 + 对 secret 类命令升级二次确认 + 给审计留痕**。

### 3.1 前置改动（必须先补）

`explanation` 走到执行层，pipeline 才能做「意图 vs 命令」比对：

```python
# executor.py: SessionCtx 加字段
@dataclass(frozen=True)
class SessionCtx:
    user_oid: str | None
    session_id: str | None
    conversation_id: str | None
    group: Group
    correlation_id: str | None = None
    explanation: str | None = None      # ← 新增

# main.py: _exec 把已收到的 explanation 放进 SessionCtx（当前只喂了审计）
sctx = SessionCtx(..., explanation=explanation)
```

### 3.2 pipeline 结构（顺序：便宜确定的先跑）

```mermaid
flowchart TD
  A["command + explanation + ctx"] --> B["1. canonicalize<br/>tokenize; 展开变量/命令替换; 解析 az service+verb"]
  B --> C{"2. 红线正则?<br/>rm -rf / ; fork bomb ; 明文 dump keyvault ..."}
  C -- "命中" --> BLOCK["BLOCK: 不 call sandbox, 直接返回 ExecResult"]
  C -- "否" --> D{"3. secret 动词?<br/>keys list / list-keys / list-connection-strings / credential show / secret show / connection-string show / config appsettings list"}
  D -- "命中" --> NA["NEEDS_APPROVAL + risk_note"]
  D -- "否" --> ALLOW["ALLOW"]
  NA --> E{"client 支持 elicitation?"}
  E -- "是(Claude Code / VS Code)" --> ELICIT["ctx.elicit(risk_note) 二次确认"]
  E -- "否(opencode)" --> BASE["退回 baseline: requiresUserInteraction 已强制一次审批"]
  ELICIT -- "拒绝" --> BLOCK
  ELICIT -- "批准" --> ALLOW
```

### 3.3 三条规则的做法

1. **canonicalize（规范化解析）**：tokenize、展开变量与 `$(...)`、在**规范化后的 token** 上判策（不要在原始字符串上正则——shell 执行前会展开/去引号，「被检查的 ≠ 执行的」）。本项目 90% 是 `az`，只解析 `az` 子命令树（service + verb），据此判读/写与影响面。
2. **红线正则（少量、绝对）**：`rm -rf /`、fork bomb、明文 dump secret 等零容忍项 → `BLOCK`。红线是极少数绝对项，其余都不用正则。
3. **secret 动词检测（不枚举命令、匹配动词语法）**：ARM 把「返回 secret 的操作」一律建模成 POST 且名字以 `list*` 开头（`listKeys`/`listConnectionStrings`/`listSecrets`/`listCredentials`/`regenerateKey`），`az` 映射成下面这一小撮动词。**匹配动词，不匹配服务目录**：

   | 动词模式（~7 类） | 命中服务举例 |
   |---|---|
   | `keys list` / `list-keys` | storage、cosmosdb、cognitiveservices、batch、maps、redis、signalr |
   | `list-connection-strings` / `connection-string show` | cosmosdb、iot hub、webapp/functionapp config |
   | `credential show` / `credential list` | acr、appconfig |
   | `secret show/list`、`key show` | keyvault |
   | `... authorization-rule keys list` | servicebus、eventhubs、relay、notification-hub |
   | `config appsettings list` | functionapp、webapp（appsettings 藏连接串） |
   | `admin-key show` / `query-key list` | search |

   命中 → `NEEDS_APPROVAL` + `risk_note`（如「该命令用于取凭据，将暴露 X 的 key」）。

### 3.4 verdict 在 `_exec` 里怎么落地

pre-exec 的**人工交互（elicitation）需要 FastMCP Context**，而 `Executor.exec` 只有 `SessionCtx`。所以把 pre-exec 的编排放在 `_exec`（它持有 `ctx`），规则本身放在可单测的 `gate/` 模块：

```python
async def _exec(group, command, ctx, explanation=None):
    sctx = SessionCtx(..., explanation=explanation)
    verdict = gate.pre_exec(sctx, command)          # 纯函数: ALLOW/BLOCK/NEEDS_APPROVAL

    if verdict.action == "BLOCK":
        result = ExecResult(exit_code=126, stdout="", stderr=f"blocked by policy: {verdict.rule}")
    else:
        if verdict.action == "NEEDS_APPROVAL":
            ok = await _elicit_or_baseline(ctx, verdict.risk_note)   # 支持则 elicit, 否则退回 baseline
            if not ok:
                result = ExecResult(exit_code=126, stdout="", stderr="declined by user")
            else:
                result = await executor.exec(sctx, command)
        else:  # ALLOW
            result = await executor.exec(sctx, command)

    result = gate.post_exec(result, sctx)            # Part 2.1 脱敏
    await audit... (gate_verdict=verdict.action, gate_rule=verdict.rule, risk_note=verdict.risk_note)
    return result.to_dict()
```

要点：

- **BLOCK 不 call sandbox**：构造 `ExecResult` 直接返回。
- **NEEDS_APPROVAL 的机制是 elicitation**：注意时序——client 端的 `requiresUserInteraction` 弹框发生在 server 收到 `tools/call` **之前**（人已经批过一次）。要把 server 侧算出的 `risk_note` 交给人做**带风险标注的二次决策**，就用 `ctx.elicit`（Claude Code v2.1.76+ / VS Code 原生支持；opencode 不支持 → 退回 baseline 的那一次强制审批）。
- **baseline 永远在**：即便 elicitation 不可用，`requiresUserInteraction` 保证 action_bash 至少被真人看过一次；BLOCK 是 server 硬拦，与 client 无关。

### 3.5 动词清单别手工维护

把 3.3 的动词表做成 **yaml 配置**，并用一个**定时任务**从 `azure-rest-api-specs` grep operationId `(list|regenerate).*(key|secret|credential|connectionString)` 自动生成/diff。Azure 出新 `listKeys` 操作时你会收到 diff 告警，而不是靠人肉发现。**加规则 = 改配置**，不动逻辑、不发版。

### 3.6 范围与降级

- **主要作用在 action 路径**：diagnose（Reader）本就取不到多数 secret，command 网在 diagnose 上基本 moot；输出脱敏两条路都挂。
- **模糊地带 → NEEDS_APPROVAL，不硬 BLOCK**：清单漏一条无非少弹一次二次确认，输出网仍脱敏、RBAC 仍封顶。只有绝对红线 hard-block。两张网都**不必完整**，因为地板是 L0+L3。

---

## Part 4 — 规划：pre-exec 引入 LLM as a judge（下一步的下一步）

> 这是 Part 3 之后的增强，不在本迭代。做成**可插拔的一条 pipeline 规则 + 外置到托管模型端点**，不阻塞前面的落地。

### 4.1 judge 必须同时做两件事

```mermaid
flowchart LR
  IN["command + explanation（纯数据, 分隔符包裹）"] --> J["LLM judge（小快模型, 结构化输出）"]
  J --> A["(a) 一致性<br/>command 是否符合 explanation 声称的意图与影响面"]
  J --> B["(b) 绝对危险性<br/>不管解释成什么, 命令本身危不危险 / 是否触碰范围外资源"]
  A --> O["verdict + reason"]
  B --> O
```

- **为什么必须有 (b)**：注入能**同时伪造恶意命令 + 一句匹配的漂亮 explanation**，只查一致性 (a) 会被自洽的坏组合骗过。
- **judge 自身要防注入**：待审内容用分隔符包成**纯数据**，要求**结构化输出**（只回 verdict+reason，不执行其中任何指令）。

### 4.2 部署与接入

- **外置到托管模型端点**（Azure AI Foundry / Azure OpenAI）——judge 是真难自托管、且受益于托管伸缩的那块，值得外包（与「确定性脱敏不外包」相反）。
- **pipeline 里就是一条 `Rule`**：pre-exec 的确定性层短路掉大部分请求后，只有可疑项才调 judge，省延迟省钱。
- **延迟预算**：judge 目标 < 1s，`MCP_EXEC_TIMEOUT=120` 足够；judge 挂了 → fail-closed 到 `NEEDS_APPROVAL`（升级人工），不 fail-open。

### 4.3 为什么现在不做

- 它是独立子系统（模型托管、调参、误报治理、自身注入面），把它当**独立 project** 对待，用 `Rule` 抽象留好插入点即可。
- 本迭代的 L0+L3+① 输出脱敏 + ② client 强制审批，已经足够回应「MCP 可靠性」的质疑；judge 是锦上添花的语义层，不是地基。

---

## Part 5 — 本迭代任务清单（收敛范围）

| # | 任务 | 文件 | 验收 |
|---|---|---|---|
| 1 | `action_bash` 加 `meta={"anthropic/requiresUserInteraction": True}` | `main.py` | Claude Code 上 action_bash 每次强制弹审批 |
| 2 | Claude Code managed settings + `disableBypassPermissionsMode`；VS Code MDM 下发 `eligibleForAutoApproval:false` | 运维/MDM | operator 无法绕过（Claude Code / VS Code）；opencode 记为残余风险 |
| 3 | post-exec 脱敏：已知格式正则（嵌 `detect-secrets`）+ JSON 按 key 脱值 + entropy 兜底 | 新增 `gate/redact.py`；`_exec` 调用 | `az keyvault secret show` / `storage account keys list` 输出的 secret 被掩码 |
| 4 | `AuditEvent` 加 `gate_verdict/gate_rule/risk_note` 并在 `_exec` 写入 | `audit.py`、`main.py` | 一行审计含 gate 结论 |
| 5 |（衔接 Part 3）`SessionCtx` 加 `explanation` 并透传 | `executor.py`、`main.py` | explanation 到达执行层，为 pre-exec 备料 |

> 顺序：先 1+2（client 强制审批，改动最小、立刻见效）→ 3+4（输出脱敏 + 审计留痕）→ 5（为 Part 3 铺路）。

---

## 参考

**项目内**
- [`实现方案-action_bash-策略网关与人工审批`](实现方案-action_bash-策略网关与人工审批.md) —— 前身设计（Part 1 标注了其已过时/已落地的部分）
- `docs/oid-log-tracking/` —— L3 审计与用户归因（已落地）
- [`multi-client 接入对比`](../multi-client-implementation/MCP-自定义Client接入-Entra与各Agent客户端支持对比.md) —— 各 client 能力
- `src/mcp-server/{main,executor,audit}.py` —— gate 落点与前置改动

**外部**
- [Claude Code – MCP（`anthropic/requiresUserInteraction`、elicitation）](https://code.claude.com/docs/en/mcp)
- [VS Code – Manage approvals（`eligibleForAutoApproval`、MDM）](https://code.visualstudio.com/docs/agents/approvals)
- [opencode – Permissions](https://opencode.ai/docs/permissions/) · elicitation FR [#8251](https://github.com/anomalyco/opencode/issues/8251) / [#23066](https://github.com/anomalyco/opencode/issues/23066)
- [FastMCP – User Elicitation（`ctx.elicit`）](https://gofastmcp.com/servers/elicitation)
- [detect-secrets（Yelp）](https://github.com/Yelp/detect-secrets) · [gitleaks](https://github.com/gitleaks/gitleaks) · [Microsoft Presidio](https://microsoft.github.io/presidio/)

# action_bash 安全护栏：输出脱敏与人工审批（文档索引）

这个文件夹收录 **Identity-Aware MCP 的「Action Gate Guardrail」**从“为 `action_bash` 设计 policy gate + 人工审批”到“收敛当前迭代范围，并落地输出脱敏与多客户端强制审批”的整条**设计与实现脉络**（对应 `policy-gate` 分支与 **PR #4**）。核心实现在 `src/mcp-server/redact.py` + `main.py`，客户端审批配置在 `.claude/settings.json`、`.vscode/settings.json` 与 `opencode.json`（功能提交 `568d4de`）。

> **⚠️ 最新结论（2026-07-18，以 #5 终稿为准）：**
> 实测发现 post-exec 脱敏的 JSON 字段法可被**非纯 JSON 输出**（`echo` 抬头、多命令拼接、tsv）以及 **scope 躲避 / jq 改字段名**平凡绕过 → **后置脱敏是 hygiene，不是安全边界**。
> 最终架构收敛为：**边界 = 身份最小权限**（`diagnose` = Reader、**零 data-plane**，读不到 secret；特权读走 `action` = 提权 + 人工审批）；**post-exec 只保留 Layer2 格式正则，且只挂在 `action_bash`**，用来遮挡 storage key / SAS / 连接串 / JWT / PEM 这类**即使 elevated 也不该回显**的账户级凭据。
> 因此 **#3 描述的「三层脱敏 + 全工具生效」已被 #4 / #5 取代**；#1–#4 保留作为**推导过程**。

**按下表顺序读**即这条线的时间顺序，也是逻辑顺序：先建立完整的纵深防御蓝图（#1）→ 根据现有 RBAC / oid-log 能力收敛本迭代并落地初版脱敏（#2、#3）→ 实测证伪结构派/熵法、并把擦除器打磨到 v2 最稳形态（#4）→ **认知拐点：脱敏不是边界，边界移交身份最小权限，post-exec 退回 Layer2-only（#5）**。**只想看当前定稿结论就直接跳 #5**；想看「为什么字段法/scope 法/熵法都不成立」的完整推导，读 #4。

| # | 阶段 | 文档 | 是什么 | 首次提交 |
|---|---|---|---|---|
| 1 | 起点 · 总体设计 | [实现方案-action_bash-策略网关与人工审批.md](实现方案-action_bash-策略网关与人工审批.md) | **蓝图篇**：围绕可信 client 遭 prompt injection 的威胁模型，设计 L0 RBAC、L1 policy gate、L2 人工审批、L3 身份审计四层纵深防御，并比较确定性规则、LLM judge 与各客户端审批能力。 | 07-13 · `331dbcb` |
| 2 | 收敛 · 落地方案 | [护栏落地方案-输出脱敏与client强制审批.md](护栏落地方案-输出脱敏与client强制审批.md) | **初版落地方案**：结合已落地的 RBAC 与 oid-log 收敛范围；本迭代落实 post-exec 输出脱敏，以及 Claude Code / VS Code / opencode 的 `action_bash` 强制人工审批。（脱敏范围后被 #4 / #5 修正） | 07-17 · `fe96b74` |
| 3 | 实现 · 脱敏深挖 | [输出脱敏实现详解-redact三层逻辑与示例.md](输出脱敏实现详解-redact三层逻辑与示例.md) | **初版代码详解**：逐层拆解 `redact.py` 的 JSON key 掩码、命令域开关、已知格式正则与可选熵兜底。（三层方案已被 #5 收敛为 Layer2-only） | 07-17 · `568d4de` |
| 4 | 推导 · 证伪与 v2 尝试 | [设计-输出脱敏对任意bash输出的健壮性-分支流程与fallback取舍.md](设计-输出脱敏对任意bash输出的健壮性-分支流程与fallback取舍.md) | **推导过程**：实测复现 non-pure-JSON 绕过，逐一证伪「按字段名打码」与「fallback 到熵」；给出 `raw_decode` 片段提取 + 确定性 scope 兜底 + 删熵的 v2 尝试（§8）。这是走到认知拐点前的最后一步。 | 07-18 · `f94d903` |
| 5 | **定稿 · 身份是边界** | [从输出脱敏到身份边界-认知收敛与Layer2终稿.md](从输出脱敏到身份边界-认知收敛与Layer2终稿.md) | **当前权威 · 终稿**：由 scope 躲避 / jq 改名两个 bypass 收敛出「后置脱敏不是边界」的认知拐点；最终架构 = 身份最小权限做边界（diagnose 零 data-plane、action 提权 + 审批）+ post-exec 退回 Layer2-only 且只作用于 `action_bash`；含 gitleaks / trufflehog / detect-secrets 选型。 | 07-18 · 待提交 |

> 排序依据：每个文档**首次被创建**的 commit 时间，也是本专题的自然演进顺序。#1 是设计蓝图；#2/#3 是 oid-log 落地后的初版实施；#4 是实测证伪结构派/熵法后的 v2 推导；**#5 是认知拐点后的最终架构与选型。读实现现状以 #5 和当前代码为准。**

**一句话脉络：** 先为 `action_bash` 建立 policy gate + HITL 的纵深防御蓝图（#1）→ 基于现有安全地板先落地初版脱敏与多客户端强制审批（#2、#3）→ 实测发现后置脱敏可被任意 bash 输出绕过、并非安全边界，先把擦除器打磨到 v2 最稳形态（#4）→ **认知收敛：边界交给身份最小权限，脱敏退回 Layer2 格式正则、只在 `action_bash` 上做 hygiene（#5）**。

# action_bash 安全护栏：输出脱敏与人工审批（文档索引）

这个文件夹收录 **Identity-Aware MCP 的「Action Gate Guardrail」**从“为 `action_bash` 设计 policy gate + 人工审批”到“收敛当前迭代范围，并落地输出脱敏与多客户端强制审批”的整条**设计与实现脉络**（对应 `policy-gate` 分支与 **PR #4**）。核心实现在 `src/mcp-server/redact.py` + `main.py`，客户端审批配置在 `.claude/settings.json`、`.vscode/settings.json` 与 `opencode.json`（功能提交 `568d4de`）。

**按下表顺序读**即这条线的时间顺序，也是逻辑顺序：先建立完整的纵深防御蓝图（#1）→ 根据现有 RBAC / oid-log 能力收敛本迭代并完成落地（#2）→ 深挖三层脱敏算法、误报控制与端到端行为（#3）。只想看**当前已经落地什么、后续做什么**就直接跳 **#2**；想理解 `redact.py` 的具体判断逻辑与 before/after 示例就直接看 **#3**。

| # | 阶段 | 文档 | 是什么 | 首次提交 |
|---|---|---|---|---|
| 1 | 起点 · 总体设计 | [实现方案-action_bash-策略网关与人工审批.md](实现方案-action_bash-策略网关与人工审批.md) | **蓝图篇**：围绕可信 client 遭 prompt injection 的威胁模型，设计 L0 RBAC、L1 policy gate、L2 人工审批、L3 身份审计四层纵深防御，并比较确定性规则、LLM judge 与各客户端审批能力。 | 07-13 · `331dbcb` |
| 2 | 收敛 · 落地方案 | [护栏落地方案-输出脱敏与client强制审批.md](护栏落地方案-输出脱敏与client强制审批.md) | **当前权威方案**：结合已经落地的 RBAC 与 oid-log 收敛范围；本迭代落实 post-exec 输出脱敏，以及 Claude Code / VS Code / opencode 的 `action_bash` 强制人工审批，同时明确 pre-exec gate 与 LLM judge 的后续路线。 | 07-17 · `fe96b74` |
| 3 | 实现 · 脱敏深挖 | [输出脱敏实现详解-redact三层逻辑与示例.md](输出脱敏实现详解-redact三层逻辑与示例.md) | **代码详解**：逐层拆解 `redact.py` 的 JSON key 掩码、命令域开关、已知格式正则与可选熵兜底；用流程图和 before/after 示例解释如何覆盖 stdout / stderr，同时控制误报。 | 07-17 · `568d4de` |

> 排序依据：每个文档**首次被创建**的 commit 时间，也是本专题的自然演进顺序。#1 是完整设计蓝图；#2 在 oid-log 已落地后重新盘点现状，是当前实施与 roadmap 的权威入口；#3 与功能代码同批提交，是 #2 中输出脱敏部分的实现级补充。阅读实现现状时，以 #2、#3 和当前代码为准。

**一句话脉络：** 先为 `action_bash` 建立 policy gate + HITL 的完整纵深防御蓝图（#1）→ 基于现有安全地板收敛范围，先落地全工具输出脱敏与多客户端强制审批（#2）→ 把三层脱敏逻辑、命令域判断和 FP 防线逐条走清楚（#3）。

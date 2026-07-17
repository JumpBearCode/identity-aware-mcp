# SP 操作的用户归因:把共享 Service Principal 的每次 tool call 追回真人(文档索引)

这个文件夹收录 **Identity-Aware MCP 的「用户归因 / OID 追踪」** 从"为什么共享 SP 模型丢了归因"到"在真实 ACA 上用一个 correlation id 把 MCP 审计表与原生 Azure 日志串起来、并沉淀成正式部署 runbook"的整条**设计与落地脉络**(对应 `oid-log` 分支,尚未并入 `main`)。核心实现:`src/mcp-server/audit.py` + `main.py`(middleware/`_exec`)+ `executor.py` / `worker.py` / `sandbox_manager.py` 的 UA 注入 + `provisioning/aca/modules/{audit,environment,rbac,storage,mcp-app}.bicep`(功能提交 `08d2f10`,退役 standalone `534dd09`)。

**一句话方案:** 权威归因由 MCP 服务端自己写一张 Log Analytics 表(层1),在原生日志里只留一把"钥匙"——注入到 User-Agent 的 correlation GUID(层2)——任何记 UA 的服务都能靠它 join 回权威表,把只显示"共享 SP + sandbox IP"的日志还原成"哪个真人、哪个 IP、哪一次调用"。

**按下表顺序读**即这条线的逻辑顺序(也大致是创建顺序):想清价值缺口(#1)→ 盘 Azure 日志体系并选型(#2)→ 给出代码与 provision 落地规格(#3)→ 真部署到 ACA 并三处闭环验证(#4)→ 沉淀成可复用的正式部署 runbook(#5)。只想**照着部署/验收**就直接跳 **#5**;想懂**为什么这么设计**就从 **#1** 起读。

| # | 阶段 | 文档 | 是什么 | 首次提交 |
|---|---|---|---|---|
| 1 | 起点 · 价值缺口 | [价值分析以及oid-tracking-start.md](价值分析以及oid-tracking-start.md) | **动机篇**:共享 SP 是本项目的安全卖点,代价是原生 Azure 日志只认得那个共享 SP、认不出背后是哪个真人、来自哪个 IP。这篇讲清归因缺口的价值与追踪的起点。 | `7f7ac86` · 07-12 |
| 2 | 选型 · 设计定稿 | [实现方案-SP操作的用户归因-Azure日志体系与最终技术选型.md](实现方案-SP操作的用户归因-Azure日志体系与最终技术选型.md) | **设计篇**:Azure 三类日志体系、三种"id"的语义辨析、7 个候选方案的取舍(**排除**靠原生 ID join),收敛到两层——层1 权威表 + 层2 注入 **GUID** 到 User-Agent;结论建立在对真实 workspace schema 的实测上。 | `7f7ac86` · 07-12 |
| 3 | 落地 · 实现规格 | [落地实现-审计工具audit-py与UA注入-代码改动与Provision清单.md](落地实现-审计工具audit-py与UA注入-代码改动与Provision清单.md) | **工程篇**:新建 `audit.py`(4 个对外接口),`main.py` 只改十来行换掉旧 `logger.info`,`executor`/`worker`/`sandbox_manager` 各加几行做 UA 带外注入;要 provision 什么全走现有 Bicep(Direct DCR 免 DCE + 自定义表 + RBAC + env)。 | `7f7ac86` · 07-12 |
| 4 | 实操 · 部署与验证 | [实操记录-DCR-DCE区别-部署与验证现状.md](实操记录-DCR-DCE区别-部署与验证现状.md) | **实操日志**:DCR / DCE 的区别、为何 `kind=Direct` 免 DCE、实际部署了什么 / 没做什么;并以同一个 correlation id 把 `MCPAudit_CL` + `StorageBlobLogs` + Key Vault 三处**闭环验证**(真人 + 真实 IP)。 | `08d2f10` · 07-13 |
| 5 | 部署 · 正式 runbook | [部署文档-从main.bicep完整部署ACA栈-参数密钥漂移与验证.md](部署文档-从main.bicep完整部署ACA栈-参数密钥漂移与验证.md) | **运维 runbook**:如何只从 `main.bicep` **收敛部署**整栈——6 个必传参数陷阱(默认值会另建平行栈 / 打回占位镜像 / 打断 OBO)、OBO 密钥 vs FIC 辨析、`what-if` 爆炸半径解读、E2E 验证、两处漂移(registries / 分布式锁)的根治。 | `fdc6d3b` · 07-17 |

> 排序依据:逻辑阅读顺序(也大致是创建顺序)。#1–#3 同属 `7f7ac86`(一批 plan/design 文档),按逻辑分:价值 → 选型 → 落地规格;#4 随审计特性并入(`08d2f10`);#5 是本轮实测收敛部署后补写的正式 runbook(`fdc6d3b`)。

**一句话脉络:** 先讲清共享 SP 丢归因的痛(#1)→ 盘 Azure 日志体系、排除靠原生 ID join、选定"权威表 + UA 注入 GUID"两层(#2)→ 给出代码与 provision 的落地规格(#3)→ 真部署到 ACA、用一个 correlation id 把 `MCPAudit_CL` + storage + KV 三处串起来验证闭环(#4)→ 沉淀成可复用的正式部署 runbook(#5)。

# 多客户端接入:让 Entra 保护的 MCP 被各 Agent 客户端握上(文档索引)

这个文件夹收录 **Identity-Aware MCP 的「多客户端接入」**从"为自定义 client 讲清 OAuth 原理"到"用 `/mcpproxy` 剥离 `resource` 代理,把 Claude Code / opencode 这类非 VS Code 客户端接上 Entra"的整条**探索与落地脉络**(对应 `client-explore` 分支,已随 **PR #2**(`b666e87`)并入 `main`)。代理实现在 `src/mcp-server/mcpproxy.py` + `main.py`(功能提交 `1506ad8`,`21d4411` 微调)。

**按下表顺序读**即这条线的时间顺序,也是逻辑顺序:摸清原理与各客户端(#1)→ 写第一版接入计划(#2)→ 真机撞墙(#3)→ 提代理方案并选型(#4)→ 落地方案 A(#5)→ 讲清嫁接原理(#6)→ 做可视化教学 App(#7)。只想看**最终能跑的形态**就直接跳 **#5**;想**边看动画边学**就直接开 **#7**。

| # | 阶段 | 文档 | 是什么 | 首次提交 |
|---|---|---|---|---|
| 1 | 起点 · 原理对比 | [MCP-自定义Client接入-Entra与各Agent客户端支持对比.md](MCP-自定义Client接入-Entra与各Agent客户端支持对比.md) | **原理篇**:为 Entra 保护的 MCP 接自定义 client 的来龙去脉 —— OAuth / PKCE / redirect_uri / pre-auth 时序,以及 Claude Code、opencode、Codex、VS Code 各 agent 客户端在 MCP 与 approval 上的支持对比。 | 07-05 · `2e56630` |
| 2 | 实施 · 第一版 | [计划-预注册-ClaudeCode-或-opencode-项目级接入.md](计划-预注册-ClaudeCode-或-opencode-项目级接入.md) | **纯实施篇**:预注册一个共享 client,把 Claude Code / opencode / VS Code 做**项目级接入**的工程步骤与配置(为什么这么做全在 #1)。 | 07-05 · `2e56630` |
| 3 | 撞墙 · Bug | [Bug剖析-AADSTS9010010-MCP的resource参数撞上Entra-v2.md](Bug剖析-AADSTS9010010-MCP的resource参数撞上Entra-v2.md) | **转折点**:客户端按 RFC 8707 发 `resource`,撞上 Entra v2 报 `AADSTS9010010`。根因剖析——正是它逼出了"要一层代理把 `resource` 删掉"。 | 07-06 · `2ba3030` |
| 4 | 选型 · 计划 | [计划-mcpproxy-同容器双端点-无DCR无Secret代理接入非VSCode客户端.md](计划-mcpproxy-同容器双端点-无DCR无Secret代理接入非VSCode客户端.md) | **选型方案**:同容器双端点 `/mcp` + `/mcpproxy`,无 DCR、无 secret;方案 A(薄过滤器 · 剥 `resource`)对比方案 B(FastMCP `OAuthProxy`),给出取舍。 | 07-11 · `15f253a` |
| 5 | 落地 · 方案 A | [实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md](实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md) | **落地方案**:`/mcpproxy` resource-剥离代理的代码剖析 + design 取舍 + 安全分析。已部署 ACA 并**端到端验证通过**(真登录换到真 Entra token → tools/list → 执行)。 | 07-11 · `1506ad8` |
| 6 | 原理 · 嫁接 | [原理-mcpproxy嫁接FastMCP-Starlette分层与鉴权四件套辨析.md](原理-mcpproxy嫁接FastMCP-Starlette分层与鉴权四件套辨析.md) | **原理篇**:`/mcpproxy` 怎么嫁接进 FastMCP —— Starlette / ASGI 分层、鉴权四件套(PRM / AS metadata / BearerAuth / RequireAuth)辨析、单-app-双端点的取舍。 | 07-12 · `21d4411` |
| 7 | 教学 · 可视化 + 答疑 | [oauth-mcp-flow-demo/](oauth-mcp-flow-demo/README.md) | **自带 OAuth 客户端的可视化教学 App**:一键切换"代理 14 步 vs 直连 11 步",逐帧动画展示每次 request/response 的真实 JSON;附[问答文档](oauth-mcp-flow-demo/问答-MCP授权流-发现机制与redirect_uri剖析.md)剖析发现机制 / well-known 探测 / `redirect_uri` 到底谁在跳。 | 07-12 · `21d4411` |

> 排序依据:每个文档**首次被创建**的 commit 时间(跟着文件移动回溯)。#1 与 #2 同属 `2e56630`(init doc,后于 `2ba3030` 归档进本文件夹);#6 与 #7 同属 `21d4411`,各是同批产出,内部按逻辑阅读顺序排。#3 虽首现于归档提交 `2ba3030`,但正文主体在 `f4ce02f` 深挖补全。

**一句话脉络:** 先摸清各客户端与 OAuth 原理(#1)→ 写第一版预注册接入计划(#2)→ 真机一跑撞上 `resource` / `AADSTS9010010`(#3)→ 提出双端点代理并选型(#4)→ 落地方案 A 剥离代理并验证(#5)→ 讲清它怎么嫁接进 FastMCP(#6)→ 做个可视化 App 把整条流走给你看(#7)。

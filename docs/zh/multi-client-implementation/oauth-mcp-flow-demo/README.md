# MCP · OAuth 授权流可视化教学 App

一个**单独的、自带 OAuth 客户端的教学 App**，把
[`/mcpproxy` resource-剥离代理](../docs/multi-client-implementation/实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md)
的 **14 步时序图一步步走一遍**，用动画呈现每一次 **request / response 的真实 JSON**。

它仿照“支持静态注册（不做 DCR）的 OAuth 客户端”（就像 Claude Code / opencode）实现：
用**静态 client_id `49af5fc1`** + **PKCE** + **loopback 回调**，最终握到一个**真 Entra token**。

顶部可**一键切换两条流程做对比**：

| | 🪄 **MCP proxy**（剥离 resource，14 步） | ⚙️ **MCP 直连**（直连 Entra，11 步） |
|---|---|---|
| 谁走这条 | Claude Code / opencode（**发** resource） | VS Code（**不发** resource） |
| PRM 的 `authorization_servers` | 指向**代理自己** | 指向 **Entra 本尊** |
| 第二份发现文档 | 代理的 AS metadata | **Entra 的 openid-configuration** |
| `/authorize` · `/token` | 打**代理**，代理删 resource 再转发 Entra | **直接**打 Entra，全程无 resource |
| 会不会撞 `AADSTS9010010` | 客户端带 resource → 需要代理删掉才不撞 | 本就不带 resource → 不撞，也不需要代理 |
| 最终 token / 门控 | **完全一样**（同一个 verifier + OBO + group） | **完全一样** |

> 两条路殊途同归：拿到的都是同一种真 Entra token，`aud/scp/oid` 一致。差别只在“发 token 之前的协议协商”——**是否需要一层代理来删 `resource`**。

---

> 📄 **配套答疑文档**：[问答-MCP授权流-发现机制与redirect_uri剖析.md](./问答-MCP授权流-发现机制与redirect_uri剖析.md)
> —— 讲清 401 机制、两轮发现、issuer 与 well-known 探测、以及 **redirect_uri 到底谁在跳**。

## 快速开始

```bash
cd docs/multi-client-implementation/oauth-mcp-flow-demo
./run.sh                 # 或者： python3 server.py
# 浏览器打开 http://localhost:8080
```

- **零依赖**：只用 Python 标准库（`python3` 就行，不用 pip install）。
- **必须是 8080**：Entra 给 client `49af5fc1` 注册的回调白名单只有
  `http://localhost:8080/callback`，所以 App 必须监听 8080，Live 登录才能落回来。
  （如果 8080 被 VS Code / Claude Code 占了，先腾出来。）

---

## 两种模式

顶部可切换：

### 🎬 Demo 回放（默认，随时可跑）
纯前端，用**贴近真实抓包**的数据把 14 步演一遍。不需要登录、不依赖服务器在线，
适合投屏讲解 / 截图。用 `▶ 播放` 自动走，或 `下一步 / ◀ 上一步` 手动走，`←/→/空格` 也行。

### 🔴 Live 真实调用（真的打你的 MCP server）
按引导点三个按钮，**每一步都是真实 HTTP**：

1. **① 真实发现** — 真的向 `/mcpproxy` 撞 401，真的 GET 两份发现元数据（步骤 1–4）。
2. **② 构造 /authorize** — 生成 PKCE + state，构造带 `resource` 的授权请求，
   并真实抓下代理返回的 **302**（你会看到 `resource` 被删、`openid/profile` 被补 —— 步骤 5–6）。
3. **③ 打开浏览器登录** — 新标签页打开 Entra 真实登录；成功后 Entra 302 回本机
   `:8080/callback`，App 自动**真实换 token**（步骤 8–12）并**真实 `tools/list`**（步骤 13–14）。
   拿到的真 token 会被**解开展示声明**（`aud / scp / oid / azp`），证明身份保真。

> 出于安全，界面里的 `access_token / refresh_token / id_token` 都做了**截断打码**，
> 只展示解出来的非敏感声明；后端**从不把完整 token 落日志**。

---

## 14 步对应关系（mcpproxy 流程，与文档 §3 时序图一致）

| 步 | 泳道 | 干什么 |
|---|---|---|
| 1–2 | Client ↔ MCP 端点 | 无 token → 401 + `WWW-Authenticate`（指向发现元数据）|
| 3 | Client → 代理 | Protected-Resource Metadata（RFC 9728）→ AS 指向代理自己 |
| 4 | Client → 代理 | Authorization-Server Metadata（RFC 8414）→ **无 `registration_endpoint`（不 DCR）** |
| 5–6 | Client → 代理 → Entra | `/authorize`（带 resource）→ 代理 302 到 Entra（**★删 resource**）|
| 7 | 浏览器 ↔ Entra | 真实交互式登录（代理不参与，用户真实 IP + MFA/CA）|
| 8 | Entra → Client | 302 回 loopback，带 authorization code（代理看不到）|
| 9–12 | Client → 代理 → Entra | `/token`（带 resource）→ 代理删 resource 转发 → **真 Entra token** 原样回传 |
| 13–14 | Client ↔ MCP 端点 | Bearer 真 token → 校验 + OBO 查 group → 按组门控返回工具 |

---

## 目录结构

```
oauth-mcp-flow-demo/
├── server.py        # 后端：静态服务器 + 真·OAuth 客户端 + 14 步抓包 + /callback（stdlib only）
├── run.sh           # 启动脚本（含 8080 占用检查）
├── README.md        # 本文
└── public/          # 可视化（按要求：不放在 src 里）
    ├── index.html   # 页面骨架
    ├── styles.css   # 深色主题 + 动画
    ├── steps.js     # 14 步“剧本”：元数据 + Demo 烘焙数据（单一数据源）
    └── app.js       # 动画引擎 + SVG 时序图 + 详情面板 + Demo/Live 控制
```

**可视化全部在 `public/`，没有放进项目的 `src/`。**

---

## 它“仿照”的是什么客户端？

仿的是**遵守 MCP 授权规范、且支持静态预注册**的那类 OAuth 客户端（Claude Code / opencode）：

- 从 `WWW-Authenticate` 顺藤摸瓜做**发现**（RFC 9728 → RFC 8414）；
- 发现到 AS metadata **没有 `registration_endpoint`**，于是**不做 DCR**，
  直接用自己**静态配置的 `client_id`（`49af5fc1`）**；
- **public client + PKCE**（无 secret）；
- 用 **loopback `redirect_uri`** 接授权码；
- 拿 code 到 token 端点换 token，再带 Bearer 调 MCP。

唯一和“直连 Entra”不同的是：它把 AS 指向了 `/mcpproxy`，中间那层只做一件事——**删掉 `resource`**，
从而绕过 `AADSTS9010010`。详见上级 `docs/` 里的实现说明与 Bug 剖析。

---

## 常见问题

- **Live 卡在“等待登录”**：确认 App 跑在 8080；确认你用的账号在 Entra 的
  diagnose / action 组里（否则 `tools/list` 可能为空，但流程仍会跑通）。
- **只想讲解、不想登录**：用 Demo 模式，数据贴近真实抓包，够讲清楚。
- **改目标服务器**：`MCP_BASE_URL=... python3 server.py`（默认指向已部署的 ACA 实例）。

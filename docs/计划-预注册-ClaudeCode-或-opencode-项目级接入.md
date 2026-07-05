---
title: "计划：预注册 Claude Code 或 opencode，项目级接入 Entra 保护的 MCP"
date: 2026-07-05
tags:
  - plan
  - mcp
  - entra
  - oauth
  - pkce
  - client
sources:
  - "docs/MCP-自定义Client接入-Entra与各Agent客户端支持对比.md"
  - "provisioning/aca/modules/identity.bicep"
  - "provisioning/aca/main.bicep"
  - "https://learn.microsoft.com/en-us/entra/identity-platform/reply-url"
  - "https://code.claude.com/docs/en/mcp"
  - "https://opencode.ai/docs/config/"
  - "https://opencode.ai/docs/mcp-servers/"
---

# 计划：预注册 Claude Code 或 opencode，项目级接入

> 目标：让 **Claude Code 或 opencode**（二选一，或都接）作为 pre-registered client 连本项目
> **Entra 保护的 MCP server**，用**项目级 mcp 配置**，并把 **redirect 回调端口钉死**。
> 本计划是 [`MCP-自定义Client接入-...md`](./MCP-自定义Client接入-Entra与各Agent客户端支持对比.md)
> 的落地版；OAuth 原理/时序图见那篇 §2.0、§3。

本计划额外回答四个问题：
1. 项目级 mcp 配置到底放哪个文件？（纠正 `.claude` / `.opencode` 的说法）
2. redirect 端口"两处 8080"是不是同一个？被占了怎么办？能不能锁死在一个 port？
3. Bicep 端**不 pre-authorize** 会发生什么？能不能**完全不改 MCP server 的 app registration** 就 work？
4. 为什么用户 login 完就能拿 authorization code？**MCP server 这边做了什么？是 RBAC 吗？**

---

## 0. 前置纠正：项目级配置读哪个文件

| Client | 项目级 MCP 配置文件 | 说明 |
|---|---|---|
| **Claude Code** | **`.mcp.json`**（repo 根） | 不是 `.claude/`。`.claude/settings.json` 是放**权限**（allow/ask/deny，见对比文档 §7），MCP server 定义在根目录的 `.mcp.json` |
| **opencode** | **`opencode.json`**（repo 根） | 不是 `.opencode/`。那个目录放 `agents/`、`commands/`、`plugins/` 等；MCP server 定义在 `opencode.json` 的 `mcp` key |

两者都是 **public client + PKCE，不需要 client secret**（原理见对比文档 §3）。

---

## 1. Entra：为每个 client 注册一个 client app registration

> 建议**每个 client 一个 app registration**（`client_id` 独立，方便按 client 审计/吊销）。也可以共用一个
> native public client app，但审计上不如分开清晰。

对 Claude Code / opencode 各做一遍：

1. 新建 App Registration，例如 `DataOps MCP – Claude Code Client` / `… – opencode Client`
2. **Authentication → Add a platform → Mobile and desktop applications**（public client）
   - Redirect URI 填 **`http://localhost:8080/callback`**（端口/ path 见 §3）
3. 打开 **Allow public client flows**（`allowPublicClient = true`），**不创建 client secret**
4. **API permissions → Add → My APIs →** 本 MCP server（`{name}-mcp-server`）→ Delegated →
   勾选 **`user_impersonation`**
5. 记下该 app 的 **Application (client) ID**，填进 §2 的 mcp 配置

> 第 4 步的"API permission"是加在**这个 client app** 上的，**不动 MCP server 的 app registration**——
> 这点对 §4 很关键。

---

## 2. 项目级 mcp 配置（两份）

把 `<mcp-url>`、`<mcp-app-id>`、各自的 `<client-app-id>` 替换成真值。

### 2.1 Claude Code — `.mcp.json`（repo 根）

```json
{
  "mcpServers": {
    "dataops-mcp": {
      "type": "http",
      "url": "https://<mcp-url>/mcp",
      "oauth": { "clientId": "<claude-code-client-app-id>", "callbackPort": 8080 }
    }
  }
}
```

命令行等价（`--scope project` 写进 `.mcp.json`）：

```bash
claude mcp add --transport http --scope project \
  --client-id <claude-code-client-app-id> \
  --callback-port 8080 \
  dataops-mcp https://<mcp-url>/mcp
```

- **没有 secret**（public client）；`callbackPort: 8080` 同时钉死本地监听端口和 redirect 端口（见 §3）。

### 2.2 opencode — `opencode.json`（repo 根）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "dataops-mcp": {
      "type": "remote",
      "url": "https://<mcp-url>/mcp",
      "oauth": {
        "clientId": "<opencode-client-app-id>",
        "scope": "api://<mcp-app-id>/user_impersonation"
      }
    }
  }
}
```

- 同样**没有 `clientSecret`**（可选字段，public client 不填）。
- ⚠️ opencode **没有"固定 callback 端口"字段**，端口由它自己挑 → 见 §3 的处理办法。

登录：Claude Code 用 `/mcp` → Authenticate；opencode 用 `opencode mcp auth dataops-mcp`。浏览器登录一次即可。

---

## 3. redirect 端口：其实只有一个 port，两处必须一致

### 3.1 "两个 8080" 是同一个 port

你担心的"第一次 callback port 和第二次 callback port"其实是**同一个端口**，只是出现在两个地方，必须相等：

| 出现位置 | 角色 |
|---|---|
| 时序图 Step 8：Claude Code 本地起 HTTP 监听 `127.0.0.1:8080` | **接** authorization code 的本地服务 |
| 时序图 Step 9 / Entra 里注册的 redirect URI `http://localhost:8080/callback` | 告诉 Entra **把 code 送到哪** |

`--callback-port 8080`（或 `oauth.callbackPort: 8080`）**一次同时设定这两处**：本地监听绑 8080，
redirect_uri 也用 8080。Entra 把 code 打到 8080，本地监听正好在 8080 接住。**所以"锁在一个 port" =
就是 `--callback-port` 干的事，本来就只有一个 port。**

### 3.2 端口被占了怎么办

你的担心是对的：**若把端口钉死成 8080，而 8080 已被占用，Step 8 的本地监听就绑不上 → OAuth 直接失败。**
两种兜底：

**办法 A（推荐给 Claude Code）：钉死一个不常用的端口。**
选一个基本不会冲突的高端口（如 `8765`、`53682`），Entra 注册 `http://localhost:8765/callback`，客户端
`--callback-port 8765`。简单、确定。

**办法 B（更抗冲突，opencode 必须用这个）：不钉端口，靠 Entra 的 localhost 端口无关匹配。**
Microsoft 官方明确：

> *"The login server cannot distinguish between redirect URIs when only the port differs."*
> 且 *"you can register `http://localhost`, `http://localhost:3000/abc`（paths and ports are okay）"*

也就是 **Entra 匹配 localhost redirect 时忽略端口、只认 path**。于是：

- 在 Entra 注册 `http://localhost:8080/callback`（端口随便填一个，**关键是 path `/callback`**）。
- 客户端**不钉端口**（Claude Code 省掉 `--callback-port`；opencode 本来就没这字段）→ 每次挑一个空闲
  随机端口 → 只要 path 是 `/callback`，Entra 照样匹配通过，天然躲开端口冲突。

> 注意 path **必须**对上（path 是被严格匹配的，端口不是）。Claude Code 的 path 固定是 `/callback`
> （官方："redirect URI of the form `http://localhost:PORT/callback`"）。
> **opencode 的 redirect path 需实测确认**——见 §6 先跑一次抓 `redirect_uri`，把它的 path 注册到 Entra。

> 另一个细节：Microsoft 推荐 native client 用 `127.0.0.1` 而非 `localhost`（避免监听到非 loopback
> 网卡），但 http + `127.0.0.1` 在门户 UI 里可能要改 manifest 的 `replyUrlsWithType`。Claude Code 用
> `localhost`，直接门户注册 `http://localhost:PORT/callback` 即可。

### 3.3 小结

- 想要**确定性** → 办法 A（钉死一个冷门端口，两处一致）。
- 想要**抗端口冲突** / 用 opencode → 办法 B（Entra 注册 `…/callback`，客户端随机端口，靠端口无关匹配）。

---

## 4. Bicep 端：pre-authorize 与否 & 能不能不改 MCP server 的 app registration

### 4.1 不 pre-authorize 会发生什么

pre-authorize（写在 **MCP server app** 的 `preAuthorizedApplications`）唯一的作用是**免掉 consent 弹窗**。
不加它，OAuth **技术上照样能走**，区别只在第一次登录：

| 租户 user-consent 设置 | 不 pre-auth 的结果 |
|---|---|
| 允许用户自助同意 | 用户第一次看到一次 consent 弹窗（"Claude Code 想代表你访问 DataOps MCP"）→ 点"同意" → 记一条 per-user grant → 之后不再弹 |
| **关闭了用户同意**（很多企业租户） | 用户点不了，显示"需管理员批准" → 卡住，需**管理员 consent 一次** |

本项目的 `user_impersonation` scope 在 `identity.bicep` 里是 `type: 'User'`（带 userConsent 文案），
**属于可用户自助同意的低权限 scope**——所以只要租户没禁用户同意，不 pre-auth 也就是"第一次多点一下"。

### 4.2 能不能完全不改 MCP server 的 app registration？——能

关键区分：**pre-authorize 改的是 server app；而让 client 拿到 scope 还有另一条路——consent grant，
它不改 server app 的定义。** 要让 Claude Code / opencode work，最少只需要：

1. 新建 **client app registration** + redirect URI + `allowPublicClient`（全新对象，**不碰 server app**）
2. 在 **client app** 上加 API permission：server 的 `user_impersonation` delegated scope（改的是 **client app**）
3. **Consent**：用户第一次点"同意"（生成 per-user grant）**或** 管理员 consent 一次（生成 grant 对象）
   —— grant 是**独立对象，不是对 server app registration 定义的编辑**

所以结论：

> **可以不改 MCP server 的 app registration 就让它 work**——前提是**能拿到 consent**（租户允许用户自助
> 同意，或有管理员愿意 consent 一次）。**pre-authorize 只是省掉那次 consent 点击的优化，不是功能必需。**

什么时候你**不得不**碰 server app（加 pre-auth）？只有当**租户禁了用户同意、又不方便让管理员逐个
consent**，想用"服务端预授权"一次性解决所有用户时——那就把 client 的 appId 加进 server app 的
`preAuthorizedApplications`（改法见对比文档 §2.2）。

### 4.3 OBO（闸③）不用动

`grant_obo_admin_consent`（AllPrincipals，MCP server SP → Graph）与用哪个 client 无关，换 client 不需要
改动。

---

## 5. 概念澄清：为什么 login 完就能拿 authorization code？MCP server 做了什么？是 RBAC 吗？

这是最容易误解的一点。**authorization code 是 Entra（authorization server）发的，不是 MCP server 发的。**

### 5.1 发 code 的是 Entra，MCP server 在这一步完全不参与

看时序图（对比文档 §2.0）：`/authorize` → 登录 → 302 带 code，全程在 **浏览器 ↔ Entra** 之间。
**MCP server 在整个 authorize / token 交换里一次都没被调用。** 它只在两头出现：

- **最前面**：Step 1–2，MCP server 返回 `401 + WWW-Authenticate`，告诉 client "去找 Entra、要这个
  scope"。
- **最后面**：Step 19–20，MCP server 收到 Bearer token，**校验 JWT**（`aud` / `scp` / 签名 / issuer）。

中间发不发 code、发给谁，**MCP server 说了不算，Entra 说了算**。

### 5.2 Entra 凭什么在 login 后就发 code？两道闸，都在 Entra

用户 login 完能拿到 code，是因为 Entra 同时满足了两件事：

1. **认证（authN，"你是谁"）**：用户在 Entra 登录页登录成功（+ MFA）。
2. **授权（authZ，"这个 client 能不能要这个 scope"）**：Entra 检查该 client 对
   `api://<mcp>/user_impersonation` 有没有被允许——**要么 pre-authorized，要么有 consent grant**（§4）。

两条都过，Entra 才把 authorization code 发到 client 的 redirect URI。**这跟 MCP server 无关，也不是
MCP server 在放行。**

### 5.3 这是 RBAC 吗？——不是

- **发 code / 发 token**：Entra 的 authN + client authZ，如上，**不是 RBAC**。
- **MCP server 的"谁能用哪个 tool"**：那是 token 校验**通过之后**、在 **runtime** 做的——MCP server 用
  **OBO** 拿用户 token 换 Graph token，查用户的 **AD group** 成员，决定 tool 可见性（见
  [`mcp_discussion.md`](./mcp_discussion.md) §2/§3）。这是**基于组的 tool 门控**，最接近"RBAC"的东西，但
  它发生在**拿到 token 之后**，和"发 code"是两码事。
- **Azure RBAC**：只作用在 **worker Service Principal** 执行 `az` 命令时的资源权限边界，和用户登录 /
  发 code 完全无关。

一句话：

> **login 后能拿 code，是 Entra 完成了"认证 + client 授权"；MCP server 只负责最前面发 401 指路、最后面
> 验 token。tool 级的组门控是 MCP server 在 runtime 用 OBO+Graph 做的，那才沾"RBAC"的边，但不在发 code
> 这条链上。**

---

## 6. 验证 checklist

1. **抓 redirect_uri**（尤其 opencode）：发起登录时看浏览器打开的 `/authorize` URL 里的 `redirect_uri=`
   参数，或看客户端本地监听地址，确认 **path**（`/callback`?）和端口策略，与 Entra 注册一致。
2. **登录能过**：浏览器 Entra 登录 → 落地页"登录成功" → 客户端显示已连接。
3. **token 正确**（可选）：解码 access token，确认 `aud = api://<mcp-app-id>`、`scp` 含
   `user_impersonation`、`azp` = 你的 client app id。
4. **`/mcp` 200 + tools 出现**：能列出 `diagnose_bash` / `action_bash`。
5. **（对比文档 §7）**：如需 `action_bash` 强制人工审批，在 `.claude/settings.json` 配 `ask` 规则。

---

## 7. 回滚

- 删掉项目根的 `.mcp.json` / `opencode.json` 里对应条目即可断开。
- Entra 侧：删掉新建的 client app registration（若共用则移除对应 redirect URI）；如加过 pre-auth，从
  server app 的 `preAuthorizedApplications` 移除该条。
- 没改过 server app / OBO 的话，无其它清理。

---

## 参考资料

- [`MCP-自定义Client接入-...md`](./MCP-自定义Client接入-Entra与各Agent客户端支持对比.md) —— OAuth 原理、时序图（§2.0）、PKCE（§3.3）、各 client 对比
- [`mcp_discussion.md`](./mcp_discussion.md) —— identity 模型、group 门控、per-tool-call hook
- `provisioning/aca/modules/identity.bicep` / `provisioning/aca/main.bicep` —— server app / `preAuthorizedApplications` / OBO
- [Microsoft – Redirect URI (reply URL) best practices（localhost 端口/path 匹配）](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)
- [Claude Code – Connect to MCP（`--client-id` / `--callback-port` / `.mcp.json`）](https://code.claude.com/docs/en/mcp)
- [opencode – Config（`opencode.json` 位置）](https://opencode.ai/docs/config/) / [MCP servers（`mcp` / `oauth`）](https://opencode.ai/docs/mcp-servers/)

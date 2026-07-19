---
title: "Identity-Aware DataOps MCP：登录与同意流程拆解"
date: 2026-06-04
tags:
  - mcp
  - entra
  - oauth
  - obo
sources:
  - "azure-dataops-mcp/mcp-server/main.py"
  - "azure-dataops-mcp/provisioning/python/provision.py"
---

# DataOps MCP：登录与同意流程拆解

## 一句话总结

从 VS Code / GitHub Copilot 连这个 MCP server,整条链路真正涉及的是
**1 次登录(认证)+ 2 道同意闸(授权委托)**;其中只有一道会真正弹给用户看,
另一道是后台静默完成的——把这两道闸都**预先处理**好,最终用户就只剩"登录一次"。

> 常见误解:把它理解成"三个弹窗"。三件事性质不同,而且其中一个根本不是弹窗。
> 下面逐一拆开。

---

## 0. 背景:这套架构在认证上的特点

三个容器,身份各自独立(详见 `azure-dataops-mcp/README.md`):

- **mcp-server**:被保护的 OAuth 资源。暴露 `api://<MCP_APP_ID>/user_impersonation`
  这个 delegated scope;校验进来的 Entra JWT;再用 **OBO** 代表用户去查 Graph 组成员。
  自身**无任何 Azure 数据平面权限**。
- **diagnose-worker / action-worker**:各自持有一个 Service Principal(client secret),
  以自己的身份跑 `az`;权限边界是它们各自的 RBAC。

本文只讲**用户 → mcp-server** 这条认证链(workers 那条是 SP + RBAC,不涉及用户登录)。

---

## 1. 完整流程:谁负责什么

| 步骤 | 动作 | 谁做的 |
|---|---|---|
| 1 | VS Code 请求 `GET /mcp` → 收到 **401**,响应头带 `WWW-Authenticate: Bearer ... resource_metadata=".../.well-known/oauth-protected-resource/mcp"` | **Server** 发 401 + 指针 |
| 2 | VS Code 读指针,拉 protected-resource metadata → 得知**授权服务器 = Entra 租户**、需要的 scope = `api://<MCP_APP_ID>/user_impersonation` | **Client** 读 / **Server** 提供文档 |
| 3 | VS Code 再向 Entra 拉 authorization-server metadata(`.well-known/openid-configuration`)→ 拿到 authorize / token 端点 | **Client** ↔ **Entra** |
| 4 | VS Code 开浏览器跳 Entra 登录页(**Authorization Code + PKCE**),用户登录 | **Client** 发起 / **用户**登录 |
| 5 | 登录成功 → Entra 带 authorization code 重定向回 VS Code 本地回调 → 出现"**已登录成功,可关闭此页**" | 那个落地页是 **Client** 起的本地 redirect handler |
| 6 | VS Code 拿 code + PKCE verifier 去 token 端点换 **access token**(audience = `api://<MCP_APP_ID>`) | **Client** ↔ **Entra** |
| 7 | VS Code 带 `Authorization: Bearer <token>` 重发 `/mcp` → 200,连上 | **Client** |
| 8 | token 存入操作系统钥匙串,之后用 refresh token **静默续期** | **Client** |

**关键点:401 处理、metadata 发现、PKCE、本地落地页、token 缓存/续期、每次挂 Bearer
——全是 VS Code(MCP client + 认证 provider)实现的,遵循 MCP Authorization 规范。**
任何合规的 MCP client(Claude 等)看到这个 401 都能照样跑通。

Server 端你只需要做三件事(FastMCP 已经帮你做了,见 `main.py`):

1. 没 token 时返回 **401 + `WWW-Authenticate`(带 resource_metadata 指针)**
2. 提供 **`/.well-known/oauth-protected-resource/mcp`** 元数据文档
3. **校验进来的 JWT**(audience、签名、scope)—— `AzureJWTVerifier` + `RemoteAuthProvider`

---

## 2. 核心模型:1 次登录 + 2 道同意闸

| | 是什么 | 用户看到的形态 | 谁来消除它 | 没消除会怎样 |
|---|---|---|---|---|
| **① 登录** | **认证 / authN**("你是谁") | Entra 登录页(选账号/输密码) | 消不掉(除非复用已有 session) | —— 本来就要 |
| **② VS Code → 你的 MCP API** | **委托同意 / authZ** | **浏览器里的 consent 页**(Accept/Cancel) | `preAuthorizedApplications`(预授权 VS Code 客户端) | 弹 consent;租户禁了用户同意 → 卡在"需管理员批准" |
| **③ MCP server → Graph(OBO)** | **委托同意 / authZ** | **不是弹窗!后台 back-channel** | `grant_obo_admin_consent`(AllPrincipals admin consent) | **server 端直接报错**(`invalid_grant` / `interaction_required`),用户看不到弹窗,流程直接挂 |

### 为什么 ③ 不是弹窗(最容易搞错的点)

①②③ 里,**只有 ② 会真正在浏览器里弹 consent 给用户。**

- **①** 是登录页,属于认证,不是"同意"。
- **③ OBO 是 server 在后台拿用户 token 去换 Graph token**,整个过程没有界面、无法弹给用户。
  所以它要么**事先被 admin 同意好**(tenant-wide grant)→ 静默成功;
  要么没同意 → **后端报错**,而不是"弹个窗让用户点"。

### 为什么 ② 和 ③ 的消除手段不同

两者本质都是"委托同意",但:

- **②** 由**交互式客户端**(VS Code)发起,Entra 能在浏览器流程里插一页 consent
  → 理论上可以"让用户点",`preAuthorizedApplications` 只是把这页**省掉**。
- **③** 是**非交互的后台 OBO**,没有界面 → 只能靠 **admin 提前 consent**。
  这正是 ③ **必须**预先解决、不能"留给用户现场点"的原因——压根没有现场可点。

---

## 3. 闸 ②:`preAuthorizedApplications`(预授权 VS Code)

```python
# provision.py —— 写在 MCP server 自己的 API app 上
api=ApiApplication(
    oauth2_permission_scopes=[ PermissionScope(value="user_impersonation", ...) ],
    pre_authorized_applications=[
        PreAuthorizedApplication(
            app_id=VSCODE_CLIENT_ID,            # aebc6443-996d-45c2-90f0-388ff96faa56
            delegated_permission_ids=[scope_id],
        )
    ],
),
```

**它改的不是 MCP server SP 自己的权限,而是"VS Code 来拿你这个 API 的 token 时要不要走 consent"。**

- **加了**:你(API 拥有者)替这个可信客户端预先同意 → Entra 直接发 token,**零 consent**,
  也不需要记录 per-user 的同意。
- **不加**:token 流程技术上还能走,但 VS Code 第一次请求该 scope 时会多一个 consent 弹窗:
  > "**VS Code** 想代表你访问 **DataOps MCP Server**(user_impersonation)。是否同意?"

  之后取决于租户的 **user consent 设置**:

  | 租户设置 | 不加 pre-auth 的结果 |
  |---|---|
  | 允许用户自助同意 | 用户点一次"同意" → 记一条 per-user grant → 之后不再弹。只是第一次多一步。 |
  | **关闭了用户同意**(很多企业租户) | 用户**点不了**,显示"需管理员批准" → **卡死**,必须管理员介入。 |

> 你能**预授权 VS Code**,是因为 `preAuthorizedApplications` 写在**你自己的 API app** 上,
> 不需要去改 VS Code 的 app registration(那个你也改不了)。
>
> Entra **不支持 Dynamic Client Registration(DCR)**,VS Code 不能临时注册客户端,
> 只能用它固定的一方 client id `aebc6443-…`——这正是必须预授权这个 id 的根因。

---

## 4. 闸 ③:OBO 的 admin consent(代表所有用户预先同意)

```python
# provision.py —— 给 MCP server SP 对 Graph 建一条租户级 delegated grant
async def grant_obo_admin_consent(graph, server_sp_id):
    graph_sp = await graph.service_principals_with_app_id(GRAPH_APP_ID).get()
    await graph.oauth2_permission_grants.post(
        OAuth2PermissionGrant(
            client_id=server_sp_id,
            consent_type="AllPrincipals",                  # 对所有用户生效
            resource_id=graph_sp.id,
            scope="User.Read email offline_access openid profile",
        )
    )
```

OBO 的用途:VS Code 给的 token 是**发给 MCP server**(audience `api://<mcp>`)的,
server **不能拿它直接调 Graph**(audience 不对)。OBO 就是这个交换——
server 用进来的用户 token 换一个**仍带用户身份**的 Graph token,从而以"这个用户"的身份
去读他的组成员(`main.py` 里的 `_user_groups_via_obo`),决定他能调哪些工具。

- **预先 admin consent(AllPrincipals)**:所有用户的 OBO 都**静默成功**,中途无任何提示。
- **不预先同意**:OBO 这步在后台**直接失败**(`interaction_required` / `consent_required`),
  表现为 server 报错,而不是给用户弹窗。

> 这些 scope(`User.Read email offline_access openid profile`)是**低权限、可用户自助同意**的,
> 所以授予这条 admin consent 用 **Application Administrator** 就够了;
> 只有高权限 Graph scope 才需要 Privileged Role Administrator / Global Admin。

---

## 5. 最终用户体验

②③ 都被预先处理后,用户真正会经历的只剩 **① 登录**:

> 选个账号 / 登录一次 → **完事**。中间**没有任何 Entra consent 弹窗**,OBO 静默成功。

之后重开 VS Code 也**不会再弹**:认证 session(含 refresh token)存在系统钥匙串里,
自动续期。只有在 token 失效 / 主动登出 / 管理员吊销 / 改密码 / Conditional Access 触发时才会再要求登录。

### 注意:还有一类"非 Entra"的 VS Code 本地弹窗

你第一次 start server 时看到的 "Allow…" 弹窗,可能是 **VS Code 客户端自己**的确认
(例如"是否允许这个 workspace 运行 MCP server" / "是否允许它用某账号登录")。
这属于 **VS Code 本地 UI 的信任 / 授权**,**不是** Entra 的 consent,跟 ②③ 不是一回事:

- "允许运行此 workspace 的 MCP server":**按 workspace 记忆**,选一次后该文件夹不再问
  (除非配置变化或重置信任)。
- "允许它使用某账号":认证 session 缓存在钥匙串,重开**静默复用**。

---

## 6. 速查表:每道闸"加了 / 不加"的对照

| 配置项 | 写在哪 | 作用 | 不加的后果 |
|---|---|---|---|
| `preAuthorizedApplications`(VS Code) | MCP server 的 **API app** | 免去"客户端→你的 API"的 consent 弹窗 | 用户第一次多一个 consent;租户禁用户同意时卡在需管理员 |
| `grant_obo_admin_consent`(AllPrincipals) | MCP server **SP → Graph** 的 oauth2 grant | 免去所有用户 OBO→Graph 的同意 | OBO 后台报错(非弹窗),组查询失败 → 工具鉴权挂掉 |

一句话:**真正会弹给用户的 Entra consent 只有 ②;③ 是后台的,不弹、只会成功或报错;
① 是登录不是同意。把 ②③ 都预先处理掉,用户就只剩登录这一下。**

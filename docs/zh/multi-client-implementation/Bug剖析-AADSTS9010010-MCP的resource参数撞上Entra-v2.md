---
title: "Bug 剖析：AADSTS9010010 —— MCP 的 resource 参数撞上 Entra v2"
date: 2026-07-06
tags:
  - bug
  - mcp
  - entra
  - oauth
  - rfc8707
  - aadsts9010010
sources:
  - "docs/MCP-自定义Client接入-Entra与各Agent客户端支持对比.md"
  - "docs/Entra OAuth Proxy vs Pre-registration MCP.md"
  - "https://www.groff.dev/blog/azure-entra-id-mcp-server-authentication-incompatibilities"
  - "https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim"
  - "https://github.com/anthropics/claude-code/issues/55993"
  - "https://github.com/anthropics/claude-code/issues/52871"
  - "https://github.com/PrefectHQ/fastmcp/issues/1846"
  - "https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614"
  - "https://www.rfc-editor.org/rfc/rfc8707.html"
  - "https://gofastmcp.com/integrations/azure"
---

# Bug 剖析：AADSTS9010010 —— MCP 的 `resource` 参数撞上 Entra v2

> 用 **Claude Code / opencode** 连本项目（Entra 保护的远程 MCP）走 OAuth 时报：
>
> ```
> AADSTS9010010: The resource parameter provided in the request doesn't match with the requested scopes.
> ```
>
> 本文专讲**这个 bug 是怎么产生的**：`scope` 和 `resource` 到底是什么、谁触发的、谁的锅、是不是所有
> Entra OAuth MCP 都废了、以及为什么"换低版本 FastMCP"规避不了。修复方案的架构细节见
> [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md)。

---

## 0. 一句话定性

> **不是配置错、不是缺 pre-authorize，而是 MCP 规范（强制客户端带 RFC 8707 `resource` 参数）与
> Microsoft Entra v2 端点（强制 `resource` 必须与 `scope` 匹配、而 MCP 的 URL 配不上）之间的协议级不兼容。** 2026-03 Entra 上线强制校验后，
> 这类请求一律被拒。影响面很广：Azure DevOps 远程 MCP、Power BI MCP、Fabric、IBM mcp-context-forge 等
> 全是同一个雷。

---

## 1. 先厘清：`scope` 和 `resource` 到底是什么

两者都在回答"这个 token 发给谁、能干什么"，但来自 OAuth 的**两个不同世代**，而且活在**两套不同的命名空间**：

| | `scope`（OAuth 2.0 原生） | `resource`（RFC 8707，v1 遗风复活） |
|---|---|---|
| 出身 | OAuth 2.0 / OIDC，Entra **v2 原生主力** | OAuth v1 的 `resource`，被 **RFC 8707**（Resource Indicators，**IETF 的 OAuth 扩展、非 MCP 自创**）在 v2 时代复活 |
| 请求参数 or token claim | **请求参数**（发给 `/authorize`、`/token`） | **请求参数**（发给 `/authorize`、`/token`） |
| 表达什么 | 一个字符串**同时**编码"目标 + 权限" | **只**点名一个"目标 API 的 audience"，不含权限 |
| 目标用什么标识 | Entra 的 **App ID URI**（`api://<guid>` 或 `api://<已验证域名>`），必须**已注册** | 该资源的**规范 URL**（如 `https://…/mcp`）——MCP 用它当 IdP 无关的 audience |
| Entra v2 怎么处理 | **主力**：从 scope 前缀推出 token 的 `aud`；`.default` 写法 `api://<appid>/.default` | **会收下并校验**：按 RFC 8707 要求 `resource` 与 scope 的属主匹配；MCP 的 URL 配不上 `api://` scope → 拒（AADSTS9010010） |

**为什么 MCP 规范偏要客户端带 `resource`？** 安全考虑（RFC 8707）：把 token 的 audience **死钉**到"你正在连
的这台 MCP server"，防止一个发给 API-A 的 token 被拿去 API-B 冒用（token 重放 / confused deputy）。所以
MCP 授权规范（2025-06 修订）规定：**客户端 MUST 在 `/authorize` 和 `/token` 都带上 `resource` = 该 MCP
server 的规范 URI**。出发点是好的，只是撞上了 Entra v2。

> 更麻烦的是：MCP 规范还假设 IdP 支持 RFC 8414（AS Metadata）、RFC 7591（DCR）、RFC 8707（Resource
> Indicators）——而 **Entra v2 这三样都没按 MCP 需要的方式实现**。本 bug 是其中 RFC 8707 那一条。

### 1.1 本项目的 `scope` 和 `resource` 长什么样

**先记住这把钥匙——`resource` 和 `scope` 是两个正交维度，本就不该相等：**

| | 回答的问题 | 干净模型里的值 |
|---|---|---|
| **`resource`** | **谁**（哪台 server = audience） | server 的 URL，如 `https://…/mcp` |
| **`scope`** | **什么**（哪个权限） | 纯权限，如 `user_impersonation`（**不含目标**） |

在遵循 RFC 8707 的 AS（Okta / Keycloak）上，这俩各管一头、永不冲突。本项目在 **Entra** 上却撞车——因为
Entra 把"谁"也编码进了 scope（`api://<appid>/…`），于是"谁"被说了两遍、还用了两套标识符：

| | 本项目实际值 |
|---|---|
| **scope**（请求参数） | `api://88de6a37-cf75-40d3-83e8-44c5ccbc0895/user_impersonation` |
| ↳ 目标（App ID URI） | `api://88de6a37-cf75-40d3-83e8-44c5ccbc0895` — MCP server app 的 identifierUri |
| ↳ 权限 | `user_impersonation` |
| **resource**（请求参数，RFC 8707） | `https://dataops-aca-mcp.icyrock-96f978c0.westus2.azurecontainerapps.io/mcp` — MCP server 的**部署 URL** |

一眼就能看出症结：**同一台 MCP server，scope 用 `api://88de6a37…` 指认它，resource 用 `https://…/mcp`
指认它——两个参数、两套完全不同的字符串。** Entra v2 拿 scope 那套（`api://…`）当 `aud`，又见到一个它不认
的 resource、还是另一个串 → `AADSTS9010010`。

> **两点要说清（免得记反、也免得冤枉 Entra）：**
> 1. **方向别搞反：是 Entra 把"目标"编码进 scope，其他 AS 才把 `resource` 与 `scope` 分开**——不是反过来。
>    别的 IdP（Okta/Auth0）scope 里只有权限（`user_impersonation`），目标由独立的 `resource`/`audience`（URL）
>    给；Entra 的 v2 scope 原生就带目标（`api://<appid>`），且不收独立 `resource`。
> 2. **但 Entra 这样并不违反 OAuth**：`resource`（RFC 8707）只是 OAuth 的**可选扩展**，core（RFC 6749）从不
>    要求实现它。是 **MCP 把这个可选项强制化（MUST）**，才逼出了冲突。
>
> 其他 AS（Okta / Auth0）的 `resource`/`scope` 长相与设计哲学 → **§1.5**；完整的"谁的锅"层级 → **§4**。

### 1.2 scope 里已经点了目标，为什么还要 `resource`？

你的直觉对——**`resource` 表达的确实也是 audience，和 scope 里那个"目标"是同一类东西。** 症结不在"重复"，
而在**两个是不同世界的标识符，且 MCP 与 Entra 各认一个**：

- **MCP 规范是 IdP 无关的。** 它不想依赖 Entra 那套 `api://<guid>` 的 scope 约定。RFC 8707 给它一个**通用**
  锚点：不管背后是 Entra、Okta 还是 Auth0，都用"**这台 server 的 URL**"当 audience——`resource =
  https://…/mcp`。这样"token 只能用在这台 server"这条 anti-replay 规则就和 IdP 无关地成立。
- **Entra 不用 URL 当 audience，用它自己注册的 App ID URI。** 它从 scope 的 `api://88de6a37…` 前缀推 `aud`，
  压根不看 resource。
- 于是 MCP 想用 `https://…/mcp` 钉 audience、Entra 想用 `api://88de6a37…` 钉 audience——**两把锁指着同一扇
  门，钥匙却不通用**；而 Entra 又干脆不收 resource 这把"钥匙"。

一句话：**不是"scope 有目标了还多带 resource"，而是"MCP 只信 URL 形式的 audience（resource），Entra 只认
`api://` 形式的 audience（scope 前缀），两者对不上、Entra 还拒收 resource"。**

### 1.3 那 MCP 的 URL 为什么不能干脆写进 scope？

因为 **Entra 的 scope 必须挂在一个已注册的 App ID URI 上**，而这个 HTTPS 部署 URL **注册不成 App ID URI**：

- Entra 的 identifierUri 只接受 `api://<guid>`、`api://<你验证过的域名>`、`https://<你验证过的域名>` 这几类；
  随便一个 `*.azurecontainerapps.io/...` **不是你验证过的域名**，Entra 不让它当 identifierUri。
- 即便你有自定义已验证域名，App ID URI 里再带 `/mcp` 这种 path 也很别扭，Entra 推 `aud` 时并不按 URL path 走。

所以 `resource` 那个 `https://…/mcp` **天生进不了 scope**，只能作为独立的 `resource` 参数存在——而这个参数
Entra v2 又不收。死结就在这。**这也是为什么"在 Entra 侧修"行不通**（见 §7）。

### 1.4 `aud` / `scp` / `azp`…：token 里到底装了什么

前面的 scope、resource 都是**请求参数**（client 发给 Entra）；它们的"结果"落在 Entra 签发的那张
**access token（JWT）**的 **claim** 里。分清"请求参数"和"token claim"是看懂全局的关键：

| 请求时（client → Entra） | 落进 token 的 claim |
|---|---|
| `scope=api://88de6a37…/user_impersonation` | `aud`（目标）+ `scp`（权限） |
| `resource=https://…/mcp`（RFC 8707 想借它设定 `aud`） | ——（Entra v2 直接拒，不产生任何 claim） |

本项目一张**正常**的 access token（VS Code 那条路能拿到）解开大致长这样：

```jsonc
{
  "aud": "api://88de6a37-cf75-40d3-83e8-44c5ccbc0895", // 谁能用：MCP server 的 App ID URI
  "iss": "https://login.microsoftonline.com/9ea91fbb-.../v2.0", // 签发者 = 你的租户
  "azp": "49af5fc1-96e6-40c1-b108-cb828cc2a00e",  // 哪个 client 申请的（CLI client / VS Code 各异）
  "scp": "user_impersonation",                     // 授予的委托权限（空格分隔）
  "oid": "<你的用户对象 ID>",                        // 你是谁（租户内稳定）
  "tid": "9ea91fbb-...",                           // 租户
  "preferred_username": "you@contoso.com",
  "exp": 1730000000, "iat": 1729996400, "nbf": 1729996400 // 过期/签发/生效
}
```

逐个 claim 的职责：

- **`aud`（audience，"这 token 发给谁"）**：resource server 的身份。本项目 = `api://88de6a37…`（某些配置下是
  裸 appId `88de6a37…`，FastMCP 的 verifier 按实际签发值校验）。**`AzureJWTVerifier` 收到 token 第一件事就是
  校验 `aud` 等于它自己的 App ID URI**，不等就拒——这正是防"把发给别的 API 的 token 拿来冒用"。而
  **`resource` 参数的全部使命就是"请把 `aud` 设成我给的值"**：支持 RFC 8707 的 IdP 上 `resource=X` → `aud=X`；
  Entra 不玩这套，`aud` 一律从 scope 的 `api://` 前缀推。
- **`scp`（scope，"这 token 能干什么"）**：委托权限清单、空格分隔。本项目 = `user_impersonation`。**请求里的
  `scope` 和 token 里的 `scp` 是一对**：前者"我想要什么"，后者"实际给了什么"；server 校验 `scp` 含所需权限。
- **`azp` / `appid`（authorized party，"哪个客户端来要的"）**：VS Code = `aebc6443…`，本项目 CLI client =
  `49af5fc1…`。这就是"换 client"唯一改变的字段，但**不影响 `aud`/`scp`**——所以 server 端校验逻辑对换不换
  client 无感。
- **`oid` / `sub`（你是谁）**：用户身份。之后 server 用 `oid` + OBO 查你的 AD group、决定 tool 可见性——那才是
  最像 "RBAC" 的一步（见原理篇 §5）。
- **`iss` / `tid`（谁签的、哪个租户）**、**`exp` / `iat` / `nbf`（时效）**：签名、issuer、过期都由 server 校验。

**回到 bug**：`resource` 是**请求参数**、目的是设定 token 的 **`aud` claim**；Entra v2 既不收这个参数、又已经
用 scope 定了 `aud` → "你给的 resource 和 scope 推出的 aud 不是一回事" → `AADSTS9010010`。**问题自始至终在
"发 token 之前的请求阶段"，token 本身（aud/scp）反而是好的。**

---

### 1.5 换个 AS 会怎样？两种 audience 设计哲学

前面的冲突，根子是**两种关于"audience 是什么"的设计哲学**：

- **Entra = 身份优先（identity-as-audience）**：Entra 是目录/IAM 系统，一个 API = 一个 **App Registration**。
  audience = `api://<appid>`（一个**注册身份**）。好处：网址随便变、蓝绿/多区域/换域名，**身份 `appId` 不变**；
  audience 指向一个带 scopes/owner/consent 的**可治理对象**；内网无公网 URL 的 API 也能有身份。代价：资源必须
  **先在目录里注册**（还要验证域名）。**scope 因此写成 `api://<appid>/user_impersonation`——身份 + 权限揉在一起。**
- **RFC 8707 = 位置优先（URL-as-audience）**：Web/OAuth 生态把资源看成"某个 URL 上的东西"，audience = server
  URL。好处：**任意 URL 零注册即可当 audience**（正是 MCP 要的：任何人任何地方起个 server）；audience 就是地址、
  `.well-known` 直接可发现。代价：URL 会漂移、无治理对象、"谁都能声称某 URL"要靠别的机制防滥用。**scope 因此是
  纯权限（如 `mcp.access`），audience 单独用 `resource` 给。**

哪种更好？**各优化不同目标，没有绝对赢家**；MCP（去中心、URL 原生、零注册）恰好和 Entra（受治理、目录中心、
身份原生）是两极。**这是哲学冲突，不是谁写错代码。** 也正因如此 Entra 不肯把裸 URL 当 audience——在它的世界观里
audience 必须是个注册过的身份，否则"谁都能为任意 URL 领 token"就破了它的治理模型（这也正是那条 `resource`↔`scope`
校验想守的安全边界，见 §10）。

把同一个"以用户身份访问 MCP server"的请求，放到不同 AS 上并排看：

| AS | `scope` | audience 怎么给 | 出来的 token |
|---|---|---|---|
| **RFC 8707 原生**（Keycloak、Okta 支持后） | 纯权限 `mcp.access` | 独立 `resource=https://mcp.example.com/mcp` | `aud=https://mcp.example.com/mcp`，`scp=mcp.access` |
| **Auth0** | 纯权限 `read:data` | 独立，但参数叫 `audience=`（**不是** `resource`） | `aud=https://…`，`scope=read:data` |
| **Entra v2** | 融合 `api://88de6a37…/user_impersonation` | **无独立参数**——身份藏在 scope 前缀 | `aud=api://88de6a37…`，`scp=user_impersonation` |

```text
# Okta / RFC 8707 原生 —— Claude 发的两个参数正好对上模型，能用
resource = https://mcp.example.com/mcp     ← 谁
scope    = mcp.access                        ← 什么
# Entra —— 身份塞进 scope、没有独立 resource 的位置；Claude 再塞 resource=URL → 两个身份打架 → AADSTS9010010
```

> **公平起见**：不是"只有 Entra 坏"。**Auth0 默认也连不上 MCP**——它用 `audience` 不认 `resource`，要专门开
> [Resource Parameter Compatibility Profile](https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile)
> 才行；微软 VS Code 仓库里也有[用 Auth0 当 MCP AS 不行](https://github.com/microsoft/vscode/issues/274226)的
> issue。真正开箱即用的是 **RFC 8707 原生**的 AS。排序：**8707 原生 ✅ > Auth0（需开开关）⚠️ > Entra（硬报错）❌**。

---

## 2. Claude Code 实际发了什么 → 为什么报错

本项目 server 的 protected-resource metadata（`/.well-known/oauth-protected-resource/mcp`）广告：

```json
{
  "resource": "https://dataops-aca-mcp.../mcp",
  "authorization_servers": ["https://login.microsoftonline.com/<tenant>/v2.0"],
  "scopes_supported": ["api://88de6a37-.../user_impersonation"]
}
```

Claude Code 按 MCP 规范，向 **Entra v2** 端点**同时**发这两个：

```text
scope    = api://88de6a37-.../user_impersonation     ← 目标是 api://88de6a37...
resource = https://dataops-aca-mcp.../mcp            ← 目标是 https://.../mcp（RFC 8707）
```

Entra v2 一看：**`resource`（`https://.../mcp`）和 `scope` 的目标（`api://88de6a37...`）根本不是同一个东西**
→ `AADSTS9010010`。

而且 `https://dataops-aca-mcp....azurecontainerapps.io/mcp` 这个 URL **无法注册成 Entra 的 Application ID
URI**（不是租户验证过的域名），所以也**没法"把两者对齐"**。加上 Entra v2 会**强制校验 `resource` 与 scope 匹配**、配不上就拒
——**唯一干净出路是请求里干脆不带 `resource`（由服务端代持 token 交换时省掉）。**

> 附带一个 Claude Code 自己的小毛病（[#52871](https://github.com/anthropics/claude-code/issues/52871)）：
> 它给 `resource` 尾部加斜杠，即使值本来对得上也会因尾斜杠再次触发 AADSTS9010010。但对本项目而言，根因是
> 上面那个"`https://…/mcp` ≠ `api://…`"的更深层不匹配。

---

## 3. 为什么 VS Code 行、Claude Code 不行

同一台 server、同一份 metadata，差别**只在客户端**：

- **VS Code**（较老的 OAuth 实现）**不发** `resource` 参数 → Entra v2 只看 scope → 正常发 token。
- **Claude Code / opencode**（更严格地遵守 2025-06 MCP 规范）**发** `resource` → Entra v2 拒 → AADSTS9010010。

**这条差别本身就是关键证据**：如果这事由服务端 FastMCP 版本决定，两个客户端连同一台 server 结果应该一样；
既然一个行一个不行，说明**发不发 `resource` 是客户端行为**（见 §6）。

---

## 4. 到底是谁的锅

没有单一元凶。先把**层级**理清：**`resource`（RFC 8707）是 IETF 的 **OAuth** 扩展、不是 MCP 发明的，而且在
OAuth 那层是**可选**的；MCP 把它抬成客户端 **MUST**；Claude Code 只是忠实照 MCP 办事。** 所以**不是谁违反了
OAuth**，而是"**MCP 强制了一个 OAuth 可选项**" + "**Entra 与 RFC 8707 是两种 audience 哲学**"（§1.5）两件事
叠加撞出来的：

| 角色 | 责任 |
|---|---|
| **OAuth / RFC 8707** | `resource` 是 **OAuth 的可选扩展**（RFC 8707）；OAuth 2.0 core（RFC 6749）**从不要求**实现它 → 各 AS 本就被允许各搞各的（实现 / 换参数名 / 不实现） |
| **MCP 规范** | **根源**：把这个**可选项抬成客户端 MUST**、还**没留"AS 不支持就回退"的余地**——等于假设所有 AS 都按 8707 实现。规范自己想改（[#1614](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614)：resource→SHOULD + 回退），但正被以安全理由挡下（§10） |
| **Entra** | 走 **identity-audience 哲学**（把 `api://<appid>` 塞进 scope），不按 8707 的 `resource`→`aud` 语义；2026-03 强制校验把默默容忍变硬报错。不是"坏了"，是**另一套世界观**（§1.5） |
| **Claude Code** | **严格照 MCP 办事**（发 `resource`）→ 撞墙。合规但不够宽容——VS Code 老实现不发 `resource` 反而能用；外加尾斜杠小 bug（#52871） |
| **FastMCP（本项目 server）** | `RemoteAuthProvider`（直连 Entra）模式下基本是**旁观者**——只广告 metadata、只验 token，不在发 token 链路里。但它**能出手修**（换 `AzureProvider` 代理，见 §7/§8） |

---

## 5. 是不是所有对接 Microsoft 的 OAuth MCP 在 Claude 上都废了？——**不是**

关键分清**四类**，只有**第 ②** 类中招：

| 类型 | 例子 | Claude 上能用吗 |
|---|---|---|
| **① 本地 stdio MCP，用本地凭据** | **Azure MCP Server（`npx @azure/mcp` / .mcpb）**、Azure DevOps 本地版(PAT)、多数 DB/工具 server | ✅ **能用**：走 `DefaultAzureCredential`（az login / MI / VS Code）或 PAT/API key，**根本没有浏览器 OAuth、没有 resource 参数** |
| **② 远程 HTTP，客户端直连 Entra v2**（pre-registration / 只验 token） | **本项目 FastMCP `RemoteAuthProvider`**；连微软自己的**远程** azure-devops-mcp 都在这坑里 | ❌ **不能**，就是 `AADSTS9010010` |
| **③ 远程 HTTP，前面加 OAuth 网关/代理**（帮 Entra 挡掉 resource） | **Azure API Management 当 OAuth 网关**；FastMCP `AzureProvider` | ✅ **能用** |
| **④ 非 Entra 的 OAuth（支持 RFC 8707）** | Okta / Auth0 等现代 IdP | ✅ 一般能用（这坑是 **Entra v2 专属**，不是"OAuth MCP 都废了"） |

**所以担心的"Azure MCP 也不能用"恰恰相反——它能用**：主流的 Azure MCP Server 是**本地 stdio + 你已有的
az 登录**，压根不做浏览器 OAuth（第①类）。真正中招的**只有第②类**：远程、且让 Claude 直接对 Entra v2 做
OAuth 的 server——**本项目正好是第②类**。

> 最有说服力的旁证：**微软官方给"如何让 Entra 保护的远程 MCP 能被 Claude 用"的答案，就是"前面架一层
> Azure API Management 当 OAuth 网关"**（[Claude-Ready Secure MCP with APIM](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim)）——本质就是第③类的代理挡 resource。

---

## 6. 为什么"换低版本 FastMCP"规避不了

**`resource` 是客户端（Claude Code）发的，不是服务端 FastMCP 选的。**

- FastMCP 的 metadata 只是广告 `resource` 的**值**；**发不发这个参数、发去哪，是 Claude Code 的行为**。
  §3 已证明：VS Code 连同一台（新版）server 能用、Claude Code 不能。
- **降 FastMCP 版本 ≠ 让 Claude Code 不发 `resource`。** 而且 `resource` 的值来自 MCP server 的 **URL**，
  跟 FastMCP 版本无关；metadata（PRM）又是**发现授权服务器所必需**的，删了它 Claude Code 根本找不到 Entra。
- 真要靠"降版本"绕，得降的是 **Claude Code 自己**到 RFC 8707 之前的行为（等于退回 VS Code 那种老实现）——
  不现实、也不是 server 端能控制的。

---

## 7. 怎么修

| 方案 | 说明 | 代价 |
|---|---|---|
| **服务端 OAuth Proxy（剥掉 `resource`）** | 换成 FastMCP 的 `AzureProvider`（OAuth Proxy 模式）：由 server 代持 Entra 的 token 交换，自己用 `scope=api://.../.default`、**不带 `resource`** 请求 Entra；客户端面对的是 proxy，proxy 接受/忽略 `resource`。见 [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md) | **架构改动**（pre-registration → proxy），能根治 |
| **前面架 APIM 网关** | 微软官方 Claude-ready 方案，等价第③类 | 基建更重 |
| **等上游修** | MCP 规范 #1614 想把 `resource` 改成可回退，但**正被以安全理由否掉**；#55993 已按 duplicate 关、#52871（尾斜杠）修了也不解决根因 | **基本指望不上**——实测见 §10 |
| **临时 bearer token** | Claude Code 用 `headers.Authorization: Bearer <token>` 绕过 OAuth（手动弄一个 aud=`api://88de6a37...`、scp=`user_impersonation` 的 Entra token） | token ~1h 过期、无刷新，只适合验证 server 侧 OK |
| **先只用 VS Code** | 不受影响，现在就能用 | 用不了 Claude Code/opencode |

**为什么不能在 Entra 侧修**：`resource` 的值是 MCP 的 URL，既无法注册成 identifierUri、也无法和 scope 对齐；
Entra v2 又会强制校验 `resource` 必须与 scope 匹配（配不上就拒）——所以"去掉 `resource`"只能在**服务端代持 token 交换**时做，即 proxy 模式。

---

## 8. 修复方向详解：OAuth Proxy 模式（把 AS 换成 FastMCP）

> 这是 §7 表里"服务端 OAuth Proxy"那一行的展开。**架构原理见**
> [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md)。
> **本项目日后再考虑是否落地**——这里先把心智模型记下。

### 8.1 Proxy ≈ DCR 吗？—— 包含关系（Proxy ⊃ DCR）

DCR 只是 proxy 顺带提供的**一个能力**（让任意 client 动态注册到 FastMCP）。proxy 本身是"两层 OAuth 世界"：

```text
Client ──OAuth（含 DCR）──> FastMCP(当 AS，自己发 token) ──OAuth（固定 client）──> Entra
```

所以 **Proxy = DCR + 帮 client↔Entra 转译/代持 token +（对本 bug 关键）向 Entra 发 token 请求时把
`resource` 剥掉**。它不只是 DCR。

### 8.2 OAuth 对象从 Entra 变成 FastMCP 本身

对。这正是 proxy 模式的定义：**client 眼里的 Authorization Server 变成 FastMCP**——client 只对 FastMCP 走
OAuth、拿到的是 **FastMCP 自己签发的 token**（不是 Entra 原始 token）；FastMCP 在背后才用**它自己那个固定的
Entra app** 去跟 Entra 换 token。

**这就是绕过 AADSTS9010010 的原理**：Claude Code 的 `resource` 参数发给的是 FastMCP（proxy 接受/忽略它），
而 FastMCP→Entra 那一跳由**你的代码控制**，用 `scope=api://.../.default`、**不带 `resource`** → Entra 满意。

### 8.3 两处要校准的认知

- **不用再在 Entra 预注册每个 client**（方向反了，反而更省事）：proxy + DCR 让 client **动态注册到
  FastMCP**（或你在 FastMCP 侧配白名单）。那个 public client（`49af5fc1`）在 proxy 模式下**不再需要**。
  Entra 里只需**一个固定的 confidential app 给 FastMCP 自己**（要 client secret / 证书，不再是 public
  client）。"认识 client"从 Entra 挪到了 FastMCP 层。
- **FastMCP 的身份 vs 用户的身份别合并**（有坑）：FastMCP 用它自己那个 Entra app 的凭据当 **OAuth 客户端**去
  换 token——这是"FastMCP 的 identity"没错；但换回来的 token **仍代表登录的那个用户**。下游调 Graph、查 AD
  group、tool 门控、OBO **必须继续用"用户身份"**，不能切成 FastMCP 的服务身份，否则整套 identity-aware
  设计（谁能看哪个 tool、按 group 授权）就塌了。

  > 记法：**FastMCP 的身份 = 跟 Entra 换 token 时的"OAuth 客户端凭据"；用户的身份 = 换回来那个 token 的
  > 主体，继续往下游流。** 两者别合并。

### 8.4 落地大致涉及（日后细化）

- **Entra**：新建一个 confidential app 给 FastMCP（client secret / 证书 + redirect `/auth/callback`）。
- **server 代码**：`RemoteAuthProvider` + `AzureJWTVerifier` → `AzureProvider`（OAuthProxy），配
  client_id / secret / tenant + 所需 scope + client 存储。
- **退役**现有 public client（`49af5fc1`）。
- **客户端 mcp.json**：去掉 `oauth.clientId`（改成对 FastMCP 做 DCR，通常零配置）。

---

### 8.5 微软怎么看 proxy / DCR？安全吗？能往公司推吗？

把三件容易混在一起的事分开：

**(1) 微软明确不支持 DCR，这是 deliberate 的安全设计，不是偷懒。** Entra 不做 MCP 想要的那种开放 DCR
（RFC 7591：任意 client 运行时自助注册）。理由是企业安全原则——**client 身份必须被治理/审核，不能自助声称**。
开放 DCR = 任意来路不明的 app 都能注册进来要 token、诱导用户 consent，是钓鱼与滥用的温床。所以"微软不喜欢
DCR"是真的，而且**理由正当**。

**(2) "proxy 这个模式"微软并不反对——他们自己就推荐一个 proxy。** 微软官方给"Entra 保护的远程 MCP 怎么让
Claude 用"的答案，就是**前面架一层 Azure API Management (APIM) 当 OAuth 网关**（[Claude-ready secure MCP
with APIM](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim)）。APIM 网关和 FastMCP 的
`AzureProvider` **是同一个架构模式**：一个中间层，对 client 当 AS、对 Entra 当 confidential client、吃掉
resource/DCR 的不兼容。所以**微软反对的不是"加个 proxy/gateway"，恰恰他们背书这个模式**——只是要你用
**受治理、被加固**的网关（APIM），而不是一个开着开放 DCR 的裸 proxy。

**(3) proxy 的"不安全"具体不安全在哪——是可配置项，不是天生的。** FastMCP proxy 本质是个成熟模式（BFF /
token broker / API gateway，企业里到处都是），风险点全都可控：

| 风险点 | 说明 | 怎么消掉 |
|---|---|---|
| **开放 DCR** | 若让 proxy 接受任意 client 自助注册，就把 Entra 刻意规避的"无治理 client"风险搬回自己家 | **别开开放 DCR**，只 pin 已知 client（治理权留在你手里） |
| **secret 集中 + 代持所有人 token** | proxy 是 confidential client、握 app secret，还替所有用户换/持 token，blast radius 比 pre-registration 大 | secret 进 Key Vault、短 TTL、最小权限、审计日志 |
| **audience 绑定被削弱** | proxy 自签 token、剥掉 `resource`，用错会削弱 RFC 8707 想要的 anti-replay | 正确设 `aud`、严格校验、不乱透传 |

**结论（给你往公司推的措辞）：**

- **你现在这套 pre-registration（`AzureJWTVerifier` 只验 token）其实正是"最贴合微软安全原则"的架构**：client
  被注册/治理、admin 可 consent、server 只验 token、无开放 DCR、无中间层代持所有人 token。它**没有安全问题**
  ——唯一毛病是撞了 `AADSTS9010010` 这个**互操作 bug**（client 侧 + 规范侧的问题，不是你架构不安全）。
- **要现在就让 Claude Code 能用**，微软给的"官方安全解"是 **APIM 网关**（受治理的 proxy 模式），不是开放
  DCR。FastMCP 的 `AzureProvider` 是同款模式的自托管版：**只要不开开放 DCR、secret 进 Key Vault，就等价于
  "自己搭的 APIM"**，可以推；**一旦打开开放 DCR，就踩到微软反对的那条线**。
- **最稳的企业叙事**：要么 (a) **维持 pre-registration，等上游把 `resource` 改成可回退**（MCP #1614 /
  Claude Code 侧），架构零妥协；要么 (b) **上 APIM（或受治理的自托管 proxy）**，client 仍走注册/治理、关掉
  开放 DCR。两条都站得住、都不违反微软 design principle。

> 一句话给管理层：**"我们不做开放 DCR。要么等标准修好（保持现状最干净），要么按微软官方蓝图上 APIM 网关；
> FastMCP proxy 只是自托管版的同款网关，同样关掉开放 DCR。"**

---

## 9. 时间线 / 关键事实速记

- **RFC 8707**（Resource Indicators）：给 OAuth 加可选 `resource` 参数，让 token 的 `aud` 精确指向某 API。
- **MCP 授权规范 2025-06**：把 `resource` 从"可选"提成客户端 **MUST**。
- **Entra v2**：scope-centric；**会**校验 `resource`，但要求它与 scope 属主匹配（非 MCP 期望的 `resource`→`aud` 语义）。
- **2026-03**：Entra 上线强制校验，`resource`+`scope` 冲突从"默默容忍"变成 **AADSTS9010010 硬报错**。
- **现状（2026-07）**：Claude Code / opencode 直连 Entra v2 的远程 MCP 全部中招；VS Code 因不发 `resource`
  幸免；本地 stdio Azure MCP 因不走浏览器 OAuth 幸免。

---

## 10. 上游会修吗？timeline 实测（2026-07-10）

> 结论先行：**短期内指望不上上游修复。** 实测下面几个关键 issue 的当前状态——那条最该修的规范提案，
> 正在被**以安全理由否掉**，不是"慢"，是"方向被堵"。所以 **proxy 才是通用正解**（对任何守规范、会发
> `resource` 的 client 都生效），不该把方案压在"等上游"上。

| Issue | 状态（2026-07-10 实测） | 含义 |
|---|---|---|
| **MCP 规范 [#1614](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614)**（把 `resource` 改成 OPTIONAL） | **OPEN，停滞**（末次活动 2026-05-11，无 PR） | 讨论**转向反对**：维护者指出 `resource`↔`scope` 校验是**故意的安全设计**（防止有人为不属于自己的 scope 冒充 resource server / confused-deputy），提案人已**自认"这提案不 secure"** → 基本判死 |
| **claude-code [#55993](https://github.com/anthropics/claude-code/issues/55993)**（就是 9010010 mismatch） | **CLOSED as `duplicate`**（2026-05-08，已锁） | 不会作为独立 bug 修 |
| **claude-code [#52871](https://github.com/anthropics/claude-code/issues/52871)**（`resource` 尾斜杠） | **OPEN，仍活跃**（2026-07-09 还在动） | 真 bug，但**修了也不解决本项目根因**：去掉尾斜杠后 `https://…/mcp` 依旧配不上 `api://88de6a37…` |
| **规范里合并的 `resource`/8707 PR** | **无**（2026 年无任何 resource-optional 相关 PR 合并） | 管道里没有救兵 |

**一个顺带的纠正**（也已修正 §0/§1/§2/§7 的措辞）：#1614 的讨论暴露一个之前讲得不够准的点——**AADSTS9010010
说明 Entra 其实"收下并校验了" `resource`**（所以报的是 "mismatch"，不是 "resource not supported"）。即 Entra
不是"不认 `resource`"，而是**按 RFC 8707 语义强制校验 `resource` 必须与 `scope` 的属主匹配**；MCP 的 URL 形式
`resource` 天生配不上 `api://` 形式的 scope，于是被拒。而这条校验被维护者认定是**正当的安全控制**——**这正是
#1614 修不动的根本原因**：放开它 ＝ 削弱一个真实的安全边界。

**对决策的影响**：既然干净的上游修复在可预见的未来不会来，"等上游"不成立；**要支持 VS Code 以外的 client，
proxy（§7/§8）就是该走的方向，而非权宜之计**。规模决定壳：小用量单容器双 path 足够，平台级才上 APIM。

---

## 参考资料

- [`MCP-自定义Client接入-...md`](./MCP-自定义Client接入-Entra与各Agent客户端支持对比.md) —— 各 client OAuth 支持对比、OAuth 时序/PKCE
- [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md) —— proxy 模式（修复方案的架构）
- [Groff – Entra ID × MCP 认证不兼容清单（proxy 剥离 resource）](https://www.groff.dev/blog/azure-entra-id-mcp-server-authentication-incompatibilities)
- [微软 – Building Claude-Ready Entra-Protected MCP with APIM](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim)
- [Claude Code #55993（resource/scope mismatch）](https://github.com/anthropics/claude-code/issues/55993) / [#52871（尾斜杠）](https://github.com/anthropics/claude-code/issues/52871)
- [FastMCP #1846（Entra 'resource' not supported）](https://github.com/PrefectHQ/fastmcp/issues/1846) / [Azure 集成（AzureProvider）](https://gofastmcp.com/integrations/azure)
- [MCP 规范 #1614（把 resource 改成可选/回退）](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614)
- [RFC 8707 – Resource Indicators for OAuth 2.0](https://www.rfc-editor.org/rfc/rfc8707.html)

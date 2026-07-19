---
title: "问答：MCP 授权流 —— 发现机制、issuer、well-known 探测、redirect_uri 到底谁在跳"
date: 2026-07-12
tags:
  - mcp
  - oauth
  - oidc
  - rfc8414
  - rfc9728
  - rfc8707
  - pkce
  - discovery
  - redirect_uri
  - mcpproxy
status: 教学问答（配套可视化 App）
sources:
  - "src/mcp-server/mcpproxy.py"
  - "src/mcp-server/main.py"
  - "docs/multi-client-implementation/实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md"
  - "docs/multi-client-implementation/Bug剖析-AADSTS9010010-MCP的resource参数撞上Entra-v2.md"
  - "docs/multi-client-implementation/oauth-mcp-flow-demo/（本目录 · 配套可视化 App）"
verified:
  - "所有 well-known 探测、401 头、PRM 内容均为对已部署实例的真实抓取（见文中实测表）"
---

# 问答：MCP 授权流 —— 发现机制、issuer、well-known 探测、redirect_uri 到底谁在跳

> 这份文档把一次关于 `/mcpproxy`（剥离 resource 的代理）与 `/mcp`（直连 Entra）两条授权流的答疑
> **整理成册**，重点讲清四件容易绕晕的事：**① 为什么无 token 会 401、② 发现（discovery）其实是两轮、
> ③ issuer 与 well-known URL 的关系、④ redirect_uri 到底是谁在做重定向**。
>
> 配套一个可跑的**可视化教学 App**（就是**本目录**下的这个 App：同级的 `server.py` + `public/`）：一步步动画演示
> 这 14 / 11 步的真实 request/response，支持 `mcpproxy ↔ mcp` 两条流程切换，含 Demo 回放与 Live 真实调用。
> 跑法见同级 `README.md`。

---

## 0. 两条流程总览（一张表看懂差别）

| | 🪄 **mcpproxy 流程**（14 步） | ⚙️ **mcp 直连流程**（11 步） |
|---|---|---|
| 谁走这条 | Claude Code / opencode（**发** RFC 8707 的 `resource`） | VS Code（**不发** `resource`） |
| step 3 PRM 的 `authorization_servers` | 指向 **代理自己** `…/mcpproxy` | 指向 **Entra** `…/v2.0` |
| step 4 找端点时问谁 | 问**代理**要 AS 元数据 | 直接问 **Entra** 要发现文档 |
| `/authorize`、`/token` | 打**代理**，代理删 `resource` 再转发 Entra | **直接**打 Entra，全程无 `resource` |
| 会不会撞 `AADSTS9010010` | 客户端带 `resource` → 必须靠代理删掉才不撞 | 本就不带 → 不撞，也不需要代理 |
| 最终 token / 门控 | 真 Entra token；同一个 verifier + OBO + group | **完全一样** |

**殊途同归**：两条路拿到的都是同一种真 Entra token（`aud/scp/oid` 一致）。差别只在“发 token 之前的协议协商”——
**要不要一层代理来删 `resource`**。

---

## 1. 为什么“无 token 调用”会返回 401（而且跟 POST 无关）

`/mcpproxy` 这个 MCP 端点被 `RequireAuthMiddleware` **包在最外层**（`src/mcp-server/mcpproxy.py:170`）：

```python
proxy_mcp_endpoint = RequireAuthMiddleware(streamable_app, required_scopes, prm_url)
Route(proxy_path, proxy_mcp_endpoint, methods=["GET", "POST", "DELETE"])   # mcpproxy.py:193
```

任何打到 `/mcpproxy` 的请求（**GET / POST / DELETE 一视同仁**）第一站都是这个中间件，轮不到 MCP/JSON-RPC 逻辑。
SDK 的实现（`mcp/server/auth/middleware/bearer_auth.py`）：

```python
async def __call__(self, scope, receive, send):
    auth_user = scope.get("user")
    if not isinstance(auth_user, AuthenticatedUser):        # 没有有效 token → 没有已认证用户
        await self._send_auth_error(send, status_code=401,  # → 直接 401，永不进入 self.app
            error="invalid_token", description="Authentication required")
        return
    ...
```

- 校验成功与否由外层认证层（`RemoteAuthProvider` + `AzureJWTVerifier`）填的 `scope["user"]` 决定；没带 token → 401。
- **不是“POST 才 401”**：GET 也一样 401（中间件在解析 body/方法之前就拦掉了）。demo 里画成 POST，只因为 MCP 的第一发
  `initialize` 是 JSON-RPC over POST。

**401 里那个 `resource_metadata=` 指针是哪个变量？** 是 `prm_url`（`mcpproxy.py:106`）：

```python
prm_path = "/.well-known/oauth-protected-resource" + proxy_path   # /.well-known/oauth-protected-resource/mcpproxy
prm_url  = f"{base}{prm_path}"                                    # base 来自 main.py 的 MCP_SERVER_BASE_URL
```

它一份数据、两处使用：**① 塞进 401 的 `WWW-Authenticate` 头**（`mcpproxy.py:173` → SDK 拼 `resource_metadata="…"`）；
**② 注册成实际能 GET 的路由**（`Route(prm_path, protected_resource_metadata)`，`mcpproxy.py:177`）。

真实抓到的 `/mcpproxy` 401 头：

```text
www-authenticate: Bearer error="invalid_token", error_description="Authentication required",
                  resource_metadata="https://…/.well-known/oauth-protected-resource/mcpproxy"
```

---

## 2. 发现（discovery）其实是**两轮**，别混一起

面对一个受保护资源，客户端要探两次：

- **第一轮（RFC 9728 · Protected Resource Metadata）——找“哪个 AS”**：顺着 401 的 `resource_metadata` 指针 GET 到
  PRM，读出 `authorization_servers`。→ 时序图 **step 3**。
- **第二轮（RFC 8414 / OIDC Discovery）——找“那个 AS 的端点”**：把上一步的 AS 标识变换成 well-known 元数据地址，
  GET 它拿到 `authorization_endpoint` / `token_endpoint`。→ 时序图 **step 4**。

探到之后客户端会**缓存**这些端点，后续请求不再重探。

### 2.1 step 3 的 PRM 里**没有** `issuer` 这个字段

实测两条流程的 PRM 字段都只有四个：

```text
keys = ['resource', 'authorization_servers', 'scopes_supported', 'bearer_methods_supported']
```

- 我们口语里说的“issuer”，指的是 **`authorization_servers` 数组里的那个值**——按 OAuth 术语，一个授权服务器就是用它的
  issuer URL 来标识的，但 **PRM 里这个字段名叫 `authorization_servers`，不叫 `issuer`**。
- 真正叫 `issuer` 的字段出现在 **step 4 的响应**（AS 元数据）里，并且规范要求它**必须等于** step 3
  `authorization_servers` 里的那个值。这就是客户端把 step 3 的指针“续”到 step 4 的凭据（防止被指到假 AS）：

| | step 3 `authorization_servers[0]` | step 4 响应里的 `issuer` |
|---|---|---|
| 代理 | `https://…/mcpproxy` | `https://…/mcpproxy` ✅ 相等 |
| 直连 | `https://login.microsoftonline.com/<tenant>/v2.0` | `https://login.microsoftonline.com/<tenant>/v2.0` ✅ 相等 |

### 2.2 step 4：从 AS 标识拼出 well-known 地址 —— 两个**正交**维度

拿到 AS 标识（`https://H/P`）后，客户端要拼发现地址。这里有**两个互相独立**的维度，别绑一起：

- **维度 A · 文档类型**：`oauth-authorization-server`（RFC 8414）**vs** `openid-configuration`（OIDC）。
- **维度 B · well-known 段的位置**：**插入式**（放在 host 和 path 之间） **vs** **追加式**（缀在 issuer 末尾）。

对 `https://H/P`：

```text
① https://H/.well-known/oauth-authorization-server/P     ← 8414 · 插入式
② https://H/P/.well-known/oauth-authorization-server     ← 8414 · 追加式
③ https://H/P/.well-known/openid-configuration           ← OIDC · 追加式
```

### 2.3 实测：每台服务器**到底提供哪种**（这才是判断依据，不是靠猜）

“怎么知道哪条流程用哪种？”——**不是靠规则，是靠 ① 读代理的路由代码 + ② 真实探测哪个返回 200。** 实测：

| URL | 结果 | 说明 |
|---|---|---|
| 代理 `…/.well-known/oauth-authorization-server/mcpproxy`（插入式） | **200** | 代理发 8414 |
| 代理 `…/mcpproxy/.well-known/oauth-authorization-server`（追加式） | **200** | 同一份文档，换个位置也发 |
| 代理 `…/mcpproxy/.well-known/openid-configuration` | **404** | 代理**不**发 OIDC（它不是 OIDC provider，不签 id_token） |
| Entra `…/v2.0/.well-known/openid-configuration`（追加式 · OIDC） | **200** | Entra 发 OIDC |
| Entra `…/v2.0/.well-known/oauth-authorization-server`（追加式 · 8414） | **404** | 该 issuer 上 8414 那几种都 404 |
| Entra 插入式两种 | **404** | |

**结论（更正一个常见误解）**：两条流程 step 4 真正的区别是 **“文档类型”**（代理发 8414 / Entra 发 OIDC），
**不是“插入式 vs 追加式”**。插入/追加只是**位置**；代理把**同一份 8414 文档**挂在插入式+追加式两个位置——这正是
`mcpproxy.py:181` 那两条 route（注释原话：“不同 MCP client 会试不同位置”）：

```python
Route("/.well-known/oauth-authorization-server" + proxy_path, authorization_server_metadata, methods=["GET"])  # 插入式
Route(proxy_path + "/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"])  # 追加式
```

### 2.4 客户端是不是“把这些 URL 挨个试到 200 为止”？

**基本对，但要精确：**

1. **只在“发现”这一步探测**，不是任何 OAuth 操作都探；探到就缓存。
2. **候选清单和顺序由规范枚举**（上面 ①②③），不是乱试。
3. **结束条件不是“随便一个 200”**，而是**第一个返回“合法且 `issuer` 匹配”的元数据**——一个 200 但 body 不合规、
   或 `issuer` 对不上，会被拒绝并继续试。
4. **各家 client 试的集合/顺序不统一**，极简的可能只试一种 → 若服务器只发另一种就会失败。**这就是服务器端常常多发
   几种来兜底的原因**（我们代理即如此）。
5. 本目录的 demo client（`server.py`）是**简化版**：我直接写死了每条流程能用的那一条（代理→插入式 8414；
   直连→Entra 的 openid-configuration），没做逐个探测。一个**合规**的 client 才会按 ①②③ 挨个试。

---

## 3. redirect_uri 到底是谁在“跳”？（authorize 做了、token 没做）

这是最容易想当然的一段。先给结论表：

| 环节 | 有没有发生 HTTP 302 重定向？ | 谁发的 | 跳去哪 |
|---|---|---|---|
| **代理 `/authorize`** | **有** | **代理**（`RedirectResponse`） | 跳去 **Entra**（不是 redirect_uri！） |
| 浏览器 → Entra 登录 | 浏览器**跟随**上面的 302 | —（浏览器行为） | Entra 登录页 |
| **Entra → loopback** | **有** | **Entra** | 跳去 **redirect_uri**（`localhost:8080/callback`），带 code+state |
| **代理 `/token`** | **没有** | —（服务器间 POST） | 不跳；直接回 JSON |

### 3.1 redirect_uri 是什么、谁校验它

`redirect_uri = http://localhost:8080/callback` 是**客户端自己的 loopback 回调地址**。它：

- 在 **`/authorize` 和 `/token` 两个请求里都出现**，而且 Entra 要求两处**必须一致**（安全要求：换 token 用的
  redirect_uri 必须和发起授权时的一样）。
- **由 Entra 校验**，比对的是 client `49af5fc1` 在 Entra 里注册的 **redirect 白名单**（当前只有
  `http://localhost:8080/callback`）。
- **代理只是原样透传它**，自己从不拿它做重定向目标（所以不存在 open-redirect：见 §5）。

### 3.2 `/authorize`：代理**确实发了 302**，但目的地是 Entra，不是 redirect_uri

代理的 authorize handler（`mcpproxy.py:139`）：

```python
async def authorize(request):
    params = dict(request.query_params)            # 含 redirect_uri、code_challenge、state、resource…
    params.pop("resource", None)                   # 只删 resource
    params["scope"] = _ensure_scopes(params.get("scope"), api_scope)
    return RedirectResponse(f"{upstream_authorize}?{urlencode(params)}", status_code=302)  # ★ 302 → Entra
```

- 这里**确实产生一个 302**（`RedirectResponse`），把浏览器**送去 Entra 的 `/authorize`**。
- `redirect_uri` 只是被**塞进 Location 的 query 里转发给 Entra**（当数据用），**代理并没有 redirect 到
  `localhost`**。你问的“authorize 肯定做了重定向”——对，但它跳的是 **Entra**，不是回 client 的 loopback。

### 3.3 “跳回 localhost 取 code+state”那一跳 —— 是 **Entra** 做的，**不是代理**

浏览器跟随上面的 302 到 Entra，用户登录/consent 成功后，**由 Entra 发下一个 302**：

```text
Location: http://localhost:8080/callback?code=<授权码>&state=<原样>
```

- **这一跳完全是 Entra → client 的 loopback，代理没有任何路由参与、也看不到 code。**
- 你的推断完全正确：*“Entra 会返回给 client 一个 redirect，让 client 跳到 localhost 去取 code 和 state。”*
- 正因为代理没经手这一跳，它才**几乎无状态**（不生成自己的 PKCE、不签 token、不存东西）——这就是“无状态”的来源。

### 3.4 `/token`：**根本不 redirect** —— 是服务器间 POST + 原样回传

代理的 token handler（`mcpproxy.py:150`）：

```python
async def token(request):
    form = dict(await request.form())
    form.pop("resource", None)                     # 删 resource
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.post(upstream_token, data=form, headers={"Accept": "application/json"})  # 服务器→服务器
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)   # ★ 原样回 JSON，无 302
```

- **没有任何重定向**。它是代理**在后台用 httpx POST** 到 Entra 的 token 端点（server-to-server），拿回 token JSON，
  再**原样**回给 client。
- `redirect_uri` 在这里只是 form 里的一个字段，**被转发给 Entra 让它核对**（code ↔ redirect_uri 一致性），
  **不触发跳转**。
- 所以回答你的问题：**authorize 端点做了 redirect（302 到 Entra）；token 端点没有做 redirect。**

### 3.5 两条流程的 redirect 差异（顺带对比）

- **mcpproxy 流程**：浏览器看到**两跳 302** —— ①代理 `/authorize` → Entra；②Entra → `localhost/callback`。
- **mcp 直连流程**：客户端**自己**构造 Entra 的 `/authorize` URL 并直接打开（没有代理那跳 302），所以只有**一跳
  302** —— Entra → `localhost/callback`。token 也是客户端**直接** POST Entra（同样不 redirect）。

### 3.6 一图看清（mcpproxy 流程）

```text
client ──GET /mcpproxy/authorize?…&redirect_uri=localhost/callback&resource=…──► 代理
代理 ──302 Location=Entra/authorize?…（删了 resource，redirect_uri 原样带上）──► client
client(浏览器) ──跟随──► Entra/authorize ──登录/consent──►
Entra ──302 Location=http://localhost:8080/callback?code=…&state=…──► client   ← 这一跳是 Entra 发的，代理看不见
client ──POST /mcpproxy/token（code+code_verifier+redirect_uri+resource）──► 代理
代理 ──httpx POST Entra/token（删 resource）──► Entra ──真 token──► 代理 ──原样 JSON（无 302）──► client
```

---

## 4. PKCE：`code_challenge`（承诺）↔ `code_verifier`（证明）

- **step 5（承诺）**：客户端在 `/authorize` 交出 `code_challenge = base64url(SHA256(code_verifier))`。Entra 把它
  和即将签发的授权码**绑定存下**。
- **step 9/10（证明）**：客户端在 `/token` 交出**原文 `code_verifier`**。Entra 现算 `SHA256(code_verifier)`，比对当初
  存的 `code_challenge`，一致才发 token。
- **作用**：防授权码被截获。就算攻击者从 redirect 里偷到 `code`，没有 `code_verifier`（从没离开过 client）也换不出
  token。对**没有 secret 的 public client**，PKCE 就是用来**替代 secret**、证明“来换 token 的和当初发起的是同一个”。
- **代理不生成自己的 PKCE，只透传客户端的**——所以 step 5 和 step 6 里 `code_challenge`/`redirect_uri` 完全一样
  （代理只删了 `resource`、补了 scope）。PKCE 是 **client ↔ Entra 端到端**的。

---

## 5. 顺带澄清：三种 Starlette 响应类的区别

| 类 | 干什么 | 本项目用在哪 |
|---|---|---|
| `JSONResponse(data)` | 序列化 JSON，`Content-Type: application/json`，默认 200 | 两份发现元数据（step 3/4）——回数据给 client 读 |
| `RedirectResponse(url, status_code=302)` | 空 body + `Location: url` + 3xx，叫浏览器跳走 | `/authorize`（step 6）——跳去 Entra |
| `Response(content, status_code, media_type)` | 最原始，body/状态/类型全手动控制 | `/token`（step 12）——**原样透传** Entra 的响应，一个字节不改 |

`JSONResponse` / `RedirectResponse` 都继承自 `Response`。**要跳转用 `RedirectResponse`，要回 JSON 用 `JSONResponse`，
要“拿到啥就原样吐啥”用裸 `Response`。**

**防 open-redirect 的点**：代理的 `/authorize` 只把参数拼到**写死的 Entra 常量 URL** 后面，从不根据用户输入决定跳去
哪；用户传的 `redirect_uri` 是转给 **Entra 的白名单**兜底的——所以别往 `49af5fc1` 的 redirect 白名单里加宽松地址。

---

## 6. 术语速查

| 词 | 指什么 |
|---|---|
| PRM（RFC 9728） | Protected Resource Metadata，step 3 那份，告诉你“我的 AS 是谁”（字段 `authorization_servers`） |
| AS metadata（RFC 8414） | Authorization Server Metadata，step 4 那份，告诉你 AS 的 authorize/token 端点（字段 `issuer` 在这里） |
| OIDC Discovery | `openid-configuration`，OIDC provider（如 Entra）发的发现文档，字段与 8414 大量重合 |
| issuer | AS 的身份标识 URL；step 3 里以 `authorization_servers` 的值出现，step 4 里以 `issuer` 字段出现，两者必须相等 |
| resource（RFC 8707） | MCP 客户端带的“受众绑定”参数；Entra v2 不认它的位置 → 撞 `AADSTS9010010`；代理的唯一动作就是删它 |
| redirect_uri | 客户端 loopback 回调；由 Entra 按 `49af5fc1` 白名单校验；**跳回它的是 Entra，不是代理** |
| PKCE | `code_challenge`（承诺，authorize 带）↔ `code_verifier`（证明，token 带），public client 的“无 secret 防截获” |

---

## 参考

- [实现说明：方案 A —— `/mcpproxy` resource-剥离代理（代码剖析 · Design 取舍 · 安全分析）](../实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md)
- [Bug 剖析：AADSTS9010010 —— MCP 的 resource 参数撞上 Entra v2](../Bug剖析-AADSTS9010010-MCP的resource参数撞上Entra-v2.md)
- 配套可视化 App：**本目录**（`python3 server.py` → http://localhost:8080）

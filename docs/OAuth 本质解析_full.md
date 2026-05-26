---
title: "OAuth 本质解析"
date: 2026-05-13T02:37:28
tags:
  - chatgpt
---

## User

OAuth 的本质包含几个参与方：Resource、Resource Owner、Authorization Server 和 Client。

它的核心流程是：
1. Client 向 Resource 发起请求。
2. 通过 Authorization Server 返回一个登录页面，让 User 进行登录。
3. 登录成功后，Authorization Server 返回一个 Authorization Code 给 Client。
4. Client 再拿 Authorization Code 交换 Access Token。
5. 拿到 Token 后，Client 才能去访问 Resource。

在这个过程中，如果 Client 没有在 Authorization Server 里注册过，就需要通过 DCR (Dynamic Client Registration) 流程去动态注册。我理解得对吗？

或者说 Client 可以预注册（Pre-register）。比如在 Azure 里，我知道 Entra 不支持 DCR，它只能预注册。也就是说，Client 必须要在 Entra 里注册一遍之后，才能代表用户（Work on behalf of user）去执行 OAuth 流程。

这个 Client 的注册，其实就是在 Entra 的 App Registration 里创建一条记录，产生一个 Client ID 和一个 Redirect URL：
- 当用户在 Client 里点击时，Client 会向 Authorization Server 发送请求。
- Authorization Server 会检查这个 Client ID 是否注册过。
- 如果已注册，并且请求中的 Redirect URL 与 Entra 中记录的一致，就直接放行；否则就不予放行。

我理解的这些关于 OAuth 的一般性原理（OAuth in general）是对的吗？Show moreShow less

---

## Assistant

是的，你的理解整体上是正确的，而且已经抓到了 OAuth 2.0 的核心。

不过里面有几个概念值得再精确地区分一下，否则后面在理解 Entra ID、On-Behalf-Of（OBO）、DCR、Confidential/Public Client 时会混淆。


我帮你按“OAuth 本质”重新整理一下。


---


# 一、OAuth 里真正的几个角色


OAuth 2.0 标准里的四个核心角色：


| 角色 | 含义 |
| --- | --- |
| Resource Owner | 用户（User） |
| Client | 想访问资源的应用程序 |
| Authorization Server | 发 Token 的服务器 |
| Resource Server | API / 资源服务器 |


你说的：


> Resource、Resource Owner、Authorization Server、Client


基本是对的。


更准确一点：


- Resource ≈ Resource Server（API）
- Resource Owner ≈ 用户


---


# 二、OAuth 的本质是什么？


OAuth 的本质：


> “用户授权某个 Client 代表自己访问 Resource。”


注意：


OAuth 不是登录协议。


OAuth 是：


- delegation（委托授权）
- token issuance（签发访问令牌）


虽然现实里很多人把 OAuth 当登录用，但真正负责“登录身份”的其实是 OpenID Connect。


---


# 三、你描述的 Authorization Code Flow 基本正确


你写的流程：


1. Client 请求 Resource
2. 跳转 Authorization Server 登录
3. 返回 Authorization Code
4. 用 Code 换 Token
5. 用 Token 访问 Resource


这是经典：


# Authorization Code Flow


稍微严谨一点：



```
User Browser    |    | 1. 用户点击登录    vClient App    |    | 2. redirect 到 Authorization Server    vAuthorization Server    |    | 3. 用户登录 + consent    |    | 4. redirect back with Authorization Code    vClient App    |    | 5. backend 用 code 换 access token    vAuthorization Server    |    | 6. 返回 access token    vClient App    |    | 7. 调用 Resource Server(API)    vResource Server
```


---


# 四、你关于 Client Registration 的理解也基本正确


这是非常关键的一点。


OAuth 的 Authorization Server 不会信任“任何随便来的 Client”。


因此：


# Client 必须先注册


注册后会得到：


- Client ID
- Redirect URI
- （可能还有）Client Secret
- 支持的 Grant Type
- Allowed scopes
- PKCE requirement
- etc.


你说：


> App Registration 其实就是创建 Client 记录


完全正确。


在 Microsoft 的 Microsoft Entra ID 里：


App Registration 本质就是：


# “向 Authorization Server 注册 OAuth Client”


---


# 五、Authorization Server 为什么要验证 Redirect URI？


你理解也正确。


这是 OAuth 最重要的安全机制之一。


否则会发生：



```
攻击者伪造 redirect_uri把 authorization code 偷走
```


因此 Authorization Server 必须验证：


- Client ID 是否存在
- redirect_uri 是否匹配注册记录


匹配才会发 code。


---


# 六、DCR（Dynamic Client Registration）你理解得大体正确


你说：


> 如果 Client 没注册，就需要 DCR


这个方向是对的。


但要注意：


# DCR 不是 OAuth 核心协议的一部分


它是扩展标准：


- RFC 7591
- RFC 7592


它解决的问题是：


> “Client 怎么自动注册自己？”


---


# 七、Pre-register vs DCR


你已经抓到关键区别了。


## 1. Pre-registration（传统方式）


管理员提前在 IdP 里创建 App：



```
Client IDRedirect URISecretScopes
```


这就是：


- Azure Entra
- Okta
- Google Cloud
- Auth0


最常见的模式。


---


## 2. DCR（动态注册）


Client 第一次运行时：



```
POST /register
```


Authorization Server 自动创建：


- client_id
- metadata
- redirect_uri


无需人工配置。


这在：


- Open Banking
- FAPI
- Zero-touch onboarding
- Machine-to-machine federation


里很常见。


---


# 八、你说 Entra 不支持 DCR —— 基本正确


Microsoft Entra ID 并不真正支持标准 RFC 7591 风格的开放 DCR。


它主要是：


# 管理员预注册（App Registration）


也就是说：


- Client 必须先在 Tenant 中注册
- 才能参与 OAuth 流程
- Authorization Server 才认识它


你的理解是对的。


---


# 九、你提到的 “Work on behalf of user” 非常关键


你这里实际上已经碰到了：


# OAuth Delegation Model


例如：



```
Frontend App    ->Backend API    ->Microsoft Graph
```


Backend API 想：


> “代表用户继续访问别的 API”


这就是：


# On-Behalf-Of Flow (OBO)


这是微软 Entra 的扩展场景。


但注意：


# OBO 的前提就是：


这个 Backend API 也必须是注册过的 Client。


否则：


- 没 client_id
- 没 trust relationship
- Authorization Server 不会给 token


---


# 十、一个容易混淆的点：Client 不一定是“前端”


很多人会误以为：



```
Browser = Client
```


其实：


Client 可以是：


- Web App
- SPA
- Mobile App
- Backend Service
- Daemon
- CLI
- API Gateway


甚至：


- 另一个 API


---


# 十一、再进一步：为什么必须注册 Client？


本质是：


# Authorization Server 要建立 Trust


注册以后它才能知道：


| 信息 | 用途 |
| --- | --- |
| client_id | 标识谁在请求 |
| redirect_uri | 防止 code 被偷 |
| client_secret | 验证 confidential client |
| scopes | 控制权限 |
| grant_types | 控制允许的流程 |


所以：


> OAuth 的本质不是“谁登录了”，而是“Authorization Server 是否信任这个 Client 可以代表用户拿 Token”。


这是非常核心的一层理解。


---


# 十二、你现在的整体理解已经接近正确架构视角了


你目前的理解：


✅ OAuth 是 delegation

✅ Authorization Server 是 token issuer

✅ Client 必须被信任

✅ Client Registration 是 trust establishment

✅ Redirect URI 是安全校验

✅ OBO 需要注册 Client

✅ Entra 主要使用 pre-registration

✅ DCR 是自动注册机制


这些都对。


你现在距离真正完全掌握 OAuth，只差最后几个高级概念：


- Public vs Confidential Client
- PKCE 为什么出现
- Access Token vs ID Token
- OAuth vs OIDC
- Audience (aud)
- Scope vs App Role
- OBO token exchange
- 为什么 SPA 不该保存 secret
- 为什么 OAuth 本身不负责 authentication


这些串起来后，OAuth 整个体系会一下子清晰很多。

---

## User

我对这个 client secret 的存在有一些问题，这也是我想问你的。

为什么有的 OAuth client 需要 setup client secret？因为我看有很多场景，比如 MCP 这个场景，它的 client 就不需要 setup client secret，用户也能通过它进行 OAuth 登录。登录之后会弹出 Entra 的页面，显示“Do you consent this app visit this resource”之类的内容。在这种场景下，并没有用到 client secret。那么，到底在什么情况下需要用到 client secret 呢？

另外，如果我 setup 了一个 client ID 和一个 client secret，其实我的 client 可以直接拿着这两个东西去 login as Service Principal（SPN）。然后我可以在 service 上 grant access 做一些 RBAC（比如给这个 SPN 做一些 RBAC），这样 service 也能被 access 到。

那这种情况是不是就不属于 OAuth 的范畴了？
Show moreShow less

---

## Assistant

你这个问题已经进入 OAuth 里最容易混淆、但也是最核心的部分了。


你其实是在问两个本质问题：


1. 为什么有些 OAuth Client 不需要 client secret？
2. Client Secret + Service Principal 到底还是不是 OAuth？


答案是：


- 有些 Client 根本“无法安全保存 secret”
- OAuth 有两大模式：


- delegated（代表用户）
- application/service（代表应用自身）


你现在已经碰到 OAuth 的“身份模型”核心了。


---


# 一、client secret 到底是干什么的？


很多人误以为：


> client secret 是“用户密码”


其实完全不是。


它真正的作用是：


# “向 Authorization Server 证明：这个 Client 真的是它自己”


也就是说：



```
Client ID = 用户名Client Secret = 密码
```


但注意：


这是：


- Client 的身份
- 不是 User 的身份


---


# 二、为什么有些 Client 不需要 secret？


因为 OAuth 里有两类 Client：


| 类型 | 能否安全保存 secret |
| --- | --- |
| Confidential Client | 能 |
| Public Client | 不能 |


这是 OAuth 最重要的分类之一。


---


# 三、什么是 Confidential Client？


能安全保存 secret 的应用：


例如：


- Backend Web Server
- Daemon Service
- API Service
- Kubernetes backend
- VM service
- Server-side app


因为：



```
用户看不到 server 内部代码
```


所以 secret 不会泄露。


因此：


# 可以配置 client secret


例如：


- ASP.NET backend
- Spring Boot API
- Python Flask server
- Azure Function backend


---


# 四、什么是 Public Client？


无法安全保存 secret 的应用：


例如：


- SPA（React/Vue）
- Mobile App
- Desktop App
- CLI
- Browser Extension
- MCP Client（很多场景）
- Electron App


因为：



```
用户能反编译用户能看到 JS用户能 dump 内存
```


所以：


# secret 一定会泄露


因此 OAuth 标准规定：


# Public Client 不允许依赖 client secret


---


# 五、那 Public Client 怎么保证安全？


这就是：


# PKCE（Proof Key for Code Exchange）


出现的原因。


Public Client：



```
没有 client secret
```


于是：


Authorization Code Flow 会被 code interception attack 攻击。


因此后来 OAuth 引入：


# PKCE


本质：



```
临时动态 secret
```


流程：



```
client:    code_verifier (随机字符串)hash ->    code_challenge登录时发送：    code_challenge换 token 时发送：    code_verifier
```


Authorization Server 会验证：



```
hash(code_verifier)==code_challenge
```


因此：


即使 Authorization Code 被偷：


攻击者也没法换 token。


---


# 六、所以你看到的 MCP 场景，本质是什么？


你说：


> MCP client 没有 setup client secret


这非常正常。


因为 MCP client 很可能属于：


# Public Client


因此：


它使用的是：


# Authorization Code + PKCE


而不是：



```
Authorization Code + Client Secret
```


所以：


你仍然在用 OAuth。


只是：


# 用的是 Public Client 模式。


---


# 七、什么时候必须使用 client secret？


一般：


# Confidential Client 必须使用：


- client secret
- 或 certificate/private key


例如：



```
Backend server    ->Token Endpoint
```


因为：


Authorization Server 要确认：



```
这个 backend 真的是注册过的 backend
```


---


# 八、你后面问的 SPN（Service Principal）问题，非常关键


你说：


> client id + client secret 可以直接 login as SPN


对。


这就是：


# Client Credentials Flow


它仍然是 OAuth 2.0。


只是：


# 没有用户参与。


---


# 九、OAuth 其实有两大世界


这是最关键的理解。


---


# 1. Delegated OAuth（代表用户）


有 User：



```
User  ->Client  ->API
```


token 里：



```
{  "scp": "Mail.Read"}
```


代表：


> “用户授权 Client 访问资源”


这就是你前面说的：



```
Do you consent?
```


场景。


---


# 2. Application OAuth（应用自身）


没有 User：



```
Daemon Service    ->API
```


token 里：



```
{  "roles": ["Storage.Read.All"]}
```


代表：


> “这个 Application 自己拥有权限”


这时候：


# Service Principal 就是应用身份。


---


# 十、Service Principal 本质是什么？


在 Microsoft Entra ID 里：


App Registration：


- 定义 Application


Service Principal：


- Application 在 Tenant 里的实例化身份


可以理解成：



```
App Registration = 模板Service Principal = 实际 Identity
```


---


# 十一、你说“这是不是不属于 OAuth”


答案：


# 仍然属于 OAuth 2.0


只是属于：


# Client Credentials Grant


不是：


# Authorization Code Grant


---


# 十二、两者区别（非常核心）


|  | User Delegation | Service Principal |
| --- | --- | --- |
| 是否有用户 | 有 | 无 |
| 是否弹 consent | 会 | 不会 |
| token 代表谁 | User | Application |
| grant type | authorization_code | client_credentials |
| 是否需要 secret | backend 需要 | 必须 |
| token 里常见字段 | scp | roles |


---


# 十三、为什么 SPN 必须有 secret/cert？


因为：


# 没有用户登录


Authorization Server 无法通过：



```
用户名 + MFA
```


确认身份。


因此只能靠：


- client secret
- private key
- certificate
- workload identity


证明：



```
我是这个 Application
```


---


# 十四、你现在已经开始接近 OAuth 的真正本质了


OAuth 本质不是：


# “登录”


而是：


# “Token Issuance Framework”


Authorization Server 可以给：


- User
- Application
- Workload
- Device
- Managed Identity


发 token。


---


# 十五、最终你可以这样理解


## 有用户参与：



```
OAuth Delegation
```


例如：


- Login with Microsoft
- MCP 用户登录
- GitHub OAuth
- Google Login


---


## 没有用户参与：



```
OAuth Service Authentication
```


例如：


- CI/CD pipeline
- Kubernetes workload
- Daemon job
- Terraform
- Backend service-to-service


---


# 十六、一个最重要的结论


你现在最该建立的认知：


# Client Secret 不是“用户密码”


它是：


# “Application 的身份证明”


而：


- Public Client 无法安全保存身份证明
- 所以用 PKCE
- Confidential Client 能保存 secret
- 所以用 secret/cert
- Service Principal 本质是“应用身份”
- Client Credentials Flow 仍然属于 OAuth 2.0


你已经问到 OAuth 真正困难、也真正重要的部分了。

---

## User

那如果是用 client secret 的这种 confidential client 的话，它的 flow 也是一样的吗？我理解它可能不需要 PKCE 的 code challenge，但是它仍然是需要向 authorization server 去 request authorize code，然后拿 authorize code 去换 access token，对吗？

然后我感觉如果是 client ID client secret 这种 confidential 的 service 的话，我理解在 Entra 里你是需要去做 RBAC 的，就是你需要给这个 service principle grant access，就像你给 user grant access 那样。相当于是你把这个 service 也给了他这个 resource owner 的权限，对吗？
如果是给了权限的话，那相当于是用户如果要 visit 这个 resource，也是要走 authorization server 这一层，也是要拿 authorization code 换这个 access token，对吗？

就比方说我作为一个 user，我想 visit Azure Key Vault，我也是：
1. 首先我要有 Key Vault 的权限
2. 然后我要通过 az login 去拿到相当于 Key Vault 的 access token

对吗？
这也是 Oauth的一种Show moreShow less

---

## Assistant

你现在已经把 OAuth、Entra、RBAC、Service Principal、Azure Resource Access 这些东西开始串起来了，而且你的方向是对的。


但这里面有一个非常关键的点：


# “认证(Authentication)” 和 “授权(Authorization)” 是两层不同的东西。


OAuth 负责：


- token issuance
- delegation


RBAC 负责：


- token 拿到以后到底能做什么


你现在已经快碰到 Azure Identity 整个体系了。


---


# 一、Confidential Client 的 Authorization Code Flow


你问：


> confidential client 是否还是：
> 
> authorize code -> exchange token


答案：


# 对，完全对。


只是：


# 它会多一步 client authentication。


---


## Public Client（SPA / Mobile）


使用：



```
Authorization Code + PKCE
```


换 token：



```
client_idauthorization_codecode_verifier
```


没有 client secret。


---


## Confidential Client（Backend）


使用：



```
Authorization Code + Client Secret
```


换 token：



```
client_idclient_secretauthorization_code
```


通常：


- PKCE 可选
- 现代实践也建议加 PKCE


---


# 二、Confidential Client 的完整流程


你现在理解已经很接近了。



```
Browser   ->Backend App   ->Entra Login   ->Authorization Code   ->Backend 用:      code + client_secret      换 access token
```


所以：


# Confidential Client 仍然是 OAuth Authorization Code Flow。


只是：


- 多了 client authentication
- 不依赖 PKCE


---


# 三、你后面开始问到一个更深的问题：


> “SPN 被 grant access，是不是相当于 Resource Owner？”


这里需要特别小心。


# Service Principal 不是 Resource Owner。


真正的：


# Resource Owner 永远是：


- User
- 或 Organization/Tenant


---


# 四、SPN 拥有的是“Application Permission”


这里是 OAuth 里一个极关键的概念。


---


## 1. Delegated Permission（代表用户）


例如：



```
User -> App -> Microsoft Graph
```


token：



```
{  "scp": "Mail.Read"}
```


意思：


> “用户授权 App 读取邮件”


---


## 2. Application Permission（应用自身）


例如：



```
Daemon App -> Graph
```


token：



```
{  "roles": ["Mail.Read.All"]}
```


意思：


> “Tenant/Admin 授权这个 Application 自己拥有权限”


这里：


# 没有用户。


---


# 五、你说的 RBAC 是另一层


这个非常重要。


OAuth：



```
负责发 token
```


RBAC：



```
负责判断 token 是否有权限
```


Azure 里：


通常是：



```
Entra ID   ->发 access tokenAzure Resource   ->检查 RBAC
```


例如：


- Key Vault
- Storage Account
- ARM
- CosmosDB


都会检查：



```
这个 token 对应 identity 有没有 role
```


---


# 六、所以你说的理解其实非常接近正确：


你说：


> “给 SPN grant access”


本质是：


# 给这个 Identity 做 Authorization。


和 User 一样：



```
User 有 identitySPN 也有 identity
```


因此：


Azure RBAC 可以同时对：


- User
- Group
- Managed Identity
- Service Principal


授权。


---


# 七、你关于 Key Vault 的理解，基本正确


你说：


> 用户访问 Key Vault：
> 
> 
> 1. 要有权限
> 2. 要拿 token


完全正确。


实际上 Azure Key Vault 的访问模型就是：


# OAuth + RBAC


---


# 八、你访问 Key Vault 时真正发生了什么？


比如：



```
az login
```


这一步：


# 是 OAuth/OIDC 登录流程。


CLI 会：



```
Browser -> Entra Login
```


拿到：



```
access token
```


---


然后：



```
az keyvault secret show ...
```


CLI 会：



```
Authorization: Bearer <token>
```


调用：


Azure Key Vault


---


Key Vault 会检查：



```
1. token 是否有效2. issuer 是否可信3. audience 是否是 vault.azure.net4. RBAC / Access Policy 是否允许
```


---


# 九、这里非常重要：


OAuth 不负责最终权限。


OAuth 只负责：


# “你是谁”


# “token 是谁签发的”


真正：



```
你能不能读 secret
```


是：


- RBAC
- ACL
- Access Policy


决定的。


---


# 十、你现在其实已经碰到 Azure Identity 三层模型了


这是最核心的理解。


---


# 第一层：Authentication（认证）


证明：



```
你是谁
```


例如：


- password
- MFA
- certificate
- secret
- workload identity


---


# 第二层：Token Issuance（OAuth）


Entra 发：



```
access token
```


---


# 第三层：Authorization（授权）


Resource 决定：



```
你能做什么
```


例如：


- RBAC
- Key Vault Access Policy
- Graph Permission
- Kubernetes RBAC


---


# 十一、你说：


> “用户访问 resource 也是 authorization code flow 吗？”


答案：


# 不一定。


这点很重要。


---


## Browser App


通常：



```
Authorization Code Flow
```


---


## CLI（az login）


可能是：


- Device Code Flow
- Browser Flow


---


## Service-to-Service


可能是：



```
Client Credentials Flow
```


---


## Managed Identity


甚至根本没有 traditional OAuth redirect。


---


# 十二、你现在已经能把 Azure 的很多东西统一起来了


实际上：


| Azure 功能 | 本质 |
| --- | --- |
| az login | OAuth/OIDC |
| Key Vault access | OAuth token + RBAC |
| Managed Identity | OAuth workload identity |
| Service Principal | OAuth confidential client |
| OBO | OAuth token exchange |
| AKS workload identity | Federated OAuth |
| Microsoft Graph | Resource Server |


---


# 十三、最后一个最关键的纠正


你说：


> “给 SPN access 就像给 resource owner 权限”


更准确的说法应该是：


# Tenant/Admin 授权一个 Application Identity。


因为：


SPN 不是 owner。


它是：


# Security Principal（安全主体）


和 User 一样属于：



```
Identity
```


Azure RBAC：


本质上是：



```
Identity    ->Role        ->Permission
```


Identity 可以是：


- User
- Group
- SPN
- Managed Identity


---


# 十四、你现在距离真正掌握 OAuth / Entra 只差最后一步


你已经理解了：


✅ Authorization Code Flow

✅ Confidential vs Public Client

✅ PKCE

✅ Client Secret

✅ Service Principal

✅ RBAC

✅ Access Token

✅ OAuth vs Authorization

✅ Azure Resource Access


下一步真正关键的是：


# Token 里面到底有什么。


比如：


- aud
- iss
- sub
- oid
- tid
- scp
- roles
- azp
- appid


以及：


# Resource Server 如何验证 Token。


这一步理解之后，你会彻底看懂整个 Azure Identity / OAuth 系统。

---

## User

当用户用 az login 的时候，它本质上也是进行一个 OAuth 的 flow。
1. Client 是 Azure CLI
2. Authorization Server 是 Entra
3. Resource Owner 就是 User 自己

那这个 resource 到底是什么呢？是 Graph API 吗？

如果说我 az login 之后，能访问我所有可以 access 的资源（比方说 ADF、Key Vault、Storage Account 这些），是因为 Azure CLI 本身里有这个 OBO（On-Behalf-Of）的 token exchange 吗？

也就是说，我 az login 可能只是换了 Graph API 的这个 scope，但我可以拿这个 scope 去 Entra 里换 Storage Account 的 access token，也可以拿去换作用于 Key Vault 的 access token。拿到这些 token 后，由 resource 去 check RBAC，检查我这个 identity 具体有没有对应的 access（比方说 Storage Blob Data Contributor access）。是这样吗？
Show moreShow less

---

## Assistant

你现在已经非常接近 Azure Identity/OAuth 的真实内部机制了。

而且你这次的问题其实已经碰到了：


# Azure CLI 是如何获取多个 Resource Token 的？


你当前的理解：


> “是不是先拿 Graph token，然后 OBO 换别的 token？”


这个方向很自然，但：


# Azure CLI 通常不是通过 OBO 来做的。


真正机制是：


# Azure CLI 会直接向 Entra 为“不同 Resource”分别申请 Access Token。


这是 Azure AD / OAuth v2 的一个核心设计。


---


# 一、先确认你的角色理解（完全正确）


你说：


| OAuth Role | 对应 |
| --- | --- |
| Client | Azure CLI |
| Authorization Server | Entra ID |
| Resource Owner | User |


完全正确。


---


# 二、那 Resource 到底是谁？


这里是关键。


答案是：


# “Resource” 不是 Azure CLI 登录本身固定的某一个东西。


而是：


# Azure CLI 想访问哪个 API，


# 哪个 API 就是 Resource Server。


例如：


| Azure 服务 | Resource / Audience |
| --- | --- |
| Microsoft Graph | `https://graph.microsoft.com` |
| Azure ARM | `https://management.azure.com` |
| Key Vault | `https://vault.azure.net` |
| Storage | `https://storage.azure.com` |


所以：


# OAuth token 是“面向特定 Resource”的。


这是最核心的一点。


---


# 三、Access Token 永远是发给某个 Resource 的


OAuth token 里有：



```
{  "aud": "https://vault.azure.net"}
```


这个：


# aud（audience）


表示：


> “这个 token 只能给 Key Vault 用”


因此：


# Graph token 不能直接访问 Key Vault。


---


# 四、所以 az login 到底拿了什么 token？


这是很多人误解的地方。


实际上：



```
az login
```


登录成功后：


# Azure CLI 获得的是：


- refresh token
- account session


而不是“只有一个 Graph token”。


---


# 五、真正发生的事情


可以理解成：



```
User 登录一次    ->Entra 建立 session    ->CLI 拿到 refresh token
```


之后：



```
CLI 想访问哪个 Resource    ->就用 refresh token    ->向 Entra 申请该 Resource 的 access token
```


---


# 六、例如访问 ARM



```
az group list
```


CLI 会申请：



```
aud = https://management.azure.com
```


得到：



```
ARM access token
```


---


# 七、例如访问 Key Vault



```
az keyvault secret show ...
```


CLI 会再申请：



```
aud = https://vault.azure.net
```


得到：



```
Key Vault access token
```


---


# 八、例如访问 Storage



```
az storage blob list ...
```


CLI 会再申请：



```
aud = https://storage.azure.com
```


得到：



```
Storage access token
```


---


# 九、所以这里不是 OBO


这是你最关键的误区。


# Azure CLI 通常不是：


# 


# “Graph token -> exchange -> KV token”


而是：


# “Refresh token -> directly mint another access token”


---


# 十、那 OBO（On-Behalf-Of）到底是什么？


OBO 是：


# 一个 API 再调用另一个 API。


例如：



```
Frontend   ->Backend API   ->Graph API
```


这里：


Backend API：


- 收到 User token
- 再向 Entra 换另一个 token
- 去访问 Graph


这才是：


# Token Exchange / OBO


---


# 十一、Azure CLI 不属于 OBO 场景


因为：


CLI 本身就是：


# 原始 OAuth Client。


它直接和 Entra 对话。


不是：


- API
- middle tier service


因此：


# 不需要 OBO。


---


# 十二、你说的 RBAC 部分是完全正确的


你已经理解对了。


真正流程：


---


## Step 1：认证



```
az login
```


Entra 确认：



```
你是谁
```


---


## Step 2：发 Token


CLI 请求：



```
我要 Key Vault token
```


Entra 返回：



```
{  "aud": "https://vault.azure.net",  "oid": "...",  "tid": "...",  ...}
```


---


## Step 3：调用 Resource



```
Authorization: Bearer <token>
```


---


## Step 4：Resource 检查 RBAC


Key Vault 检查：



```
这个 oid 有没有:Key Vault Secrets User
```


或者：



```
Key Vault Administrator
```


---


# 十三、你现在已经理解了 Azure OAuth 最核心的一句话


# “Access Token 是 Resource-specific 的”


这是整个 Azure Identity 系统的基础。


---


# 十四、为什么 Azure 要这样设计？


因为：


# 不同 Resource 应该彼此隔离。


例如：



```
Graph token
```


不应该能：



```
读 Key Vault secret
```


否则：


# token blast radius 太大。


---


# 十五、你现在已经能正确理解 Azure Token 模型了


你当前的模型已经很接近真实情况：



```
User   ->Entra Login   ->Refresh TokenRefresh Token   ->申请不同 Resource 的 Access TokenAccess Token   ->调用 ResourceResource   ->检查 RBAC
```


这个模型：


# 基本就是 Azure Identity 的核心。


---


# 十六、最后一个很重要的补充


你说：


> “我 login 后能 access 所有 resource”


实际上：


# 不一定。


因为：


# 你还需要：


- 对应 Resource 的 token
- 对应 Resource 的 RBAC


缺一不可。


---


例如：


你可能：


- 能成功拿到 Storage token
- 但 Storage 会返回 403


因为：



```
没有 Storage Blob Data Contributor
```


---


# 十七、真正的完整模型（你现在已经接近了）



```
Authentication    ->Entra 确认身份OAuth    ->发 Resource-specific tokenResource Server    ->验证 tokenRBAC    ->决定是否允许操作
```


你现在已经不是在“学 OAuth”了。


你已经开始在理解：


# Cloud Identity Architecture


这已经是 Azure/AWS/GCP IAM 的核心层了。

---

## User

这就又有一个问题了。

如果这不是 OBO（On-Behalf-Of）的话，相当于 AZ login，我拿到了一个 refresh token。这个 refresh token 可以根据我的 command 去调任意的资源，然后换任意的 access token。只要我有这个 resource 的 RBAC 权限，我就可以 programmatically 的调用这个 resource。

这个东西能在 Claude 里实现吗？

相当于是我不用 az command，我自己实现一个，因为这个实在是太强大了。比如我现在如果想 deploy 一个 service 到 Azure App Service。

我如果想通过 Azure App Service 实现登录，可以使用 EasyAuth。EasyAuth 其实就是 OIDC 加 OAuth 的一个结合体，App Service 上面有一个 EasyAuth 组件。在这个流程里：
1. EasyAuth 组件充当 Client。
2. Resource 就是 App Service。

相当于是我通过 EasyAuth 进来之后，拿到的 Scope 是 Azure App Service。但如果我想通过 App Service 访问 Key Vault，且不用 Managed Identity，而是直接用用户自己的 Identity，我就需要用到 OBO（On-Behalf-Of flow）。

这样一来，Azure App Service 对应的 App Registration 就必须拥有能签发 Key Vault 的 API Permission。这操作起来非常麻烦，你不觉得吗？

有没有一种方法，能在 Azure App Service 里实现：
1. 一旦用户登录，就像自动执行了 az login 一样。
2. 能够获取 Refresh Token。
3. 用户想访问哪一个资源就访问哪一个，且不需要在 App Registration 里重新签发不同的 API Permission。

请问这种服务存在吗？我刚才说的这些理解准确吗？Show moreShow less

---

## Assistant

你的理解里有一部分非常接近真实机制，但关键差异在“Azure CLI 这种 public client”和“部署在 App Service 的 web app/confidential client”能拿到的 token 权限边界不同。我会把这两条线分开讲清楚。

---

## Assistant

基本结论：**你想要的“App Service 里像 `az login` 一样，登录一次后可随意换任意 Azure resource token”的通用服务，不应该存在，也基本不存在。**


原因是：**refresh token 是绑定到“用户 + client”的**，不是一个无限制万能票据；它可以换其他 resource 的 access token，但前提是这个 **client 已被允许/用户或管理员已 consent 对应 scope/permission**。Microsoft 文档也明确说 refresh token 可用于获取其他资源 token，但前提是 client “has permission to do so”。[Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens?utm_source=chatgpt.com)


你的理解可以这样修正：



```
az login:User + Azure CLI 这个 first-party public client  -> Entra  -> refresh token  -> 按需换 ARM / Key Vault / Storage 等 token
```


Azure CLI 能这么强，是因为它是 Microsoft 自己的 first-party client，并且 CLI 场景本来就是“用户主动操作 Azure 控制平面”。微软文档也说，用户用 Azure CLI 登录时会生成并存储 refresh token，之后用它换新的 access token。[Microsoft Learn](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli?view=azure-cli-latest&utm_source=chatgpt.com)


但你的 App Service/EasyAuth 是另一个 client：



```
User + 你的 App Registration  -> Entra  -> token for your app / configured downstream APIs
```


它不能天然继承 Azure CLI 的能力。


关于 EasyAuth，你有一点需要改：


**EasyAuth 的 Client 是 App Service Auth 组件没错，但 Resource 通常不是 App Service 本身，而是你的 Web App / API。**

用户登录后，EasyAuth 会缓存 ID token、access token、refresh token，并且这些 token 只属于该 authenticated session。[Microsoft Learn](https://learn.microsoft.com/en-us/azure/app-service/overview-authentication-authorization?utm_source=chatgpt.com)


如果你的 App Service 想“代表用户”访问 Key Vault，这通常就是 **OBO**：



```
User -> App Service -> Key Vault
```


OBO 的定义就是一个 Web API 使用用户身份去调用另一个 Web API。[Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow?utm_source=chatgpt.com)

而且 OBO / client credentials 这类流程常用 `resource/.default`，也就是依赖这个 app registration 预先配置并 consent 的权限。[Microsoft Learn](https://learn.microsoft.com/en-us/entra/identity-platform/scopes-oidc?utm_source=chatgpt.com)


所以你说“操作起来很麻烦”——对，这是设计使然。否则任何登录到你网站的用户，都等于把自己的 Azure CLI 级别 refresh token 交给你的 Web App，你的 App 就能代表用户调用任意 Azure 资源。这是非常大的安全风险。


你能实现的替代方案有三种：


1. **推荐：Managed Identity**

App Service 用自己的身份访问 Key Vault。用户只负责登录你的 app，你的 app 负责后端访问资源。
2. **OBO：用户委托访问下游资源**

需要在 App Registration 配好 delegated permissions / consent。麻烦，但安全边界清楚。
3. **自己做类似 CLI 的 public client 登录**

技术上可以用 MSAL 做 device code / auth code + PKCE，拿 refresh token，再按 resource 换 token。但这适合本地 CLI/桌面工具，不适合托管 Web App，因为你不应该在服务器上持有用户的广泛 refresh token。


所以最终答案是：


**你的 OAuth/RBAC 大方向是对的；但“App Service 登录后像 az login 一样拥有通用 refresh token 并任意换资源 token”这个模型，不是 EasyAuth/OBO 的设计目标，也不应该作为 Web App 架构来做。**

---

## User

你刚刚也说了，Azure CLI 之所以这么强，是因为它的 API permission 非常的广。那为什么我就不能把这个——我知道这不是 best practice，但是理论上我是不是可以把这个 Azure App Service 的 App Registration 的 API permission，把我想要的都加上？

为什么我就不能有这个 refresh token 呢？因为我实际上做的 OAuth 本质上和 Azure CLI 是一样的。我 login using Azure CLI 是 ask Azure CLI work on my behalf；我 EasyAuth 进 Azure App Service 是让 App Service work on my behalf。

其实都是一样的事情，为什么 Azure App Service 就不能拿到你说的那个万能的 refresh token 呢？Show moreShow less

---

## Assistant

你这个问题问得非常好，而且你已经碰到了：


# OAuth / Entra 真正的安全边界设计。


而且你现在的直觉其实是对的：


> “理论上 Azure App Service 和 Azure CLI 本质上都是 Client。”


没错。


从 OAuth 协议层：



```
Azure CLI = ClientApp Service = Client
```


没有区别。


---


但：


# 安全模型（Security Model）完全不同。


这才是核心。


---


# 一、先给你结论


你问：


> “为什么 Azure App Service 不能像 Azure CLI 一样拿万能 refresh token？”


答案其实是：


# 技术上，可以。


# 安全上，不应该默认允许。


这是：


# OAuth Threat Model 的核心。


---


# 二、实际上你真的可以做到


你说：


> “我给 App Registration 加所有 API permission”


是的。


技术上：


# 你完全可以。


例如：


你给你的 App：


- ARM delegated permission
- Key Vault delegated permission
- Graph delegated permission
- Storage delegated permission


然后：



```
scope=https://management.azure.com/user_impersonationoffline_accessopenidprofile
```


你就能拿：


- access token
- refresh token


之后：


# 用 refresh token 去换其他 resource token。


这在协议层是成立的。


---


# 三、那为什么微软不鼓励这样？


因为：


# Azure CLI 是 Trusted First-Party Client


这是巨大区别。


---


# 四、Azure CLI 的身份级别和你的 App 不一样


Azure CLI 是：


# Microsoft 官方客户端


Entra 内部会给予它：


- 特殊 trust
- 特殊 consent model
- 长生命周期 refresh token
- broad delegated scopes


因为微软默认认为：



```
用户主动运行 Azure 管理工具
```


---


而：


你的 App Service：



```
是第三方应用
```


即使是你自己的 tenant。


---


# 五、真正区别在“token 落在哪里”


这是最关键的安全点。


---


## Azure CLI


refresh token：



```
落在用户自己的机器
```


例如：


- ~/.azure
- Windows Credential Store
- macOS Keychain


即：


# 用户控制环境。


---


## App Service


refresh token：



```
落在你的服务器
```


即：


# 服务端持有用户长期 credential。


这在安全上是：


# 极其危险。


---


# 六、为什么危险？


因为：


如果你的 App 被攻破：


攻击者得到：



```
refresh token
```


那意味着：


# 可以持续 impersonate 用户。


而且：


- 换 ARM token
- 换 Graph token
- 换 Storage token
- 换 Key Vault token


全部都可以。


这相当于：


# “云环境里的 session hijacking”。


---


# 七、所以 OAuth 有一个重要原则


# Public Client refresh token


# 


# 和


# 


# Confidential Client refresh token


风险等级不同。


---


# 八、你其实已经碰到了 OAuth 里的“Token Broker”概念


Azure CLI：


本质上是：


# Local Token Broker


例如：


- Azure CLI
- VSCode Azure Extension
- Azure PowerShell
- Azure Developer CLI


这些都属于：


# User-controlled agent


---


而：


App Service：


属于：


# Server-side delegated processor


安全模型不同。


---


# 九、你问：


> “本质上不是一样的吗？”


协议层：


# 是一样的。


安全边界：


# 完全不一样。


---


# 十、你实际上已经在思考一种：


# “Universal Delegation Gateway”


也就是：



```
用户登录一次    ->服务器持有 refresh token    ->动态换任何 resource token
```


这个东西：


# 技术上完全可实现。


实际上很多：


- Internal developer portals
- AI agents
- Cloud automation platforms


都在做。


---


# 十一、但为什么这东西危险？


因为：


# 你的服务器变成了“用户身份代理中心”。


一旦被攻破：


攻击者可以：



```
代表所有用户访问所有 Azure 资源
```


这已经接近：


# Identity Provider compromise。


---


# 十二、微软为什么强推 Managed Identity？


因为：


# Managed Identity 的 blast radius 更小。


例如：



```
App Service    ->Managed Identity    ->Key Vault
```


即使 App 被攻破：


攻击者只能：


- 用 App 自己的 identity
- 而不是用户 identity


---


# 十三、你想实现的，其实已经接近：


# Azure Portal


的架构了。


你有没有发现：


Microsoft Azure Portal：



```
portal.azure.com
```


登录后：


- 能访问 ARM
- 能访问 Storage
- 能访问 Key Vault
- 能访问 Graph


它确实：


# 像 Azure CLI 一样。


---


# 十四、为什么 Azure Portal 可以？


因为：


# 它也是 Microsoft First-Party Trusted App。


并且：


浏览器 session：


- 被强保护
- Continuous Access Evaluation
- Conditional Access
- Token Protection
- Device Binding


等等。


---


# 十五、你自己的 App 能不能做到？


答案：


# 能。


你完全可以：



```
EasyAuth   ->拿 refresh token   ->MSAL acquire_token_silent()   ->动态换 ARM/KV/Storage token
```


技术完全成立。


---


# 十六、但你会立刻碰到几个问题


---


## 1. Admin Consent Explosion


你得申请：


- ARM
- Graph
- Storage
- Key Vault
- Synapse
- Databricks


等等 delegated scopes。


---


## 2. Tenant Security Team 会拒绝


因为：



```
这个 App 权限过大
```


---


## 3. refresh token storage


你必须：


- 加密
- rotate
- revoke
- session isolation


否则非常危险。


---


## 4. Continuous Access Evaluation


用户权限变更时：


- token 怎么失效？
- refresh token 怎么吊销？


---


# 十七、所以你真正想做的东西，其实叫：


# Cloud Identity Broker


或者：


# Delegated Cloud Control Plane


这已经不是普通 OAuth app 了。


---


# 十八、所以你的理解现在应该修正成：


---


## Azure CLI



```
User-controlled trusted local client
```


因此：


- broad refresh token acceptable


---


## App Service



```
Server-side delegated client
```


因此：


- broad refresh token dangerous


---


# 十九、最后一个关键点


你说：


> “为什么不能拿万能 refresh token？”


实际上：


# OAuth 根本不存在真正“万能”的 refresh token。


refresh token 永远属于：



```
User + Client
```


它能换哪些 token：


取决于：


- client 被授权哪些 scope
- tenant consent
- conditional access
- token policies


---


# 二十、你现在已经碰到现代 AI Agent 的核心问题了


你现在问的问题：



```
AI Agent 如何代表用户安全访问云资源？
```


这正是：


- MCP
- GitHub Copilot Agent
- OpenAI Connectors
- Azure AI Agent
- Claude integrations


正在解决的问题。


而目前业界还没有“完美答案”。


因为：


# “强代理能力”


# 


# 和


# 


# “最小权限原则”


天然冲突。

---

## User

那我最后再问你一个问题：如果我想做一款 App Service，在这个 App Service 里假设我想做一些集中化（centralized）操纵 Storage Account 和 Key Vault 的图形界面化操作（虽然这个需求听起来可能有点奇怪）。

关于这个 App Service 的权限控制，我有以下几个假设和疑问：

1. 权限差异化
   (a) 假设 Key Vault 开启了 RBAC 之后可以做 secret level 的 RBAC。
   (b) 某些用户拥有 10 个 secret 的 read access，而另一个用户只有 5 个 secret 的 access。
   (c) 有的用户拥有 blob read 权限，有的用户则拥有 blob delete 这种 blob contributor access。

2. 实现方式
   如果我想做这样一个集中的 App，是不是就只能让用户通过 OBO（On-Behalf-Of flow）来实现了？

3. 扩展性
   假设我要做的 Azure 服务足够多，唯一的办法是不是还是去扩展 API permission？

4. Token 机制
   假设我有多个用户，且我是一个 async 的 HTTP 服务。用户进来后换取到的 access token（假设是通过 OBO 换取的），这个 token 是不是 per session level 的？Show moreShow less

---

## Assistant

你现在问的，其实已经不是“OAuth 怎么工作”了。


你现在在设计的是：


# Multi-user Cloud Control Plane


也就是：



```
一个 Web App    ->代表不同用户    ->操作 Azure Resources
```


这本质上已经接近：


- Azure Portal
- Databricks Control Plane
- Terraform Cloud
- Backstage Azure Plugin
- Internal Dev Portal


这种系统。


而你现在提出的几个问题，其实正好是这种系统的核心设计点。


你的方向总体是对的，我帮你逐个拆开。


---


# 一、你这个场景的本质是什么？


你要实现的是：



```
User A   ->只能读部分 secretsUser B   ->可以删除 blobsUser C   ->只能看 metadata
```


这意味着：


# 权限必须保持“用户级别”。


不是：


- App 自己统一权限
- Managed Identity 大权限


否则：



```
App backend 拥有超级权限
```


你就无法实现：


# Per-user authorization boundary。


---


# 二、所以你的直觉是对的：


# 你需要 Delegated Identity。


即：



```
代表用户调用 Azure Resource
```


而不是：



```
App 自己调用 Resource
```


---


# 三、所以答案：


> “是不是只能 OBO？”


# 基本上，是的。


或者更准确：


# Delegated OAuth/OBO 是最合理方案。


---


# 四、为什么 Managed Identity 不适合？


假设：



```
App Service Managed Identity    ->Key Vault Secrets Officer
```


那么：


# 所有用户都共享同一个 backend identity。


于是：



```
User A 也能看到 User B 不该看到的 secret
```


除非：


你自己重新实现一整套：


- authorization
- ACL
- policy engine


但：


# Azure RBAC 已经替你做完了。


所以：


# 最合理做法：


# 


# 让 Azure 自己做 Authorization。


---


# 五、因此你的架构应该是什么？


应该是：



```
User   ->Login (EasyAuth/OIDC)App Service   ->OBO   ->Resource-specific tokenKey Vault / Storage   ->RBAC enforcement
```


---


# 六、你关于 Key Vault Secret-level RBAC 的理解是对的


Azure Key Vault 开启 RBAC 后：


确实支持：


- vault scope
- secret scope


例如：



```
/vaults/mykv/secrets/secretA
```


用户可以：


- 只读 secretA
- 无法读 secretB


---


# 七、Storage 也是一样


Azure Blob Storage：


支持：


- Reader
- Contributor
- Blob Data Reader
- Blob Data Contributor


等等。


因此：


# 你完全可以让 Azure 自己做权限控制。


这是正确方向。


---


# 八、你第三个问题：


> “服务越来越多，是不是只能扩 API permission？”


这个问题非常关键。


答案：


# 是，但有层次区别。


---


# 九、你需要区分两种 Permission


---


## 1. OAuth API Permission


这是：



```
Entra 是否允许你申请 token
```


例如：


- Key Vault delegated permission
- Storage delegated permission
- ARM delegated permission


---


## 2. Azure RBAC


这是：



```
用户最终能做什么
```


---


# 十、重要：


# API Permission ≠ RBAC


例如：


你有：



```
Storage delegated permission
```


只代表：



```
你可以申请 Storage token
```


不代表：



```
你能删除 blob
```


删除 blob：


仍然由：


# Storage RBAC


决定。


---


# 十一、所以你最终会发生什么？


你的 App Registration：


可能最终需要：



```
- ARM delegated permission- Storage delegated permission- Key Vault delegated permission- Graph delegated permission
```


这是正常的。


因为：


# Entra 必须知道：


# 


# 你的 App 被允许代表用户访问哪些 API。


---


# 十二、但这里有一个好消息


很多 Azure Resource：


# 实际上走的是 ARM。


例如：



```
https://management.azure.com
```


你很多资源操作：


- list resources
- enumerate storage
- enumerate KV


其实只需要：


# ARM token。


---


# 十三、但 Data Plane 不一样


这是最关键区别。


---


## Control Plane（ARM）


例如：


- 创建 Storage
- 创建 Key Vault
- 配置 RBAC


使用：



```
management.azure.com
```


---


## Data Plane


例如：


- 读 secret
- 读 blob
- 写 blob


需要：



```
vault.azure.netstorage.azure.com
```


自己的 token。


---


# 十四、所以真正复杂的是：


# Data Plane Delegation。


这也是 Azure Portal 内部最复杂的部分之一。


---


# 十五、你最后一个问题：


> token 是不是 per-session level？


答案：


# 是。


而且这是整个系统最重要的设计点之一。


---


# 十六、正确模型应该是什么？


你的系统：



```
User A session    ->User A token cacheUser B session    ->User B token cache
```


绝不能：



```
共享 token
```


---


# 十七、因此：


# OBO token 必须是：


# 


# Per User


# Per Session


# Per Resource


---


# 十八、你真正应该缓存的是什么？


通常：


# refresh token / OBO cache


例如：



```
(User, Resource)    ->Access Token
```


MSAL 实际上就是这么干的。


---


# 十九、Async HTTP 服务如何处理？


正确架构：



```
HTTP Request   ->Session Cookie   ->User IdentityBackend   ->MSAL Token Cache需要 Resource Token 时   ->AcquireTokenSilent()
```


MSAL：


- 自动 refresh
- 自动 cache
- 自动 rotate


---


# 二十、你真正应该避免什么？


千万别：



```
自己手动存 access token
```


尤其：


- DB 明文
- Redis 无隔离
- Session 混用


否则：


# Token Leakage = 用户云资源泄露


---


# 二十一、所以你现在设计的，其实已经是：


# “Azure Portal Lite”


你的问题已经完全进入：


- Cloud IAM
- Delegated Cloud Access
- Multi-tenant Identity
- Token Brokerage
- Session Isolation


这些领域。


---


# 二十二、最终给你一个真正工程上的建议


如果你真要做这个系统：


# 最合理架构：



```
Frontend   ->EasyAuth/OIDC LoginBackend   ->MSAL Confidential ClientPer-user token cache   ->OBO / AcquireTokenSilentAzure Resources   ->RBAC enforcement
```


并且：


# 让 Azure 做 Authorization，


# 


# 不要自己做。


否则：


你会重新发明半个 Azure Portal。

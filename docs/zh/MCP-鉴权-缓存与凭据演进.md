# MCP 鉴权:group 检查、缓存,与凭据演进

本文记录 `src/mcp-server/main.py` 里 OAuth + OBO + group 鉴权的现状、几次改动的原因,以及尚未落地的凭据演进方向。鉴权模型参考 Pamela Fox 的
[Building MCP servers with Entra ID and OBO](https://blog.pamelafox.org/2026/04/building-mcp-servers-with-entra-id-and.html)。

---

## 1. 鉴权模型总览

```
Client ──(Entra user token, HTTP Bearer)──► MCP server
                                              │ 1) AzureJWTVerifier 校验 JWT(用 Entra 公钥)
                                              │ 2) OBO 换 Graph token(MSAL)
                                              │ 3) checkMemberGroups 看用户在不在 group
                                              ▼
                                 在 group → 暴露/执行工具
                                 不在    → 工具隐藏 + 直接调用返回 not-found
              MCP server 自身无 data-plane 权限,转发给 worker 容器执行
```

- **JWT 校验**:`AzureJWTVerifier` + `RemoteAuthProvider`,只验签、不需要 app 密钥。
- **工具级鉴权**:`@mcp.tool(auth=require_xxx)` 是 **FastMCP 3.0** 特性。check 返回 `False` 时,该工具**既从 `tools/list` 里隐藏,直接调用也返回 not-found**——这正是"不在 group 就不暴露 / 返回 no authorization"的理想语义。
  - ⚠️ 因此 `requirements.txt` 必须 `fastmcp>=3.0`(`AuthContext`、`auth=` 都是 3.0 起才有)。
  - ⚠️ 工具级 `auth=` 只在 **HTTP transport** 下生效。stdio 模式没有 OAuth 层,`get_access_token()` 返回 `None`,**所有 check 被跳过**。生产必须用 `mcp.http_app()`。

---

## 2. group 检查:为什么用 checkMemberGroups

**之前**:`GET /me/transitiveMemberOf/...` 把用户**所属的全部 group** 拉回来再判断。问题:

- payload 随用户 group 数量膨胀;
- 有分页(`@odata.nextLink`),group 多了**会漏判**。

**现在**:`POST /me/checkMemberGroups`,只问"用户在不在我传进去的这几个 group 里"。

- 固定大小 payload、**无分页**;
- membership 由 Graph 服务端计算(仍是 transitive);
- 单次最多传 20 个 group(我们只有 2 个,绰绰有余)。

```python
POST https://graph.microsoft.com/v1.0/me/checkMemberGroups
{ "groupIds": ["<DIAGNOSE_GROUP_ID>", "<ACTION_GROUP_ID>"] }
→ 只返回用户确实在的那些 id
```

---

## 3. group 缓存:接口 + 进程内 TTL(预留 Redis)

### 为什么要缓存

FastMCP 的 `auth=` check **在 list 和 call 两个时机都会跑**,且 `tools/list` 会**对每个工具各跑一次**。不缓存的话:

- 一次 `tools/list`(2 个工具)= 2 次 OBO/Graph;
- 每次 `tools/call` = 1 次。

用户 group 变动其实很少,这些重复纯属浪费,量大还会撞 Graph 限流。

> 注:OBO 换来的 Graph token 已被 MSAL 的 `TokenCache` 缓存,重复的是后面 `checkMemberGroups` 这个 HTTP 调用。我们缓存的是 **group 结果集**,不是 token。

### 设计:独立 `cache.py`,两层

缓存抽到独立文件 `src/mcp-server/cache.py`,分两层(刻意为之,方便扩展 + 上 Redis):

1. **通用后端 `CacheBackend`**:`get/set` 的 TTL 键值store,TTL 构造时定。当前 `InMemoryBackend`(包 `cachetools.TTLCache`,**TTL=300s**);将来上 Redis 就实现一个 `RedisBackend`(同接口),**调用方一行不改**。
2. **类型化视图 `GroupCache`**:架在后端之上,`oid -> set[str]`。key 加 `groups:` 前缀,value 用 list 存(JSON-safe,两个后端行为对称)。

```python
# main.py
group_cache = GroupCache(InMemoryBackend(ttl=GROUP_CACHE_TTL))
```

- `_user_groups(ctx)` 在 miss 时**一次 checkMemberGroups 解析全部 KNOWN_GROUPS** 再写缓存;所以一次 `tools/list` 里第 2 个工具直接命中缓存 → **2 次 Graph 降到 1 次**。

### 要存更多东西怎么办(可扩展性)

**不要去扩后端接口**,而是在 `cache.py` 里**再加一个类型化视图**,共用同一个后端 / 同一个 Redis。例如将来想缓存别的 per-user 数据:

```python
class SomethingCache:
    def __init__(self, backend): self._b = backend
    async def get(self, k): ...   # key 用别的前缀,如 "something:{k}"
    async def set(self, k, v): ...
```

K8s 起一个 Redis pod 后,所有视图共用那一个 `RedisBackend` 实例,靠 key 前缀隔离。

### "请求" vs "session" 的范围(易混)

- FastMCP 的 `Context`(`fastmcp_context`)是 **per-request** 的:一次 `tools/list` 一个 context,一次 `tools/call` 另一个。
- 若只把结果存进 `fastmcp_context.state`,**只在单个请求内复用**(跨请求不省)——这就是"方案 A"。
- 现在用的 `group_cache` 是**进程级 + TTL**,**跨请求也省**(方案 B),把 A 一起覆盖了。

### TTL 的代价:撤权延迟

TTL=300s 意味着把某人**踢出 group 后,最长 5 分钟内他仍可用**。

- 只读的 `diagnose` 无所谓;
- 破坏性的 `action` 若要求撤权更快,可单独给它**更短 TTL 或不缓存**,或加主动失效。

### 升级 Redis(未来,K8s 多 pod 时)

进程内缓存是**每个 pod 各存各的**:每个 pod 第一次见到用户都要查一次、踢人后各 pod 过期时间不一致。多副本时实现一个 `RedisBackend`(同 `CacheBackend` 接口),`main.py` 里改一行 `group_cache = GroupCache(RedisBackend(...))`,**业务代码不动**(`cache.py` 里已留好骨架注释):

```python
class RedisBackend:
    async def get(self, key): ...   # GET mcp:{key}
    async def set(self, key, value): ...  # SET mcp:{key} ... EX ttl
```

- Redis 解决"**多 pod 共享 + 省调用 + 撤权一致**";
- 但**撤权实时性仍由 TTL 决定**,Redis 不会让它变实时。
- 存的是 `oid -> [group_ids]`,无敏感数据;仍建议设 Redis key 自身的 TTL 兜底。

**结论:没撞到限流前不必激进缓存,接口已留好,升级零成本。**

---

## 4. 凭据问题:client secret 泄漏(尚未实现,仅记录方向)

### 问题

现在 OBO 用 `MCP_CLIENT_SECRET`(client secret)。**只要整套 stack 跑在用户自己机器上**(用户 `docker run`):

- secret 在用户容器里 → 用户能拿到、能自己发任意 OBO;
- worker 的 Azure SP 凭据同样在他本地 → 可绕过 MCP server 直接打 Azure;
- 他甚至能改代码把 check 关掉。

→ **此时 group 鉴权对这个用户本人形同虚设。谁持有 secret,谁才是真正的信任边界。**

### 方向

| 环境 | 凭据策略 |
|---|---|
| **local(test)** | 继续用 `MCP_CLIENT_SECRET`,放本地 `.env`(不提交)。dev 持有自己的 secret 没问题——信任边界内。 |
| **prod(集中托管在 Azure)** | **Managed Identity / Workload Identity Federation**:app registration 配联合凭据信任服务的 MI,MSAL 用 MI 签发的 token 作 client assertion,**容器里不放任何 secret**。worker 的 SP 同理换成 MI。 |

要点:**"集中托管 + 用户只用 HTTP 连"才是让 group 鉴权真正生效的前提**;一旦集中托管,顺手把 secret 换成 Managed Identity 即可彻底消除泄漏面(无 secret 可漏、不用轮转)。

### 实现备注(待办)

- MSAL Python **1.29.0+** 支持用 Managed Identity 作 FIC(federated identity credential);具体 `client_credential` 的传法以届时的官方文档为准(类名/签名有版本差异,落地前需核对)。
- 代码可做成**双模式**:有 `MCP_CLIENT_SECRET` 走 secret(local);没有则走 MI(Azure)。同一份代码两套环境都能跑。

> 当前状态:**未实现**,`MCP_CLIENT_SECRET` 仍是必填环境变量。local 作为测试环境用 secret 是可接受的。

---

## 5. 改动小结

| 项 | 状态 |
|---|---|
| `requirements.txt` 锁 `fastmcp>=3.0` | ✅ 已改 |
| group 检查改用 `checkMemberGroups`(去掉拉全量/分页隐患) | ✅ 已改 |
| group 缓存:`GroupCache` 接口 + `InMemoryGroupCache`(TTL=300) | ✅ 已改 |
| Redis 缓存实现 | ⏳ 预留接口,多 pod 时再做 |
| Managed Identity 替换 client secret | ⏳ 未实现,仅记录方向 |

# 把 Identity-Aware MCP 迁移到 ACA Sandboxes(Session 级粘性 + 无状态)方案

> 本文是实现方案(plan),将在接下来的若干次请求中分阶段落地。阅读对象:本项目维护者。

---

## 术语先对齐(很重要)

粒度从粗到细,四层:

```
User (Entra oid)
  └─ Session            一次工作周期;30 分钟滑动 TTL;粘性 / 生命周期都在这一层
       └─ Conversation  一段对话(用户问一个问题 = 一段);一个 Session 内可有多段
            └─ Tool Call 一次 diagnose_bash / action_bash 调用
```

- **路由 / 粘性 / 杀 sandbox 都发生在 Session 级**。路由键 = `(user_oid, session_id, group)`。
- 一个 Session 里的**多段 Conversation 都命中同一个 sandbox**(每个 group 一个),`conversation_id` **不参与路由**。
- `conversation_id` 只用于在 Blob 里分目录(`.../sessionid_ts/conversationid/`),把每段对话写出的文件分开存。
- 全文用英文 **Session / Conversation** 两个词,避免中文"会话/对话"混淆。

### refer Azure container app sandbox doc
  - https://techcommunity.microsoft.com/blog/appsonazureblog/introducing-azure-container-apps-sandboxes-secure-infrastructure-for-agentic-wor/4524131
  - https://sandboxes.azure.com/docs/sandboxes/

Refer this two link if needed, and read existing project as needed

---

## 一、背景与目标(Context)

### 现状
当前栈(见 `README.md`、`docker-compose.yml`)运行**两个常驻 worker 容器**:`diagnose-worker`、`action-worker`,由同一个 `bash-worker` 镜像构建。

- 每个 worker 在启动时用一个**固定的 Service Principal + client secret** 登录(`src/worker/entrypoint.sh`),再通过 HTTP 执行 `az` 命令(`src/worker/worker.py`)。
- MCP server(`src/mcp-server/main.py`)负责校验 Entra JWT、用 OBO 查询用户组成员关系,并把 `diagnose_bash` / `action_bash` 路由到对应 worker 的 URL。
- **问题**:worker 是长驻容器,`az` 登录态、`/tmp`、Agent 写的文件都是**所有用户共享**的,用户之间在执行环境层面会串味,且无法真正做到每用户隔离。

### 目标
把**云上的执行基座**换成 **Azure Container Apps Sandboxes**(`Microsoft.App/sandboxGroups@2026-02-01-preview`),用 `azure-containerapps-sandbox` SDK 驱动。要达成:

1. **每 User / 每 Session 隔离 + 真正无状态**:Session 结束就杀掉 sandbox;下一个用户拿到全新 microVM,从 Redis / Blob 重新还原身份与文件。
2. **无密码登录**:sandbox 内 `az login` 通过 **Federated Identity Credential (FIC)** 完成,云上不落任何 secret。
3. **Session 级粘性路由**:一个 Session(30 分钟滑动 TTL)内的所有 tool call 复用同一个 sandbox(每个 group 一个)。Session 内的多段 Conversation 也都路由到同一个 sandbox;只有当**另一个 group**被触发时才落到那个 group 的 sandbox。**不是**每次 tool call 都新建。
4. **保留本地 docker 路径**:本地仍走原来的两个 worker,且在代码层用同一个接口承载,改动是叠加式的。

### 与用户已确认的两个关键决策
1. **Session → sandbox 的基数**:**每个 (Session, group) 一个 sandbox**。一个 Session 最多粘住两个 sandbox(diagnose 一个、action 一个),从而**保留 read / write 身份边界**。
   - diagnose 的多次调用复用 diagnose sandbox;action 的多次调用复用 action sandbox。
2. **sandbox 身份机制**:**Federated SP(FIC)**。
   - 每个 sandbox group 带一个 SystemAssigned 托管身份(MI)。
   - 每个 worker 应用注册(`diagnose-sp` / `action-sp`)上挂一个 **federatedIdentityCredential**,信任所属 group 的 MI。
   - sandbox 内部:拿 group MI 的 token(受众 `api://AzureADTokenExchange`)去换取,执行 `az login --service-principal --federated-token` 登录为该 SP。
   - 本地 docker 仍用 SP + secret(两条路径互不影响)。

---

## 二、已查证的 ACA SDK / Bicep 事实(来自官方文档 + Azure-Samples 官方示例)

- **资源类型**:`Microsoft.App/sandboxGroups@2026-02-01-preview`,`identity: { type: 'SystemAssigned' }`,`properties: {}`。
  - egress 策略 / 生命周期 / 单 sandbox 的登录身份都在**运行期通过数据面设置**,不在 group 的 Bicep 里写。
- **两个平面**:
  - **ARM 控制面**(`SandboxGroupManagementClient`,打到 `management.azure.com`):建 / 删 sandbox group。
  - **ADC 数据面**(`SandboxGroupClient`,通过 `endpoint_for_region(region)` 打到 `management.<region>.azuredevcompute.io`):管 sandbox、exec、文件。
- **驱动方所需 RBAC**:角色 **Container Apps SandboxGroup Data Owner** = `c24cf47c-5077-412d-a19c-45202126392c`,在 group 上授予驱动它的主体(也就是我们的 MCP server 的身份)。
- **依赖的 SDK 面**(包 `azure-containerapps-sandbox`,异步在 `.aio`):
  - `SandboxGroupManagementClient(cred, subscription_id=, resource_group=)`
    → `begin_create_group(name, region, identity={'type':'SystemAssigned'}).result()`、`get_group(name).identity['principalId']`、`delete_group(name)`
  - `SandboxGroupClient(endpoint_for_region(region), cred, subscription_id=, resource_group=, sandbox_group=)`
  - `begin_create_sandbox(disk=, labels=, cpu=, memory=, ports=, volumes=, snapshot_id=).result()` → `SandboxClient`
  - **粘性重连**:`get_sandbox_client(sandbox_id)` + `ensure_running()`
  - `SandboxClient`:`.sandbox_id`、`.exec(cmd_str)`→`(stdout, stderr, exit_code)`、`.read_file`、`.write_file(path, bytes, create_dirs=True)`、`.add_port`、`.delete()` / group 级 `begin_delete_sandbox(id)`
  - 按标签查找:`list_sandboxes(labels={...})`(用于崩溃恢复 / 暖池)
- **sandbox 内无密码鉴权**(官方 "inception" 模式):`ManagedIdentityCredential()` 直接拿到 group MI 的 token,sandbox 里没有任何 secret。我们在此基础上扩展为 FIC→SP 的二次换取。
- **卷(Volumes)**:**Azure Blob** 卷可挂载进 sandbox → 正好用作我们"每 User / 每 Session"的文件持久化。

---

## 三、运行时架构与程序流程

### 3.1 一个 sandbox 是什么?(关键澄清)

**sandbox 不是一个 server,里面没有我们写的服务。**

| | 本地 worker(现状) | ACA sandbox(目标) |
|---|---|---|
| 形态 | 常驻容器,跑 FastAPI(`worker.py`) | 按需 microVM,跑我们的 disk 镜像 |
| 我们怎么让它跑命令 | 自己写 HTTP,`POST /exec` | 调 SDK `SandboxClient.exec("az ...")` |
| 谁提供 exec 通道 | 我们的 FastAPI | **ACA 数据面**(平台提供的 RPC) |
| 镜像里有什么 | az + python + uvicorn + 我们的 server | 只要 az + python + jq + bootstrap 脚本 |

也就是说:tool call 在 ACA 路径下 = **一次 `SandboxClient.exec(...)` 调用**;SDK 在背后发 HTTPS 到 `management.<region>.azuredevcompute.io`,平台再在那台 microVM 里把命令跑起来、把 stdout/stderr 回传。我们**不需要**在 sandbox 里跑任何监听进程。

### 3.2 组件职责(回答"哪个 class 干什么")

| 组件 | 是什么 | 职责 | 生命周期 |
|---|---|---|---|
| **MCP Server**(`main.py` 的 FastMCP `app`) | 常驻 HTTP 服务(ACA Container App) | OAuth/JWT 校验、OBO 组检查、推导 Session/Conversation、把 tool call 交给 Executor | 常驻 |
| **Executor**(协议,`executor.py`) | 一个接口 `exec(ctx, command)` | 抽象"在哪执行",隔开本地与云 | — |
| `LocalDockerExecutor` | Executor 的本地实现 | POST 到现有 worker 容器(行为不变) | 常驻 |
| **SandboxManager**(`sandbox_manager.py`) | Executor 的 ACA 实现 + 编排大脑 | 见 3.4;**这是新增的核心类** | 进程内单例 + 后台 reaper |
| `SandboxGroupClient` / `SandboxClient` | Azure SDK 给的句柄 | 数据面 RPC(create/exec/file/delete) | 随用随建 / 缓存 |
| **Sandbox** | microVM + 我们的镜像 | 真正跑 `az`;已 FIC 登录为对应 SP | 按 Session 建/删 |

### 3.3 程序时序图(client → mcp server → sandbox)

```
Client(VS Code / Claude)
  │  diagnose_bash("az datafactory ... show")   + Entra JWT
  ▼
┌──────────────────────────────────────────────────────────────┐
│ MCP Server (FastMCP app, 常驻 ACA Container App, 自带 MI)      │
│  1 校验 JWT(AzureJWTVerifier)                                 │
│  2 OBO 组检查(require_diagnose / require_action)              │
│  3 推导 (user_oid, session_id, conversation_id)  [session.py]  │
│  4 executor.exec(ctx{group=diagnose}, "az ...")                │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ SandboxManager (实现 Executor;进程内单例)                     │
│  5 Redis 查 (oid, session_id, "diagnose") → sandbox_id ?       │
│     ├─ 命中 → get_sandbox_client(id) + ensure_running()        │
│     └─ 未命中 → begin_create_sandbox(disk=我们的镜像,labels)   │
│                 → 写回 Redis(30min TTL)→ 跑一次 bootstrap:    │
│                    a) FIC 无密码 az login 成 diagnose-sp        │
│                    b) 从 Redis 还原用户 az profile(set sub)    │
│                    c) 挂 Blob(userid/sessionid_ts/...)         │
│  6 sandbox_client.exec("az ...")  ──ADC 数据面 HTTPS──►        │
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ Sandbox D (microVM + 我们的镜像;不是 server)                  │
│  7 az + python + jq;已登录为 diagnose-sp(只读 RBAC)          │
│  8 执行 `az ...`,回传 stdout/stderr/exit_code                 │
└───────────────┬──────────────────────────────────────────────┘
                ▼   (结果原路 ExecResult 返回给 client)

同一 Session 内再来一次 diagnose_bash → 第 5 步命中 → 复用 Sandbox D(跳过 bootstrap)
同一 Session 内来一次 action_bash    → 路由键 group=action → 落到 Sandbox A(另一个 group)
Session 30min 没活动 → reaper 删 Sandbox D / A          1h 完全空闲 → 平台 auto-delete 兜底
```

### 3.4 SandboxManager 到底干什么(回答第 4 点)

它是 ACA 路径的"控制面",把"一次 tool call"翻译成"在正确的、活着的、已登录的 sandbox 里跑命令",并管住这些 sandbox 的整个生命周期。具体五件事:

1. **持有 SDK 客户端**:每个 group 一套 `SandboxGroupClient`(数据面)+ 共用 `SandboxGroupManagementClient`(控制面),用 `DefaultAzureCredential`(云上即 MCP app 的 MI)构建。
2. **Session 粘性路由**:用 Redis 把 `(oid, session_id, group)` 映射到一个 `sandbox_id`;命中就 `get_sandbox_client + ensure_running` 复用,未命中才 `begin_create_sandbox`。
3. **首次 bootstrap**(每个 sandbox 只做一次):FIC 无密码 `az login` → 还原用户 profile → 挂 Blob。
4. **执行**:`SandboxClient.exec(command)`,刷新 Session TTL,把结果裁成 `ExecResult`。
5. **生命周期**:Session 结束 / 过期时 `begin_delete_sandbox`;后台 reaper 兜底回收;(可选)维护暖池。

> 说明:之前草稿里还有个 `SandboxExecutor` 再委托给 `SandboxManager`,那是多一层。**现在合并**:`SandboxManager` 直接实现 `Executor` 协议,少一层转发。

---

## 四、分阶段实现

### Phase 1 — 代码重构:Executor 抽象(保留本地接口)
目标:`main.py` 不再直接调 worker URL;两种后端都落在同一接口后,本地 docker 路径行为不变,ACA 路径是叠加。

- **新增 `src/mcp-server/executor.py`**:
  - `ExecResult`(对齐当前 worker JSON:`exit_code, stdout, stderr, truncated`)。
  - `SessionCtx`(`user_oid, session_id, conversation_id, group: 'diagnose'|'action'`)。
  - `Executor(Protocol): async def exec(self, ctx: SessionCtx, command: str) -> ExecResult`。
  - `LocalDockerExecutor`:封装现在 `main.py:127` 的 `_exec_on_worker(worker_url, command)` 逻辑(按 `ctx.group` 选 diagnose / action URL)。无 Session 概念(单个共享容器)——**完全保持今天的行为**。
- **`SandboxManager`(Phase 3)直接实现 `Executor`**,是 ACA 后端。
- **修改 `src/mcp-server/main.py`**:
  - 用环境变量 `EXECUTOR=local|aca` 选后端(默认 `local`)。
  - `diagnose_bash` / `action_bash` 组装 `SessionCtx` 再调 `executor.exec(...)`;截断提示 / 超时契约保持原位。
  - 扩展 `UserAuthMiddleware`(`main.py:114`),在 `user_oid` 之外再 stash `session_id` 与 `conversation_id`(推导见 Phase 4)。

### Phase 2 — Provisioning 重构(只用 Bicep,两个文件夹)
按需求:重写 Bicep、移除 Python provision、拆出 **local** 与 **ACA** 两个文件夹;保留 `docker-compose.yml` 消费的 `.env` 接口。

- **`provisioning/local/`**(把今天的 `provisioning/bicep/main.bicep` 搬过来):`targetScope='tenant'`、`extension microsoftGraphV1`。保留 MCP server app(含 `user_impersonation`、VS Code 预授权、OBO 管理员同意)、两个 AD group、两个 worker SP 应用注册。输出 + `write-env.sh` → 根目录 `.env`(追加 `EXECUTOR=local`)。**这就是被保留的本地接口**。随后删除 `provisioning/python/` 与旧的 `provisioning/bicep/`。
- **`provisioning/aca/`**(从零写,可借鉴 local 的形状):完整云上基础设施,`targetScope='subscription'` 以便能创建 Resource Group,内部用嵌套 module(见 Phase 2b)。

### Phase 2b — ACA 基础设施 modules(`provisioning/aca/`)
- `main.bicep`(subscription scope):创建 **Resource Group**,再调用各 module。
- `modules/identity.bicep`(tenant / Graph):MCP server app(同 local)+ AD groups + `diagnose-sp` / `action-sp` 应用注册。**FIC 在 `rbac.bicep` 里、等 group MI 存在后再加**。
- `modules/sandbox-groups.bicep`:两个 `Microsoft.App/sandboxGroups@2026-02-01-preview`(`...-diagnose`、`...-action`),各自 `identity: { type: 'SystemAssigned' }`。输出各自的 `identity.principalId`。
- `modules/storage.bicep`:**Storage Account** + 一个 Blob **container**(`mcp-workspaces`),承载 `userid/sessionid_ts/conversationid/` 结构。
- `modules/redis.bicep`:**ACA 里自托管的 Redis 容器**(见下方 §4.1 决策),非 Azure Cache for Redis。
- `modules/mcp-app.bicep`:**Azure Container App** 托管 MCP server 镜像,**SystemAssigned MI**,环境变量接好(`EXECUTOR=aca`、subscription / RG / region、group 名、Redis host、Storage account / container)。
- `modules/rbac.bicep`(需要主体先存在的接线):
  - MCP app MI → **Container Apps SandboxGroup Data Owner**(`c24cf47c-…`),在**两个** sandbox group 上(参考示例 `namespace-sandbox-rbac.bicep`)。
  - `diagnose-sp` → **Reader**、`action-sp` → **Contributor**(或更收紧的写权限),作用域用参数驱动(沿用今天"由你选 scope"的做法)。
  - MCP app MI → Storage Account 上 **Storage Blob Data Contributor**。
  - 在每个 worker app 上加 **`Microsoft.Graph/applications/federatedIdentityCredentials@v1.0`**,信任其 group MI(受众 `api://AzureADTokenExchange`)。⚠️ *app 信任托管身份* 的精确 `issuer`/`subject`(预览特性)需要在落地前对照 Entra "configure an app to trust a managed identity" 文档 + 示例里的 inception lab 确认;`subject` = group MI 的 object id。
- `write-env.sh`:产出云上 `.env`(subscription、RG、region、group 名、app ID、Redis、Storage、`EXECUTOR=aca`)。secret 只在本地回退的 SP 路径需要;云上是无密码的。

#### §4.1 决策:Redis 用 ACA 里的容器,而不是 Azure Cache for Redis
**结论:推荐在 ACA 里跑一个 `redis:7-alpine` 容器,更省钱。**

- 我们只存两类数据,且**丢了能自愈**:
  - `session→sandbox` 映射(TTL 30min):Redis 重启丢了 → 旧 sandbox 变孤儿 → 被 **1 小时空闲 auto-delete 兜底**回收,用户下次调用拿到新 sandbox。
  - 用户 profile 缓存:丢了 → 下次登录重新派生。
  - 所以**不需要持久化 / HA**,而 Azure Cache 最低档(Basic C0 ~$16/mo)就是为这些付费。
- 两种落地方式:
  - **A(推荐)**:独立一个 internal-only 的 Container App(internal ingress,TCP 6379),`minReplicas = 1`,MCP app 走内部 DNS 访问。
  - **B**:作为 MCP Container App 的 **sidecar 容器**(同 app 多容器,`localhost:6379`)。省一个 app,但 Redis 跟 MCP 一起扩缩。
- ⚠️ 约束:Redis **不能 scale-to-0**(否则连接断、数据丢),`minReplicas` 必须 = 1 → 它是常驻的,省不到 0,但仍比 Azure Cache 便宜。要持久化可选挂一个 Azure Files 卷(可选,通常不需要)。

### Phase 3 — SandboxManager(SDK 集成、粘性路由、bootstrap、生命周期)
> 职责总览见 §3.4。这里是落地细节。

- **新增 `src/mcp-server/sandbox_manager.py`**,实现 `Executor`:
  - `get_or_create(ctx) -> SandboxClient`:
    1. 在 Redis 查 `session_sandbox[(oid, session_id, group)]`(Phase 4)。
    2. 命中 → `get_sandbox_client(id)` + `ensure_running()`;若 `NotFound` 则继续往下。
    3. 未命中 → `begin_create_sandbox(disk=<我们的镜像>, labels={user,session,group})`,把 id 带 TTL 写回,然后 **bootstrap**(每个 sandbox 只做一次;在 Redis 标记 `bootstrapped`):
       - **无密码登录**:取 group MI 对 `api://AzureADTokenExchange` 的 token,执行
         `az login --service-principal -u <sp-app-id> --tenant <tid> --federated-token <token> --allow-no-subscriptions`。
       - **还原用户 profile**(Phase 4):`az account set --subscription <sub>`(+ 默认项)。
       - **挂 Blob**(Phase 5):挂 blob 卷,或 `cd` 进同步好的 workspace 目录。
  - `exec(ctx, command)`:`get_or_create` 后 `.exec(command)`;成功则刷新 Session TTL;结果映射成 `ExecResult`(复用 `worker.py:45` 的截断逻辑)。
  - `end_session(oid, session_id)`:对两个 group 各 `begin_delete_sandbox`;清掉 Redis key。
- 本阶段要敲定的具体来源:sandbox 内如何拿到 MI→`AzureADTokenExchange` 的 token(一段用 `ManagedIdentityCredential` 的 bootstrap 脚本);以及用户 subscription 的首登来源(worker 可见的 sub,或一个配置的默认值)。

### Phase 4 — Redis 层(Session 路由 + 用户 Azure profile)
- **扩展 `src/mcp-server/cache.py`**(不扩 backend 接口——在 `cache.py:42` 已有草图旁加 typed view 与 `RedisBackend`):
  - `RedisBackend`:实现 `get/set`(就是现有草图),供所有 view 共用。
  - `SessionSandboxCache`:`(oid, session_id, group) → sandbox_id`,**30 分钟滑动 TTL**(每次调用刷新)——这既是 Session 粘性来源,也是 Session 结束信号。
  - `UserProfileCache`:`oid → {subscription_id, tenant_id, default_rg?}`——**剥掉 token**,只存可持久的 profile 元数据。首登写入,之后每个新 sandbox 还原。
  - `GroupCache` 保持不变(`cache.py:58`)。
- **Session / Conversation 推导**(新增 `src/mcp-server/session.py`,供中间件使用):
  - `user_oid` 来自 JWT(已有)。
  - `session_id`:Redis 里按用户做滑动窗口——若上次活动 < 30 分钟则**复用当前 Session**(哪怕跨了多段 Conversation),否则铸造 `sessionid + '_' + 时间戳` 作为新 Session。(待定项:若 FastMCP 传输层的 session id 足够稳定,优先用它;否则用用户指定的 TTL 启发式作为兜底。)
  - `conversation_id`:与 `Context` 里的 FastMCP 请求 / session id 关联,**仅用于 Blob 分目录,不进路由键**。这是 MCP 协议本身不天然携带的标识,落地时需确认来源。

### Phase 5 — Blob 持久化(每 User / Session / Conversation,无状态)
- **新增 `src/mcp-server/blob.py`**:`azure-storage-blob` 助手,落 `mcp-workspaces/{userid}/{sessionid_时间戳}/{conversationid}/` 结构(session id 后缀加时间戳,便于查找时知道 Session 发生的时间)。
- 主方案:创建时**挂 Azure Blob 卷**进 sandbox(`volumes=[...]`),作用域到该 User / Session 前缀,这样 bash 命令写出的文件(python 文件、JSON profile)自动持久化,sandbox 保持无状态。
- 兜底:若卷前缀粒度太粗,则用显式 `SandboxClient.read_file/write_file` ↔ Blob 按 Conversation 同步。

### Phase 6 — 生命周期管理(都在 Session 级)
- **Session 级杀(主路径,符合需求)**:Session 过期 / 结束时,删掉它的两个 sandbox(`SandboxManager.end_session`)。触发 = MCP app 内一个轻量 reaper 任务捕获 Redis 的 Session key TTL 过期(扫描 / 失效通知),外加客户端断连时的显式 end hook。
- **1 小时空闲兜底**:创建时给每个 sandbox 设 auto-delete / auto-suspend,使 1 小时无 tool call 的孤儿即便 reaper 漏了也会被回收。
- **暖池(可选,省成本)**:保留约 2 个预建空闲 sandbox(每 group 一个),让 Session 首调达到 sub-second;claim-then-replenish,真正空闲靠 scale-to-zero 省钱。标记为正确性落地后的后续项。

### Phase 7 — 镜像与本地 compose
- **新增 `src/sandbox-image/Dockerfile`**:ACA sandbox 的 disk 镜像——`az` CLI + python3 + jq + bootstrap 脚本(FIC 登录 + profile 还原)。**不再有 FastAPI 服务**(数据面就是传输层,见 §3.1)。推到镜像仓库,由 `begin_create_sandbox(disk=…)` 引用。
- **保留 `src/worker/`** 供本地路径(仍是 FastAPI bash executor)。
- **修改 `docker-compose.yml`**:为本地路径加一个 **redis** 服务;两个 worker 保留;`mcp-server` 加 `EXECUTOR=local` + `REDIS_URL`。
- **修改 `src/mcp-server/requirements.txt`**:加 `azure-containerapps-sandbox`、`azure-identity`、`azure-mgmt-resource`、`azure-mgmt-authorization`、`azure-storage-blob`、`redis`。
- **修改 `.env.example`**:加 `EXECUTOR`、`AZURE_SUBSCRIPTION_ID`、`ACA_RESOURCE_GROUP`、`ACA_REGION`、`DIAGNOSE_SANDBOX_GROUP`、`ACTION_SANDBOX_GROUP`、`REDIS_URL`、`STORAGE_ACCOUNT`、`BLOB_CONTAINER`,以及 Session / 空闲 TTL 旋钮。

### Phase 8 — 文档
- 更新根 `README.md`(local vs ACA 两条路径、新的身份图),新增 `provisioning/aca/README.md`。刷新 `docs/` 里引用"两 worker 模型"的笔记。

---

## 五、关键文件清单

| 区域 | 路径 | 动作 |
|---|---|---|
| 后端接口 | `src/mcp-server/executor.py` | 新增 |
| ACA SDK + 粘性 + bootstrap + 生命周期 | `src/mcp-server/sandbox_manager.py` | 新增(实现 `Executor`) |
| Session / Conversation key | `src/mcp-server/session.py` | 新增 |
| Redis view + RedisBackend | `src/mcp-server/cache.py` | 扩展 |
| Blob 持久化 | `src/mcp-server/blob.py` | 新增 |
| Tool 接线 | `src/mcp-server/main.py` | 修改 |
| 依赖 | `src/mcp-server/requirements.txt` | 修改 |
| 本地 provisioning(Bicep) | `provisioning/local/` | 新增(搬 `provisioning/bicep/`) |
| ACA provisioning(Bicep) | `provisioning/aca/main.bicep` + `modules/*` | 从零新增 |
| Sandbox disk 镜像 | `src/sandbox-image/Dockerfile` | 新增 |
| 本地栈 | `docker-compose.yml`、`.env.example` | 修改 |
| 删除 | `provisioning/python/`、`provisioning/bicep/` | 搬完后删 |

---

## 六、验收(Verification)

- **本地路径不变**:`docker compose up --build`(现含 redis),在 VS Code 以 diagnose-group 用户登录,跑一次 `diagnose_bash` → 行为与今天一致(对 Executor 重构做回归)。
- **ACA 部署**:`az deployment sub create -f provisioning/aca/main.bicep` 部署 RG、两个 sandbox group(含 MI)、worker apps(含 FIC)、storage、redis 容器、MCP Container App(含 MI 且在两个 group 上有 Data Owner)。确认角色传播。
- **Session 粘性**:在**同一个 Session**里连发多次 `diagnose_bash`(可以跨多段 Conversation),断言全部解析到**同一个** `sandbox_id`(看 labels / MCP 日志),而 `action_bash` 解析到**第二个** sandbox。30 分钟内的新 Conversation → 还是这两个 sandbox;超过 30 分钟(新 Session)→ 新的。
- **无密码**:sandbox 内 `az account show` 成功且**没有任何 secret**(FIC→SP)。
- **无状态**:结束 Session → 两个 sandbox 被删;上个 Session 写的文件在 sandbox 里没了、但在 Blob(`userid/sessionid_ts/...`)里有;新 Session 从 Redis 还原 profile、从 Blob 还原文件。
- **空闲回收**:1 小时无调用的 sandbox 被 auto-delete。

---

## 七、落地时需确认的开放项(已标注,不阻塞计划)
1. *app 信任托管身份* 的精确 FIC `issuer`/`subject`(预览)——对照 Entra 文档 + inception lab 验证。
2. `session_id` / `conversation_id` 在 MCP / FastMCP 层的可靠来源 vs TTL 启发式兜底。
3. Blob 卷前缀作用域是否够细到按 Session 隔离,否则改用 read/write 同步。

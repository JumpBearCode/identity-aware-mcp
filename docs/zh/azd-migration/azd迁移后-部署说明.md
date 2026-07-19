# azd 迁移落地后的部署说明(local vs. ACA)

> 配套 [`README.md`](README.md)(迁移*方案*)。本文讲的是**实现落地之后**部署
> 到底长什么样,并回答四个具体问题。
>
> **实现说明:** 落地版和方案有一点出入 —— 最终只用了**一个 `postprovision`
> hook**(不是 `pre` + `post` 两个),OBO secret 走 azd-env 往返注入(即方案里的
> "稳妥版 P2"),并且**直接删掉了** `provisioning/aca/write-env.sh` —— azd env
> (`.azure/<env>/.env`)成为唯一事实源,`e2e_deployed.py` 直接读它(见 §4)。

---

## 1. local 的 deploy 是怎样的?(没变)

local 路径(`EXECUTOR=local`)**不归 azd 管**,也没改动。仍是 Bicep(tenant
scope)+ `write-env.sh` + docker-compose:

```bash
cd provisioning/local
az deployment tenant create -n dataops-mcp-provision -l eastus -f main.bicep
./write-env.sh dataops-mcp-provision      # reset 3 个 app 密钥 -> ../../.env
# 把 users 加进 AD 组;给 worker SP 授 RBAC(都手动)
cd ../.. && docker compose up --build
```

为什么 local 保持原样:

- **scope**:它是 `az deployment tenant create`(tenant scope),只建 Entra
  app / AD 组 / worker SP。azd 是 subscription + RG scope。
- **目标**:它部署到本地 docker-compose,不是 Azure。azd 是部署到云的。
- **密钥**:local 的 worker 用 **SP + client secret** 登录,所以 local 的
  `write-env.sh` **必须**继续把这些密钥落进 `.env`(给 compose 消费)。这是它在
  local 里的真正职责,保留。(和 §4 形成对比。)

---

## 2. ACA 的 deploy 又是怎样的?(`azd up`)

整个云端现在一条 `azd up` 搞定。azd 跑这套生命周期:

```
azd up
├─ provision      (Bicep)  provisioning/aca/main.bicep —— RG、identity、sandbox
│                          groups、storage、ACR、env、redis、MCP Container App
│                          (先起在公共占位镜像上)、RBAC、FIC。Bicep outputs 自动
│                          被 azd 捕获进 azd env。
├─ postprovision  (hook)   provisioning/aca/hooks/postprovision.sh ——
│                            1. az acr build → 把 mcp-sandbox:latest 打进 ACR
│                            2. reset + 注入 MCP OBO client secret(仅一次)
│                            3. 打印"把 users 加进 AD 组"的提示
└─ deploy         (azd)    在 ACR 里构建 src/mcp-server(remoteBuild)并换进
                           Container App —— 靠 `azd-service-name: mcp` tag 定位。
```

老的手动步骤被谁接管:

| 老 ACA 步骤 | 现在 |
|---|---|
| `az deployment sub create` | `azd provision`(同一份 Bicep,逻辑没改) |
| `write-env.sh` 读 outputs → `.env.aca` | azd 自动把 outputs 捕获进 `.azure/<env>/.env` |
| `docker build/push` mcp-server + 换镜像 | `azd deploy`(ACR 构建 + 靠 tag 换镜像) |
| `docker build/push` sandbox 镜像 | `postprovision` hook(`az acr build`) |
| `credential reset` + `containerapp secret set` | `postprovision` hook(仅一次,带 guard) |
| `--set-env-vars SANDBOX_DISK_IMAGE=...` | `mcp-app.bicep` 里确定性引用 |
| 加 users 到 AD 组 | **仍手动** |

两个值得知道的设计点:

- **OBO secret 是一次性 bootstrap,不是每次 deploy 的动作。** 首次 `azd up`,
  `postprovision` 跑 `az ad app credential reset`,用 `azd env set MCP_CLIENT_SECRET`
  把值存下,再用 `az containerapp secret set` 应用它。之后每次 provision 都把这个
  值当 secure 参数 `mcpClientSecret` 传回去,所以重 provision **绝不会**把它打回
  占位 —— hook 也会跳过 reset。`azd deploy` 从不碰 secret。要轮换就刻意执行
  `azd env set MCP_CLIENT_SECRET "" && azd provision`。
- **镜像回读**(`mcpAppExists`):azd 带外换镜像,所以 `mcp-app.bicep` 在重
  provision 时回读当前已部署的镜像,而不是打回占位(azd 喂
  `SERVICE_MCP_RESOURCE_EXISTS`)。

之后迭代:改代码 `azd deploy`,改基础设施 `azd provision`,两者都改 `azd up`。

---

## 3. ACA 的 deployment 步骤是怎样的?

**前置**:装 `azd`(`https://aka.ms/azd-install`)、Azure CLI、`az login` 为一个
拥有 subscription **Owner / User Access Administrator** + Entra
**Application/Group Administrator** + **Privileged Role Administrator**(OBO 同意
和 FIC 需要)的主体,并选一个支持 `Microsoft.App/sandboxGroups` 的 region
(如 `westus2`、`eastus2`)。

```bash
# 0. 登录(azd 用于 provision/deploy;az 用于 hook 里的 az acr build / secret set)
azd auth login
az login

# 1. 建一个 azd 环境(会提示选订阅 + region)
azd env new dataops-mcp-aca

# 2. 一条命令:provision + postprovision hook + deploy
azd up

# 3. 手动:给真人授权(按人分配,故意不自动化)
az ad group member add --group "$(azd env get-value DIAGNOSE_GROUP_ID)" --member-id <user-object-id>
az ad group member add --group "$(azd env get-value ACTION_GROUP_ID)"   --member-id <user-object-id>

# 4. 把 MCP 客户端指向服务器
echo "https://$(azd env get-value MCP_FQDN)/mcp"
```

整个流程就这些。唯一仍手动的是第 3 步的组成员管理,因为"给谁访问权限"是人的决定。

---

## 4. write-env.sh 是不是在 ACA 这可以删了?

**已经删了。** `provisioning/aca/write-env.sh` 和仓库根的 `.env.aca` 都没了。
`.azure/<env>/.env`(azd 环境)现在是已部署栈的**唯一事实源**。

为什么删了也不会断:

- 运行中的 Container App **从来不读** `.env.aca`:server 读 `os.environ`,云上
  这些变量由 `mcp-app.bicep` 注入,部署链路从不依赖它。
- 唯一的本地消费者 `src/mcp-server/tests/e2e_deployed.py` 现在**自己加载 azd env**:
  一个小的 `_load_azd_env()` 在启动时读 `.azure/config.json` → `defaultEnvironment`
  → `.azure/<env>/.env`,灌进 `os.environ`,并从 `MCP_FQDN` output 推导出
  `MCP_SERVER_URL`。所以 `python e2e_deployed.py` 直接就能对着 `azd env select`
  选中的那个环境跑 —— 不用 `source`,也没有 `.env.aca`。显式 shell 变量仍优先
  (`MCP_SERVER_URL=... python e2e_deployed.py`)。

**别**把它和 **local** 的 `provisioning/local/write-env.sh` 搞混 —— 那个**没删**,
它还要把 worker SP 的 client secret 落进 `.env` 给 docker-compose 用。

---

## 5. 深入:三个常见疑问

### 5.1 为什么 sandbox 镜像不能直接用 `azd deploy` 构建?

因为 `azd deploy` 部署的是**会运行起来的 service**,而 sandbox 镜像不是。

`azd deploy` 对一个 `containerapp` service 做的事就是:构建镜像 → 推 ACR →
**更新那个 container app 的 `image`**。它需要一个运行目标去挂这个镜像。`mcp-server`
有(就是 MCP Container App),所以适配。

而 **sandbox 镜像是一个基础磁盘镜像**,不是被托管运行的 app。部署时没有任何东西
运行它 —— `SandboxManager` 在**运行时**才拉它,用户首次调用工具时才 boot 一个
per-Session microVM。没有任何 container app 的 `image` 是 sandbox 镜像,所以
`azd deploy` 没有可挂的目标。它是个**依赖产物**,更像数据而不是 service。azd 也
没有干净的"只构建推送、不部署"的 service 类型,所以放进 hook 里用 `az acr build`
把它塞进 ACR、供运行时拉取,是最诚实的做法。

### 5.2 `containerapp secret set` 是什么?(顺带:reset 只做一次)

先纠正一个常见误解:**它不是每次 deploy 都新建一个 client secret**。`reset` 每个
azd 环境**只跑一次**(hook 有 guard:azd env 里已有就跳过),`azd deploy` 从不碰它。
即:首次 `azd up` reset 一次,之后每次 deploy 都不动。(为什么这个动作叫 "reset":
Azure 只在创建那一刻返回 secret 明文,存量读不回来,所以想拿到一个能注入的值只能
新建一个 —— 这是一次性 bootstrap,不是每次部署。)

Container Apps 把**存 secret** 和**暴露 secret** 分成两步:

1. `az containerapp secret set` 把值写进 app 的**加密 secret 存储**
   (`configuration.secrets`),名字叫 `mcp-client-secret`,加密落盘,不在资源
   JSON 里以明文出现。
2. **env var 是另一个引用**。`mcp-app.bicep` 里容器的 env 已经有:
   ```bicep
   { name: 'MCP_CLIENT_SECRET', secretRef: 'mcp-client-secret' }
   ```
   `secretRef`(不是 `value`)意思是"这个 env var 的值,从名为 `mcp-client-secret`
   的 secret 里取"。

所以链路是:`secret set` 填满加密存储 → `secretRef` 把它作为 `MCP_CLIENT_SECRET`
env var 暴露进容器 → Python server 读 `os.environ["MCP_CLIENT_SECRET"]` 做 OBO。
只 `secret set` 不配 `secretRef`,app 拿不到;只 `secretRef` 不 set,值是空的。两者
都得有。

### 5.3 那些 env var 是什么?azd 能喂 param 改 app 行为吗?

能。容器的 env 块**就是 app 的全部运行时配置**,Python server 通过 `os.environ`
读它来改行为。`SANDBOX_DISK_IMAGE` 只是 `mcp-app.bicep` 里 ~23 条之一,分三类:

| 类别 | 例子 | 作用 |
|---|---|---|
| **行为/逻辑开关** | `EXECUTOR`(local vs aca 后端)、`SANDBOX_DISTRIBUTED_LOCK`(Redis 锁开关)、`SANDBOX_DISK_IMAGE`、审计开关(`AUDIT_DCR_*`) | 真正改变 app 干什么 |
| **连线/连接** | `REDIS_URL`、`STORAGE_ACCOUNT`、`BLOB_CONTAINER`、`MCP_SERVER_BASE_URL` | 把 app 指向正确资源 |
| **身份/鉴权** | `AZURE_TENANT_ID`、`MCP_APP_ID`、`MCP_IDENTIFIER_URI`、`MCP_CLIENT_SECRET`、`*_GROUP_ID` | 驱动 JWT 校验 + OBO |

注意你说的"多个 Redis 副本"**不是** app env var —— Redis 副本数是**基础设施伸缩**,
在 `redis.bicep` 的 `scale { minReplicas / maxReplicas }` 块里。层次不同:app 行为 =
容器 env var;某个 service 跑几个副本 = Bicep 基础设施。

azd 喂进去改行为的链路:

```
azd 环境变量 ──► main.parameters.json (${VAR}) ──► main.bicep param
   ──► mcp-app.bicep env 块 ──► 容器 env ──► Python 里的 os.environ
```

**这些行为开关现在已经全部接到 azd 了**(完整清单见 §6)。要改一个,直接两步:

```bash
azd env set SANDBOX_DISTRIBUTED_LOCK 1   # 不设 -> 走代码默认(这里是 "0")
azd provision                            # 或 azd up
```

背后链路(已接好,你不用碰):`main.parameters.json` 里的
`"sandboxDistributedLock": { "value": "${SANDBOX_DISTRIBUTED_LOCK=}" }` → main.bicep 的
`param sandboxDistributedLock` → 汇进一个 `envOverrides` object → `mcp-app.bicep` 的
`optionalEnv` 把**非空**的那些注入容器 env(空的直接丢弃,让 server 走它
`os.environ.get(..., 默认)` 的代码默认)。所以"不设就走 default"是天然的,而且
default 只有一处 —— 代码里,Bicep 不重复声明,零漂移。

**要新增一个当前清单外的 knob**(前提:代码已经 `os.environ.get("X")` 读它),只要两步:
① `main.bicep` 加 `param x string = ''`,并在 `envOverrides` object 里加一行 `X: x`;
② `main.parameters.json` 加 `"x": { "value": "${X=}" }`。
**不用碰 `mcp-app.bicep`** —— `optionalEnv` 是通用机制,自动注入任何非空的 override。

一句话:**行为开关 → app env var,现在全部 `azd env set` 可控、不设走代码默认;像
Redis 副本数那种伸缩 → Bicep 基础设施 param。两者都能从 azd 触达,只是层次不同。**

---

## 6. Bicep 注入了哪些 env,以及哪些能 azd 传值

`mcp-app.bicep` 往 MCP 容器注入 **固定 23 条** env,**外加你 `azd env set` 设过的任何
调优 knob**(经 `optionalEnv` 动态拼上)。Python server 只读 `os.environ`。按"能不能/
该不该用 `azd env set` 去改"分三类:

| 类别 | env var | 值来源 | azd 传值? |
|---|---|---|---|
| **① 勿动 —— provision 产物 / 身份 / 连线** | `EXECUTOR`、`MCP_SERVER_BASE_URL`、`AZURE_TENANT_ID`、`AZURE_SUBSCRIPTION_ID`、`ACA_RESOURCE_GROUP`、`ACA_REGION`、`MCP_APP_ID`、`MCP_IDENTIFIER_URI`、`DIAGNOSE_GROUP_ID`、`ACTION_GROUP_ID`、`DIAGNOSE_SANDBOX_GROUP`、`ACTION_SANDBOX_GROUP`、`DIAGNOSE_SP_APP_ID`、`ACTION_SP_APP_ID`、`REDIS_URL`、`STORAGE_ACCOUNT`、`BLOB_CONTAINER`、`BLOB_CONTAINER_RESOURCE_ID`、`AUDIT_DCR_ENDPOINT`、`AUDIT_DCR_RULE_ID`、`AUDIT_STREAM_NAME` | 硬编码(`'aca'`)/ 派生 var / Azure 上下文函数(`tenant()`、`subscription()`)/ 各 module 的 `output`(identity、redis、storage、sandboxGroups、observability) | ❌ 不该动 —— provision 出来的资源属性和身份,改了就和实际资源对不上 |
| **② 密钥** | `MCP_CLIENT_SECRET` | `parameters.json` 的 `${MCP_CLIENT_SECRET}`,由 postprovision hook `azd env set` | ✅ 已可控;平时只在轮换时动(`azd env set MCP_CLIENT_SECRET "" && azd provision`) |
| **③ 行为 / 调优 knob —— 全部可 `azd env set`,不设走代码默认** | `SANDBOX_DISK_IMAGE`、`SANDBOX_DISTRIBUTED_LOCK`、`MCP_EXEC_TIMEOUT`、`MAX_OUTPUT_BYTES`、`MCP_SESSION_TTL`、`MCPPROXY_ENABLED`、`SANDBOX_AUTO_SUSPEND_SECONDS`、`SANDBOX_AUTO_DELETE_SECONDS`、`SANDBOX_REAPER_INTERVAL`、`SANDBOX_REAPER_LEASE`、`SANDBOX_LOCK_TTL`、`SANDBOX_LOCK_WAIT`、`SANDBOX_CREATE_TIMEOUT`、`SANDBOX_CPU`、`SANDBOX_MEMORY`、`SANDBOX_DISK`、`SANDBOX_DISK_ID`、`BLOB_MOUNTPOINT`、`AUDIT_TIMEOUT`、`AUDIT_UA_INCLUDE_OID` | `main.parameters.json` 的 `${…=}` → main.bicep param → `envOverrides` → 空则不注入(走代码默认) | ✅ **本次已全部接好** |

**这 20 个(③)现在都能:**

```bash
azd env set <ENV_NAME> <value>   # 例:azd env set MCP_EXEC_TIMEOUT 300
azd provision                    # 或 azd up;不设 -> server 走它 os.environ.get 的默认
```

- 机制见 §5.3(`envOverrides` → `optionalEnv`,空值自动丢弃)。default 只有一处:代码。
- 特例 `SANDBOX_DISK_IMAGE`:不设时**不是**"不注入",而是走 main.bicep 算好的确定性引用
  `<acr-login-server>/mcp-sandbox:latest`;设了才覆盖(指向别的 sandbox 镜像)。
- ① 勿动(provision 决定);② 由 hook 管。

> 另有 `DIAGNOSE_WORKER_URL` / `ACTION_WORKER_URL` 只在 **local docker** 路径用
> (`docker-compose.yml` 里设),ACA 不涉及,故不在上表。

# azd 迁移方案:把 ACA 路径收敛成 `azd up`

> 目标读者:维护本仓库部署流程的人。
> 结论先行:**只把 ACA(云)路径 azd 化,local 路径保持现状。**
> `azd up` 用一条命令替掉现在 ACA 的「`az deployment sub create` + `write-env.sh` + 4 步手动命令」。

---

## 0. TL;DR

| | 现在 | azd 之后 |
|---|---|---|
| ACA 部署 | 6 个手动步骤(见 §1.2) | `azd up` 一条命令 + 1 个仍手动的组授权 |
| `write-env.sh`(读 output 那半份) | 手动跑,写 `.env.aca` | **删掉** —— azd 自动把 Bicep outputs 灌进 `.azure/<env>/.env` |
| 镜像 build/push + 换镜像 | 手动 `docker build/push` + `az containerapp update --image` | `azd deploy` 内建 |
| OBO secret reset + 注入 | 手动两条命令 | `preprovision` hook + secure param |
| sandbox 第二镜像 | 手动 build/push + `--set-env-vars` | `postprovision` hook(`az acr build`)+ Bicep 里确定性引用 |
| 加 users 到 AD 组 | 手动 | **仍手动**(azd 管不了,也不该管) |

local 路径**不动**,原因见 §2。

---

## 1. 现状盘点

### 1.1 local 路径现状(`EXECUTOR=local`)

`provisioning/local/` + `docker-compose.yml`。5 步:

1. `az deployment tenant create -f provisioning/local/main.bicep` —— **tenant scope**,只建 Entra:MCP app(delegated `user_impersonation` + OBO admin consent)、2 个 AD 组、`diagnose-sp` / `action-sp` 两个 worker app。
2. `write-env.sh` —— 读 outputs **+ 对 3 个 app 各跑一次 `az ad app credential reset` 当场生成密钥**,写 `.env`。
3. `az ad group member add` 把 users 加进两个组(手动)。
4. `az role assignment create` 给两个 worker SP 授 Reader / Contributor(手动,故意不在 Bicep 里)。
5. `docker compose up --build` —— 起 mcp-server + diagnose-worker + action-worker + redis。worker 用 **SP + client secret** 登录(见 `docker-compose.yml` 的 `AZURE_CLIENT_SECRET`)。

关键点:local 的 worker 身份**是有密码的**,密钥必须在 `write-env.sh` 里落地,`.env` 被 compose 消费。

### 1.2 ACA 路径现状(`EXECUTOR=aca`)

`provisioning/aca/` 一个 `targetScope = 'subscription'` 的 `main.bicep` fan-out 到 11 个 module。来自 `provisioning/aca/README.md` 的 6 步:

| 步 | 命令 | 性质 |
|---|---|---|
| 1 | `az deployment sub create -f main.bicep` —— 建 RG、identity、sandbox groups、storage、ACR、env、redis、**MCP app(占位公共镜像)**、RBAC、FIC | 声明式 ✅ |
| 2 | `write-env.sh` 读 outputs → `.env.aca`,`source` 它 | 胶水(**azd 可吃掉**) |
| 3 | `docker build/push` **两个**镜像(mcp-server + sandbox)到 ACR | 命令式 |
| 4 | `az ad app credential reset` MCP 的 OBO secret + `az containerapp secret set` | 命令式 |
| 5 | `az containerapp registry set --identity system` + `az containerapp update --image <真镜像> --set-env-vars SANDBOX_DISK_IMAGE=...` | 命令式(换镜像) |
| 6 | `az ad group member add` 加 users;客户端指向 `https://<MCP_FQDN>/mcp` | 手动 |

worker 身份在云上**是无密码的**(sandbox-group MI 经 FIC 换 worker SP token,`az login --federated-token`),所以这里**没有 worker secret**;唯一的密钥是 MCP server 自己的 OBO client secret(第 4 步)。

**已经很 azd-friendly 的地方**(利好):
- `main.bicep` 已经是 `targetScope = 'subscription'` 且自己建 RG —— 正是 azd 的原生形状,不用改结构。
- 镜像已用**占位公共镜像**(`mcr.microsoft.com/k8se/quickstart:latest`)+ 后置换真镜像 —— 正是 azd ACA 模板的标准套路。
- 所有资源 ID 已经通过 `output` 暴露 —— azd 直接接管。

---

## 2. 为什么 local 不做 azd

- **scope 不对**:local 是 `az deployment tenant create`(tenant scope),纯建 Entra/Graph 资源。azd `provision` 是 subscription + RG 模型,不部署 tenant-scope Graph。
- **目标不对**:local 部署目标是本地 docker-compose,不是 Azure。azd 是「部署到云」的工具,不会替你跑 compose。
- 所以 local 的 `write-env.sh`(读 output **+ reset 3 个 SP secret**)留着不动。

> 一句话:azd 化的收益完全在 ACA 路径。local 强行套 azd 只会增加复杂度、无收益。

---

## 3. `azd up` 之后 ACA 长什么样

### 3.1 一条命令

```bash
azd auth login
azd env new dataops-mcp-aca      # 首次;选订阅、region=westus2
azd up                           # = provision(Bicep)+ deploy(build/push/换镜像)
# 之后唯一手动:把 users 加进 AD 组
az ad group member add --group "$(azd env get-value DIAGNOSE_GROUP_ID)" --member-id <oid>
```

改代码后重发只需 `azd deploy`(不重跑 infra),改 infra 用 `azd provision`。

### 3.2 生命周期映射(现状步骤 → azd 阶段)

```
azd up
├─ preprovision  (hook)   → reset MCP OBO secret,存进 azd env(仅首次)         ← 现状步4上半
├─ provision     (bicep)  → 现状步1 全套基础设施 + 用 secure param 播种 secret    ← 现状步1 + 步4下半
│                           outputs 自动灌进 .azure/<env>/.env                    ← 现状步2(write-env.sh)整份消失
├─ deploy        (azd)    → build/push mcp-server 镜像、换镜像(靠 azd-service-name tag) ← 现状步3上半 + 步5
└─ postprovision (hook)   → az acr build 打 sandbox 镜像                           ← 现状步3下半
```

### 3.3 `azure.yaml`(仓库根)

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Azure/azure-dev/main/schemas/v1.0/azure.yaml.json
name: identity-aware-mcp
metadata:
  template: identity-aware-mcp@0.1
infra:
  provider: bicep
  path: provisioning/aca
  module: main
services:
  mcp:
    project: src/mcp-server
    language: docker
    host: containerapp
    docker:
      path: Dockerfile
      remoteBuild: true          # 用 ACR 远程构建,hook/本地都不需要 docker daemon
hooks:
  preprovision:
    windows: { shell: pwsh, run: provisioning/aca/hooks/preprovision.ps1 }
    posix:   { shell: sh,   run: provisioning/aca/hooks/preprovision.sh }
  postprovision:
    windows: { shell: pwsh, run: provisioning/aca/hooks/postprovision.ps1 }
    posix:   { shell: sh,   run: provisioning/aca/hooks/postprovision.sh }
```

---

## 4. 需要改什么(改动清单)

### 4.1 新增 `azure.yaml`(见 §3.3)
根目录,声明 infra 路径 + `mcp` 这个 containerapp service + 两个 hook。

### 4.2 改 `mcp-app.bicep`:让 azd 认领 container app

azd `deploy` 靠 **tag `azd-service-name`** 找到要换镜像的 container app,靠 **ACR endpoint output** 找到往哪 push。要动三处:

1. **打 tag**,值 = `azure.yaml` 里的 service 名 `mcp`:
   ```bicep
   resource app 'Microsoft.App/containerApps@2024-03-01' = {
     name: '${name}-mcp'
     location: location
     tags: { 'azd-service-name': 'mcp' }   // ← 新增
     ...
   ```
2. **镜像回读**(最关键的坑,见 §5.1):重跑 `provision` 不能把 azd 换好的真镜像打回占位镜像。采用 azd ACA 模板的 `exists` 套路 —— 传一个 `mcpAppExists` bool,存在就从现有 app 回读当前镜像:
   ```bicep
   param mcpAppExists bool = false
   resource existing 'Microsoft.App/containerApps@2024-03-01' existing = if (mcpAppExists) {
     name: '${name}-mcp'
   }
   var effectiveImage = mcpAppExists
     ? existing.properties.template.containers[0].image
     : mcpImage
   ```
   `azd` 会自动计算并传入 `<service>Exists`(`mcpExists` → 映射到 `mcpAppExists`),无需手动维护。
3. **`SANDBOX_DISK_IMAGE` 确定性化**(消灭现状步5的 `--set-env-vars`):ACR login server 在 provision 时已知,镜像 tag 固定,直接在 Bicep 里拼:
   ```bicep
   // 把 param sandboxImage 默认改成确定性引用(main.bicep 里传 registry.outputs.loginServer)
   { name: 'SANDBOX_DISK_IMAGE', value: '${acrLoginServer}/mcp-sandbox:latest' }
   ```
   env 指向「镜像将来会在的位置」即可;只要 `postprovision` 在 SandboxManager 首次用它之前把镜像 push 上去就行。

### 4.3 `main.bicep`:补 azd 约定的 output + 传新 param

azd 的 containerapp target 需要知道 ACR endpoint。加一个约定名 output(值复用已有的):

```bicep
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
// 现有的 REGISTRY_LOGIN_SERVER / MCP_FQDN 等 output 全部保留,azd 会一并灌进 env
```

并把 `mcpAppExists`、`mcpClientSecret`(secure)透传给 `mcp-app` module。

### 4.4 `write-env.sh`:退休(ACA 版)

`provisioning/aca/write-env.sh` **整个删除**。它做的两件事都被接管:
- 读 outputs → azd 自动写 `.azure/<env>/.env`(用 `azd env get-values` 取用)。
- 那段「已删除 `az ad app update --identifier-uris`」的注释历史(AADSTS500011 漂移)也随之一起归档 —— azd 不引入任何 identifier-uri 覆盖,和 `fix-identify-uri-overwrite` 的方向一致。

> 若运行时/脚本仍依赖文件名 `.env.aca`,可保留一个 3 行的兼容脚本:`azd env get-values > ../../.env.aca`。

### 4.5 OBO secret → `preprovision` hook

```sh
# provisioning/aca/hooks/preprovision.sh  (节选)
set -euo pipefail
# 幂等:已存在就不 reset(credential reset 会作废旧密钥,不能每次 azd up 都轮换)
if [ -z "$(azd env get-value MCP_CLIENT_SECRET 2>/dev/null || true)" ]; then
  APP_ID="$(azd env get-value MCP_APP_ID 2>/dev/null || true)"
  if [ -n "$APP_ID" ]; then
    SECRET="$(az ad app credential reset --id "$APP_ID" --display-name aca --query password -o tsv)"
    azd env set MCP_CLIENT_SECRET "$SECRET"
  fi
fi
```

- azd 把 `MCP_CLIENT_SECRET`(azd env 里)映射到 secure bicep param `mcpClientSecret`,Bicep 直接播种 container app secret,**省掉现状步4的 `az containerapp secret set`**。
- 首次 `azd up` 时 `MCP_APP_ID` 还没有(identity module 还没跑)→ hook 跳过,secret 用占位;需要一次「provision 建 app → 拿到 appId → 二次 provision 播种 secret」。**更稳的做法**:把 secret 注入放 `postprovision`(此时 outputs 已含 `MCP_APP_ID`),回到 `az containerapp secret set`,只是从「手动」变「自动」。§6 里按这个稳妥版落地。
- 注意:azd env 的 `.env` 是**明文落盘**,和今天的 `.env.aca` 风险等价,不是回退。

### 4.6 sandbox 第二镜像 → `postprovision` hook

sandbox 镜像不是一个「部署成 container app」的 service,SandboxManager 运行时才拉它,所以它**不能**是普通 azd service。用 hook 在 ACR 里服务端构建(不需要本地 docker):

```sh
# provisioning/aca/hooks/postprovision.sh  (节选)
set -euo pipefail
REG="$(azd env get-value REGISTRY_NAME)"
az acr build -r "$REG" -t mcp-sandbox:latest ../../src/sandbox-image
```

### 4.7 仍然手动的部分

把真人加进 AD 组(`az ad group member add`)—— 给谁授权是人的决定,不进自动化。`postprovision` 结尾 `echo` 出组 ID + 命令模板即可(照抄现状 write-env.sh 的收尾提示)。

---

## 5. 风险与坑

### 5.1 container app 镜像归属权之争(最大的坑)
azd `deploy` 在 provision 之外**带外**更新 container app 的 image。若 Bicep 仍硬写 `image: 占位镜像`,下次 `azd provision`/`azd up` 会把真镜像打回占位。**必须**用 §4.2 的 `exists` 回读套路。这是 azd + ACA 模板里最常见的返工点,先做对。

### 5.2 AcrPull 时序
现状:首次用**公共占位镜像**冷启动(此时 app MI 还没 AcrPull),`rbac.bicep` 之后才授 AcrPull,再换 ACR 私有镜像。azd 下顺序天然成立:`provision`(公共占位 + 授 AcrPull)→ `deploy`(push 私有镜像 + 换镜像,MI 已有 AcrPull)。`mcp-app.bicep` 里那段 `useAcrRegistry = contains(mcpImage,'azurecr.io')` 的 registry 挂载逻辑要跟 `exists` 回读的 `effectiveImage` 对齐,别再看占位 param。

### 5.3 Graph 扩展仍是 preview
`identity.bicep` / `fic.bicep` 用 `Microsoft.Graph` Bicep 扩展(public preview)。azd 只是换了触发器,preview 的粗糙(尤其 `preAuthorizedApplications`、FIC issuer/subject)不变。迁移**不改** Graph 逻辑,降低变量。

### 5.4 权限面不变
`azd up` 的执行者仍需 subscription **Owner/UAA**(role assignment)+ Entra **Application/Group Admin** + **Privileged Role Admin**(OBO consent + FIC)。azd 不降低前置权限要求。

### 5.5 secret 明文落盘
azd env 的 `.azure/<env>/.env` 明文存 `MCP_CLIENT_SECRET`,与今天 `.env.aca` 同级风险。已在 `.gitignore` 覆盖(`.azure/` 需补进 gitignore)。

---

## 6. 分阶段落地建议

| 阶段 | 内容 | 可验证的产出 |
|---|---|---|
| **P0** | 加 `azure.yaml` + `main.bicep` 补 `AZURE_CONTAINER_REGISTRY_ENDPOINT` output;container app 打 `azd-service-name` tag。**先不碰镜像回读**,`azd provision` 跑通、outputs 进 azd env | `azd provision` 成功,`azd env get-values` 看到全部变量 |
| **P1** | `mcp-app.bicep` 加 `exists` 回读(§4.2)+ `azd deploy` 换 mcp-server 镜像;`postprovision` 打 sandbox 镜像 + 确定性 `SANDBOX_DISK_IMAGE` | `azd up` 端到端;重跑 `azd up` 不把镜像打回占位 |
| **P2** | secret 注入进 `postprovision`(稳妥版,§4.5);删 `aca/write-env.sh`(或保留 3 行兼容脚本);更新 `provisioning/aca/README.md` 与根 `README.md` 的 Quickstart | 全新订阅上 `azd up` 一把梭,只剩「加 users 到组」手动 |

> local 路径三个阶段都不动。

---

## 附:改动文件一览

| 文件 | 动作 |
|---|---|
| `azure.yaml`(新) | 声明 infra + `mcp` service + hooks |
| `provisioning/aca/main.bicep` | 加 `AZURE_CONTAINER_REGISTRY_ENDPOINT` output;透传 `mcpAppExists` / secure secret |
| `provisioning/aca/modules/mcp-app.bicep` | `azd-service-name` tag;`exists` 镜像回读;确定性 `SANDBOX_DISK_IMAGE` |
| `provisioning/aca/hooks/{pre,post}provision.sh`(新) | secret 注入 + `az acr build` sandbox 镜像 + 组授权提示 |
| `provisioning/aca/write-env.sh` | 删除(或降级为 3 行 `.env.aca` 兼容导出) |
| `provisioning/aca/README.md` / 根 `README.md` | Quickstart 改成 `azd up` |
| `.gitignore` | 补 `.azure/` |

---
title: "部署文档：从 main.bicep 完整部署 ACA 栈 —— 参数 / 密钥 / 漂移与验证"
date: 2026-07-13
tags:
  - deploy
  - runbook
  - bicep
  - aca
  - audit
  - oid-tracking
status: 已按本文实测收敛部署成功（2026-07-13），main.bicep 为唯一真相来源
sources:
  - "provisioning/aca/main.bicep（sub 作用域，一把起整栈）"
  - "provisioning/aca/modules/{environment,mcp-observability,rbac,storage,mcp-app}.bicep"
  - ".env.aca（云 profile，由 write-env.sh 生成）"
  - "实测：sub ee5f77a1-…, RG dataops-aca-rg, workspace dataops-aca-logs (4de6b3e7-…)"
verified:
  - "what-if：0 create / 0 delete / 0 recreate；审计 DCR/表/角色 = NoChange"
  - "收敛部署 provisioningState=Succeeded；OBO 密钥保留(len 40)；registries 加固后 what-if 不再显示被移除"
  - "E2E：cid bf825733… 串起 MCPAudit_CL + StorageBlobLogs，join 回真人 jumpbear0920@outlook.com / 107.136.48.82"
---

# 部署文档：从 `main.bicep` 完整部署 ACA 栈

> 承接 [实操记录 §7](./实操记录-DCR-DCE区别-部署与验证现状.md) 遗留的"正式 deploy 文档"。
> 回答:**如何只从 `provisioning/` 的正式 bicep 部署整套(含审计),不依赖已淘汰的
> `audit-standalone.bicep`,并把参数 / 密钥 / 漂移讲清楚,使部署不打断线上。**

---

## 0. 两种部署场景(先分清)

| 场景 | 前提 | 本文覆盖 |
|---|---|---|
| **收敛部署 (converge)** | 栈已在跑,想让 `main.bicep` 成为唯一真相 / 上新镜像 | ✅ 主线(§3–§7) |
| **冷部署 (cold)** | 全新环境,RG / 身份 / 资源都不存在 | ✅ 差异见 §8 |

当前生产就是**收敛**场景。**除非另说,下文都指收敛部署。**

---

## 1. 前置条件

- **以真人身份登录 `az`**,且对订阅有 Owner/Contributor:`az account show` 应为
  `jumpbear0920@outlook.com` / sub `ee5f77a1-…`。**不能用 worker SP**(diagnose/action SP 权限不够跑
  订阅级部署 + Graph 模块)。
- `az bicep`(自带 `microsoftGraphV1` 扩展);`jq`。
- 栈标识:RG `dataops-aca-rg`、name 前缀 `dataops-aca`、region `westus2`、workspace `dataops-aca-logs`、
  ACR `dataopsacaacrvyq3trlvkn4za.azurecr.io`。

---

## 2. 必传参数 ★(默认值对线上是**错**的)

`main.bicep` 的默认值是给"全新示例栈"的,直接用会打坏线上。**必须显式传下面 6 个**:

| 参数 | 传什么 | 来源 | 不传 / 用默认会怎样 |
|---|---|---|---|
| `name` | `dataops-aca` | 固定前缀 | 默认 `dataops-mcp` → **另建一整套平行栈** |
| `resourceGroupName` | `dataops-aca-rg` | `.env.aca` `ACA_RESOURCE_GROUP` | 默认 `${name}-aca-rg`=`dataops-aca-aca-rg` → **错 RG** |
| `location` | `westus2` | `.env.aca` `ACA_REGION` | —— |
| `mcpImage` | `…azurecr.io/mcp-server:<tag>` | 线上 app 当前镜像 | 默认公共占位镜像 → **把 MCP 打回占位镜像** |
| `sandboxImage` | `…azurecr.io/mcp-sandbox:latest` | 线上 `SANDBOX_DISK_IMAGE` | 默认空 → sandbox 退回公共 ubuntu 盘 |
| `mcpClientSecret` | (云 OBO 密钥,**现取**) | container app secret,见 §4 | 默认空 → bicep 塞占位符 → **打断 OBO 登录** |

> 当前镜像 tag 查:`az containerapp show -g dataops-aca-rg -n dataops-aca-mcp --query "properties.template.containers[0].image" -o tsv`

---

## 3. 密钥专题:为什么 `mcpClientSecret` 必须传真实 ★

**常见误解**:"云端不是 FIC 了吗,还要密钥?" —— 这是把两个不同的 credential 混了:

| credential | app | 用途 | FIC 取代了吗 |
|---|---|---|---|
| Worker SP 密钥(diagnose/action) | `d09dfd39` / `c58c15ae` | worker 拿 Azure 令牌执行 az | ✅ **是**(FIC:sandbox MI → worker SP) |
| **MCP OBO 密钥** (`mcp-client-secret`) | MCP app `88de6a37` | MCP 服务端 **OBO 换令牌调 Graph 查组** | ❌ **否** |

证据:MCP app `88de6a37` **无任何 federated credential**,只有 1 把 client secret;代码
`MCP_CLIENT_SECRET = os.environ["MCP_CLIENT_SECRET"]`(硬依赖),每次 tool call `acquire_token_on_behalf_of`。

- **根目录 `.env` 里的 `MCP_CLIENT_SECRET` 是本地 app `ec0442d1` 的,不是云 app,别拿来用。**
- 云密钥只存在于 container app secret(只写),**现取**(值不落盘、不打印):
  ```bash
  MCP_SECRET="$(az containerapp secret show -g dataops-aca-rg -n dataops-aca-mcp \
    --secret-name mcp-client-secret --query value -o tsv)"
  ```
- 传空 / 传错的后果:`AADSTS7000215 invalid client secret` → 组员解析失败 → **两个工具都不暴露**。

---

## 4. 完整部署命令(收敛)★

```bash
cd provisioning/aca

# (可选但推荐) 预检爆炸半径,见 §5
# az deployment sub what-if --name pre-check --location westus2 -f main.bicep --parameters ...(同下)

# 1) 现取云 OBO 密钥(同一 shell,值不打印)
MCP_SECRET="$(az containerapp secret show -g dataops-aca-rg -n dataops-aca-mcp \
  --secret-name mcp-client-secret --query value -o tsv)"
[ -z "$MCP_SECRET" ] && { echo "ABORT: secret fetch failed"; exit 1; }

# 2) 从 main.bicep 收敛整栈(幂等)
az deployment sub create \
  --name main-converge --location westus2 \
  --template-file main.bicep \
  --parameters \
      name=dataops-aca \
      resourceGroupName=dataops-aca-rg \
      location=westus2 \
      mcpImage=dataopsacaacrvyq3trlvkn4za.azurecr.io/mcp-server:<TAG> \
      sandboxImage=dataopsacaacrvyq3trlvkn4za.azurecr.io/mcp-sandbox:latest \
      mcpClientSecret="$MCP_SECRET"
```

> **不再需要手动补 registry。** 老版本 `mcp-app.bicep` 没声明 `configuration.registries`,部署会把它抹掉,
> 得事后 `az containerapp registry set` 补。现已在 bicep 里根治(§7),`azurecr.io` 镜像会自动声明 registry。

`--name` 每次换个没用过的(sub 部署名会被占用;`main` 已被历史占用,别用)。

---

## 5. 部署会动什么 —— what-if 爆炸半径

`az deployment sub what-if`(只读)传**和 §4 完全一样的参数**,实测结论:

```
Create: 0   Delete: 0   Recreate: 0
Modify: 10 (全是噪音)   NoChange: 4   Unsupported: 11
```

- **4 NoChange(逐字节复现)**:RG、**审计 DCR `dataops-aca-audit-dcr`**、workspace `dataops-aca-logs`、
  storage account。→ 证明审计设施被**认领**,不重建。
- **10 Modify = 噪音**:平台默认属性显示成 `→ null`(retention / encryption / `ingress.traffic` /
  `runningStatus` / 表的 `plan` 等)、未解析的 `reference()`、预览类型 `sandboxGroups`(无 Bicep schema)。
  ARM 增量实际不动这些。**加固后 `registries` 已不在移除列表**。
- **11 Unsupported = 角色分配**(名字是 `guid(scope, principalId, role)`,要运行时算 principalId)。
  名字确定性 → 幂等认领(含审计的 Monitoring Metrics Publisher)。

解读诀窍:看 **Create/Delete/Recreate 是否为 0**;Modify 里凡是 `具体值 → null` 基本都是噪音。

---

## 6. 部署后验证(E2E)★

```bash
# a) app 在跑 + 新 revision ready + 关键状态没被打坏
az containerapp show -g dataops-aca-rg -n dataops-aca-mcp \
  --query "{running:properties.runningStatus, rev:properties.latestReadyRevisionName, \
            registries:properties.configuration.registries[0].server}" -o json
# secret 长度应仍为 40(未被占位符覆盖)
az containerapp secret show -g dataops-aca-rg -n dataops-aca-mcp \
  --secret-name mcp-client-secret --query value -o tsv | tr -d '\n' | wc -c

# b) 走真实前门跑一次 diagnose_bash(会执行 = OBO 通),命令里 echo "$AZURE_HTTP_USER_AGENT" 拿 cid
# c) 用 cid join 三处(workspace GUID = 4de6b3e7-fff6-492f-9334-c6c6cf01351e):
```

```kusto
// 层1:权威行
MCPAudit_CL | where correlation_id == "<cid>"
| project TimeGenerated, user_upn, client_ip, tool, command
// 层2:原生 storage 日志 join 回真人(storage 摄入延迟 5–15min,实测 ~4min)
StorageBlobLogs | extend c = extract(@"mcp/([0-9a-f]+)", 1, UserAgentHeader)
| where c == "<cid>"
| join kind=inner (MCPAudit_CL) on $left.c == $right.correlation_id
| project TimeGenerated, OperationName, StatusCode, real_user=user_upn, real_ip=client_ip
// key vault 换成 AzureDiagnostics + extract(..., clientInfo_s)
```

**通过标准**:`MCPAudit_CL` 立即有权威行(真人 + 真实 IP);storage/KV 延迟后 join 回同一真人。

---

## 7. 已根治的漂移 & 已淘汰的脚手架

| 项 | 处理 | commit |
|---|---|---|
| `configuration.registries`(部署会抹掉 → 拉不到 ACR 镜像) | `mcp-app.bicep` 用 `useAcrRegistry = contains(mcpImage,'azurecr.io')` 门控声明:ACR 镜像自动带 registry,公共占位镜像不带(冷部署不卡 AcrPull) | `08d2f10` |
| env `SANDBOX_DISTRIBUTED_LOCK`(部署会移除) | 提为 param,默认 `'0'`(对齐线上) | `08d2f10` |
| `audit-standalone.bicep`(一次性补丁) | 文件 **已删**(commit);两条部署记录 `audit-standalone` / `audit-standalone2` 另经 `az deployment group delete` 清除(纯元数据、不进 git) | `534dd09` |

> 加固后重跑 what-if 已确认:MCP app 的 delta 里 `registries` **不再**被移除,只剩纯噪音。

---

## 8. 冷部署差异(全新环境,附录)

冷部署时 AcrPull 角色要等 app 建好、`rbac` 模块跑完才有,而私有镜像在 app 创建时就要拉 →
**先用公共占位镜像 bootstrap**,再切私有镜像:

1. `az deployment sub create ... mcpImage=<默认公共占位> mcpClientSecret=''`
   —— 此时 `useAcrRegistry=false` → 不声明 registry,app 用公共镜像起来,`rbac` 授 AcrPull。
2. `az acr build` / push 真实 `mcp-server` + `mcp-sandbox` 镜像到 ACR。
3. 重跑 `az deployment sub create ... mcpImage=<ACR 镜像> mcpClientSecret=<reset 后的真实值>`
   —— 此时 `useAcrRegistry=true` → 自动声明 registry,AcrPull 已就绪,拉私有镜像成功。
4. `provisioning/aca/write-env.sh <deployment-name>` 生成 `.env.aca`;按提示把用户加进 AD 组、
   `az ad app update --identifier-uris api://<appId>`、`az containerapp secret set` 注入 OBO 密钥。

收敛部署(§4)直接就是"第 3 步"的稳态形态。

---

## 9. 回滚

部署是**增量幂等**,失败不删资源。若某次部署让 MCP app revision 异常:

```bash
az containerapp revision list -g dataops-aca-rg -n dataops-aca-mcp \
  --query "[].{name:name, active:properties.active, healthy:properties.healthState}" -o table
az containerapp revision activate -g dataops-aca-rg -n dataops-aca-mcp --revision <上一个 healthy revision>
```

OBO 密钥若被误覆盖:`az containerapp secret set -g dataops-aca-rg -n dataops-aca-mcp \
  --secrets mcp-client-secret=<真实值>`(真实值需从别处取回或 `az ad app credential reset` 重发)。

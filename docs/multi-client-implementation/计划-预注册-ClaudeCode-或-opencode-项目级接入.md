---
title: "实施篇：预注册共享 Client，项目级接入 Claude Code / opencode / VS Code"
date: 2026-07-05
tags:
  - plan
  - implementation
  - mcp
  - entra
  - oauth
sources:
  - "docs/MCP-自定义Client接入-Entra与各Agent客户端支持对比.md"
  - "provisioning/aca/modules/identity.bicep"
  - "provisioning/aca/main.bicep"
  - "https://learn.microsoft.com/en-us/entra/identity-platform/reply-url"
  - "https://code.claude.com/docs/en/mcp"
  - "https://opencode.ai/docs/config/"
  - "https://opencode.ai/docs/mcp-servers/"
  - "https://code.visualstudio.com/docs/copilot/chat/mcp-servers"
---

# 实施篇：预注册共享 Client，项目级接入

> **这是纯实施篇**——只放工程步骤和配置。**为什么这么做**（OAuth 时序、redirect URI、PKCE、pre-auth
> 与否、是否 RBAC、端口原理）全部在原理篇
> [`MCP-自定义Client接入-...md`](./MCP-自定义Client接入-Entra与各Agent客户端支持对比.md)，本文只在需要处给页内指针。
>
> 目标：让 **VS Code / Claude Code / opencode** 三个 client 都能连本项目 **Entra 保护的 MCP server**，
> 全部**项目级配置、互不冲突**，回调端口锁定。

**两个已定决策：**
1. **Claude Code 和 opencode 共用一个 public client**（同一个 `client_id`）。VS Code 用它自己的内置
   first-party client，无需注册。
2. 三个 client 各自的 mcp 配置放在**各自的文件**里（见 §0），天然不打架。

---

## 0. 三个 client 的配置文件位置（互不冲突）

| Client | 配置文件（项目级） | 顶层 key | 需要写 OAuth 吗 |
|---|---|---|---|
| **VS Code** | `.vscode/mcp.json` | `servers` | **否**——VS Code 是内置 pre-authorized client，**只写 URL** |
| **Claude Code** | `.mcp.json`（repo 根） | `mcpServers` | 是（`oauth.clientId` + `callbackPort`） |
| **opencode** | `opencode.json`（repo 根） | `mcp` | 是（`oauth.clientId` + `scope`） |

**为什么不冲突**：三个是**不同文件、不同顶层 key**，每个 client 只读自己那一个，互相看不见。
即使都在同一个 repo，也不会互相覆盖或误读。

> 常见误区纠正：
> - Claude Code 项目级 MCP 在 **`.mcp.json`（repo 根）**，不是 `.claude/`。`.claude/settings.json` 是放
>   **权限**（原理篇 §10）。
> - opencode 项目级配置在 **`opencode.json`（repo 根）**，不是 `.opencode/`（那目录放 agents/commands/
>   plugins 等）。
> - VS Code 的 MCP 在 **`.vscode/mcp.json`**（和 `settings.json` 分开）。

---

## 1. Entra：注册一个共享 public client（给 Claude Code + opencode 共用）

只建**一个** app registration，两个 CLI 共用：

1. 新建 App Registration，例如 `DataOps MCP – CLI Client (shared)`
2. **Authentication → Add a platform → Mobile and desktop applications**（public client），加 Redirect URI：
   - `http://localhost:8080/callback` —— 给 **Claude Code**（钉死 8080，见 §3）
   - opencode 的回调地址（**path 需实测**，见 §3 / §5 第 1 步）——加为**第二条** Redirect URI
3. 打开 **Allow public client flows**（`allowPublicClient = true`），**不创建 client secret**
4. **API permissions → Add → My APIs →** 本 MCP server（`{name}-mcp-server`）→ Delegated → 勾选
   **`user_impersonation`**
5. 记下该 app 的 **Application (client) ID = `<shared-cli-client-id>`**，填进 §2

> 一个 app registration 可以有多条 redirect URI，所以两个 CLI 共用没问题。
> 第 4 步的 API permission 加在**这个 client app** 上，**不动 MCP server 的 app registration**
> （能不能完全不改 server app、pre-auth 与否，见原理篇 §6）。

---

## 2. 三份项目级 mcp 配置

把 `<mcp-url>`、`<mcp-app-id>`、`<shared-cli-client-id>` 换成真值。

### 2.1 VS Code — `.vscode/mcp.json`（只写 URL）

```json
{
  "servers": {
    "dataops-mcp": {
      "type": "http",
      "url": "https://<mcp-url>/mcp"
    }
  }
}
```

VS Code 是内置 pre-authorized client，碰到 401 会自己走 OAuth，**不需要写 clientId / 端口**。

### 2.2 Claude Code — `.mcp.json`（repo 根）

```json
{
  "mcpServers": {
    "dataops-mcp": {
      "type": "http",
      "url": "https://<mcp-url>/mcp",
      "oauth": { "clientId": "<shared-cli-client-id>", "callbackPort": 8080 }
    }
  }
}
```

命令行等价（`--scope project` 写进 `.mcp.json`）：

```bash
claude mcp add --transport http --scope project \
  --client-id <shared-cli-client-id> \
  --callback-port 8080 \
  dataops-mcp https://<mcp-url>/mcp
```

**没有 secret**（public client）。登录：`/mcp` → Authenticate。

### 2.3 opencode — `opencode.json`（repo 根）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "dataops-mcp": {
      "type": "remote",
      "url": "https://<mcp-url>/mcp",
      "oauth": {
        "clientId": "<shared-cli-client-id>",
        "scope": "api://<mcp-app-id>/user_impersonation"
      }
    }
  }
}
```

**没有 `clientSecret`**（可选字段，public client 不填）。opencode **没有固定端口字段** → 端口策略见 §3。
登录：`opencode mcp auth dataops-mcp`。

---

## 3. 端口锁定：本项目的选择

> 原理（"两处 8080 是同一个 port"、端口被占怎么办、Entra 对 localhost 忽略端口只认 path）见原理篇 §3.4。

本项目采用：

- **Claude Code：钉死 8080。** 配 `--callback-port 8080`（或 `oauth.callbackPort: 8080`），Entra 注册
  `http://localhost:8080/callback`。两处一致、确定。
  - 若本机 8080 常被占，换一个冷门端口（如 `8765`），两处同步改。
- **opencode：不钉端口（它没有该字段），靠 Entra 的 localhost 端口无关匹配。**
  - **先实测抓 opencode 的 redirect path**（§5 第 1 步），把该 **path**（端口随便，Entra 忽略端口）注册到
    共享 client 的 Redirect URI，例如 `http://localhost:8080/<opencode-callback-path>`。
  - path **必须**和 opencode 实际用的一致（path 被严格匹配，端口不是）。

---

## 4. Bicep：pre-authorize 共享 client（推荐，免 consent 弹窗）

> 是否非改不可？不改也能 work（靠 consent），取舍见原理篇 §6.2 / §6.3。下面是"预授权、免弹窗"的落地改动。

`provisioning/aca/modules/identity.bicep`——加一个参数，并把共享 client 加进 `preAuthorizedApplications`：

```bicep
@description('Client ID of VS Code (well-known).')
param vscodeClientId string = 'aebc6443-996d-45c2-90f0-388ff96faa56'

@description('Client ID of the shared CLI public client (Claude Code + opencode). Empty = 不预授权。')
param cliClientId string = ''

// ... mcpServerApp.api 里：
    preAuthorizedApplications: [
      {
        appId: vscodeClientId
        delegatedPermissionIds: [ userImpersonationScopeId ]
      }
      ...(empty(cliClientId) ? [] : [
        {
          appId: cliClientId
          delegatedPermissionIds: [ userImpersonationScopeId ]
        }
      ])
    ]
```

`provisioning/aca/main.bicep`——透传参数：

```bicep
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: name
    cliClientId: cliClientId   // 从顶层 param 传入 §1 拿到的 <shared-cli-client-id>
  }
}
```

- 传了 `cliClientId` → Claude Code / opencode 首次连接**无 consent 弹窗**，和 VS Code 体验一致。
- 不传（留空）→ 走 consent（用户第一次点一次同意，或 admin consent 一次），**不改 server app 的定义**
  （原理篇 §6.3）。
- **OBO（闸③）不用动**（原理篇 §6.4）。

---

## 5. 验证 checklist

1. **抓 opencode 的 redirect_uri**：`opencode mcp auth dataops-mcp` 时，看浏览器打开的 `/authorize` URL
   里的 `redirect_uri=` 参数（拿到它的 **path** 和端口行为），把该 path 注册到共享 client（§1 第 2 步 / §3）。
2. **VS Code**：打开工作区 → MCP server 连接 → 首次触发登录 → 浏览器 Entra 登录 → 连上。
3. **Claude Code**：`/mcp` → Authenticate → 浏览器登录 → 落地页"登录成功" → 已连接。
4. **opencode**：`opencode mcp auth dataops-mcp` → 登录 → 连接；`opencode mcp list` 看 auth 状态。
5. **token 正确**（可选）：解码 access token，确认 `aud = api://<mcp-app-id>`、`scp` 含
   `user_impersonation`、`azp = <shared-cli-client-id>`。
6. **三者并存**：三个 client 各读各的文件（`.vscode/mcp.json` / `.mcp.json` / `opencode.json`），互不影响。
7. **（可选）action_bash 强制审批**：按原理篇 §10.3 在 `.claude/settings.json` 配 `ask` 规则。

---

## 6. 回滚

- 删项目根 `.mcp.json` / `opencode.json` 及 `.vscode/mcp.json` 里对应条目即断开。
- Entra：删共享 client app registration（或移除其 Redirect URI / API permission）。
- Bicep：把 `cliClientId` 留空（或移除该 param 与 preAuthorizedApplications 里那条）重新部署。
- 没动过 server app 定义 / OBO 的话，无其它清理。

---

## 参考资料

- [`MCP-自定义Client接入-...md`](./MCP-自定义Client接入-Entra与各Agent客户端支持对比.md) —— **原理篇**（时序图 §3、PKCE §4、发 code/RBAC §5、pre-auth §6、端口 §3.4）
- `provisioning/aca/modules/identity.bicep` / `provisioning/aca/main.bicep`
- [Microsoft – Redirect URI (reply URL) best practices（localhost 端口/path 匹配）](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)
- [Claude Code – Connect to MCP（`--client-id` / `--callback-port` / `.mcp.json`）](https://code.claude.com/docs/en/mcp)
- [opencode – Config（`opencode.json` 位置）](https://opencode.ai/docs/config/) / [MCP servers](https://opencode.ai/docs/mcp-servers/)
- [VS Code – MCP servers（`.vscode/mcp.json`）](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)

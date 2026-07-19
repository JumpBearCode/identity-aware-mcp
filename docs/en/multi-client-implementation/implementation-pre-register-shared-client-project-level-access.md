---
title: "Implementation: Pre-register Shared Client, Project-level Access for Claude Code / opencode / VS Code"
date: 2026-07-05
tags:
  - plan
  - implementation
  - mcp
  - entra
  - oauth
sources:
  - "docs/en/multi-client-implementation/connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md"
  - "provisioning/aca/modules/identity.bicep"
  - "provisioning/aca/main.bicep"
  - "https://learn.microsoft.com/en-us/entra/identity-platform/reply-url"
  - "https://code.claude.com/docs/en/mcp"
  - "https://opencode.ai/docs/config/"
  - "https://opencode.ai/docs/mcp-servers/"
  - "https://code.visualstudio.com/docs/copilot/chat/mcp-servers"
---

# Implementation: Pre-register Shared Client, Project-level Access

> **This is a pure implementation guide**—only engineering steps and configuration. **The rationale** (OAuth sequence, redirect URI, PKCE, pre-auth
> or not, RBAC or not, port principles) is all in the principles guide
> [`connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md`](./connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md). This document only provides in-page pointers where needed.
>
> Goal: Enable **VS Code / Claude Code / opencode** all to connect to this project's **Entra-protected MCP server**,
> all with **project-level configuration, no conflicts**, callback port locked down.

**Two established decisions:**
1. **Claude Code and opencode share a single public client** (same `client_id`). VS Code uses its own built-in
   first-party client, no registration needed.
2. The MCP configurations for the three clients are placed in **their respective files** (see §0), naturally avoiding conflicts.

---

## 0. Configuration File Locations for the Three Clients (No Conflicts)

| Client | Config File (Project-level) | Top-level Key | Need to Write OAuth? |
|---|---|---|---|
| **VS Code** | `.vscode/mcp.json` | `servers` | **No**—VS Code is a built-in pre-authorized client, **only write the URL** |
| **Claude Code** | `.mcp.json` (repo root) | `mcpServers` | Yes (`oauth.clientId` + `callbackPort`) |
| **opencode** | `opencode.json` (repo root) | `mcp` | Yes (`oauth.clientId` + `scope`) |

**Why no conflicts**: The three are **different files, different top-level keys**. Each client only reads its own, invisible to the others.
Even within the same repo, they won't overwrite or misread each other.

> Common misconception corrections:
> - Claude Code project-level MCP is in **`.mcp.json` (repo root)**, not `.claude/`. `.claude/settings.json` is for
>   **permissions** (Principles Guide §10).
> - opencode project-level configuration is in **`opencode.json` (repo root)**, not `.opencode/` (that directory holds agents/commands/
>   plugins, etc.).
> - VS Code's MCP is in **`.vscode/mcp.json`** (separate from `settings.json`).

---

## 1. Entra: Register a Shared Public Client (for Claude Code + opencode)

Create only **one** app registration, shared by the two CLIs:

1. Create a new App Registration, e.g., `DataOps MCP – CLI Client (shared)`
2. **Authentication → Add a platform → Mobile and desktop applications** (public client), add Redirect URI:
   - `http://localhost:8080/callback` — for **Claude Code** (locked to 8080, see §3)
   - opencode's callback address (**path needs actual testing**, see §3 / §5 step 1)—add as a **second** Redirect URI
3. Enable **Allow public client flows** (`allowPublicClient = true`), **do not create a client secret**
4. **API permissions → Add → My APIs →** this MCP server (`{name}-mcp-server`)→ Delegated → check
   **`user_impersonation`**
5. Note down the app's **Application (client) ID = `<shared-cli-client-id>`**, fill into §2

> One app registration can have multiple redirect URIs, so sharing between two CLIs is fine.
> Step 4's API permission is added on **this client app**, **do not modify the MCP server's app registration**
> (whether the server app can be left completely unchanged, pre-auth or not, see Principles Guide §6).

---

## 2. Three Project-level MCP Configurations

Replace `<mcp-url>`, `<mcp-app-id>`, `<shared-cli-client-id>` with actual values.

### 2.1 VS Code — `.vscode/mcp.json` (Write URL Only)

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

VS Code is a built-in pre-authorized client. When encountering a 401, it will perform OAuth automatically, **no need to write clientId / port**.

### 2.2 Claude Code — `.mcp.json` (repo root)

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

Command-line equivalent (`--scope project` written into `.mcp.json`):

```bash
claude mcp add --transport http --scope project \
  --client-id <shared-cli-client-id> \
  --callback-port 8080 \
  dataops-mcp https://<mcp-url>/mcp
```

**No secret** (public client). Login: `/mcp` → Authenticate.

### 2.3 opencode — `opencode.json` (repo root)

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

**No `clientSecret`** (optional field, leave empty for public client). opencode **has no fixed port field** → port strategy see §3.
Login: `opencode mcp auth dataops-mcp`.

---

## 3. Port Lockdown: This Project's Choice

> Principles ("the two 8080s are the same port", what if port is occupied, Entra ignores port for localhost only matches path) see Principles Guide §3.4.

This project adopts:

- **Claude Code: Locked to 8080.** Configure `--callback-port 8080` (or `oauth.callbackPort: 8080`), Entra registers
  `http://localhost:8080/callback`. Both are consistent and confirmed.
  - If localhost 8080 is frequently occupied, switch to a less common port (e.g., `8765`), synchronize changes in both places.
- **opencode: Do not lock port (it has no such field), rely on Entra's localhost port-agnostic matching.**
  - **First, test to capture opencode's redirect path** (§5 step 1), register that **path** (any port, Entra ignores port) into the
    shared client's Redirect URI, e.g., `http://localhost:8080/<opencode-callback-path>`.
  - The path **must** match what opencode actually uses (path is strictly matched, port is not).

---

## 4. Bicep: Pre-authorize Shared Client (Recommended, Eliminates Consent Prompt)

> Is modification mandatory? It works without modification (via consent), trade-offs see Principles Guide §6.2 / §6.3. Below is the implementation change for "pre-authorization, no prompt".

`provisioning/aca/modules/identity.bicep`—add a parameter, and add the shared client to `preAuthorizedApplications`:

```bicep
@description('Client ID of VS Code (well-known).')
param vscodeClientId string = 'aebc6443-996d-45c2-90f0-388ff96faa56'

@description('Client ID of the shared CLI public client (Claude Code + opencode). Empty = no pre-authorization.')
param cliClientId string = ''

// ... inside mcpServerApp.api:
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

`provisioning/aca/main.bicep`—pass through parameter:

```bicep
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    name: name
    cliClientId: cliClientId   // Pass in <shared-cli-client-id> obtained from §1 via top-level param
  }
}
```

- If `cliClientId` is provided → Claude Code / opencode first connection has **no consent prompt**, same experience as VS Code.
- If left empty → goes through consent (user clicks agree once on first use, or admin consent once), **does not change the server app definition**
  (Principles Guide §6.3).
- **OBO (Gate ③) does not need modification** (Principles Guide §6.4).

---

## 5. Verification Checklist

1. **Capture opencode's redirect_uri**: When running `opencode mcp auth dataops-mcp`, look at the `/authorize` URL opened in the browser
   for the `redirect_uri=` parameter (get its **path** and port behavior), register that path to the shared client (§1 step 2 / §3).
2. **VS Code**: Open workspace → MCP server connection → First trigger login → Browser Entra login → Connected.
3. **Claude Code**: `/mcp` → Authenticate → Browser login → Landing page "Login successful" → Connected.
4. **opencode**: `opencode mcp auth dataops-mcp` → Login → Connected; `opencode mcp list` check auth status.
5. **Token correctness** (optional): Decode access token, confirm `aud = api://<mcp-app-id>`, `scp` contains
   `user_impersonation`, `azp = <shared-cli-client-id>`.
6. **All three coexist**: Three clients each read their own files (`.vscode/mcp.json` / `.mcp.json` / `opencode.json`), no interference.
7. **(Optional) action_bash mandatory approval**: Configure `ask` rules in `.claude/settings.json` per Principles Guide §10.3.

---

## 6. Rollback

- Delete the corresponding entries in project root `.mcp.json` / `opencode.json` and `.vscode/mcp.json` to disconnect.
- Entra: Delete the shared client app registration (or remove its Redirect URI / API permission).
- Bicep: Leave `cliClientId` empty (or remove that param and the entry in preAuthorizedApplications) and redeploy.
- If the server app definition / OBO was never modified, no other cleanup is needed.

---

## References

- [`connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md`](./connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md) —— **Principles Guide** (Sequence Diagram §3, PKCE §4, Issuing Code/RBAC §5, Pre-auth §6, Port §3.4)
- `provisioning/aca/modules/identity.bicep` / `provisioning/aca/main.bicep`
- [Microsoft – Redirect URI (reply URL) best practices (localhost port/path matching)](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)
- [Claude Code – Connect to MCP (`--client-id` / `--callback-port` / `.mcp.json`)](https://code.claude.com/docs/en/mcp)
- [opencode – Config (`opencode.json` location)](https://opencode.ai/docs/config/) / [MCP servers](https://opencode.ai/docs/mcp-servers/)
- [VS Code – MCP servers (`.vscode/mcp.json`)](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)
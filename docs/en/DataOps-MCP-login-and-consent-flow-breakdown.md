---
title: "Identity-Aware DataOps MCP: Login and Consent Flow Breakdown"
date: 2026-06-04
tags:
  - mcp
  - entra
  - oauth
  - obo
sources:
  - "azure-dataops-mcp/mcp-server/main.py"
  - "azure-dataops-mcp/provisioning/python/provision.py"
---

# DataOps MCP: Login and Consent Flow Breakdown

## One-Sentence Summary

When connecting from VS Code / GitHub Copilot to this MCP server, the entire chain involves
**1 login (authentication) + 2 consent gates (delegated authorization)**; only one of these will actually be presented to the user as a popup,
the other is completed silently in the background — if you **pre-provision** both gates, the end user is left with just "login once."

> Common misconception: thinking of it as "three popups." The three items are different in nature, and one of them is not a popup at all.
> The breakdown follows.

---

## 0. Background: Authentication Characteristics of This Architecture

Three containers, each with its own identity (see `azure-dataops-mcp/README.md` for details):

- **mcp-server**: A protected OAuth resource. Exposes the delegated scope `api://<MCP_APP_ID>/user_impersonation`;
  validates incoming Entra JWTs; then uses **OBO** to query Graph group membership on behalf of the user.
  Itself has **no Azure data plane permissions**.
- **diagnose-worker / action-worker**: Each holds a Service Principal (client secret),
  runs `az` with its own identity; their permission boundaries are defined by their respective RBAC.

This document only covers the **user → mcp-server** authentication chain (the workers use SP + RBAC and do not involve user login).

---

## 1. Complete Flow: Who Does What

| Step | Action | Performed By |
|---|---|---|
| 1 | VS Code requests `GET /mcp` → receives **401**, response header includes `WWW-Authenticate: Bearer ... resource_metadata=".../.well-known/oauth-protected-resource/mcp"` | **Server** sends 401 + pointer |
| 2 | VS Code reads the pointer, fetches protected-resource metadata → learns **authorization server = Entra tenant**, required scope = `api://<MCP_APP_ID>/user_impersonation` | **Client** reads / **Server** provides document |
| 3 | VS Code then fetches authorization-server metadata from Entra (`.well-known/openid-configuration`) → obtains authorize / token endpoints | **Client** ↔ **Entra** |
| 4 | VS Code opens a browser, navigates to the Entra login page (**Authorization Code + PKCE**), user logs in | **Client** initiates / **User** logs in |
| 5 | Login succeeds → Entra redirects with authorization code back to VS Code's local callback → "**Login successful, you can close this page**" appears | That landing page is a local redirect handler started by the **Client** |
| 6 | VS Code exchanges the code + PKCE verifier at the token endpoint for an **access token** (audience = `api://<MCP_APP_ID>`) | **Client** ↔ **Entra** |
| 7 | VS Code resends `/mcp` with `Authorization: Bearer <token>` → 200, connected | **Client** |
| 8 | Token is stored in the OS keychain; subsequently refreshed **silently** using the refresh token | **Client** |

**Key point: 401 handling, metadata discovery, PKCE, local landing page, token caching/refresh, attaching Bearer on every request
— all of this is implemented by VS Code (MCP client + authentication provider), following the MCP Authorization specification.**
Any compliant MCP client (Claude, etc.) seeing this 401 can complete the flow in the same way.

On the server side, you only need to do three things (FastMCP already handles this, see `main.py`):

1. Return **401 + `WWW-Authenticate` (with resource_metadata pointer)** when no token is present
2. Serve the **`/.well-known/oauth-protected-resource/mcp`** metadata document
3. **Validate the incoming JWT** (audience, signature, scope) — `AzureJWTVerifier` + `RemoteAuthProvider`

---

## 2. Core Model: 1 Login + 2 Consent Gates

| | What It Is | What the User Sees | Who Eliminates It | What Happens If Not Eliminated |
|---|---|---|---|---|
| **① Login** | **Authentication / authN** ("Who are you") | Entra login page (select account / enter password) | Cannot be eliminated (unless reusing an existing session) | — Required by nature |
| **② VS Code → Your MCP API** | **Delegated consent / authZ** | **Consent page in the browser** (Accept/Cancel) | `preAuthorizedApplications` (pre-authorize the VS Code client) | Consent popup appears; if tenant disables user consent → stuck at "Needs admin approval" |
| **③ MCP server → Graph (OBO)** | **Delegated consent / authZ** | **Not a popup! Back-channel** | `grant_obo_admin_consent` (AllPrincipals admin consent) | **Server-side error** (`invalid_grant` / `interaction_required`), user sees no popup, flow fails directly |

### Why ③ Is Not a Popup (Easiest Point to Get Wrong)

Among ①②③, **only ②** will actually present a consent popup to the user in the browser.

- **①** is a login page, which is authentication, not "consent."
- **③ OBO is the server exchanging the user's token for a Graph token in the background**; there is no UI and no way to present a popup to the user.
  So it either **has been consented to by an admin beforehand** (tenant-wide grant) → succeeds silently;
  or it hasn't → **backend error**, not "a popup for the user to click."

### Why the Elimination Methods Differ for ② and ③

Both are essentially "delegated consent," but:

- **②** is initiated by an **interactive client** (VS Code); Entra can insert a consent page in the browser flow
  → theoretically, "the user could click it"; `preAuthorizedApplications` just **skips** this page.
- **③** is a **non-interactive back-channel OBO** with no UI → it can only rely on **admin consent given in advance**.
  This is precisely why ③ **must** be resolved beforehand and cannot be "left for the user to click on the spot" — there is simply no spot to click.

---

## 3. Gate ②: `preAuthorizedApplications` (Pre-authorize VS Code)

```python
# provision.py —— written on the MCP server's own API app
api=ApiApplication(
    oauth2_permission_scopes=[ PermissionScope(value="user_impersonation", ...) ],
    pre_authorized_applications=[
        PreAuthorizedApplication(
            app_id=VSCODE_CLIENT_ID,            # aebc6443-996d-45c2-90f0-388ff96faa56
            delegated_permission_ids=[scope_id],
        )
    ],
),
```

**This does not change the permissions of the MCP server SP itself; it controls whether "VS Code obtaining a token for your API" requires consent.**

- **With it**: You (the API owner) pre-consent on behalf of this trusted client → Entra issues the token directly, **zero consent**,
  and no per-user consent record is needed.
- **Without it**: The token flow can still technically proceed, but VS Code will show an additional consent popup the first time it requests this scope:
  > "**VS Code** wants to access **DataOps MCP Server** on your behalf (user_impersonation). Do you consent?"

  After that, the outcome depends on the tenant's **user consent settings**:

  | Tenant Setting | Result Without Pre-auth |
  |---|---|
  | User self-service consent allowed | User clicks "Consent" once → a per-user grant is recorded → no more popups afterward. Just one extra step the first time. |
  | **User consent disabled** (many enterprise tenants) | User **cannot click**, shows "Needs admin approval" → **stuck**, requires admin intervention. |

> You **can pre-authorize VS Code** because `preAuthorizedApplications` is written on **your own API app**;
> you don't need to modify VS Code's app registration (which you cannot modify anyway).
>
> Entra **does not support Dynamic Client Registration (DCR)**, so VS Code cannot register a client on the fly;
> it must use its fixed first-party client id `aebc6443-…` — this is the root reason why pre-authorizing this id is necessary.

---

## 4. Gate ③: OBO Admin Consent (Pre-consent on Behalf of All Users)

```python
# provision.py —— creates a tenant-wide delegated grant from the MCP server SP to Graph
async def grant_obo_admin_consent(graph, server_sp_id):
    graph_sp = await graph.service_principals_with_app_id(GRAPH_APP_ID).get()
    await graph.oauth2_permission_grants.post(
        OAuth2PermissionGrant(
            client_id=server_sp_id,
            consent_type="AllPrincipals",                  # applies to all users
            resource_id=graph_sp.id,
            scope="User.Read email offline_access openid profile",
        )
    )
```

Purpose of OBO: The token provided by VS Code is **issued to the MCP server** (audience `api://<mcp>`);
the server **cannot use it directly to call Graph** (wrong audience). OBO performs this exchange —
the server uses the incoming user token to obtain a Graph token that **still carries the user's identity**, thereby reading the user's group memberships
(as in `main.py`'s `_user_groups_via_obo`) to determine which tools the user can invoke.

- **With pre-provisioned admin consent (AllPrincipals)**: OBO succeeds **silently** for all users, with no prompts along the way.
- **Without pre-provisioned consent**: The OBO step **fails directly** in the background (`interaction_required` / `consent_required`),
  manifesting as a server error, not a popup for the user.

> These scopes (`User.Read email offline_access openid profile`) are **low-privilege and user-consentable**,
> so granting this admin consent only requires the **Application Administrator** role;
> only high-privilege Graph scopes would require Privileged Role Administrator / Global Admin.

---

## 5. End User Experience

With both ② and ③ pre-provisioned, the only thing the user actually experiences is **① Login**:

> Select an account / log in once → **done**. **No Entra consent popups** in between; OBO succeeds silently.

Reopening VS Code later will **not prompt again**: the authentication session (including the refresh token) is stored in the system keychain
and is automatically renewed. A new login is only required when the token expires / the user actively logs out / an admin revokes it / the password changes / a Conditional Access policy is triggered.

### Note: There Are Also "Non-Entra" VS Code Local Popups

The "Allow…" popup you see when starting the server for the first time might be a **VS Code client's own** confirmation
(e.g., "Do you want to allow this workspace to run the MCP server?" / "Allow it to use a certain account?").
This is a **VS Code local UI trust / authorization**, **not** an Entra consent, and is different from ②③:

- "Allow the MCP server in this workspace to run": **Remembered per workspace**; once chosen, the folder won't ask again
  (unless the configuration changes or trust is reset).
- "Allow it to use a certain account": The authentication session is cached in the keychain; reopening **reuses it silently**.

---

## 6. Quick Reference: "With / Without" Comparison for Each Gate

| Configuration Item | Where It's Written | Effect | Consequence Without It |
|---|---|---|---|
| `preAuthorizedApplications` (VS Code) | MCP server's **API app** | Eliminates the consent popup for "client → your API" | User sees an extra consent on first use; if tenant disables user consent, it gets stuck at "Needs admin approval" |
| `grant_obo_admin_consent` (AllPrincipals) | MCP server **SP → Graph** oauth2 grant | Eliminates consent for all users' OBO → Graph | OBO fails with a backend error (no popup), group query fails → tool authorization breaks |

In short: **The only Entra consent that actually pops up for the user is ②; ③ is back-channel, no popup, only success or failure;
① is login, not consent. Pre-provision both ② and ③, and the user is left with just the login step.**
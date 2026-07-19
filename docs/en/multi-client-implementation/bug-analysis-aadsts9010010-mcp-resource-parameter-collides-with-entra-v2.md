---
title: "Bug Analysis: AADSTS9010010 — MCP's resource Parameter Collides with Entra v2"
date: 2026-07-06
tags:
  - bug
  - mcp
  - entra
  - oauth
  - rfc8707
  - aadsts9010010
sources:
  - "docs/en/multi-client-implementation/connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md"
  - "docs/en/Entra OAuth Proxy vs Pre-registration MCP.md"
  - "https://www.groff.dev/blog/azure-entra-id-mcp-server-authentication-incompatibilities"
  - "https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim"
  - "https://github.com/anthropics/claude-code/issues/55993"
  - "https://github.com/anthropics/claude-code/issues/52871"
  - "https://github.com/PrefectHQ/fastmcp/issues/1846"
  - "https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614"
  - "https://www.rfc-editor.org/rfc/rfc8707.html"
  - "https://gofastmcp.com/integrations/azure"
---

# Bug Analysis: AADSTS9010010 — MCP's `resource` Parameter Collides with Entra v2

> When using **Claude Code / opencode** to connect to this project (an Entra-protected remote MCP) via OAuth, the following error occurs:
>
> ```
> AADSTS9010010: The resource parameter provided in the request doesn't match with the requested scopes.
> ```
>
> This article focuses on **how this bug arises**: what `scope` and `resource` actually are, who triggers it, whose fault it is, whether all Entra OAuth MCPs are broken, and why "downgrading FastMCP" cannot circumvent it. For architectural details of the fix, see
> [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md).

---

## 0. One-Sentence Summary

> **This is not a configuration error, nor a missing pre-authorization. It is a protocol-level incompatibility between the MCP specification (which mandates that clients include the RFC 8707 `resource` parameter) and the Microsoft Entra v2 endpoint (which mandates that `resource` must match `scope`, but the MCP's URL cannot match).** After Entra enforced this validation in March 2026, such requests are uniformly rejected. The impact is broad: Azure DevOps remote MCP, Power BI MCP, Fabric, IBM mcp-context-forge, etc., all hit the same landmine.

---

## 1. First, Clarify: What Exactly Are `scope` and `resource`?

Both answer the question "who is this token for and what can it do," but they come from **two different generations** of OAuth and live in **two different namespaces**:

| | `scope` (OAuth 2.0 Native) | `resource` (RFC 8707, v1 Legacy Revived) |
|---|---|---|
| Origin | OAuth 2.0 / OIDC, Entra **v2 primary mechanism** | OAuth v1's `resource`, revived in the v2 era by **RFC 8707** (Resource Indicators, an **IETF OAuth extension, not an MCP invention**) |
| Request Parameter or Token Claim | **Request parameter** (sent to `/authorize`, `/token`) | **Request parameter** (sent to `/authorize`, `/token`) |
| What it expresses | A single string that **simultaneously** encodes "target + permission" | **Only** names a "target API's audience," without permissions |
| How the target is identified | Entra's **App ID URI** (`api://<guid>` or `api://<verified domain>`), must be **registered** | The resource's **canonical URL** (e.g., `https://…/mcp`）——MCP), used as an IdP-agnostic audience |
| How Entra v2 handles it | **Primary**: derives the token's `aud` from the scope prefix; the `.default` form `api://<appid>/.default` | **Accepts and validates**: per RFC 8707, requires `resource` to match the scope owner; MCP's URL cannot match the `api://` scope → rejected (AADSTS9010010) |

**Why does the MCP spec insist that clients send `resource`?** Security considerations (RFC 8707): it **nails** the token's audience to "the specific MCP server you are connecting to," preventing a token issued for API-A from being misused for API-B (token replay / confused deputy). Therefore, the MCP Authorization specification (revised June 2025) stipulates: **the client MUST include `resource` = the canonical URI of the MCP server in both `/authorize` and `/token` requests**. The intention is good; it just collided with Entra v2.

> More troublesome: the MCP spec also assumes the IdP supports RFC 8414 (AS Metadata), RFC 7591 (DCR), and RFC 8707 (Resource Indicators) — and **Entra v2 has not implemented any of these three in the way MCP requires**. This bug is the RFC 8707 piece.

### 1.1 What `scope` and `resource` Look Like in This Project

**First, remember this key — `resource` and `scope` are two orthogonal dimensions and should never be equal:**

| | Question Answered | Value in a Clean Model |
|---|---|---|
| **`resource`** | **Who** (which server = audience) | Server URL, e.g., `https://…/mcp` |
| **`scope`** | **What** (which permission) | Pure permission, e.g., `user_impersonation` (**without target**) |

On an AS that follows RFC 8707 (Okta / Keycloak), these two handle separate concerns and never conflict. In this project, they collide on **Entra** — because Entra encodes "who" into the scope (`api://<appid>/…`), so "who" is stated twice, using two different identifiers:

| | Actual Value in This Project |
|---|---|
| **scope** (request parameter) | `api://88de6a37-cf75-40d3-83e8-44c5ccbc0895/user_impersonation` |
| ↳ Target (App ID URI) | `api://88de6a37-cf75-40d3-83e8-44c5ccbc0895` — the MCP server app's identifierUri |
| ↳ Permission | `user_impersonation` |
| **resource** (request parameter, RFC 8707) | `https://dataops-aca-mcp.icyrock-96f978c0.westus2.azurecontainerapps.io/mcp` — the MCP server's **deployment URL** |

The crux is immediately visible: **for the same MCP server, scope identifies it using `api://88de6a37…`, while resource identifies it using `https://…/mcp` — two parameters, two completely different strings.** Entra v2 uses the scope's set (`api://…`) as the `aud`, then sees a resource it doesn't recognize, which is yet another string → `AADSTS9010010`.

> **Two points to clarify (to avoid confusion and unfair blame on Entra):**
> 1. **Don't get the direction wrong: it is Entra that encodes the "target" into the scope; other ASes separate `resource` from `scope`** — not the other way around. In other IdPs (Okta/Auth0), the scope contains only permissions (`user_impersonation`), and the target is given by a separate `resource`/`audience` (URL); Entra's v2 scope natively carries the target (`api://<appid>`) and does not accept an independent `resource`.
> 2. **But Entra is not violating OAuth by doing this**: `resource` (RFC 8707) is only an **optional extension** to OAuth; the core (RFC 6749) never requires its implementation. It is **MCP that made this optional feature mandatory (MUST)**, forcing the conflict.
>
> For the appearance and design philosophy of `resource`/`scope` in other ASes (Okta / Auth0) → **§1.5**; for the complete "whose fault" hierarchy → **§4**.

### 1.2 If the Target Is Already in `scope`, Why Is `resource` Needed?

Your intuition is correct — **what `resource` expresses is indeed also the audience, the same kind of thing as the "target" in `scope`.** The crux is not "redundancy," but that **the two are identifiers from different worlds, and MCP and Entra each recognize only one**:

- **The MCP specification is IdP-agnostic.** It does not want to depend on Entra's `api://<guid>` scope convention. RFC 8707 gives it a **universal** anchor: regardless of whether the backend is Entra, Okta, or Auth0, use "**this server's URL**" as the audience — `resource = https://…/mcp`. This way, the anti-replay rule "this token can only be used on this server" holds independently of the IdP.
- **Entra does not use a URL as the audience; it uses its own registered App ID URI.** It derives the `aud` from the `api://88de6a37…` prefix of the scope, ignoring the resource entirely.
- Thus, MCP wants to use `https://…/mcp` to nail the audience, while Entra wants to use `api://88de6a37…` to nail the audience — **two locks pointing at the same door, but the keys are incompatible**; and Entra simply refuses to accept the `resource` "key."

In one sentence: **It's not "scope already has a target, so why also send resource," but rather "MCP only trusts URL-form audiences (resource), while Entra only recognizes `api://`-form audiences (scope prefix); the two don't match, and Entra rejects resource."**

### 1.3 Then Why Can't the MCP's URL Just Be Written into the `scope`?

Because **Entra's scope must be attached to a registered App ID URI**, and this HTTPS deployment URL **cannot be registered as an App ID URI**:

- Entra's identifierUri only accepts forms like `api://<guid>`, `api://<your verified domain>`, `https://<your verified domain>`; a random `*.azurecontainerapps.io/...` **is not a domain you have verified**, so Entra won't allow it as an identifierUri.
- Even if you have a custom verified domain, adding a path like `/mcp` to the App ID URI is awkward; Entra does not follow URL paths when deriving the `aud`.

Therefore, the `resource` value `https://…/mcp` **inherently cannot enter the scope**; it can only exist as an independent `resource` parameter — and Entra v2 does not accept this parameter. That is the deadlock. **This is also why "fixing it on the Entra side" is not feasible** (see §7).

### 1.4 `aud` / `scp` / `azp`…: What's Actually Inside the Token

The `scope` and `resource` discussed earlier are both **request parameters** (sent by the client to Entra); their "results" land in the **claims** of the **access token (JWT)** issued by Entra. Distinguishing "request parameters" from "token claims" is key to understanding the whole picture:

| During Request (client → Entra) | Claim Landed in Token |
|---|---|
| `scope=api://88de6a37…/user_impersonation` | `aud` (target) + `scp` (permission) |
| `resource=https://…/mcp`（RFC (RFC 8707 intends this to set `aud`) | —— (Entra v2 rejects outright, produces no claim) |

A **normal** access token for this project (obtainable via the VS Code path) roughly looks like this when decoded:

```jsonc
{
  "aud": "api://88de6a37-cf75-40d3-83e8-44c5ccbc0895", // Who can use it: MCP server's App ID URI
  "iss": "https://login.microsoftonline.com/9ea91fbb-.../v2.0", // Issuer = your tenant
  "azp": "49af5fc1-96e6-40c1-b108-cb828cc2a00e",  // Which client requested it (CLI client / VS Code differ)
  "scp": "user_impersonation",                     // Granted delegated permissions (space-separated)
  "oid": "<your user object ID>",                  // Who you are (stable within tenant)
  "tid": "9ea91fbb-...",                           // Tenant
  "preferred_username": "you@contoso.com",
  "exp": 1730000000, "iat": 1729996400, "nbf": 1729996400 // Expiry / Issued At / Not Before
}
```

Responsibilities of each claim:

- **`aud` (audience, "who this token is for")**: The identity of the resource server. In this project = `api://88de6a37…` (in some configurations, the bare appId `88de6a37…`; FastMCP's verifier validates against the actual issued value). **The first thing `AzureJWTVerifier` does upon receiving a token is verify that `aud` equals its own App ID URI**; if not, it rejects — this is precisely to prevent "using a token issued for another API." And **the entire mission of the `resource` parameter is "please set `aud` to the value I provide"**: on an IdP supporting RFC 8707, `resource=X` → `aud=X`; Entra doesn't play this game, `aud` is always derived from the `api://` prefix of the scope.
- **`scp` (scope, "what this token can do")**: List of delegated permissions, space-separated. In this project = `user_impersonation`. **The `scope` in the request and the `scp` in the token are a pair**: the former is "what I want," the latter is "what was actually granted"; the server verifies that `scp` contains the required permission.
- **`azp` / `appid` (authorized party, "which client came to ask")**: VS Code = `aebc6443…`, this project's CLI client = `49af5fc1…`. This is the only field that changes when "switching clients," but it **does not affect `aud`/`scp`** — so the server-side validation logic is indifferent to client changes.
- **`oid` / `sub` (who you are)**: User identity. Later, the server uses `oid` + OBO to query your AD groups and determine tool visibility — that is the step most resembling "RBAC" (see Principles §5).
- **`iss` / `tid` (who signed, which tenant)**, **`exp` / `iat` / `nbf` (validity period)**: Signature, issuer, and expiry are all validated by the server.

**Back to the bug**: `resource` is a **request parameter** whose purpose is to set the token's **`aud` claim**; Entra v2 neither accepts this parameter nor has already set `aud` using the scope → "the resource you gave and the aud derived from scope are not the same thing" → `AADSTS9010010`. **The problem is entirely in the "pre-token-issuance request phase"; the token itself (aud/scp) is actually fine.**

---

### 1.5 What If We Switch AS? Two Audience Design Philosophies

The conflict above is rooted in **two design philosophies about what an "audience" is**:

- **Entra = Identity-first (identity-as-audience)**: Entra is a directory/IAM system; an API = an **App Registration**. audience = `api://<appid>` (a **registered identity**). Advantage: the URL can change arbitrarily (blue-green, multi-region, domain change), but the **identity `appId` remains constant**; the audience points to a **governable object** with scopes/owner/consent; APIs without public URLs can still have an identity. Cost: the resource must be **pre-registered in the directory** (and domain verified). **Scope is therefore written as `api://<appid>/user_impersonation` — identity and permission are fused together.**
- **RFC 8707 = Location-first (URL-as-audience)**: The Web/OAuth ecosystem treats a resource as "something at a certain URL"; audience = server URL. Advantage: **any URL can be an audience with zero registration** (exactly what MCP wants: anyone can spin up a server anywhere); the audience is the address, directly discoverable via `.well-known`. Cost: URLs can drift, there is no governance object, and "anyone can claim a URL" requires other mechanisms to prevent abuse. **Scope is therefore a pure permission (e.g., `mcp.access`), and the audience is given separately via `resource`.**

Which is better? **Each optimizes for different goals; there is no absolute winner.** MCP (decentralized, URL-native, zero-registration) and Entra (governed, directory-centric, identity-native) are at opposite poles. **This is a philosophical conflict, not a coding error by anyone.** This is also why Entra refuses to treat a bare URL as an audience — in its worldview, the audience must be a registered identity; otherwise, "anyone could get a token for any URL," breaking its governance model (this is also the security boundary that the `resource`↔`scope` validation aims to protect, see §10).

Placing the same request ("access MCP server as a user") side-by-side on different ASes:

| AS | `scope` | How audience is given | Resulting token |
|---|---|---|---|
| **RFC 8707 Native** (Keycloak, Okta after support) | Pure permission `mcp.access` | Independent `resource=https://mcp.example.com/mcp` | `aud=https://mcp.example.com/mcp`，`scp=mcp.access` |
| **Auth0** | Pure permission `read:data` | Independent, but parameter is called `audience=` (**not** `resource`) | `aud=https://…`，`scope=read:data` |
| **Entra v2** | Fused `api://88de6a37…/user_impersonation` | **No independent parameter** — identity hidden in scope prefix | `aud=api://88de6a37…`, `scp=user_impersonation` |

```text
# Okta / RFC 8707 Native —— The two parameters Claude sends exactly match the model, works
resource = https://mcp.example.com/mcp     ← Who
scope    = mcp.access                        ← What
# Entra —— Identity stuffed into scope, no place for independent resource; Claude adds resource=URL → two identities clash → AADSTS9010010
```

> **To be fair**: it's not "only Entra is bad." **Auth0 also cannot connect to MCP by default** — it uses `audience` and doesn't recognize `resource`; you need to specifically enable the [Resource Parameter Compatibility Profile](https://auth0.com/ai/docs/mcp/guides/resource-param-compatibility-profile). There's even an issue in the Microsoft VS Code repository about [using Auth0 as an MCP AS not working](https://github.com/microsoft/vscode/issues/274226). What truly works out of the box is an **RFC 8707 native** AS. Ranking: **8707 Native ✅ > Auth0 (needs switch) ⚠️ > Entra (hard error) ❌**.

---

## 2. What Claude Code Actually Sent → Why It Errors

This project's server protected-resource metadata (`/.well-known/oauth-protected-resource/mcp`) advertises:

```json
{
  "resource": "https://dataops-aca-mcp.../mcp",
  "authorization_servers": ["https://login.microsoftonline.com/<tenant>/v2.0"],
  "scopes_supported": ["api://88de6a37-.../user_impersonation"]
}
```

Following the MCP spec, Claude Code sends **both** of these to the **Entra v2** endpoint **simultaneously**:

```text
scope    = api://88de6a37-.../user_impersonation     ← Target is api://88de6a37...
resource = https://dataops-aca-mcp.../mcp            ← Target is https://.../mcp（RFC (RFC 8707)
```

Entra v2 sees: **`resource` (`https://.../mcp`) and the target of `scope` (`api://88de6a37...`) are not the same thing at all** → `AADSTS9010010`.

Moreover, the URL `https://dataops-aca-mcp....azurecontainerapps.io/mcp` **cannot be registered as an Entra Application ID URI** (it's not a tenant-verified domain), so there is **no way to "align the two."** Coupled with Entra v2 **mandatorily validating that `resource` matches `scope`** and rejecting mismatches — **the only clean way out is to not include `resource` in the request at all (stripped during server-side proxied token exchange).**

> A minor quirk of Claude Code itself ([#52871](https://github.com/anthropics/claude-code/issues/52871)):
> It appends a trailing slash to `resource`, which can re-trigger AADSTS9010010 even if the values would otherwise match. But for this project, the root cause is the deeper mismatch of "`https://…/mcp` ≠ `api://…`" described above.

---

## 3. Why VS Code Works but Claude Code Doesn't

Same server, same metadata; the difference is **only in the client**:

- **VS Code** (older OAuth implementation) **does not send** the `resource` parameter → Entra v2 only looks at scope → issues token normally.
- **Claude Code / opencode** (more strictly adhering to the June 2025 MCP spec) **sends** `resource` → Entra v2 rejects → AADSTS9010010.

**This difference itself is key evidence**: if this were determined by the server-side FastMCP version, both clients connecting to the same server should yield the same result; since one works and the other doesn't, it shows that **whether `resource` is sent is client behavior** (see §6).

---

## 4. Whose Fault Is It, Really?

There is no single culprit. First, clarify the **layers**: **`resource` (RFC 8707) is an IETF **OAuth** extension, not an MCP invention, and is **optional** at the OAuth layer; MCP elevated it to a client **MUST**; Claude Code merely faithfully follows MCP.** So **no one violated OAuth**; rather, "**MCP mandated an optional OAuth feature**" + "**Entra and RFC 8707 represent two different audience philosophies**" (§1.5) collided:

| Role | Responsibility |
|---|---|
| **OAuth / RFC 8707** | `resource` is an **optional OAuth extension** (RFC 8707); OAuth 2.0 core (RFC 6749) **never requires** its implementation → ASes are inherently allowed to do their own thing (implement / rename parameter / not implement) |
| **MCP Specification** | **Root cause**: elevated this **optional feature to a client MUST**, and **left no room for "fallback if AS doesn't support it"** — effectively assuming all ASes implement 8707. The spec wants to change ([#1614](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614): resource→SHOULD + fallback), but is being blocked on security grounds (§10) |
| **Entra** | Follows the **identity-audience philosophy** (stuffs `api://<appid>` into scope), does not follow 8707's `resource`→`aud` semantics; the March 2026 enforcement turned silent tolerance into a hard error. Not "broken," just **a different worldview** (§1.5) |
| **Claude Code** | **Strictly follows MCP** (sends `resource`) → hits the wall. Compliant but not tolerant enough — VS Code's older implementation, which doesn't send `resource`, works instead; plus the trailing slash bug (#52871) |
| **FastMCP (this project's server)** | In `RemoteAuthProvider` (direct-to-Entra) mode, it's basically a **bystander** — only advertises metadata, only validates tokens, not in the token issuance path. But it **can step in to fix** (switch to `AzureProvider` proxy, see §7/§8) |

---

## 5. Are All OAuth MCPs Connecting to Microsoft Broken on Claude? — **No**

It's crucial to distinguish **four categories**; only **category ②** is affected:

| Category | Example | Works on Claude? |
|---|---|---|
| **① Local stdio MCP, using local credentials** | **Azure MCP Server (`npx @azure/mcp` / .mcpb)**, Azure DevOps local (PAT), most DB/tool servers | ✅ **Works**: uses `DefaultAzureCredential` (az login / MI / VS Code) or PAT/API key, **no browser OAuth at all, no resource parameter** |
| **② Remote HTTP, client directly connects to Entra v2** (pre-registration / token validation only) | **This project's FastMCP `RemoteAuthProvider`**; even Microsoft's own **remote** azure-devops-mcp is in this pit | ❌ **Does not work**, hits `AADSTS9010010` |
| **③ Remote HTTP, with an OAuth gateway/proxy in front** (shields Entra from resource) | **Azure API Management as OAuth gateway**; FastMCP `AzureProvider` | ✅ **Works** |
| **④ Non-Entra OAuth (supports RFC 8707)** | Modern IdPs like Okta / Auth0 | ✅ Generally works (this pit is **Entra v2 specific**, not "all OAuth MCPs are broken") |

**So the worry that "Azure MCP also won't work" is the opposite — it works**: the mainstream Azure MCP Server is **local stdio + your existing az login**, doing no browser OAuth at all (category ①). What's truly affected is **only category ②**: remote servers that let Claude do OAuth directly against Entra v2 — **this project happens to be category ②**.

> The most convincing corroboration: **Microsoft's official answer to "how to make an Entra-protected remote MCP usable by Claude" is "put an Azure API Management layer in front as an OAuth gateway"** ([Claude-Ready Secure MCP with APIM](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim)) — essentially the category ③ proxy that blocks `resource`.

---

## 6. Why "Downgrading FastMCP" Cannot Circumvent This

**`resource` is sent by the client (Claude Code), not chosen by the server-side FastMCP.**

- FastMCP's metadata only advertises the **value** of `resource`; **whether to send this parameter, and where to send it, is Claude Code's behavior**. §3 already proved: VS Code can use the same (new version) server, Claude Code cannot.
- **Downgrading FastMCP ≠ making Claude Code stop sending `resource`.** Moreover, the value of `resource` comes from the MCP server's **URL**, unrelated to the FastMCP version; the metadata (PRM) is **necessary for discovering the authorization server** — deleting it would prevent Claude Code from finding Entra at all.
- To truly "downgrade" around this, you'd have to downgrade **Claude Code itself** to pre-RFC 8707 behavior (equivalent to reverting to VS Code's old implementation) — unrealistic and not something the server side can control.

---

## 7. How to Fix

| Solution | Description | Cost |
|---|---|---|
| **Server-side OAuth Proxy (strip `resource`)** | Switch to FastMCP's `AzureProvider` (OAuth Proxy mode): the server proxies the Entra token exchange, requesting Entra with `scope=api://.../.default`, **without `resource`**; the client faces the proxy, which accepts/ignores `resource`. See [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md) | **Architectural change** (pre-registration → proxy), but cures the root cause |
| **Place APIM gateway in front** | Microsoft's official Claude-ready solution, equivalent to category ③ | Heavier infrastructure |
| **Wait for upstream fix** | MCP spec #1614 wants to make `resource` fallback-able, but is **being rejected on security grounds**; #55993 closed as duplicate, #52871 (trailing slash) fix doesn't solve the root cause | **Basically can't count on it** — see §10 for real-world status |
| **Temporary bearer token** | Claude Code uses `headers.Authorization: Bearer <token>` to bypass OAuth (manually obtain an Entra token with aud=`api://88de6a37...`, scp=`user_impersonation`) | Token expires ~1h, no refresh, only suitable for verifying server-side is OK |
| **Use only VS Code for now** | Unaffected, works right now | Can't use Claude Code/opencode |

**Why it can't be fixed on the Entra side**: The value of `resource` is the MCP's URL, which can neither be registered as an identifierUri nor aligned with the scope; Entra v2 mandatorily validates that `resource` must match the scope (rejects mismatches) — so "removing `resource`" can only be done during **server-side proxied token exchange**, i.e., proxy mode.

---

## 8. Fix Direction Detail: OAuth Proxy Mode (Making the AS FastMCP)

> This expands the "Server-side OAuth Proxy" row in the §7 table. **For architectural principles, see**
> [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md).
> **This project will consider implementation later** — here we just record the mental model.

### 8.1 Does Proxy ≈ DCR? — Containment Relationship (Proxy ⊃ DCR)

DCR is just **one capability** that the proxy incidentally provides (allowing arbitrary clients to dynamically register with FastMCP). The proxy itself is a "two-layer OAuth world":

```text
Client ──OAuth (including DCR)──> FastMCP (acting as AS, issues its own token) ──OAuth (fixed client)──> Entra
```

So **Proxy = DCR + translating/proxying tokens for client↔Entra + (crucial for this bug) stripping `resource` when sending token requests to Entra**. It's more than just DCR.

### 8.2 The OAuth Target Changes from Entra to FastMCP Itself

Yes. This is precisely the definition of proxy mode: **the Authorization Server in the client's eyes becomes FastMCP** — the client only does OAuth against FastMCP and receives a **token signed by FastMCP itself** (not the raw Entra token); behind the scenes, FastMCP uses **its own fixed Entra app** to exchange tokens with Entra.

**This is the principle for bypassing AADSTS9010010**: Claude Code's `resource` parameter is sent to FastMCP (the proxy accepts/ignores it), while the FastMCP→Entra hop is **controlled by your code**, using `scope=api://.../.default`, **without `resource`** → Entra is happy.

### 8.3 Two Perceptions to Calibrate

- **No need to pre-register every client in Entra** (the direction is reversed; it's actually less work): proxy + DCR allows clients to **dynamically register with FastMCP** (or you configure a whitelist on the FastMCP side). That public client (`49af5fc1`) is **no longer needed** in proxy mode. Entra only needs **one fixed confidential app for FastMCP itself** (requiring client secret / certificate, no longer a public client). "Knowing the client" moves from Entra to the FastMCP layer.
- **Don't conflate FastMCP's identity with the user's identity** (there's a pitfall): FastMCP uses its own Entra app's credentials as an **OAuth client** to exchange tokens — this is indeed "FastMCP's identity"; but the token obtained back **still represents the logged-in user**. Downstream calls to Graph, AD group queries, tool gating, OBO **must continue using the "user identity"**, not switch to FastMCP's service identity, otherwise the entire identity-aware design (who can see which tool, group-based authorization) collapses.

  > Mnemonic: **FastMCP's identity = the "OAuth client credential" when exchanging tokens with Entra; the user's identity = the subject of the token obtained back, which continues to flow downstream.** Don't merge the two.

### 8.4 Implementation Roughly Involves (to be detailed later)

- **Entra**: Create a new confidential app for FastMCP (client secret / certificate + redirect `/auth/callback`).
- **Server code**: `RemoteAuthProvider` + `AzureJWTVerifier` → `AzureProvider` (OAuthProxy), configured with client_id / secret / tenant + required scopes + client storage.
- **Decommission** the existing public client (`49af5fc1`).
- **Client mcp.json**: Remove `oauth.clientId` (switch to DCR against FastMCP, usually zero-config).

---

### 8.5 What Does Microsoft Think of Proxy / DCR? Is It Secure? Can We Pitch It to the Company?

Separate three things that are easily conflated:

**(1) Microsoft explicitly does not support DCR; this is a deliberate security design, not laziness.** Entra does not implement the open DCR that MCP wants (RFC 7591: arbitrary clients self-registering at runtime). The reason is enterprise security principles — **client identities must be governed/audited, not self-asserted**. Open DCR = any unknown app can register, request tokens, and induce user consent, a breeding ground for phishing and abuse. So "Microsoft dislikes DCR" is true, and **for good reason**.

**(2) Microsoft does not oppose "the proxy pattern" — they themselves recommend a proxy.** Microsoft's official answer to "how to make an Entra-protected remote MCP usable by Claude" is **placing an Azure API Management (APIM) layer in front as an OAuth gateway** ([Claude-ready secure MCP with APIM](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim)). The APIM gateway and FastMCP's `AzureProvider` **are the same architectural pattern**: a middle layer that acts as the AS to the client, acts as a confidential client to Entra, and absorbs the resource/DCR incompatibility. So **Microsoft does not oppose "adding a proxy/gateway"; on the contrary, they endorse this pattern** — they just want you to use a **governed, hardened** gateway (APIM), not a bare proxy with open DCR.

**(3) Where exactly is the proxy "insecure" — it's configurable, not inherent.** The FastMCP proxy is essentially a mature pattern (BFF / token broker / API gateway, ubiquitous in enterprises), and the risk points are all controllable:

| Risk Point | Description | How to Mitigate |
|---|---|---|
| **Open DCR** | If the proxy accepts arbitrary client self-registration, it brings the "ungoverned client" risk that Entra deliberately avoids back into your own house | **Don't enable open DCR**, only pin known clients (governance stays in your hands) |
| **Secret concentration + proxying everyone's tokens** | The proxy is a confidential client holding an app secret, and exchanges/holds tokens for all users; blast radius is larger than pre-registration | Secret in Key Vault, short TTL, least privilege, audit logs |
| **Audience binding weakened** | The proxy self-signs tokens and strips `resource`; misuse could weaken the anti-replay protection that RFC 8707 intends | Correctly set `aud`, strict validation, no careless passthrough |

**Conclusion (wording for pitching to the company):**

- **Your current pre-registration setup (`AzureJWTVerifier` only validates tokens) is actually the architecture "most aligned with Microsoft security principles"**: clients are registered/governed, admin can consent, server only validates tokens, no open DCR, no middle layer proxying everyone's tokens. It has **no security issue** — the only flaw is hitting the `AADSTS9010010` **interoperability bug** (a client-side + spec-side problem, not your architecture being insecure).
- **To make Claude Code work right now**, Microsoft's given "official secure solution" is the **APIM gateway** (governed proxy mode), not open DCR. FastMCP's `AzureProvider` is a self-hosted version of the same pattern: **as long as you don't enable open DCR and put the secret in Key Vault, it's equivalent to "a self-built APIM"** and can be pitched; **once open DCR is enabled, you step on the line Microsoft opposes**.
- **The most solid enterprise narrative**: either (a) **maintain pre-registration, wait for upstream to make `resource` fallback-able** (MCP #1614 / Claude Code side), zero architectural compromise; or (b) **adopt APIM (or a governed self-hosted proxy)**, clients still go through registration/governance, open DCR turned off. Both stand firm and do not violate Microsoft's design principles.

> One sentence for management: **"We are not doing open DCR. Either wait for the standard to be fixed (keeping the status quo is cleanest), or follow Microsoft's official blueprint with an APIM gateway; the FastMCP proxy is just a self-hosted version of the same gateway, likewise with open DCR turned off."**

---

## 9. Timeline / Key Facts Quick Reference

- **RFC 8707** (Resource Indicators): Added an optional `resource` parameter to OAuth, allowing a token's `aud` to precisely target a specific API.
- **MCP Authorization Spec June 2025**: Elevated `resource` from "optional" to a client **MUST**.
- **Entra v2**: scope-centric; **does** validate `resource`, but requires it to match the scope owner (not the `resource`→`aud` semantics MCP expects).
- **March 2026**: Entra enabled mandatory validation; `resource`+`scope` conflicts went from "silently tolerated" to a hard **AADSTS9010010 error**.
- **Current Status (July 2026)**: All remote MCPs where Claude Code / opencode connects directly to Entra v2 are affected; VS Code is spared because it doesn't send `resource`; local stdio Azure MCPs are spared because they don't use browser OAuth.

---

## 10. Will Upstream Fix It? Real-World Timeline Check (2026-07-10)

> TL;DR: **Don't count on an upstream fix in the short term.** Real-world check of the current status of key issues below — the most relevant spec proposal is being **rejected on security grounds**, not "slow," but "direction blocked." Therefore, **proxy is the universal correct solution** (effective for any spec-compliant client that sends `resource`); don't bet the plan on "waiting for upstream."

| Issue | Status (2026-07-10 Check) | Meaning |
|---|---|---|
| **MCP Spec [#1614](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614)** (make `resource` OPTIONAL) | **OPEN, stalled** (last activity 2026-05-11, no PR) | Discussion **turned towards rejection**: maintainers pointed out that `resource`↔`scope` validation is **intentional security design** (preventing someone from impersonating a resource server for scopes they don't own / confused-deputy); proposer has **self-admitted "this proposal is not secure"** → essentially dead |
| **claude-code [#55993](https://github.com/anthropics/claude-code/issues/55993)** (the 9010010 mismatch) | **CLOSED as `duplicate`** (2026-05-08, locked) | Will not be fixed as an independent bug |
| **claude-code [#52871](https://github.com/anthropics/claude-code/issues/52871)** (`resource` trailing slash) | **OPEN, still active** (still moving 2026-07-09) | Real bug, but **fixing it doesn't solve this project's root cause**: removing the trailing slash, `https://…/mcp` still cannot match `api://88de6a37…` |
| **Merged `resource`/8707 PR in spec** | **None** (no resource-optional related PR merged in 2026) | No rescue in the pipeline |

**An incidental correction** (also corrected wording in §0/§1/§2/§7): The #1614 discussion exposed a point previously not stated precisely enough — **AADSTS9010010 shows that Entra actually "accepts and validates" `resource`** (hence the error is "mismatch," not "resource not supported"). That is, Entra is not "ignoring `resource`," but rather **mandatorily validating per RFC 8707 semantics that `resource` must match the scope owner**; MCP's URL-form `resource` inherently cannot match the `api://`-form scope, thus rejected. And this validation is deemed by maintainers to be a **legitimate security control** — **this is the fundamental reason #1614 cannot be fixed**: relaxing it = weakening a real security boundary.

**Impact on decision-making**: Since a clean upstream fix is not coming in the foreseeable future, "waiting for upstream" is not viable; **to support clients other than VS Code, proxy (§7/§8) is the direction to take, not a stopgap**. Scale determines the shell: for small usage, a single container with dual paths is sufficient; at platform scale, adopt APIM.

---

## References

- [`MCP-Custom Client Access-...md`](./connecting-custom-clients-to-entra-protected-mcp-principles-and-explanation.md) — OAuth support comparison across clients, OAuth sequence/PKCE
- [`Entra OAuth Proxy vs Pre-registration MCP.md`](../Entra%20OAuth%20Proxy%20vs%20Pre-registration%20MCP.md) — Proxy mode (architecture of the fix)
- [Groff – Entra ID × MCP Auth Incompatibility Checklist (proxy strips resource)](https://www.groff.dev/blog/azure-entra-id-mcp-server-authentication-incompatibilities)
- [Microsoft – Building Claude-Ready Entra-Protected MCP with APIM](https://developer.microsoft.com/blog/claude-ready-secure-mcp-apim)
- [Claude Code #55993 (resource/scope mismatch)](https://github.com/anthropics/claude-code/issues/55993) / [#52871 (trailing slash)](https://github.com/anthropics/claude-code/issues/52871)
- [FastMCP #1846 (Entra 'resource' not supported)](https://github.com/PrefectHQ/fastmcp/issues/1846) / [Azure Integration (AzureProvider)](https://gofastmcp.com/integrations/azure)
- [MCP Spec #1614 (make resource optional/fallback)](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1614)
- [RFC 8707 – Resource Indicators for OAuth 2.0](https://www.rfc-editor.org/rfc/rfc8707.html)
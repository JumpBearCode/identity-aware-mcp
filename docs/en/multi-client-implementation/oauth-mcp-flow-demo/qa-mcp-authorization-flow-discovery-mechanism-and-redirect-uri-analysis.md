---
title: "Q&A: MCP Authorization Flow — Discovery Mechanism, issuer, well-known Probing, and Who Actually Redirects redirect_uri"
date: 2026-07-12
tags:
  - mcp
  - oauth
  - oidc
  - rfc8414
  - rfc9728
  - rfc8707
  - pkce
  - discovery
  - redirect_uri
  - mcpproxy
status: Teaching Q&A (companion visualization app)
sources:
  - "src/mcp-server/mcpproxy.py"
  - "src/mcp-server/main.py"
  - "docs/en/multi-client-implementation/implementation-notes-plan-a-mcpproxy-resource-stripping-proxy.md"
  - "docs/en/multi-client-implementation/bug-analysis-aadsts9010010-mcp-resource-parameter-collides-with-entra-v2.md"
  - "docs/en/multi-client-implementation/oauth-mcp-flow-demo/ (this directory · companion visualization app)"
verified:
  - "All well-known probes, 401 headers, and PRM contents are real captures from deployed instances (see measured tables in the document)"
---

# Q&A: MCP Authorization Flow — Discovery Mechanism, issuer, well-known Probing, and Who Actually Redirects redirect_uri

> This document consolidates a Q&A session about two authorization flows — `/mcpproxy` (the resource-stripping proxy) and `/mcp` (direct connection to Entra) —
> **into a reference**, focusing on four easily confusing aspects: **① Why a missing token results in 401, ② Discovery is actually two rounds,
> ③ The relationship between issuer and the well-known URL, ④ Who actually performs the redirect for redirect_uri**.
>
> It comes with a runnable **visual teaching app** (the app in **this directory**: sibling `server.py` + `public/`): it animates
> the real request/response for these 14/11 steps step-by-step, supports switching between the `mcpproxy ↔ mcp` flows, and includes demo playback and live real calls.
> See the sibling `README.md` for how to run it.

---

## 0. Overview of the Two Flows (a table to see the differences at a glance)

| | 🪄 **mcpproxy Flow** (14 steps) | ⚙️ **mcp Direct Flow** (11 steps) |
|---|---|---|
| Who uses this | Claude Code / opencode (**sends** RFC 8707 `resource`) | VS Code (**does not send** `resource`) |
| step 3 PRM `authorization_servers` | Points to **the proxy itself** `…/mcpproxy` | Points to **Entra** `…/v2.0` |
| Who to ask for endpoints in step 4 | Ask the **proxy** for AS metadata | Directly ask **Entra** for the discovery document |
| `/authorize`, `/token` | Hit the **proxy**, proxy deletes `resource` then forwards to Entra | **Directly** hit Entra, no `resource` throughout |
| Will it hit `AADSTS9010010` | Client brings `resource` → must rely on proxy to delete it to avoid the error | Doesn't bring it in the first place → won't hit it, no proxy needed |
| Final token / gating | Real Entra token; same verifier + OBO + group | **Exactly the same** |

**Different paths, same destination**: Both paths obtain the same real Entra token (consistent `aud/scp/oid`). The only difference lies in the "protocol negotiation before issuing the token" —
**whether a proxy layer is needed to delete `resource`**.

---

## 1. Why a "tokenless call" returns 401 (and it has nothing to do with POST)

The `/mcpproxy` MCP endpoint is wrapped **at the outermost layer** by `RequireAuthMiddleware` (`src/mcp-server/mcpproxy.py:170`):

```python
proxy_mcp_endpoint = RequireAuthMiddleware(streamable_app, required_scopes, prm_url)
Route(proxy_path, proxy_mcp_endpoint, methods=["GET", "POST", "DELETE"])   # mcpproxy.py:193
```

Any request hitting `/mcpproxy` (**GET / POST / DELETE treated equally**) first encounters this middleware; it never reaches the MCP/JSON-RPC logic.
SDK implementation (`mcp/server/auth/middleware/bearer_auth.py`):

```python
async def __call__(self, scope, receive, send):
    auth_user = scope.get("user")
    if not isinstance(auth_user, AuthenticatedUser):        # No valid token → no authenticated user
        await self._send_auth_error(send, status_code=401,  # → Direct 401, never enters self.app
            error="invalid_token", description="Authentication required")
        return
    ...
```

- Success or failure of validation is determined by `scope["user"]` populated by the outer auth layer (`RemoteAuthProvider` + `AzureJWTVerifier`); no token → 401.
- **It's not "only POST gets 401"**: GET also gets 401 (the middleware intercepts before parsing body/method). The demo draws it as POST only because MCP's first `initialize` call is JSON-RPC over POST.

**Which variable is that `resource_metadata=` pointer in the 401?** It's `prm_url` (`mcpproxy.py:106`):

```python
prm_path = "/.well-known/oauth-protected-resource" + proxy_path   # /.well-known/oauth-protected-resource/mcpproxy
prm_url  = f"{base}{prm_path}"                                    # base comes from MCP_SERVER_BASE_URL in main.py
```

One piece of data, two uses: **① Inserted into the 401 `WWW-Authenticate` header** (`mcpproxy.py:173` → SDK assembles `resource_metadata="…"`);
**② Registered as an actual GET route** (`Route(prm_path, protected_resource_metadata)`, `mcpproxy.py:177`).

Real captured `/mcpproxy` 401 header:

```text
www-authenticate: Bearer error="invalid_token", error_description="Authentication required",
                  resource_metadata="https://…/.well-known/oauth-protected-resource/mcpproxy"
```

---

## 2. Discovery is actually **two rounds**, don't mix them up

Facing a protected resource, the client must probe twice:

- **First round (RFC 9728 · Protected Resource Metadata) — Find "which AS"**: Follow the 401's `resource_metadata` pointer, GET the
  PRM, read `authorization_servers`. → Sequence diagram **step 3**.
- **Second round (RFC 8414 / OIDC Discovery) — Find "that AS's endpoints"**: Transform the AS identifier from the previous step into a well-known metadata address,
  GET it to obtain `authorization_endpoint` / `token_endpoint`. → Sequence diagram **step 4**.

After probing, the client will **cache** these endpoints; subsequent requests won't re-probe.

### 2.1 The PRM in step 3 does **not** have an `issuer` field

Measured PRM fields for both flows have only four:

```text
keys = ['resource', 'authorization_servers', 'scopes_supported', 'bearer_methods_supported']
```

- What we colloquially call "issuer" refers to **the value in the `authorization_servers` array** — in OAuth terminology, an authorization server is identified by its
  issuer URL, but **in the PRM, this field is named `authorization_servers`, not `issuer`**.
- The field actually named `issuer` appears in the **step 4 response** (AS metadata), and the spec requires it to **be exactly equal** to the value in step 3's
  `authorization_servers`. This is how the client "chains" the step 3 pointer to the step 4 credential (preventing redirection to a fake AS):

| | step 3 `authorization_servers[0]` | step 4 response `issuer` |
|---|---|---|
| Proxy | `https://…/mcpproxy` | `https://…/mcpproxy` ✅ Equal |
| Direct | `https://login.microsoftonline.com/<tenant>/v2.0` | `https://login.microsoftonline.com/<tenant>/v2.0` ✅ Equal |

### 2.2 Step 4: Constructing the well-known address from the AS identifier — two **orthogonal** dimensions

Given an AS identifier (`https://H/P`), the client needs to construct the discovery address. There are **two mutually independent** dimensions; don't tie them together:

- **Dimension A · Document type**: `oauth-authorization-server` (RFC 8414) **vs** `openid-configuration` (OIDC).
- **Dimension B · Position of the well-known segment**: **Inserted** (placed between host and path) **vs** **Appended** (suffixed to the end of the issuer).

For `https://H/P`：

```text
① https://H/.well-known/oauth-authorization-server/P     ← 8414 · Inserted
② https://H/P/.well-known/oauth-authorization-server     ← 8414 · Appended
③ https://H/P/.well-known/openid-configuration           ← OIDC · Appended
```

### 2.3 Measured: Which type does each server **actually provide** (this is the basis for judgment, not guesswork)

"How do you know which type each flow uses?" — **Not by rules, but by ① reading the proxy's route code + ② real probing to see which returns 200.** Measured:

| URL | Result | Notes |
|---|---|---|
| Proxy `…/.well-known/oauth-authorization-server/mcpproxy` (Inserted) | **200** | Proxy serves 8414 |
| Proxy `…/mcpproxy/.well-known/oauth-authorization-server` (Appended) | **200** | Same document, also served at a different position |
| Proxy `…/mcpproxy/.well-known/openid-configuration` | **404** | Proxy does **not** serve OIDC (it's not an OIDC provider, doesn't sign id_tokens) |
| Entra `…/v2.0/.well-known/openid-configuration` (Appended · OIDC) | **200** | Entra serves OIDC |
| Entra `…/v2.0/.well-known/oauth-authorization-server` (Appended · 8414) | **404** | All 8414 variants on this issuer are 404 |
| Entra Inserted (both types) | **404** | |

**Conclusion (correcting a common misconception)**: The real difference in step 4 between the two flows is the **"document type"** (proxy serves 8414 / Entra serves OIDC),
**not "inserted vs appended"**. Inserted/appended is just the **position**; the proxy mounts the **same 8414 document** at both the inserted and appended positions — this is exactly
the two routes at `mcpproxy.py:181` (comment verbatim: "Different MCP clients try different positions"):

```python
Route("/.well-known/oauth-authorization-server" + proxy_path, authorization_server_metadata, methods=["GET"])  # Inserted
Route(proxy_path + "/.well-known/oauth-authorization-server", authorization_server_metadata, methods=["GET"])  # Appended
```

### 2.4 Does the client "try these URLs one by one until it gets a 200"?

**Basically correct, but needs precision:**

1. **Probing only happens during the "discovery" step**, not for every OAuth operation; once found, it's cached.
2. **The candidate list and order are enumerated by the spec** (①②③ above), not random trial.
3. **The termination condition is not "any 200"**, but **the first metadata that is "valid and has a matching `issuer`"** — a 200 with a non-compliant body
   or mismatched `issuer` will be rejected, and probing continues.
4. **The set/order tried varies across clients**; a minimal one might only try one type → if the server only serves the other type, it fails. **This is why servers often serve
   multiple types as a fallback** (as our proxy does).
5. The demo client in this directory (`server.py`) is a **simplified version**: I hardcoded the one usable URL per flow (proxy→inserted 8414;
   direct→Entra's openid-configuration), without sequential probing. A **compliant** client would try ①②③ in order.

---

## 3. Who actually "redirects" redirect_uri? (authorize does, token doesn't)

This is the most easily misunderstood part. Let's start with a conclusion table:

| Step | Does an HTTP 302 redirect occur? | Who sends it | Redirects to |
|---|---|---|---|
| **Proxy `/authorize`** | **Yes** | **Proxy** (`RedirectResponse`) | Redirects to **Entra** (not redirect_uri!) |
| Browser → Entra login | Browser **follows** the above 302 | — (browser behavior) | Entra login page |
| **Entra → loopback** | **Yes** | **Entra** | Redirects to **redirect_uri** (`localhost:8080/callback`), with code+state |
| **Proxy `/token`** | **No** | — (server-to-server POST) | No redirect; directly returns JSON |

### 3.1 What redirect_uri is and who validates it

`redirect_uri = http://localhost:8080/callback` is the **client's own loopback callback address**. It:

- **Appears in both the `/authorize` and `/token` requests**, and Entra requires them to be **exactly identical** (security requirement: the redirect_uri used to exchange the token must match the one used when initiating authorization).
- **Validated by Entra**, checked against the **redirect whitelist** registered for client `49af5fc1` in Entra (currently only
  `http://localhost:8080/callback`）。
- **The proxy only passes it through verbatim**, never uses it as a redirect target itself (hence no open-redirect: see §5).

### 3.2 `/authorize`: The proxy **does send a 302**, but the destination is Entra, not redirect_uri

The proxy's authorize handler (`mcpproxy.py:139`):

```python
async def authorize(request):
    params = dict(request.query_params)            # Contains redirect_uri, code_challenge, state, resource…
    params.pop("resource", None)                   # Only deletes resource
    params["scope"] = _ensure_scopes(params.get("scope"), api_scope)
    return RedirectResponse(f"{upstream_authorize}?{urlencode(params)}", status_code=302)  # ★ 302 → Entra
```

- Here a **302 is indeed generated** (`RedirectResponse`), sending the browser **to Entra's `/authorize`**.
- `redirect_uri` is merely **stuffed into the Location query and forwarded to Entra** (used as data), **the proxy does not redirect to
  `localhost`**. You asked "authorize definitely does a redirect" — correct, but it redirects to **Entra**, not back to the client's loopback.

### 3.3 The "redirect back to localhost to get code+state" jump — is done by **Entra**, **not the proxy**

The browser follows the above 302 to Entra; after the user logs in/consents, **Entra sends the next 302**:

```text
Location: http://localhost:8080/callback?code=<authorization code>&state=<original>
```

- **This jump is entirely Entra → client's loopback; the proxy has no route involvement and never sees the code.**
- Your inference is completely correct: *"Entra will return a redirect to the client, making the client jump to localhost to get the code and state."*
- Precisely because the proxy doesn't handle this jump, it remains **almost stateless** (doesn't generate its own PKCE, doesn't sign tokens, doesn't store anything) — this is the source of "statelessness".

### 3.4 `/token`: **No redirect at all** — it's a server-to-server POST + verbatim pass-through

The proxy's token handler (`mcpproxy.py:150`):

```python
async def token(request):
    form = dict(await request.form())
    form.pop("resource", None)                     # Delete resource
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.post(upstream_token, data=form, headers={"Accept": "application/json"})  # Server→Server
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)   # ★ Returns JSON verbatim, no 302
```

- **No redirect whatsoever**. It's the proxy **POSTing in the background using httpx** to Entra's token endpoint (server-to-server), getting back token JSON,
  and returning it **verbatim** to the client.
- `redirect_uri` here is just a field in the form, **forwarded to Entra for it to verify** (code ↔ redirect_uri consistency),
  **does not trigger a redirect**.
- So to answer your question: **the authorize endpoint does a redirect (302 to Entra); the token endpoint does not do a redirect.**

### 3.5 Redirect differences between the two flows (comparison by the way)

- **mcpproxy flow**: The browser sees **two 302 jumps** — ①Proxy `/authorize` → Entra; ②Entra → `localhost/callback`.
- **mcp direct flow**: The client **itself** constructs Entra's `/authorize` URL and opens it directly (no proxy 302 jump), so there's only **one 302
  jump** — Entra → `localhost/callback`. Token is also **directly** POSTed by the client to Entra (also no redirect).

### 3.6 A diagram to see clearly (mcpproxy flow)

```text
client ──GET /mcpproxy/authorize?…&redirect_uri=localhost/callback&resource=…──► Proxy
Proxy ──302 Location=Entra/authorize?…（resource deleted, redirect_uri carried verbatim）──► client
client(browser) ──follow──► Entra/authorize ──login/consent──►
Entra ──302 Location=http://localhost:8080/callback?code=…&state=…──► client   ← This jump is sent by Entra, invisible to the proxy
client ──POST /mcpproxy/token（code+code_verifier+redirect_uri+resource）──► Proxy
Proxy ──httpx POST Entra/token（delete resource）──► Entra ──real token──► Proxy ──verbatim JSON（no 302）──► client
```

---

## 4. PKCE: `code_challenge` (promise) ↔ `code_verifier` (proof)

- **step 5 (promise)**: The client submits `code_challenge = base64url(SHA256(code_verifier))` at `/authorize`. Entra binds it
  to the upcoming authorization code and **stores it**.
- **step 9/10 (proof)**: The client submits the **plaintext `code_verifier`** at `/token`. Entra computes `SHA256(code_verifier)` on the spot, compares it to the previously
  stored `code_challenge`, and issues the token only if they match.
- **Purpose**: Prevents authorization code interception. Even if an attacker steals the `code` from the redirect, without `code_verifier` (which never left the client) they cannot exchange it for a
  token. For **public clients without a secret**, PKCE is used to **replace the secret**, proving that "the one exchanging the token is the same one who initiated the request".
- **The proxy does not generate its own PKCE, only passes through the client's** — so in step 5 and step 6, `code_challenge`/`redirect_uri` are exactly the same
  (the proxy only deleted `resource` and supplemented scope). PKCE is **client ↔ Entra end-to-end**.

---

## 5. Clarification by the way: Differences between the three Starlette response classes

| Class | What it does | Where used in this project |
|---|---|---|
| `JSONResponse(data)` | Serializes JSON, `Content-Type: application/json`, default 200 | Two discovery metadata endpoints (step 3/4) — returns data for the client to read |
| `RedirectResponse(url, status_code=302)` | Empty body + `Location: url` + 3xx, tells the browser to jump | `/authorize` (step 6) — jumps to Entra |
| `Response(content, status_code, media_type)` | Most primitive, full manual control over body/status/type | `/token` (step 12) — **passes through** Entra's response verbatim, not a single byte changed |

`JSONResponse` / `RedirectResponse` both inherit from `Response`. **Use `RedirectResponse` to redirect, `JSONResponse` to return JSON,
and bare `Response` to "spit out whatever you received verbatim".**

**Open-redirect prevention point**: The proxy's `/authorize` only appends parameters to the **hardcoded Entra constant URL**, never decides where to jump based on user input;
the user-supplied `redirect_uri` is handed off to **Entra's whitelist** as the safeguard — so don't add loose addresses to `49af5fc1`'s redirect whitelist.

---

## 6. Quick Terminology Reference

| Term | What it refers to |
|---|---|
| PRM (RFC 9728) | Protected Resource Metadata, the document in step 3, tells you "who my AS is" (field `authorization_servers`) |
| AS metadata (RFC 8414) | Authorization Server Metadata, the document in step 4, tells you the AS's authorize/token endpoints (field `issuer` is here) |
| OIDC Discovery | `openid-configuration`, the discovery document served by an OIDC provider (like Entra), fields largely overlap with 8414 |
| issuer | The AS's identity URL; appears as the value of `authorization_servers` in step 3, and as the `issuer` field in step 4; the two must be equal |
| resource (RFC 8707) | The "audience binding" parameter brought by MCP clients; Entra v2 doesn't recognize its position → hits `AADSTS9010010`; the proxy's sole action is to delete it |
| redirect_uri | The client's loopback callback; validated by Entra against the `49af5fc1` whitelist; **it is Entra that redirects back to it, not the proxy** |
| PKCE | `code_challenge` (promise, sent with authorize) ↔ `code_verifier` (proof, sent with token), the public client's "secretless anti-interception" mechanism |

---

## References

- [Implementation Notes: Plan A — `/mcpproxy` resource-stripping proxy (code analysis · design trade-offs · security analysis)](../implementation-notes-plan-a-mcpproxy-resource-stripping-proxy.md)
- [Bug Analysis: AADSTS9010010 — MCP's resource parameter clashes with Entra v2](../bug-analysis-aadsts9010010-mcp-resource-parameter-collides-with-entra-v2.md)
- Companion visualization app: **this directory** (`python3 server.py` → http://localhost:8080）
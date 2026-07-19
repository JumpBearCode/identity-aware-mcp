/*
 * steps.js — The "script" for two flows.
 *
 *   ● mcpproxy flow (14 steps): The client insists on sending the RFC 8707 resource parameter (Claude Code/opencode),
 *     so a /mcpproxy proxy layer is inserted to strip the resource parameter before forwarding to Entra.
 *   ● mcp flow (11 steps): The client does not send the resource parameter (VS Code), the PRM points directly to Entra,
 *     the client connects directly to Entra, no proxy is needed, and AADSTS9010010 is avoided.
 *
 * Metadata (title/lanes/direction/label/highlight) are defined separately for each flow; the animation logic (app.js) is fully shared.
 * Demo mode uses data close to real packet captures; Live mode uses real packet captures from server.py to overwrite steps with the same name.
 *
 * Corresponding documentation: docs/en/multi-client-implementation/implementation-notes-plan-a-mcpproxy-resource-stripping-proxy.md
 */
(function () {

// ── Constants from the real deployment (all from project configuration / packet captures) ──────────────────────────────
const CFG = {
  base: "https://dataops-aca-mcp.icyrock-96f978c0.westus2.azurecontainerapps.io",
  tenant: "9ea91fbb-1313-4312-a601-b6d9ab7d4de3",
  clientId: "49af5fc1-96e6-40c1-b108-cb828cc2a00e",
  apiAppId: "88de6a37-cf75-40d3-83e8-44c5ccbc0895",
  redirectUri: "http://localhost:8080/callback",
  codeChallenge: "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
  codeVerifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
  state: "af0ifjsldkj-3n2b8s7",
  authCode: "0.AXcA...M9k2b7q1x9pQ<demo-code>",
};
CFG.apiScope = `api://${CFG.apiAppId}/user_impersonation`;
CFG.proxyUrl = CFG.base + "/mcpproxy";
CFG.mcpUrl = CFG.base + "/mcp";
CFG.entraIssuer = `https://login.microsoftonline.com/${CFG.tenant}/v2.0`;
CFG.entraOpenid = `${CFG.entraIssuer}/.well-known/openid-configuration`;
CFG.entraAuthorize = `https://login.microsoftonline.com/${CFG.tenant}/oauth2/v2.0/authorize`;
CFG.entraToken = `https://login.microsoftonline.com/${CFG.tenant}/oauth2/v2.0/token`;
CFG.jwks = `https://login.microsoftonline.com/${CFG.tenant}/discovery/v2.0/keys`;

const jrpc = (obj) => JSON.stringify(obj, null, 2);

// Data shared by both flows for initialize / tools/list
const INIT_BODY = jrpc({
  jsonrpc: "2.0", id: 1, method: "initialize",
  params: { protocolVersion: "2025-06-18", capabilities: {},
    clientInfo: { name: "oauth-mcp-flow-demo", version: "1.0.0" } },
});
const TOOLS_RESULT = jrpc({
  jsonrpc: "2.0", id: 2,
  result: { tools: [
    { name: "diagnose_bash", description: "Run a read-only shell command for Azure diagnostics.",
      inputSchema: { type: "object", properties: { command: { type: "string" } }, required: ["command"] } },
    { name: "action_bash", description: "Run a write/modify shell command for Azure operations.",
      inputSchema: { type: "object", properties: { command: { type: "string" }, explanation: { type: "string" } },
        required: ["command", "explanation"] } },
  ] },
});
const TOKEN_CLAIMS = {
  aud: CFG.apiAppId, iss: `https://sts.windows.net/${CFG.tenant}/`, azp: CFG.clientId,
  scp: "user_impersonation", oid: "b04a03e0-6e07-4d55-83b2-7dedeb56c56d", tid: CFG.tenant,
};
const TOKEN_BODY = jrpc({
  token_type: "Bearer", scope: CFG.apiScope, expires_in: 3599, ext_expires_in: 3599,
  access_token: "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiIsImtpZCI6Ii1LSTNROW5OUjdiUm9meG1lWm9YcWJIWkdldyJ9.<payload>.<sig>",
  refresh_token: "0.AXcAu5GqnhMTEkOmAbbZq02t3sH_r0nmlm5ItgjLg...<redacted>",
  id_token: "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiI.<payload>.<sig>",
});

// ══════════════════════════════════════════════════════════════════════════
//  Flow A: mcpproxy (strip resource) — 14 steps, 4 lanes
// ══════════════════════════════════════════════════════════════════════════
const LANES_PROXY = [
  { key: "client", label: "Demo Client", sub: "Simulates Claude Code · Static Registration", icon: "🧩" },
  { key: "proxy", label: "/mcpproxy Proxy Route", sub: "OAuth glue written in this container", icon: "🪄" },
  { key: "entra", label: "Entra ID", sub: "login.microsoftonline.com", icon: "🔐" },
  { key: "mcp", label: "/mcpproxy MCP Endpoint", sub: "Same AzureJWTVerifier", icon: "⚙️" },
];

const STEPS_PROXY = [
  { n: 1, from: "client", to: "mcp", dir: "req", label: "POST /mcpproxy (no auth)",
    title: "Call MCP endpoint without token",
    desc: "The client does not have a token yet and directly initiates an initialize request to the MCP endpoint, deliberately hitting a 401 to discover the authorization server from the response.",
    request: { method: "POST", url: CFG.proxyUrl,
      headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" },
      body: INIT_BODY } },
  { n: 2, from: "mcp", to: "client", dir: "res", label: "401 WWW-Authenticate",
    title: "401 + WWW-Authenticate",
    desc: "The server returns a 401 and provides the resource_metadata address in the WWW-Authenticate header (RFC 9728's pointer for 'where to find the authorization server').",
    highlights: [{ type: "key", text: "resource_metadata is the address to GET in the next step" }],
    response: { status: 401, statusText: "Unauthorized",
      headers: { "content-type": "application/json",
        "www-authenticate": `Bearer error="invalid_token", error_description="Authentication required", resource_metadata="${CFG.base}/.well-known/oauth-protected-resource/mcpproxy"` },
      body: jrpc({ error: "invalid_token", error_description: "Authentication required" }) } },
  { n: 3, from: "client", to: "proxy", dir: "req", label: "GET protected-resource",
    title: "Discovery ①: Protected-Resource Metadata (RFC 9728)",
    desc: "Follow the pointer and GET this metadata. It says: my authorization server is myself (the proxy), not Entra.",
    highlights: [{ type: "key", text: "authorization_servers points to the proxy itself → the client treats the proxy as the AS" }],
    request: { method: "GET", url: `${CFG.base}/.well-known/oauth-protected-resource/mcpproxy`, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ resource: CFG.proxyUrl, authorization_servers: [CFG.proxyUrl], scopes_supported: [CFG.apiScope], bearer_methods_supported: ["header"] }) } },
  { n: 4, from: "client", to: "proxy", dir: "req", label: "GET auth-server meta",
    title: "Discovery ②: Authorization-Server Metadata (RFC 8414)",
    desc: "GET the authorization server metadata to obtain the authorize/token endpoints. Key point: there is no registration_endpoint, so no DCR is triggered; a static client_id is used.",
    highlights: [
      { type: "key", text: "No registration_endpoint → Dynamic Client Registration (DCR) is not triggered" },
      { type: "key", text: "token_endpoint_auth_methods=[\"none\"] → public client, no secret" }],
    request: { method: "GET", url: `${CFG.base}/.well-known/oauth-authorization-server/mcpproxy`, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ issuer: CFG.proxyUrl, authorization_endpoint: `${CFG.proxyUrl}/authorize`, token_endpoint: `${CFG.proxyUrl}/token`,
        response_types_supported: ["code"], response_modes_supported: ["query", "fragment"],
        grant_types_supported: ["authorization_code", "refresh_token"], code_challenge_methods_supported: ["S256"],
        token_endpoint_auth_methods_supported: ["none"], scopes_supported: [CFG.apiScope, "offline_access", "openid", "profile"] }) } },
  { n: 5, from: "client", to: "proxy", dir: "req", label: "GET /authorize (+resource)",
    title: "Client initiates /authorize (with resource)",
    desc: "The client generates PKCE and state, and requests authorization with the static client_id, loopback redirect_uri, and the RFC 8707 resource parameter.",
    highlights: [{ type: "removed-preview", text: "Note that resource=… is included — the proxy will strip it in the next step" }],
    request: { method: "GET", urlBase: `${CFG.proxyUrl}/authorize`, headers: {},
      query: [["response_type", "code"], ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri],
        ["scope", `${CFG.apiScope} offline_access`], ["code_challenge", CFG.codeChallenge], ["code_challenge_method", "S256"],
        ["state", CFG.state], ["resource", CFG.proxyUrl, "stripped"]] } },
  { n: 6, from: "proxy", to: "client", dir: "res", label: "302 → Entra (−resource)",
    title: "Proxy 302 → Entra (★strips resource)",
    desc: "The proxy's only substantive action: strip resource, add offline_access/openid/profile, and 302 to Entra. PKCE/state/redirect_uri are passed through unchanged.",
    highlights: [
      { type: "removed", text: "resource parameter removed — the key to bypassing AADSTS9010010" },
      { type: "added", text: "openid / profile added" }],
    response: { status: 302, statusText: "Found",
      headers: { "location": { urlBase: CFG.entraAuthorize,
        query: [["response_type", "code"], ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri],
          ["scope", `${CFG.apiScope} offline_access openid profile`, "added"], ["code_challenge", CFG.codeChallenge],
          ["code_challenge_method", "S256"], ["state", CFG.state]] } } } },
  { n: 7, from: "client", to: "entra", dir: "note", label: "🔒 Browser ↔ Entra Login",
    title: "Browser ↔ Entra Interactive Login",
    desc: "The browser follows the 302 to the Entra login page. The user enters their credentials, passes MFA/Conditional Access here. The proxy does not participate and cannot see the credentials.",
    note: { title: "Why is there no JSON for this hop?", lines: [
      "This is a real browser interactive login, not a backend API call.",
      "The user's real IP + MFA/Conditional Access happen on this hop — strong authentication is not affected by the proxy.",
      "The proxy exits after sending the 302 in the previous step and remains completely invisible throughout."] } },
  { n: 8, from: "entra", to: "client", dir: "res", label: "302 → loopback (code)",
    title: "Entra 302 back to loopback (with code)",
    desc: "After successful login, Entra directly 302s back to the client's loopback, with the code + the original state. The proxy never handles the code — this is the source of 'almost stateless'.",
    highlights: [{ type: "key", text: "Callback goes directly to the client's loopback; the proxy cannot see the code" }],
    response: { status: 302, statusText: "Found",
      headers: { "location": { urlBase: CFG.redirectUri, query: [["code", CFG.authCode], ["state", CFG.state]] } } } },
  { n: 9, from: "client", to: "proxy", dir: "req", label: "POST /token (+resource)",
    title: "Client exchanges code for token (with resource)",
    desc: "The client POSTs to the proxy's /token, using authorization_code + code_verifier to exchange for a token. Public client, no secret; the resource parameter is included again.",
    highlights: [{ type: "removed-preview", text: "resource=… appears again in the form — the proxy will strip it" }],
    request: { method: "POST", url: `${CFG.proxyUrl}/token`, headers: { "Content-Type": "application/x-www-form-urlencoded" },
      form: [["grant_type", "authorization_code"], ["code", CFG.authCode], ["code_verifier", CFG.codeVerifier],
        ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri], ["resource", CFG.proxyUrl, "stripped"]] } },
  { n: 10, from: "proxy", to: "entra", dir: "req", label: "POST Entra /token (−resource)",
    title: "Proxy forwards to Entra /token (★strips resource)",
    desc: "The proxy strips the resource parameter from the form and forwards the rest unchanged to Entra. It does not touch the scope (Entra derives the scope from the code during token exchange).",
    highlights: [{ type: "removed", text: "resource removed; rest of the form forwarded unchanged" }],
    request: { method: "POST", url: CFG.entraToken,
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      form: [["grant_type", "authorization_code"], ["code", CFG.authCode], ["code_verifier", CFG.codeVerifier],
        ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri]] } },
  { n: 11, from: "entra", to: "proxy", dir: "res", label: "real token",
    title: "Entra returns the [real Entra token]",
    desc: "Entra returns the real access_token (+ refresh/id). Decoded: aud is pinned to our API by the scope, oid is the user identity, no AADSTS9010010 encountered.",
    highlights: [{ type: "key", text: "aud=our API, scp=user_impersonation, oid=user — identity is preserved" }],
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: TOKEN_BODY,
      decoded: { title: "Decoded access_token (JWT payload, for educational purposes)", claims: TOKEN_CLAIMS } } },
  { n: 12, from: "proxy", to: "client", dir: "res", label: "relay token",
    title: "Proxy relays the token back to the client unchanged",
    desc: "The proxy relays the Entra response status code and body back unchanged, never logging or modifying the content. The client ultimately holds the real Entra token.",
    highlights: [{ type: "key", text: "The proxy does not issue its own token or store anything — it is purely a pass-through" }],
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: TOKEN_BODY } },
  { n: 13, from: "client", to: "mcp", dir: "req", label: "Bearer + tools/list",
    title: "Call MCP with Bearer token",
    desc: "The client first initializes to get an mcp-session-id, then POSTs tools/list — both with the real Entra token. (This step shows the tools/list request.)",
    request: { method: "POST", url: CFG.proxyUrl,
      headers: { "Authorization": "Bearer eyJ0eXAiOiJKV1Qi...<real Entra token>", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "mcp-session-id": "b3f1c9a2-...(returned by initialize)" },
      body: jrpc({ jsonrpc: "2.0", id: 2, method: "tools/list" }) } },
  { n: 14, from: "mcp", to: "client", dir: "res", label: "tools ✓ (group guardrail)",
    title: "Validation → OBO → group guardrail → return tools",
    desc: "The MCP endpoint validates using the same AzureJWTVerifier as /mcp, then uses OBO to check the user's AD groups and filters the tools accordingly.",
    actorNote: { title: "What the server does internally in this step", lines: [
      "1. AzureJWTVerifier validates aud=our API, scp, and signature.",
      "2. OBO exchanges for a Graph token → checkMemberGroups checks which known groups the user belongs to (cached by oid).",
      "3. Filters tools by group: diagnose group → diagnose_bash; action group → action_bash."] },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json", "mcp-session-id": "b3f1c9a2-..." }, body: TOOLS_RESULT } },
];

// ══════════════════════════════════════════════════════════════════════════
//  Flow B: mcp (direct Entra, no proxy) — 11 steps, 3 lanes (VS Code uses this)
// ══════════════════════════════════════════════════════════════════════════
const LANES_DIRECT = [
  { key: "client", label: "Demo Client", sub: "Simulates VS Code · Does not send resource", icon: "🧩" },
  { key: "mcp", label: "/mcp Endpoint + PRM", sub: "MCP server (no proxy)", icon: "⚙️" },
  { key: "entra", label: "Entra ID", sub: "Directly the Authorization Server (AS)", icon: "🔐" },
];

const STEPS_DIRECT = [
  { n: 1, from: "client", to: "mcp", dir: "req", label: "POST /mcp (no auth)",
    title: "Call /mcp without token",
    desc: "Same as the proxy flow: first hit a 401 without a token, discover the authorization server from the response. The difference is entirely in 'who is the authorization server'.",
    request: { method: "POST", url: CFG.mcpUrl,
      headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" }, body: INIT_BODY } },
  { n: 2, from: "mcp", to: "client", dir: "res", label: "401 WWW-Authenticate",
    title: "401 + WWW-Authenticate",
    desc: "The 401 provides a resource_metadata pointer pointing to the PRM for /mcp.",
    highlights: [{ type: "key", text: "Pointer points to …/oauth-protected-resource/mcp" }],
    response: { status: 401, statusText: "Unauthorized",
      headers: { "content-type": "application/json",
        "www-authenticate": `Bearer resource_metadata="${CFG.base}/.well-known/oauth-protected-resource/mcp"` },
      body: jrpc({ error: "invalid_token", error_description: "Authentication required" }) } },
  { n: 3, from: "client", to: "mcp", dir: "req", label: "GET protected-resource",
    title: "Discovery ①: Protected-Resource Metadata (RFC 9728)",
    desc: "GET the PRM for /mcp. ★★ The biggest difference from the proxy flow: authorization_servers points directly to Entra, with no intermediate layer.",
    highlights: [{ type: "diff", text: "authorization_servers = Entra itself (not the proxy) — the client will connect directly to Entra" }],
    request: { method: "GET", url: `${CFG.base}/.well-known/oauth-protected-resource/mcp`, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ resource: CFG.mcpUrl, authorization_servers: [CFG.entraIssuer], scopes_supported: [CFG.apiScope], bearer_methods_supported: ["header"] }) } },
  { n: 4, from: "client", to: "entra", dir: "req", label: "GET Entra discovery document",
    title: "Discovery ②: Request discovery document directly from Entra (OIDC)",
    desc: "Since the AS is Entra, the client directly GETs Entra's openid-configuration to obtain the authorize/token/jwks endpoints.",
    highlights: [
      { type: "key", text: "issuer/authorize/token are all Entra's own endpoints" },
      { type: "key", text: "Again, no registration_endpoint → VS Code uses a static client, no DCR" }],
    request: { method: "GET", url: CFG.entraOpenid, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ issuer: CFG.entraIssuer, authorization_endpoint: CFG.entraAuthorize, token_endpoint: CFG.entraToken,
        jwks_uri: CFG.jwks, response_types_supported: ["code", "id_token", "code id_token", "id_token token"],
        response_modes_supported: ["query", "fragment", "form_post"], scopes_supported: ["openid", "profile", "email", "offline_access"],
        subject_types_supported: ["pairwise"], id_token_signing_alg_values_supported: ["RS256"],
        "//registration_endpoint": "Field missing → DCR not advertised" }) } },
  { n: 5, from: "client", to: "entra", dir: "req", label: "GET Entra /authorize (no resource)",
    title: "Client connects directly to Entra /authorize (no resource)",
    desc: "The client, with PKCE/state/loopback redirect_uri, hits Entra's /authorize directly. ★ Key: does not send resource (like VS Code).",
    highlights: [{ type: "clean", text: "No resource parameter → no proxy needed, and AADSTS9010010 is avoided" }],
    request: { method: "GET", urlBase: CFG.entraAuthorize, headers: {},
      query: [["response_type", "code"], ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri],
        ["scope", `${CFG.apiScope} offline_access openid profile`], ["code_challenge", CFG.codeChallenge],
        ["code_challenge_method", "S256"], ["state", CFG.state]] } },
  { n: 6, from: "client", to: "entra", dir: "note", label: "🔒 Browser ↔ Entra Login",
    title: "Browser ↔ Entra Interactive Login",
    desc: "The browser opens the Entra login page; the user enters their credentials, passes MFA/Conditional Access, and provides consent.",
    note: { title: "Same hop as in the proxy flow", lines: [
      "Both flows are identical here: a real interactive login, with the user's real IP + MFA.",
      "The only difference is that in this flow, the client reaches Entra [directly], without going through any proxy."] } },
  { n: 7, from: "entra", to: "client", dir: "res", label: "302 → loopback (code)",
    title: "Entra 302 back to loopback (with code)",
    desc: "Login successful, Entra 302s back to the client's loopback with the authorization code + the original state.",
    highlights: [{ type: "key", text: "code goes directly to the client's loopback" }],
    response: { status: 302, statusText: "Found",
      headers: { "location": { urlBase: CFG.redirectUri, query: [["code", CFG.authCode], ["state", CFG.state]] } } } },
  { n: 8, from: "client", to: "entra", dir: "req", label: "POST Entra /token (no resource)",
    title: "Client connects directly to Entra /token to exchange for token (no resource)",
    desc: "The client directly POSTs to Entra's /token, using code + code_verifier to exchange for a token. Public client, no secret; again, no resource parameter.",
    highlights: [{ type: "clean", text: "No resource; no proxy forwarding — two fewer hops" }],
    request: { method: "POST", url: CFG.entraToken,
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      form: [["grant_type", "authorization_code"], ["code", CFG.authCode], ["code_verifier", CFG.codeVerifier],
        ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri]] } },
  { n: 9, from: "entra", to: "client", dir: "res", label: "real token",
    title: "Entra returns the [real Entra token]",
    desc: "The same type of real token as obtained in the proxy flow: aud is pinned to our API by the scope, oid is the user.",
    highlights: [{ type: "key", text: "aud/scp/oid are identical to the proxy flow — different paths, same destination" }],
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: TOKEN_BODY,
      decoded: { title: "Decoded access_token (JWT payload, for educational purposes)", claims: TOKEN_CLAIMS } } },
  { n: 10, from: "client", to: "mcp", dir: "req", label: "Bearer + tools/list",
    title: "Call /mcp with Bearer token",
    desc: "After initialization, the client POSTs tools/list with the real Entra token.",
    request: { method: "POST", url: CFG.mcpUrl,
      headers: { "Authorization": "Bearer eyJ0eXAiOiJKV1Qi...<real Entra token>", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "mcp-session-id": "a1c2...(returned by initialize)" },
      body: jrpc({ jsonrpc: "2.0", id: 2, method: "tools/list" }) } },
  { n: 11, from: "mcp", to: "client", dir: "res", label: "tools ✓ (group guardrail)",
    title: "Validation → OBO → group guardrail → return tools",
    desc: "The same AzureJWTVerifier + OBO + group guardrail — identical to the last step of the proxy flow.",
    actorNote: { title: "What the server does internally in this step (same as proxy flow)", lines: [
      "1. AzureJWTVerifier validates aud=our API, scp, and signature.",
      "2. OBO → checkMemberGroups checks user groups (cached by oid).",
      "3. Filters tools by group."] },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json", "mcp-session-id": "a1c2..." }, body: TOOLS_RESULT } },
];

// ── Export both flows ─────────────────────────────────────────────────────────────
window.FLOWS = {
  CFG,
  mcpproxy: {
    key: "mcpproxy", name: "MCP proxy (strip resource)",
    tagline: "The client insists on sending the resource parameter (Claude Code / opencode) → a proxy layer is inserted to strip the resource parameter before forwarding to Entra.",
    lanes: LANES_PROXY, steps: STEPS_PROXY,
    live: { discover: [1, 4], authorize: [5, 6], login: [7, 14] },
  },
  mcp: {
    key: "mcp", name: "MCP (direct Entra)",
    tagline: "The client does not send the resource parameter (VS Code) → the PRM points directly to Entra, the client connects directly to Entra, no proxy needed, no AADSTS9010010.",
    lanes: LANES_DIRECT, steps: STEPS_DIRECT,
    live: { discover: [1, 4], authorize: [5, 5], login: [6, 11] },
  },
};

})();

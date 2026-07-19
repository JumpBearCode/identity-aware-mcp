/*
 * steps.js — 两条流程的“剧本”。
 *
 *   ● mcpproxy 流程（14 步）：客户端坚持发 RFC 8707 的 resource（Claude Code/opencode），
 *     所以插一层 /mcpproxy 代理，把 resource 删掉再转发 Entra。
 *   ● mcp 流程（11 步）：客户端不发 resource（VS Code），PRM 直接指向 Entra，
 *     客户端与 Entra 直连，不需要代理、也不会撞 AADSTS9010010。
 *
 * 元数据（标题/泳道/方向/label/highlight）两条流程各自定义，动画逻辑（app.js）完全共用。
 * Demo 模式用贴近真实抓包的数据；Live 模式由 server.py 用真实抓包覆盖同名步骤。
 *
 * 对应文档：docs/multi-client-implementation/实现说明-方案A-mcpproxy-resource剥离代理-代码与安全分析.md
 */
(function () {

// ── 真实部署里的常量（全部来自项目配置 / 抓包）──────────────────────────────
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

// 两条流程共用的 initialize / tools/list 数据
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
//  流程 A：mcpproxy（剥离 resource）—— 14 步，4 条泳道
// ══════════════════════════════════════════════════════════════════════════
const LANES_PROXY = [
  { key: "client", label: "Demo Client", sub: "仿 Claude Code · 静态注册", icon: "🧩" },
  { key: "proxy", label: "/mcpproxy 代理路由", sub: "本容器自写的 OAuth 胶水", icon: "🪄" },
  { key: "entra", label: "Entra ID", sub: "login.microsoftonline.com", icon: "🔐" },
  { key: "mcp", label: "/mcpproxy MCP 端点", sub: "同一个 AzureJWTVerifier", icon: "⚙️" },
];

const STEPS_PROXY = [
  { n: 1, from: "client", to: "mcp", dir: "req", label: "POST /mcpproxy (no auth)",
    title: "无 token 调用 MCP 端点",
    desc: "客户端还没有 token，直接向 MCP 端点发起 initialize，故意“撞”401，好从响应里发现授权服务器。",
    request: { method: "POST", url: CFG.proxyUrl,
      headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" },
      body: INIT_BODY } },
  { n: 2, from: "mcp", to: "client", dir: "res", label: "401 WWW-Authenticate",
    title: "401 + WWW-Authenticate",
    desc: "服务端回 401，并在 WWW-Authenticate 里给出 resource_metadata 地址（RFC 9728 的“去哪找授权服务器”指针）。",
    highlights: [{ type: "key", text: "resource_metadata 就是下一步要 GET 的地址" }],
    response: { status: 401, statusText: "Unauthorized",
      headers: { "content-type": "application/json",
        "www-authenticate": `Bearer error="invalid_token", error_description="Authentication required", resource_metadata="${CFG.base}/.well-known/oauth-protected-resource/mcpproxy"` },
      body: jrpc({ error: "invalid_token", error_description: "Authentication required" }) } },
  { n: 3, from: "client", to: "proxy", dir: "req", label: "GET protected-resource",
    title: "发现 ①：Protected-Resource Metadata (RFC 9728)",
    desc: "顺着指针 GET 这份元数据。它说：我的授权服务器就是我自己（代理），不是 Entra。",
    highlights: [{ type: "key", text: "authorization_servers 指向代理自己 → 客户端把代理当成 AS" }],
    request: { method: "GET", url: `${CFG.base}/.well-known/oauth-protected-resource/mcpproxy`, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ resource: CFG.proxyUrl, authorization_servers: [CFG.proxyUrl], scopes_supported: [CFG.apiScope], bearer_methods_supported: ["header"] }) } },
  { n: 4, from: "client", to: "proxy", dir: "req", label: "GET auth-server meta",
    title: "发现 ②：Authorization-Server Metadata (RFC 8414)",
    desc: "GET 授权服务器元数据拿 authorize/token 端点。关键：没有 registration_endpoint，所以不 DCR，用静态 client_id。",
    highlights: [
      { type: "key", text: "没有 registration_endpoint → 不触发动态注册(DCR)" },
      { type: "key", text: "token_endpoint_auth_methods=[\"none\"] → public client，无 secret" }],
    request: { method: "GET", url: `${CFG.base}/.well-known/oauth-authorization-server/mcpproxy`, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ issuer: CFG.proxyUrl, authorization_endpoint: `${CFG.proxyUrl}/authorize`, token_endpoint: `${CFG.proxyUrl}/token`,
        response_types_supported: ["code"], response_modes_supported: ["query", "fragment"],
        grant_types_supported: ["authorization_code", "refresh_token"], code_challenge_methods_supported: ["S256"],
        token_endpoint_auth_methods_supported: ["none"], scopes_supported: [CFG.apiScope, "offline_access", "openid", "profile"] }) } },
  { n: 5, from: "client", to: "proxy", dir: "req", label: "GET /authorize (+resource)",
    title: "客户端发起 /authorize（带 resource）",
    desc: "客户端生成 PKCE 和 state，带着静态 client_id、loopback redirect_uri，以及 RFC 8707 的 resource 参数请求授权。",
    highlights: [{ type: "removed-preview", text: "注意带了 resource=… —— 下一步代理会把它删掉" }],
    request: { method: "GET", urlBase: `${CFG.proxyUrl}/authorize`, headers: {},
      query: [["response_type", "code"], ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri],
        ["scope", `${CFG.apiScope} offline_access`], ["code_challenge", CFG.codeChallenge], ["code_challenge_method", "S256"],
        ["state", CFG.state], ["resource", CFG.proxyUrl, "stripped"]] } },
  { n: 6, from: "proxy", to: "client", dir: "res", label: "302 → Entra (−resource)",
    title: "代理 302 → Entra（★删掉 resource）",
    desc: "代理唯一的实质动作：删 resource、补 offline_access/openid/profile，302 到 Entra。PKCE/state/redirect_uri 原样透传。",
    highlights: [
      { type: "removed", text: "resource 参数已删除 —— 绕过 AADSTS9010010 的关键" },
      { type: "added", text: "补齐 openid / profile" }],
    response: { status: 302, statusText: "Found",
      headers: { "location": { urlBase: CFG.entraAuthorize,
        query: [["response_type", "code"], ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri],
          ["scope", `${CFG.apiScope} offline_access openid profile`, "added"], ["code_challenge", CFG.codeChallenge],
          ["code_challenge_method", "S256"], ["state", CFG.state]] } } } },
  { n: 7, from: "client", to: "entra", dir: "note", label: "🔒 浏览器 ↔ Entra 登录",
    title: "浏览器 ↔ Entra 交互式登录",
    desc: "浏览器跟着 302 打开 Entra 登录页。用户在这里输账号密码、过 MFA/条件访问。代理不参与、看不到凭据。",
    note: { title: "为什么这一跳没有 JSON？", lines: [
      "这是真实的浏览器交互式登录，不是后端 API 调用。",
      "用户真实 IP + MFA/条件访问都在这一跳 —— 强认证不受代理影响。",
      "代理上一步发完 302 就退场了，全程隐形。"] } },
  { n: 8, from: "entra", to: "client", dir: "res", label: "302 → loopback (code)",
    title: "Entra 302 回 loopback（带 code）",
    desc: "登录成功后 Entra 直接 302 回客户端 loopback，带 code + 原样 state。代理完全没经手 code —— 这就是“几乎无状态”的来源。",
    highlights: [{ type: "key", text: "回调直达客户端 loopback，代理看不到 code" }],
    response: { status: 302, statusText: "Found",
      headers: { "location": { urlBase: CFG.redirectUri, query: [["code", CFG.authCode], ["state", CFG.state]] } } } },
  { n: 9, from: "client", to: "proxy", dir: "req", label: "POST /token (+resource)",
    title: "客户端拿 code 换 token（带 resource）",
    desc: "客户端向代理 /token POST，用 authorization_code + code_verifier 换 token。public client 无 secret；又带上了 resource。",
    highlights: [{ type: "removed-preview", text: "form 里又出现 resource=… —— 代理照删" }],
    request: { method: "POST", url: `${CFG.proxyUrl}/token`, headers: { "Content-Type": "application/x-www-form-urlencoded" },
      form: [["grant_type", "authorization_code"], ["code", CFG.authCode], ["code_verifier", CFG.codeVerifier],
        ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri], ["resource", CFG.proxyUrl, "stripped"]] } },
  { n: 10, from: "proxy", to: "entra", dir: "req", label: "POST Entra /token (−resource)",
    title: "代理转发到 Entra /token（★删掉 resource）",
    desc: "代理把 form 里的 resource 删掉，其余原样转发 Entra。不碰 scope（换 token 时 Entra 从 code 推 scope）。",
    highlights: [{ type: "removed", text: "resource 已删除；其余 form 原样转发" }],
    request: { method: "POST", url: CFG.entraToken,
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      form: [["grant_type", "authorization_code"], ["code", CFG.authCode], ["code_verifier", CFG.codeVerifier],
        ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri]] } },
  { n: 11, from: "entra", to: "proxy", dir: "res", label: "real token",
    title: "Entra 发回【真 Entra token】",
    desc: "Entra 返回真 access_token(+refresh/id)。解开看：aud 被 scope 钉成我们的 API，oid 是用户身份，全程无 AADSTS9010010。",
    highlights: [{ type: "key", text: "aud=我们的 API、scp=user_impersonation、oid=用户 —— 身份保真" }],
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: TOKEN_BODY,
      decoded: { title: "解开 access_token（JWT payload，教学用）", claims: TOKEN_CLAIMS } } },
  { n: 12, from: "proxy", to: "client", dir: "res", label: "relay token",
    title: "代理原样把 token 回给客户端",
    desc: "代理把 Entra 的响应状态码 + body 原样回传，绝不落日志、不改内容。客户端最终握着真 Entra token。",
    highlights: [{ type: "key", text: "代理不签自己的 token、不存任何东西 —— 只是透传" }],
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: TOKEN_BODY } },
  { n: 13, from: "client", to: "mcp", dir: "req", label: "Bearer + tools/list",
    title: "带 Bearer token 调用 MCP",
    desc: "客户端先 initialize 拿 mcp-session-id，再 POST tools/list —— 都带上真 Entra token。（这里展示 tools/list 这一发。）",
    request: { method: "POST", url: CFG.proxyUrl,
      headers: { "Authorization": "Bearer eyJ0eXAiOiJKV1Qi...<真 Entra token>", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "mcp-session-id": "b3f1c9a2-...(initialize 返回)" },
      body: jrpc({ jsonrpc: "2.0", id: 2, method: "tools/list" }) } },
  { n: 14, from: "mcp", to: "client", dir: "res", label: "tools ✓ (group 门控)",
    title: "校验 → OBO → group 门控 → 返回工具",
    desc: "MCP 端点用与 /mcp 完全相同的 AzureJWTVerifier 校验，再用 OBO 查用户 AD group，据此过滤工具。",
    actorNote: { title: "服务端在这一步内部做的事", lines: [
      "1. AzureJWTVerifier 校验 aud=我们的 API、scp、签名。",
      "2. OBO 换 Graph token → checkMemberGroups 查用户属于哪些已知组（按 oid 缓存）。",
      "3. 按组过滤工具：diagnose 组 → diagnose_bash；action 组 → action_bash。"] },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json", "mcp-session-id": "b3f1c9a2-..." }, body: TOOLS_RESULT } },
];

// ══════════════════════════════════════════════════════════════════════════
//  流程 B：mcp（直连 Entra，无 proxy）—— 11 步，3 条泳道（VS Code 走这条）
// ══════════════════════════════════════════════════════════════════════════
const LANES_DIRECT = [
  { key: "client", label: "Demo Client", sub: "仿 VS Code · 不发 resource", icon: "🧩" },
  { key: "mcp", label: "/mcp 端点 + PRM", sub: "MCP server（无代理）", icon: "⚙️" },
  { key: "entra", label: "Entra ID", sub: "直接就是授权服务器 (AS)", icon: "🔐" },
];

const STEPS_DIRECT = [
  { n: 1, from: "client", to: "mcp", dir: "req", label: "POST /mcp (no auth)",
    title: "无 token 调用 /mcp",
    desc: "和代理流程一样：先无 token 撞 401，从响应里发现授权服务器。区别全在“授权服务器是谁”。",
    request: { method: "POST", url: CFG.mcpUrl,
      headers: { "Content-Type": "application/json", "Accept": "application/json, text/event-stream" }, body: INIT_BODY } },
  { n: 2, from: "mcp", to: "client", dir: "res", label: "401 WWW-Authenticate",
    title: "401 + WWW-Authenticate",
    desc: "401 里给出 resource_metadata 指针，指向 /mcp 的 PRM。",
    highlights: [{ type: "key", text: "指针指向 …/oauth-protected-resource/mcp" }],
    response: { status: 401, statusText: "Unauthorized",
      headers: { "content-type": "application/json",
        "www-authenticate": `Bearer resource_metadata="${CFG.base}/.well-known/oauth-protected-resource/mcp"` },
      body: jrpc({ error: "invalid_token", error_description: "Authentication required" }) } },
  { n: 3, from: "client", to: "mcp", dir: "req", label: "GET protected-resource",
    title: "发现 ①：Protected-Resource Metadata (RFC 9728)",
    desc: "GET /mcp 的 PRM。★★ 与代理流程最大的不同：authorization_servers 直接指向 Entra，没有中间层。",
    highlights: [{ type: "diff", text: "authorization_servers = Entra 本尊（不是代理自己）—— 客户端将直连 Entra" }],
    request: { method: "GET", url: `${CFG.base}/.well-known/oauth-protected-resource/mcp`, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ resource: CFG.mcpUrl, authorization_servers: [CFG.entraIssuer], scopes_supported: [CFG.apiScope], bearer_methods_supported: ["header"] }) } },
  { n: 4, from: "client", to: "entra", dir: "req", label: "GET Entra 发现文档",
    title: "发现 ②：直接问 Entra 要发现文档 (OIDC)",
    desc: "既然 AS 就是 Entra，客户端直接 GET Entra 的 openid-configuration 拿 authorize/token/jwks。",
    highlights: [
      { type: "key", text: "issuer/authorize/token 全是 Entra 自己的端点" },
      { type: "key", text: "同样没有 registration_endpoint → VS Code 用静态 client，不 DCR" }],
    request: { method: "GET", url: CFG.entraOpenid, headers: { "Accept": "application/json" } },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" },
      body: jrpc({ issuer: CFG.entraIssuer, authorization_endpoint: CFG.entraAuthorize, token_endpoint: CFG.entraToken,
        jwks_uri: CFG.jwks, response_types_supported: ["code", "id_token", "code id_token", "id_token token"],
        response_modes_supported: ["query", "fragment", "form_post"], scopes_supported: ["openid", "profile", "email", "offline_access"],
        subject_types_supported: ["pairwise"], id_token_signing_alg_values_supported: ["RS256"],
        "//registration_endpoint": "字段缺失 → 不宣告 DCR" }) } },
  { n: 5, from: "client", to: "entra", dir: "req", label: "GET Entra /authorize (no resource)",
    title: "客户端直连 Entra /authorize（无 resource）",
    desc: "客户端带 PKCE/state/loopback redirect_uri，直接打 Entra 的 /authorize。★ 关键：不发 resource（像 VS Code）。",
    highlights: [{ type: "clean", text: "没有 resource 参数 → 不需要代理，也不会撞 AADSTS9010010" }],
    request: { method: "GET", urlBase: CFG.entraAuthorize, headers: {},
      query: [["response_type", "code"], ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri],
        ["scope", `${CFG.apiScope} offline_access openid profile`], ["code_challenge", CFG.codeChallenge],
        ["code_challenge_method", "S256"], ["state", CFG.state]] } },
  { n: 6, from: "client", to: "entra", dir: "note", label: "🔒 浏览器 ↔ Entra 登录",
    title: "浏览器 ↔ Entra 交互式登录",
    desc: "浏览器打开 Entra 登录页，用户输账号密码、过 MFA/条件访问、consent。",
    note: { title: "和代理流程一样的一跳", lines: [
      "两条流程到这里是相同的：真实交互式登录，用户真实 IP + MFA。",
      "唯一区别是：这条流程里客户端是【直接】到的 Entra，没经过任何代理。"] } },
  { n: 7, from: "entra", to: "client", dir: "res", label: "302 → loopback (code)",
    title: "Entra 302 回 loopback（带 code）",
    desc: "登录成功，Entra 302 回客户端 loopback，带 authorization code + 原样 state。",
    highlights: [{ type: "key", text: "code 直达客户端 loopback" }],
    response: { status: 302, statusText: "Found",
      headers: { "location": { urlBase: CFG.redirectUri, query: [["code", CFG.authCode], ["state", CFG.state]] } } } },
  { n: 8, from: "client", to: "entra", dir: "req", label: "POST Entra /token (no resource)",
    title: "客户端直连 Entra /token 换 token（无 resource）",
    desc: "客户端直接 POST Entra 的 /token，用 code + code_verifier 换 token。public client 无 secret；同样不带 resource。",
    highlights: [{ type: "clean", text: "没有 resource；没有代理转发 —— 少两跳" }],
    request: { method: "POST", url: CFG.entraToken,
      headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
      form: [["grant_type", "authorization_code"], ["code", CFG.authCode], ["code_verifier", CFG.codeVerifier],
        ["client_id", CFG.clientId], ["redirect_uri", CFG.redirectUri]] } },
  { n: 9, from: "entra", to: "client", dir: "res", label: "real token",
    title: "Entra 发回【真 Entra token】",
    desc: "和代理流程拿到的是同一种真 token：aud 由 scope 钉死成我们的 API，oid 是用户。",
    highlights: [{ type: "key", text: "aud/scp/oid 与代理流程完全一致 —— 两条路殊途同归" }],
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json" }, body: TOKEN_BODY,
      decoded: { title: "解开 access_token（JWT payload，教学用）", claims: TOKEN_CLAIMS } } },
  { n: 10, from: "client", to: "mcp", dir: "req", label: "Bearer + tools/list",
    title: "带 Bearer token 调用 /mcp",
    desc: "客户端 initialize 后 POST tools/list，带真 Entra token。",
    request: { method: "POST", url: CFG.mcpUrl,
      headers: { "Authorization": "Bearer eyJ0eXAiOiJKV1Qi...<真 Entra token>", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "mcp-session-id": "a1c2...(initialize 返回)" },
      body: jrpc({ jsonrpc: "2.0", id: 2, method: "tools/list" }) } },
  { n: 11, from: "mcp", to: "client", dir: "res", label: "tools ✓ (group 门控)",
    title: "校验 → OBO → group 门控 → 返回工具",
    desc: "同一个 AzureJWTVerifier + OBO + group 门控 —— 和代理流程的最后一步完全一样。",
    actorNote: { title: "服务端在这一步内部做的事（与代理流程相同）", lines: [
      "1. AzureJWTVerifier 校验 aud=我们的 API、scp、签名。",
      "2. OBO → checkMemberGroups 查用户组（按 oid 缓存）。",
      "3. 按组过滤工具。"] },
    response: { status: 200, statusText: "OK", headers: { "content-type": "application/json", "mcp-session-id": "a1c2..." }, body: TOOLS_RESULT } },
];

// ── 导出两条流程 ─────────────────────────────────────────────────────────────
window.FLOWS = {
  CFG,
  mcpproxy: {
    key: "mcpproxy", name: "MCP proxy（剥离 resource）",
    tagline: "客户端坚持发 resource（Claude Code / opencode）→ 插一层代理把 resource 删掉再转发 Entra。",
    lanes: LANES_PROXY, steps: STEPS_PROXY,
    live: { discover: [1, 4], authorize: [5, 6], login: [7, 14] },
  },
  mcp: {
    key: "mcp", name: "MCP（直连 Entra）",
    tagline: "客户端不发 resource（VS Code）→ PRM 直接指向 Entra，客户端与 Entra 直连，无需代理、不撞 AADSTS9010010。",
    lanes: LANES_DIRECT, steps: STEPS_DIRECT,
    live: { discover: [1, 4], authorize: [5, 5], login: [6, 11] },
  },
};

})();

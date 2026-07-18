"""
Identity-aware MCP server for Azure DataOps.

- Validates Entra JWTs (RemoteAuthProvider + AzureJWTVerifier)
- Looks up user group membership via OBO + Graph
- Exposes diagnose_bash / action_bash; routes to the appropriate worker container
- Holds NO Azure data-plane permissions — workers carry their own SPs
"""

import logging
import os

import httpx
import audit
import redact
from cache import (
    GroupCache,
    InMemoryBackend,
    RedisBackend,
    UserSessionCache,
    make_redis_client,
)
from executor import SessionCtx, make_executor
from session import SessionResolver
from fastmcp import Context, FastMCP
from fastmcp.server.auth import AuthContext, RemoteAuthProvider
from fastmcp.server.auth.providers.azure import AzureJWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from msal import ConfidentialClientApplication, TokenCache
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("dataops-mcp")

TENANT_ID = os.environ["AZURE_TENANT_ID"]
MCP_APP_ID = os.environ["MCP_APP_ID"]
MCP_CLIENT_SECRET = os.environ["MCP_CLIENT_SECRET"]
DIAGNOSE_GROUP_ID = os.environ["DIAGNOSE_GROUP_ID"]
ACTION_GROUP_ID = os.environ["ACTION_GROUP_ID"]
BASE_URL = os.environ.get("MCP_SERVER_BASE_URL", "http://localhost:8080")

# Plan A: expose a second /mcpproxy endpoint that strips the RFC 8707 `resource`
# param before talking to Entra, so clients like Claude Code / opencode dodge
# AADSTS9010010. /mcp (VS Code, direct-to-Entra) is left untouched. See mcpproxy.py
# and docs/multi-client-implementation/计划-mcpproxy-*.md. Default on; set false to
# fall back to /mcp only.
MCPPROXY_ENABLED = os.environ.get("MCPPROXY_ENABLED", "true").lower() in ("1", "true", "yes")

# Execution backend: local docker workers (default) or ACA sandboxes. Both sit
# behind the same Executor interface; see executor.py / sandbox_manager.py.
executor = make_executor()

# Session derivation: a Redis-backed 30-min sliding window per user (see
# session.py). Falls back to transport ids if no Redis is configured.
SESSION_TTL = int(os.environ.get("MCP_SESSION_TTL", "1800"))
REDIS_URL = os.environ.get("REDIS_URL")
_redis = make_redis_client(REDIS_URL) if REDIS_URL else None
session_resolver = (
    SessionResolver(UserSessionCache(RedisBackend(_redis, ttl=SESSION_TTL)))
    if _redis is not None
    else None
)

# --- JWT verification: validate Entra access tokens against Entra JWKS ---
verifier = AzureJWTVerifier(
    client_id=MCP_APP_ID,
    tenant_id=TENANT_ID,
    required_scopes=["user_impersonation"],
)
auth = RemoteAuthProvider(
    token_verifier=verifier,
    authorization_servers=[f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"],
    base_url=BASE_URL,
)

# --- MSAL client for OBO (used to call Graph on behalf of the user) ---
msal_app = ConfidentialClientApplication(
    client_id=MCP_APP_ID,
    client_credential=MCP_CLIENT_SECRET,
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
    token_cache=TokenCache(),
)


# --- Group-membership cache ---------------------------------------------------
# Cache "which of OUR groups this user belongs to", keyed by oid, to collapse the
# repeated Graph calls (tools/list runs the auth check once per tool; tools/call
# runs it again). Backed by an in-memory store today; swap InMemoryBackend for a
# RedisBackend to share across pods. See cache.py and docs/MCP-鉴权-缓存与凭据演进.md.
GROUP_CACHE_TTL = 300  # seconds; also the max window a revoked user stays allowed
KNOWN_GROUPS = [DIAGNOSE_GROUP_ID, ACTION_GROUP_ID]

group_cache = GroupCache(InMemoryBackend(ttl=GROUP_CACHE_TTL))


async def _user_groups(ctx: AuthContext) -> set[str]:
    """Subset of KNOWN_GROUPS the current user belongs to, cached by oid.

    On a cache miss, one OBO exchange + Graph POST /me/checkMemberGroups resolves
    all known groups at once (fixed-size payload, no pagination, transitive),
    then caches the result.
    """
    oid = ctx.token.claims.get("oid") if hasattr(ctx.token, "claims") else None
    if oid is not None and (cached := await group_cache.get(oid)) is not None:
        return cached

    obo = msal_app.acquire_token_on_behalf_of(
        user_assertion=ctx.token.token,
        scopes=["https://graph.microsoft.com/.default"],
    )
    if "access_token" not in obo:
        logger.error("OBO failed: %s", obo.get("error_description"))
        return set()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://graph.microsoft.com/v1.0/me/checkMemberGroups",
            headers={"Authorization": f"Bearer {obo['access_token']}"},
            json={"groupIds": KNOWN_GROUPS},
        )
        r.raise_for_status()
        groups = set(r.json().get("value", []))

    if oid is not None:
        await group_cache.set(oid, groups)
    return groups


def _require_group(group_id: str):
    async def check(ctx: AuthContext) -> bool:
        if ctx.token is None:
            return False
        return group_id in await _user_groups(ctx)

    return check


require_diagnose = _require_group(DIAGNOSE_GROUP_ID)
require_action = _require_group(ACTION_GROUP_ID)


# --- Middleware: stash user oid + Session/Conversation ids for routing & audit -
class UserAuthMiddleware(Middleware):
    """Stash the identifiers downstream tools need.

    `user_oid` is the audit identity. `session_id` / `conversation_id` drive the
    ACA Session-sticky routing and Blob layout; the local backend ignores them.

    Phase 4 swaps `_derive_ids` for the Redis-backed sliding-window logic in
    `session.py`. For now ids come straight off the FastMCP context so the
    state keys exist and the wiring is in place.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        token = get_access_token()
        claims = token.claims if token and hasattr(token, "claims") else {}
        oid = claims.get("oid")
        fctx = context.fastmcp_context
        if fctx is not None:
            session_id, conversation_id = await _derive_ids(oid, fctx)
            await fctx.set_state("user_oid", oid)
            await fctx.set_state("session_id", session_id)
            await fctx.set_state("conversation_id", conversation_id)
            # Attribution (docs/oid-log-tracking): mint a per-call correlation id
            # (also injected into the worker's User-Agent, layer 2) plus the
            # identity/ip that only exist at the ingress; _exec emits them.
            await fctx.set_state("user_upn", claims.get("preferred_username") or claims.get("upn"))
            await fctx.set_state("client_ip", audit.client_ip())
            await fctx.set_state("correlation_id", audit.new_correlation_id())
        return await call_next(context)


async def _derive_ids(oid, fctx) -> tuple[str | None, str | None]:
    """Session/Conversation ids — Redis sliding window when available.

    Never let a Redis hiccup hang or fail a tool call: fall back to transport
    ids (degraded stickiness) on any error.
    """
    transport_sid = getattr(fctx, "session_id", None)
    request_id = getattr(fctx, "request_id", None)
    if session_resolver is not None:
        try:
            return await session_resolver.resolve(oid, transport_sid, request_id)
        except Exception as e:
            logger.warning("session resolve via Redis failed (%s); using transport ids", e)
    return (transport_sid or oid), request_id


mcp = FastMCP("Azure DataOps", auth=auth, middleware=[UserAuthMiddleware()])


async def _exec(group: str, command: str, ctx: Context, explanation: str | None = None):
    """Build the routing context off stashed state and run on the backend."""
    correlation_id = await ctx.get_state("correlation_id")
    sctx = SessionCtx(
        user_oid=await ctx.get_state("user_oid"),
        session_id=await ctx.get_state("session_id"),
        conversation_id=await ctx.get_state("conversation_id"),
        group=group,  # type: ignore[arg-type]
        correlation_id=correlation_id,
    )
    result = await executor.exec(sctx, command)
    # Post-exec gate (docs/action-gate-guardrail/护栏落地方案-...md §2.1): mask any
    # secret in the output before it leaves the server. Runs for BOTH diagnose and
    # action (this is the shared path). Always REDACT — never blocks, never audits.
    result = redact.redact_result(result, command=command)
    # One structured audit row per tool call (replaces the old scattered
    # logger.info lines). correlation_id joins this row to the native Azure log
    # via the User-Agent the executor injects. Never blocks/fails the call.
    await audit.get_audit_sink().record(audit.AuditEvent(
        correlation_id=correlation_id,
        tool=f"{group}_bash",
        group=group,
        user_oid=sctx.user_oid,
        user_upn=await ctx.get_state("user_upn"),
        client_ip=await ctx.get_state("client_ip"),
        session_id=sctx.session_id,
        conversation_id=sctx.conversation_id,
        command=command,
        explanation=explanation,
        exit_code=result.exit_code,
    ))
    return result.to_dict()


@mcp.tool(
    auth=require_diagnose,
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def diagnose_bash(command: str, ctx: Context) -> dict:
    """Run a read-only shell command for Azure diagnostics.

    The diagnose-worker is a bash shell with the Azure CLI (`az`). Standard shell
    glue — pipes, loops, `jq`, etc. — is available to combine `az` calls. Use it
    for READ-only investigation, e.g.:
        az datafactory pipeline-run show --factory-name F --run-id R -o json
        for rg in $(az group list --query "[].name" -o tsv); do az ...; done

    Keep work to Azure; running unrelated shell just burns tokens.
    Returns {exit_code, stdout, stderr}.
    """
    return await _exec("diagnose", command, ctx)


@mcp.tool(
    auth=require_action,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
    # Force a human approval on every action_bash call (docs/action-gate-guardrail
    # §2.2). Claude Code honors this _meta and turns even a --permission-prompt-tool
    # "allow" into "deny". Other clients ignore _meta and enforce via their own
    # settings (see README "Connect a client"). Verify end-to-end per client version
    # (Claude Code v2.1.199+; confirm _meta passes through your fastmcp build).
    meta={"anthropic/requiresUserInteraction": True},
)
async def action_bash(command: str, explanation: str, ctx: Context) -> dict:
    """Run a write/modify shell command for Azure operations.

    The action-worker is a bash shell with the Azure CLI (`az`). Standard shell
    glue — pipes, loops, `jq`, etc. — is available to combine `az` calls. Use it
    for commands that CHANGE state, e.g.:
        az datafactory pipeline create-run --factory-name F --name P
        az datafactory trigger start --factory-name F --name T
        az vm restart --ids "$(az vm list -g G --query "[].id" -o tsv)"

    Keep work to Azure.

    `explanation` is REQUIRED: one short, plain-language sentence (for the human
    who approves this) stating what the command does and its blast radius — e.g.
    "Reruns the daily_customer_load ADF pipeline; may duplicate rows already
    loaded downstream."

    Returns {exit_code, stdout, stderr}.
    """
    return await _exec("action", command, ctx, explanation=explanation)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok"})


app = mcp.http_app()

# Plan A: bolt the resource-stripping /mcpproxy endpoint onto the same app. It
# reuses this app's StreamableHTTP session manager and the same AzureJWTVerifier,
# so /mcpproxy clients end up holding a real Entra token — oid / OBO / group gating
# are identical to /mcp. All /mcp routes are left untouched.
if MCPPROXY_ENABLED:
    from mcpproxy import install_proxy_endpoint

    install_proxy_endpoint(
        app,
        mcp_path="/mcp",
        base_url=BASE_URL,
        tenant_id=TENANT_ID,
        mcp_app_id=MCP_APP_ID,
        required_scopes=verifier.required_scopes,
    )

# Ping Redis at startup: a clear connectivity log, and it warms the connection
# pool on the server's event loop (so the first request never pays first-connect
# latency or hits a loop-binding surprise). Wraps the FastMCP lifespan.
if _redis is not None:
    import contextlib

    _orig_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan_with_redis_ping(app_):
        try:
            pong = await _redis.ping()
            logger.info("redis startup ping ok: %s", pong)
        except Exception as e:
            logger.warning("redis startup ping FAILED: %s", e)
        async with _orig_lifespan(app_):
            yield

    app.router.lifespan_context = _lifespan_with_redis_ping

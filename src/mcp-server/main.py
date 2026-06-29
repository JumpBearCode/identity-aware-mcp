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
from cache import GroupCache, InMemoryBackend
from executor import SessionCtx, make_executor
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

# Execution backend: local docker workers (default) or ACA sandboxes. Both sit
# behind the same Executor interface; see executor.py / sandbox_manager.py.
executor = make_executor()

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
        oid = token.claims.get("oid") if token and hasattr(token, "claims") else None
        fctx = context.fastmcp_context
        if fctx is not None:
            session_id, conversation_id = _derive_ids(oid, fctx)
            await fctx.set_state("user_oid", oid)
            await fctx.set_state("session_id", session_id)
            await fctx.set_state("conversation_id", conversation_id)
        logger.info("tool call by user_oid=%s tool=%s", oid, context.message.name)
        return await call_next(context)


def _derive_ids(oid, fctx) -> tuple[str | None, str | None]:
    """Best-effort Session/Conversation ids off the transport (Phase 4 refines)."""
    session_id = getattr(fctx, "session_id", None) or oid
    conversation_id = getattr(fctx, "request_id", None)
    return session_id, conversation_id


mcp = FastMCP("Azure DataOps", auth=auth, middleware=[UserAuthMiddleware()])


async def _exec(group: str, command: str, ctx: Context):
    """Build the routing context off stashed state and run on the backend."""
    sctx = SessionCtx(
        user_oid=await ctx.get_state("user_oid"),
        session_id=await ctx.get_state("session_id"),
        conversation_id=await ctx.get_state("conversation_id"),
        group=group,  # type: ignore[arg-type]
    )
    result = await executor.exec(sctx, command)
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
    logger.info("diagnose_bash: %s", command)
    return await _exec("diagnose", command, ctx)


@mcp.tool(
    auth=require_action,
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
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
    user_oid = await ctx.get_state("user_oid")
    logger.info(
        "action_bash: user=%s explanation=%s command=%s",
        user_oid,
        explanation,
        command,
    )
    return await _exec("action", command, ctx)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok"})


app = mcp.http_app()

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
DIAGNOSE_WORKER_URL = os.environ.get("DIAGNOSE_WORKER_URL", "http://diagnose-worker:9001")
ACTION_WORKER_URL = os.environ.get("ACTION_WORKER_URL", "http://action-worker:9002")

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


async def _user_groups_via_obo(user_jwt: str) -> set[str]:
    """OBO-exchange the user's MCP token for a Graph token, then list group IDs."""
    obo = msal_app.acquire_token_on_behalf_of(
        user_assertion=user_jwt,
        scopes=["https://graph.microsoft.com/.default"],
    )
    if "access_token" not in obo:
        logger.error("OBO failed: %s", obo.get("error_description"))
        return set()
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group?$select=id",
            headers={"Authorization": f"Bearer {obo['access_token']}"},
        )
        r.raise_for_status()
        return {g["id"] for g in r.json().get("value", [])}


def _require_group(group_id: str):
    async def check(ctx: AuthContext) -> bool:
        if ctx.token is None:
            return False
        return group_id in await _user_groups_via_obo(ctx.token.token)

    return check


require_diagnose = _require_group(DIAGNOSE_GROUP_ID)
require_action = _require_group(ACTION_GROUP_ID)


# --- Middleware: stash user oid for audit ---
class UserAuthMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        token = get_access_token()
        oid = token.claims.get("oid") if token and hasattr(token, "claims") else None
        if context.fastmcp_context is not None:
            await context.fastmcp_context.set_state("user_oid", oid)
        logger.info("tool call by user_oid=%s tool=%s", oid, context.message.name)
        return await call_next(context)


mcp = FastMCP("Azure DataOps", auth=auth, middleware=[UserAuthMiddleware()])


async def _exec_on_worker(worker_url: str, command: str) -> dict:
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{worker_url}/exec", json={"command": command})
        r.raise_for_status()
        return r.json()


@mcp.tool(auth=require_diagnose)
async def diagnose_bash(command: str) -> dict:
    """Run a read-only diagnostic command on the diagnose-worker.

    Routed to a Service Principal with read-only RBAC. No human approval required;
    the SP's RBAC is the safety boundary.
    """
    logger.info("diagnose_bash: %s", command)
    return await _exec_on_worker(DIAGNOSE_WORKER_URL, command)


@mcp.tool(auth=require_action)
async def action_bash(command: str, ctx: Context) -> dict:
    """Run a write command on the action-worker (gated by approval hook).

    The action-worker enforces a per-tool-call approval hook before executing.
    """
    user_oid = await ctx.get_state("user_oid")
    logger.info("action_bash: user=%s command=%s", user_oid, command)
    return await _exec_on_worker(
        ACTION_WORKER_URL, command  # worker prompts for approval internally
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok"})


app = mcp.http_app()

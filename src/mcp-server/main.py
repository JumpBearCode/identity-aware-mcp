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
MCP_EXEC_TIMEOUT = float(os.environ.get("MCP_EXEC_TIMEOUT", "120"))

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


async def _user_in_groups(user_jwt: str, group_ids: list[str]) -> set[str]:
    """OBO-exchange the user's MCP token for a Graph token, then ask Graph which
    of `group_ids` the user is a (transitive) member of. Returns that subset.

    Uses POST /me/checkMemberGroups instead of listing every group the user
    belongs to: fixed-size payload, no pagination, evaluates membership server-side
    (transitive). Accepts up to 20 group IDs per call.
    """
    obo = msal_app.acquire_token_on_behalf_of(
        user_assertion=user_jwt,
        scopes=["https://graph.microsoft.com/.default"],
    )
    if "access_token" not in obo:
        logger.error("OBO failed: %s", obo.get("error_description"))
        return set()
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://graph.microsoft.com/v1.0/me/checkMemberGroups",
            headers={"Authorization": f"Bearer {obo['access_token']}"},
            json={"groupIds": group_ids},
        )
        r.raise_for_status()
        return set(r.json().get("value", []))


def _require_group(group_id: str):
    async def check(ctx: AuthContext) -> bool:
        if ctx.token is None:
            return False
        return group_id in await _user_in_groups(ctx.token.token, [group_id])

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
    # httpx waits MCP_EXEC_TIMEOUT; the worker kills the subprocess 10s earlier so
    # a timeout comes back as a structured result instead of an httpx ReadTimeout.
    async with httpx.AsyncClient(timeout=MCP_EXEC_TIMEOUT) as client:
        r = await client.post(
            f"{worker_url}/exec",
            json={"command": command, "timeout": MCP_EXEC_TIMEOUT - 10},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool(
    auth=require_diagnose,
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def diagnose_bash(command: str) -> dict:
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
    return await _exec_on_worker(DIAGNOSE_WORKER_URL, command)


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
    return await _exec_on_worker(ACTION_WORKER_URL, command)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok"})


app = mcp.http_app()

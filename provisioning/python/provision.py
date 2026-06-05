"""
Provision everything needed for the identity-aware DataOps MCP stack.

Creates in your Entra tenant:
  1. MCP Server App registration (with user_impersonation scope + VS Code pre-auth +
     client secret), plus admin consent for OBO -> Graph (User.Read email
     offline_access openid profile)
  2. Two AD security groups (mcp-diagnose-users, mcp-action-admins)
  3. Two Service Principals (diagnose-sp, action-sp) with their own client secrets

No Azure RBAC is granted here — the worker SPs have NO resource access until you
assign it yourself later.

Writes the resulting IDs/secrets to ../../.env so docker-compose picks them up.

On-Behalf-Of (OBO), in one breath:
  The client (e.g. VS Code) signs the user in and gets a token for the MCP server
  (scope api://<mcp>/user_impersonation). The server can't reuse that token to call
  Graph — it's audienced to the server, not Graph. OBO is the exchange: the server
  trades the incoming user token for a NEW token to a downstream API (Graph), still
  carrying the user's identity. So Graph sees "this user", not "the server" — every
  downstream call is evaluated against the user's own permissions, not an app's.
  The admin consent below is what lets that exchange succeed without a per-user
  consent prompt mid-flow.

Prereqs:
  - `az login` already done with an account that has:
      * Application Administrator + Group Administrator in Entra
      * Application Administrator (or Cloud App Admin) is enough to grant the OBO
        admin consent here — these are low-privilege, user-consentable scopes.
        Only high-privilege scopes would need Privileged Role Admin / Global Admin.
  - uv sync (see pyproject.toml)

Run:
  uv run python provision.py
"""

import asyncio
import os
import pathlib
import sys
import uuid

from azure.identity.aio import AzureCliCredential
from msgraph import GraphServiceClient
from msgraph.generated.applications.item.add_password.add_password_post_request_body import (
    AddPasswordPostRequestBody,
)
from msgraph.generated.models.api_application import ApiApplication
from msgraph.generated.models.application import Application
from msgraph.generated.models.group import Group
from msgraph.generated.models.o_auth2_permission_grant import OAuth2PermissionGrant
from msgraph.generated.models.password_credential import PasswordCredential
from msgraph.generated.models.permission_scope import PermissionScope
from msgraph.generated.models.pre_authorized_application import PreAuthorizedApplication
from msgraph.generated.models.public_client_application import PublicClientApplication
from msgraph.generated.models.required_resource_access import RequiredResourceAccess
from msgraph.generated.models.resource_access import ResourceAccess
from msgraph.generated.models.service_principal import ServicePrincipal

# --- Configuration ----------------------------------------------------------
TENANT_ID = os.environ["AZURE_TENANT_ID"]

VSCODE_CLIENT_ID = "aebc6443-996d-45c2-90f0-388ff96faa56"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"

# Delegated Graph permission IDs (declared on the app so they show in the portal).
GRAPH_USER_READ = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"
GRAPH_EMAIL = "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0"
GRAPH_OFFLINE_ACCESS = "7427e0e9-2fba-42fe-b0c0-848c9e6a8182"
GRAPH_OPENID = "37f7f235-527c-4136-accd-4a02d197296e"
GRAPH_PROFILE = "14dad69e-099b-42c9-810b-d002981feec1"

# Space-delimited scope string the MCP server is admin-consented for (OBO -> Graph).
GRAPH_OBO_SCOPE = "User.Read email offline_access openid profile"

ENV_OUT = pathlib.Path(__file__).resolve().parents[2] / ".env"


# --- Helpers ----------------------------------------------------------------
def env_block(d: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in d.items()) + "\n"


def write_env(values: dict) -> None:
    ENV_OUT.write_text(env_block(values))
    print(f"\nWrote {ENV_OUT}")


# --- MCP Server App ---------------------------------------------------------
async def create_mcp_server_app(graph: GraphServiceClient) -> tuple[str, str, str]:
    """Returns (object_id, client_id, client_secret)."""
    scope_id = str(uuid.uuid4())
    app = await graph.applications.post(
        Application(
            display_name="DataOps MCP Server",
            sign_in_audience="AzureADMyOrg",
            required_resource_access=[
                RequiredResourceAccess(
                    resource_app_id=GRAPH_APP_ID,
                    resource_access=[
                        ResourceAccess(id=GRAPH_USER_READ, type="Scope"),
                        ResourceAccess(id=GRAPH_EMAIL, type="Scope"),
                        ResourceAccess(id=GRAPH_OFFLINE_ACCESS, type="Scope"),
                        ResourceAccess(id=GRAPH_OPENID, type="Scope"),
                        ResourceAccess(id=GRAPH_PROFILE, type="Scope"),
                    ],
                )
            ],
            api=ApiApplication(
                requested_access_token_version=2,
                oauth2_permission_scopes=[
                    PermissionScope(
                        id=scope_id,
                        admin_consent_description="Access the DataOps MCP server as the signed-in user.",
                        admin_consent_display_name="Access DataOps MCP",
                        user_consent_description="Access the DataOps MCP server on your behalf.",
                        user_consent_display_name="Access DataOps MCP",
                        value="user_impersonation",
                        type="User",
                        is_enabled=True,
                    )
                ],
                pre_authorized_applications=[
                    PreAuthorizedApplication(
                        app_id=VSCODE_CLIENT_ID,
                        delegated_permission_ids=[scope_id],
                    )
                ],
            ),
        )
    )
    assert app and app.id and app.app_id
    print(f"  MCP Server App created: {app.app_id}")

    # Set identifier URI (required for AzureJWTVerifier audience match)
    await graph.applications.by_application_id(app.id).patch(
        Application(identifier_uris=[f"api://{app.app_id}"])
    )

    # SP backing the app (needed for admin consent / OBO)
    sp = await graph.service_principals.post(
        ServicePrincipal(app_id=app.app_id, display_name="DataOps MCP Server")
    )
    assert sp and sp.id
    print(f"  MCP Server SP created: {sp.id}")

    # Pre-consent OBO -> Graph for the whole tenant. These scopes are low-privilege
    # and user-consentable, so each user COULD click through a consent prompt
    # themselves — this just does it once for everyone so nobody sees a prompt
    # mid-flow (and so OBO doesn't fail with interaction_required).
    await grant_obo_admin_consent(graph, sp.id)

    # Client secret
    pw = await graph.applications.by_application_id(app.id).add_password.post(
        AddPasswordPostRequestBody(
            password_credential=PasswordCredential(display_name="local-dev")
        )
    )
    assert pw and pw.secret_text
    return app.id, app.app_id, pw.secret_text


# --- AD Groups --------------------------------------------------------------
async def ensure_group(graph: GraphServiceClient, display_name: str, mail_nickname: str) -> str:
    g = await graph.groups.post(
        Group(
            display_name=display_name,
            mail_nickname=mail_nickname,
            security_enabled=True,
            mail_enabled=False,
        )
    )
    assert g and g.id
    print(f"  Group '{display_name}' -> {g.id}")
    return g.id


# --- Worker Service Principals ----------------------------------------------
async def create_worker_sp(
    graph: GraphServiceClient, display_name: str
) -> tuple[str, str, str]:
    """Create app + SP + secret. Returns (sp_object_id, client_id, client_secret)."""
    app = await graph.applications.post(
        Application(
            display_name=display_name,
            sign_in_audience="AzureADMyOrg",
            # Workers are confidential clients used non-interactively
            public_client=PublicClientApplication(redirect_uris=[]),
        )
    )
    assert app and app.id and app.app_id
    sp = await graph.service_principals.post(
        ServicePrincipal(app_id=app.app_id, display_name=display_name)
    )
    assert sp and sp.id
    pw = await graph.applications.by_application_id(app.id).add_password.post(
        AddPasswordPostRequestBody(
            password_credential=PasswordCredential(display_name="local-dev")
        )
    )
    assert pw and pw.secret_text
    print(f"  Worker SP '{display_name}': appId={app.app_id}, spObjId={sp.id}")
    return sp.id, app.app_id, pw.secret_text


# --- Admin consent (OBO -> Graph) -------------------------------------------
async def grant_obo_admin_consent(graph: GraphServiceClient, server_sp_id: str) -> None:
    """Tenant-wide delegated grant: MCP server SP -> Graph, for the OBO scopes."""
    graph_sp = await graph.service_principals_with_app_id(GRAPH_APP_ID).get()
    assert graph_sp and graph_sp.id
    await graph.oauth2_permission_grants.post(
        OAuth2PermissionGrant(
            client_id=server_sp_id,
            consent_type="AllPrincipals",
            resource_id=graph_sp.id,
            scope=GRAPH_OBO_SCOPE,
        )
    )
    print(f"  Admin consent (OBO -> Graph): {GRAPH_OBO_SCOPE}")


# --- Main -------------------------------------------------------------------
async def main() -> None:
    if ENV_OUT.exists():
        print(f"Refusing to overwrite existing {ENV_OUT} — move it aside first.")
        sys.exit(1)

    cred = AzureCliCredential()
    graph = GraphServiceClient(credentials=cred, scopes=["https://graph.microsoft.com/.default"])

    print("==> MCP Server App")
    _, mcp_app_id, mcp_secret = await create_mcp_server_app(graph)

    print("\n==> AD Groups")
    diag_group = await ensure_group(graph, "mcp-diagnose-users", "mcp-diagnose-users")
    act_group = await ensure_group(graph, "mcp-action-admins", "mcp-action-admins")

    print("\n==> Worker SPs")
    _, diag_sp_id, diag_sp_secret = await create_worker_sp(graph, "dataops-diagnose-sp")
    _, act_sp_id, act_sp_secret = await create_worker_sp(graph, "dataops-action-sp")

    write_env({
        "AZURE_TENANT_ID": TENANT_ID,
        "MCP_APP_ID": mcp_app_id,
        "MCP_CLIENT_SECRET": mcp_secret,
        "DIAGNOSE_GROUP_ID": diag_group,
        "ACTION_GROUP_ID": act_group,
        "DIAGNOSE_SP_CLIENT_ID": diag_sp_id,
        "DIAGNOSE_SP_CLIENT_SECRET": diag_sp_secret,
        "ACTION_SP_CLIENT_ID": act_sp_id,
        "ACTION_SP_CLIENT_SECRET": act_sp_secret,
        "MCP_SERVER_BASE_URL": "http://localhost:8080",
    })

    print("\nNext steps:")
    print("  1. Add your user(s) to the AD groups above (Azure portal or `az ad group member add`).")
    print("  2. Grant the worker SPs whatever Azure RBAC they need — none is assigned yet.")
    print("  3. cd ../.. && docker compose up --build")


if __name__ == "__main__":
    asyncio.run(main())

"""
Provision everything needed for the identity-aware DataOps MCP stack.

Creates in your Entra tenant + Azure subscription:
  1. MCP Server App registration (with user_impersonation scope + VS Code pre-auth + client secret)
  2. Two AD security groups (mcp-diagnose-users, mcp-action-admins)
  3. Two Service Principals (diagnose-sp, action-sp) with their own client secrets
  4. RBAC role assignments scoped to PROVISION_TARGET_SCOPE:
       - diagnose-sp  -> Reader
       - action-sp    -> Contributor   (override per environment as needed)

Writes the resulting IDs/secrets to ../../.env so docker-compose picks them up.

Prereqs:
  - `az login` already done with an account that has:
      * Application Administrator + Group Administrator in Entra
      * Owner / User Access Administrator on PROVISION_TARGET_SCOPE
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
from azure.identity import AzureCliCredential as SyncAzureCliCredential
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.authorization.models import RoleAssignmentCreateParameters
from msgraph import GraphServiceClient
from msgraph.generated.applications.item.add_password.add_password_post_request_body import (
    AddPasswordPostRequestBody,
)
from msgraph.generated.models.api_application import ApiApplication
from msgraph.generated.models.application import Application
from msgraph.generated.models.group import Group
from msgraph.generated.models.password_credential import PasswordCredential
from msgraph.generated.models.permission_scope import PermissionScope
from msgraph.generated.models.pre_authorized_application import PreAuthorizedApplication
from msgraph.generated.models.public_client_application import PublicClientApplication
from msgraph.generated.models.required_resource_access import RequiredResourceAccess
from msgraph.generated.models.resource_access import ResourceAccess
from msgraph.generated.models.service_principal import ServicePrincipal

# --- Configuration ----------------------------------------------------------
TENANT_ID = os.environ["AZURE_TENANT_ID"]
SUBSCRIPTION_ID = os.environ["AZURE_SUBSCRIPTION_ID"]
TARGET_SCOPE = os.environ.get(
    "PROVISION_TARGET_SCOPE",
    f"/subscriptions/{SUBSCRIPTION_ID}",  # default: subscription-wide; tighten to RG in prod
)

VSCODE_CLIENT_ID = "aebc6443-996d-45c2-90f0-388ff96faa56"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_USER_READ = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"

# Built-in role IDs (Reader, Contributor)
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
CONTRIBUTOR_ROLE_ID = "b24988ac-6180-42a0-ab88-20f7382dd24c"

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
                    resource_access=[ResourceAccess(id=GRAPH_USER_READ, type="Scope")],
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


# --- RBAC -------------------------------------------------------------------
def assign_rbac(sp_object_id: str, role_id: str, scope: str) -> None:
    cred = SyncAzureCliCredential()
    auth_client = AuthorizationManagementClient(cred, SUBSCRIPTION_ID)
    role_def_id = f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/{role_id}"
    auth_client.role_assignments.create(
        scope=scope,
        role_assignment_name=str(uuid.uuid4()),
        parameters=RoleAssignmentCreateParameters(
            role_definition_id=role_def_id,
            principal_id=sp_object_id,
            principal_type="ServicePrincipal",
        ),
    )
    print(f"  RBAC: {sp_object_id} -> role {role_id} on {scope}")


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
    diag_sp_obj, diag_sp_id, diag_sp_secret = await create_worker_sp(graph, "dataops-diagnose-sp")
    act_sp_obj, act_sp_id, act_sp_secret = await create_worker_sp(graph, "dataops-action-sp")

    print("\n==> RBAC")
    assign_rbac(diag_sp_obj, READER_ROLE_ID, TARGET_SCOPE)
    assign_rbac(act_sp_obj, CONTRIBUTOR_ROLE_ID, TARGET_SCOPE)

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
    print("  2. In Entra portal: MCP Server App -> API permissions -> Grant admin consent")
    print("     (so OBO -> Graph works without per-user consent).")
    print("  3. cd ../.. && docker compose up --build")


if __name__ == "__main__":
    asyncio.run(main())

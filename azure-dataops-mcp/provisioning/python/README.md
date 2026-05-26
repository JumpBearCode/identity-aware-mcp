# Python SDK provisioning

Creates all Entra + Azure resources for the local DataOps MCP stack.

## Required env vars

```bash
export AZURE_TENANT_ID=<your tenant>
export AZURE_SUBSCRIPTION_ID=<your subscription>
# Optional — scope where workers get RBAC. Defaults to the subscription.
# Tighten this in real environments:
export PROVISION_TARGET_SCOPE=/subscriptions/<sub>/resourceGroups/<rg>
```

## Required permissions (on the running user)

- Entra: **Application Administrator** + **Group Administrator** (or Global Admin)
- Azure: **Owner** or **User Access Administrator** on `PROVISION_TARGET_SCOPE`

## Run

```bash
az login --tenant $AZURE_TENANT_ID
uv sync
uv run python provision.py
```

Outputs `../../.env` consumed by `docker-compose.yml`.

## What gets created

| Resource | Purpose |
|---|---|
| Entra App: `DataOps MCP Server` | Protected resource. Exposes `user_impersonation` scope. VS Code pre-authorized. Has a client secret for OBO. |
| AD Group: `mcp-diagnose-users` | Members can call `diagnose_bash` |
| AD Group: `mcp-action-admins` | Members can call `action_bash` (plus diagnose) |
| Entra App + SP: `dataops-diagnose-sp` | Worker identity, read-only |
| Entra App + SP: `dataops-action-sp` | Worker identity, write |
| RBAC: Reader on target scope | Granted to diagnose-sp |
| RBAC: Contributor on target scope | Granted to action-sp |

## Post-provision manual steps

1. Add users to the AD groups (`az ad group member add --group <id> --member-id <user-oid>`).
2. **Admin consent** for the MCP Server App in Entra portal (one click) — required so OBO -> Graph works without per-user consent screens.

## Tear down

There's no destroy script — delete the three app registrations and two groups in the Entra portal; RBAC assignments cascade out when the SPs are deleted.

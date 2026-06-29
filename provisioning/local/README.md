# Local provisioning (Bicep)

Identity-only provisioning for the **local docker** path (`EXECUTOR=local`):
the MCP server app (delegated `user_impersonation` + OBO admin consent), the two
AD groups, and the `diagnose-sp` / `action-sp` worker app registrations. The two
worker containers log in with these SPs + a client secret.

For the cloud (ACA sandboxes) path, see [`../aca/`](../aca/) instead — it provisions
the full Azure footprint (sandbox groups, storage, redis, the MCP Container App)
and uses passwordless Federated Identity Credentials, no worker secrets.

## Prereqs

- Azure CLI ≥ 2.62 (Bicep extensibility + `Microsoft.Graph` extension)
- `az login` as a principal with:
  - Entra: **Application Administrator** + **Group Administrator**
  - **Privileged Role Administrator** / **Global Admin** for the OBO admin-consent grant
- `jq`

## Deploy

```bash
az deployment tenant create \
  --name dataops-mcp-provision \
  --location eastus \
  --template-file main.bicep

./write-env.sh dataops-mcp-provision
```

`write-env.sh` calls `az ad app credential reset` for the three app registrations and writes `../../.env`.

## What's NOT in Bicep

| Concern | Why not | How to handle |
|---|---|---|
| Client secrets | Bicep cannot safely emit secrets through deployment outputs | `write-env.sh` runs `az ad app credential reset` |
| Group membership | Per-user, dynamic | `az ad group member add --group <id> --member-id <oid>` |
| Worker RBAC | Intentionally not granted — decide access later | `az role assignment create` at the scope you choose |

> OBO admin consent **is** done in-template now via `Microsoft.Graph/oauth2PermissionGrants` (scope `User.Read email offline_access openid profile`).

## Re-running

The Graph extension is idempotent via `uniqueName`, so re-deploying the same parameters is a no-op.

**Don't** re-run `write-env.sh` casually — it resets secrets and breaks any running worker until you redistribute the new `.env`.

## Limitations to be aware of

- The `Microsoft.Graph` Bicep extension is still in public preview. Expect rough edges (especially around updating `preAuthorizedApplications`).
- For multi-environment (dev/staging/prod), parametrize `name` and deploy three times.

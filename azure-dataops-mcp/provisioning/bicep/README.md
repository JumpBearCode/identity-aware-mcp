# Bicep provisioning

Same end-state as the Python SDK route, declarative.

## Prereqs

- Azure CLI ≥ 2.62 (Bicep extensibility + `Microsoft.Graph` extension)
- `az login` as a principal with:
  - Entra: **Application Administrator** + **Group Administrator**
  - Azure: **Owner** / **User Access Administrator** on the target scope
- `jq`

## Deploy

```bash
# Optional: override the RBAC scope (defaults to the whole subscription)
SCOPE="/subscriptions/$AZURE_SUBSCRIPTION_ID/resourceGroups/<rg>"

az deployment sub create \
  --name dataops-mcp-provision \
  --location eastus \
  --template-file main.bicep \
  --parameters targetScope_="$SCOPE"

./write-env.sh dataops-mcp-provision
```

`write-env.sh` calls `az ad app credential reset` for the three app registrations and writes `../../.env`.

## What's NOT in Bicep

| Concern | Why not | How to handle |
|---|---|---|
| Client secrets | Bicep cannot safely emit secrets through deployment outputs | `write-env.sh` runs `az ad app credential reset` |
| Admin consent | Not modeled in the Graph Bicep extension yet | `az ad app permission admin-consent --id <mcp_app_id>` |
| Group membership | Per-user, dynamic | `az ad group member add --group <id> --member-id <oid>` |

## Re-running

The Graph extension is idempotent via `uniqueName`. The RBAC role assignments use deterministic GUIDs (`guid(scope, principal, role)`), so re-deploying the same parameters is a no-op.

**Don't** re-run `write-env.sh` casually — it resets secrets and breaks any running worker until you redistribute the new `.env`.

## Limitations to be aware of

- The `Microsoft.Graph` Bicep extension is still in public preview. Expect rough edges (especially around updating `preAuthorizedApplications`).
- For multi-environment (dev/staging/prod), parametrize `name` and deploy three times.

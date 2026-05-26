#!/usr/bin/env bash
# Reads outputs from the Bicep deployment, generates client secrets for the
# three app registrations, and writes ../../.env consumed by docker-compose.
#
# Usage:
#   AZURE_SUBSCRIPTION_ID=...  AZURE_TENANT_ID=...  ./write-env.sh <deployment-name>
#
# Idempotency: `az ad app credential reset` invalidates prior secrets — only run once,
# or rotate by re-running and updating .env on all consumers.

set -euo pipefail

DEPLOYMENT_NAME="${1:-dataops-mcp-provision}"
ENV_OUT="$(cd "$(dirname "$0")/../.." && pwd)/.env"

if [[ -f "$ENV_OUT" ]]; then
  echo "Refusing to overwrite existing $ENV_OUT — move it aside first."
  exit 1
fi

echo "==> Reading deployment outputs..."
outputs=$(az deployment sub show -n "$DEPLOYMENT_NAME" --query properties.outputs -o json)
mcp_app_id=$(jq -r '.MCP_APP_ID.value' <<<"$outputs")
diag_group=$(jq -r '.DIAGNOSE_GROUP_ID.value' <<<"$outputs")
act_group=$(jq -r '.ACTION_GROUP_ID.value' <<<"$outputs")
diag_sp_id=$(jq -r '.DIAGNOSE_SP_CLIENT_ID.value' <<<"$outputs")
act_sp_id=$(jq -r '.ACTION_SP_CLIENT_ID.value' <<<"$outputs")
tenant_id=$(jq -r '.AZURE_TENANT_ID.value' <<<"$outputs")

echo "==> Resetting client secrets..."
mcp_secret=$(az ad app credential reset --id "$mcp_app_id" --display-name local-dev --query password -o tsv)
diag_secret=$(az ad app credential reset --id "$diag_sp_id" --display-name local-dev --query password -o tsv)
act_secret=$(az ad app credential reset --id "$act_sp_id" --display-name local-dev --query password -o tsv)

echo "==> Writing $ENV_OUT"
cat > "$ENV_OUT" <<EOF
AZURE_TENANT_ID=$tenant_id
MCP_APP_ID=$mcp_app_id
MCP_CLIENT_SECRET=$mcp_secret
DIAGNOSE_GROUP_ID=$diag_group
ACTION_GROUP_ID=$act_group
DIAGNOSE_SP_CLIENT_ID=$diag_sp_id
DIAGNOSE_SP_CLIENT_SECRET=$diag_secret
ACTION_SP_CLIENT_ID=$act_sp_id
ACTION_SP_CLIENT_SECRET=$act_secret
MCP_SERVER_BASE_URL=http://localhost:8080
EOF

echo "Done. Next:"
echo "  1. Add users to the AD groups:"
echo "       az ad group member add --group $diag_group --member-id <user-object-id>"
echo "       az ad group member add --group $act_group  --member-id <user-object-id>"
echo "  2. Admin-consent the MCP server app (Graph User.Read for OBO):"
echo "       az ad app permission admin-consent --id $mcp_app_id"
echo "  3. docker compose up --build"

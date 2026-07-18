#!/usr/bin/env bash
# Reads outputs from the ACA subscription deployment and writes ../../.env.aca
# (the cloud profile). The cloud path is passwordless for the WORKER identities
# — no diagnose/action SP secrets here. The MCP server still needs its own OBO
# client secret; reset it and inject it as the Container App secret separately
# (see provisioning/aca/README.md).
#
# Usage:
#   ./write-env.sh [deployment-name]

set -euo pipefail

DEPLOYMENT_NAME="${1:-dataops-mcp-aca}"
ENV_OUT="$(cd "$(dirname "$0")/../.." && pwd)/.env.aca"

echo "==> Reading deployment outputs..."
outputs=$(az deployment sub show -n "$DEPLOYMENT_NAME" --query properties.outputs -o json)
# ARM returns output keys with mangled casing (ACTION_GROUP_ID -> actioN_GROUP_ID),
# so match case-insensitively.
get() { jq -r --arg k "$1" 'to_entries[] | select((.key|ascii_upcase)==($k|ascii_upcase)) | .value.value' <<<"$outputs"; }

redis_host=$(get REDIS_HOST)

# NOTE: no post-deploy `az ad app update --identifier-uris` step here anymore.
# The server advertises its scope under MCP_IDENTIFIER_URI (= the Bicep-declared
# friendly URI api://<name>-mcp-server, set as a Container App env below), which
# already matches the app's identifierUris. Bicep is the single source of truth,
# so there is nothing left to patch — and the old overwrite was the AADSTS500011
# drift (docs/multi-client-implementation/Bug剖析-AADSTS500011-...md).

echo "==> Writing $ENV_OUT"
cat > "$ENV_OUT" <<EOF
EXECUTOR=aca
AZURE_TENANT_ID=$(get AZURE_TENANT_ID)
AZURE_SUBSCRIPTION_ID=$(get AZURE_SUBSCRIPTION_ID)
ACA_RESOURCE_GROUP=$(get RESOURCE_GROUP)
ACA_REGION=$(get LOCATION)

MCP_APP_ID=$(get MCP_APP_ID)
MCP_IDENTIFIER_URI=$(get MCP_IDENTIFIER_URI)
DIAGNOSE_GROUP_ID=$(get DIAGNOSE_GROUP_ID)
ACTION_GROUP_ID=$(get ACTION_GROUP_ID)
DIAGNOSE_SP_APP_ID=$(get DIAGNOSE_SP_APP_ID)
ACTION_SP_APP_ID=$(get ACTION_SP_APP_ID)

DIAGNOSE_SANDBOX_GROUP=$(get DIAGNOSE_SANDBOX_GROUP)
ACTION_SANDBOX_GROUP=$(get ACTION_SANDBOX_GROUP)

REDIS_URL=redis://${redis_host}:6379
STORAGE_ACCOUNT=$(get STORAGE_ACCOUNT)
BLOB_CONTAINER=$(get BLOB_CONTAINER)
BLOB_CONTAINER_RESOURCE_ID=$(get BLOB_CONTAINER_RESOURCE_ID)

REGISTRY_LOGIN_SERVER=$(get REGISTRY_LOGIN_SERVER)
REGISTRY_NAME=$(get REGISTRY_NAME)
MCP_APP_NAME=$(get MCP_APP_NAME)
MCP_FQDN=$(get MCP_FQDN)
EOF

echo "Done. Next:"
echo "  1. Add users to the AD groups (DIAGNOSE_GROUP_ID / ACTION_GROUP_ID)."
echo "  2. Build + push the MCP and sandbox images to ACR (REGISTRY_LOGIN_SERVER)."
echo "  3. Reset the MCP app secret and set it on the Container App:"
echo "       az containerapp secret set -n <MCP_APP_NAME> -g <RG> --secrets mcp-client-secret=<secret>"
echo "  4. Point your MCP client at https://<MCP_FQDN>/mcp"

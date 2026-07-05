#!/bin/bash
# Passwordless login for an ACA sandbox.
#
# Run once by SandboxManager right after a sandbox is created. It exchanges the
# sandbox group's managed identity (visible from inside the microVM, ACA
# "inception") for the worker Service Principal via that SP's Federated Identity
# Credential, then restores the user's `az` context. No secret is ever present
# in the cloud — the FIC trust + the inception MI are the whole mechanism.
#
# Env (injected at sandbox create by SandboxManager):
#   SP_APP_ID              worker SP app id to assume (diagnose-sp / action-sp)
#   AZURE_TENANT_ID        tenant for `az login`
#   AZURE_SUBSCRIPTION_ID  (optional) subscription to select after login
set -euo pipefail

: "${SP_APP_ID:?SP_APP_ID required}"
: "${AZURE_TENANT_ID:?AZURE_TENANT_ID required}"

# 1. Group MI token for the token-exchange audience. ManagedIdentityCredential
#    covers IMDS and the IDENTITY_ENDPOINT/MSI variants ACA may expose.
FED_TOKEN="$(python3 - <<'PY'
from azure.identity import ManagedIdentityCredential
print(ManagedIdentityCredential().get_token("api://AzureADTokenExchange/.default").token)
PY
)"

# 2. Exchange it: become the worker SP through its federated credential.
az login --service-principal \
  --username "$SP_APP_ID" \
  --tenant "$AZURE_TENANT_ID" \
  --federated-token "$FED_TOKEN" \
  --allow-no-subscriptions \
  --output none

# 3. Restore user az context (best effort; worker may hold no subscription role).
if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID" 2>/dev/null || true
fi

echo "bootstrap-ok sp=$SP_APP_ID account=$(az account show --query 'user.name' -o tsv 2>/dev/null || echo none)"

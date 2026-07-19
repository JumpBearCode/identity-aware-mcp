#!/usr/bin/env sh
# azd postprovision hook (ACA path). Runs after `azd provision`, before deploy,
# with all Bicep outputs available as environment variables and CWD = repo root.
#
# Does the three things Bicep/azd can't do declaratively:
#   1. Build + push the sandbox disk image into ACR (server-side; no local docker).
#      The MCP container's SANDBOX_DISK_IMAGE already points at this ref (Bicep).
#   2. Inject the MCP server's OBO client secret — ONCE per azd environment.
#   3. Remind the operator to add users to the AD groups (per-user, stays manual).
set -eu

echo "==> [postprovision] Building sandbox image in ACR ($REGISTRY_NAME)..."
az acr build -r "$REGISTRY_NAME" -t mcp-sandbox:latest ./src/sandbox-image

# Inject the MCP server's OBO client secret straight into the Container App.
# We deliberately do NOT round-trip it through the azd env: calling `azd env set`
# from inside a hook is unreliable (azd re-entrancy) and silently no-op'd the first
# time — see deployment-gotchas §3. Trade-off of not storing it: Bicep reseeds the
# placeholder every `provision` (the mcpClientSecret param stays empty), so we reset
# + inject on every run. `credential reset` returns the new password only here; it is
# piped straight into `secret set` and never echoed, so it does not leak into logs.
echo "==> [postprovision] Resetting + injecting MCP OBO client secret..."
secret=$(az ad app credential reset --id "$MCP_APP_ID" --display-name azd --query password -o tsv)
az containerapp secret set -n "$MCP_APP_NAME" -g "$RESOURCE_GROUP" \
  --secrets mcp-client-secret="$secret" >/dev/null

echo ""
echo "==> [postprovision] Done. Remaining manual step — add users to the AD groups:"
echo "      az ad group member add --group $DIAGNOSE_GROUP_ID --member-id <user-object-id>"
echo "      az ad group member add --group $ACTION_GROUP_ID   --member-id <user-object-id>"
echo "    Then point your MCP client at: https://$MCP_FQDN/mcp"

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

# The OBO secret value is only returned at creation time, and `credential reset`
# invalidates the prior one — so reset it once, stash it in the azd env (passed
# back as the secure `mcpClientSecret` param on every later provision so re-runs
# never clobber it with the placeholder), and apply it now so THIS deploy works.
# Rotate deliberately with:  azd env set MCP_CLIENT_SECRET ""  &&  azd provision
if [ -z "$(azd env get-value MCP_CLIENT_SECRET 2>/dev/null || true)" ]; then
  echo "==> [postprovision] Resetting + injecting MCP OBO client secret..."
  secret=$(az ad app credential reset --id "$MCP_APP_ID" --display-name azd --query password -o tsv)
  azd env set MCP_CLIENT_SECRET "$secret"
  az containerapp secret set -n "$MCP_APP_NAME" -g "$RESOURCE_GROUP" \
    --secrets mcp-client-secret="$secret" >/dev/null
else
  echo "==> [postprovision] MCP OBO secret already in azd env; skipping reset."
fi

echo ""
echo "==> [postprovision] Done. Remaining manual step — add users to the AD groups:"
echo "      az ad group member add --group $DIAGNOSE_GROUP_ID --member-id <user-object-id>"
echo "      az ad group member add --group $ACTION_GROUP_ID   --member-id <user-object-id>"
echo "    Then point your MCP client at: https://$MCP_FQDN/mcp"

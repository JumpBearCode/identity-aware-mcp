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

# Create the worker-SP <- sandbox-group-MI federated credentials HERE, not in Bicep.
# A Microsoft.Graph FIC must name its parent SP app as `uniqueName/cred`, which ARM
# preflights as an `existing` lookup BEFORE the SP exists on a clean deploy — an
# unavoidable race that failed both a standalone fic.bicep and co-locating it in
# identity.bicep (deployment-gotchas §1). By now provision has created the SP apps, so
# `az ad app federated-credential create` just works. delete-then-create keeps it
# idempotent and picks up a changed MI subject on re-provision.
create_fic() {  # $1=sp-app-id  $2=cred-name  $3=mi-principal-id
  az ad app federated-credential delete --id "$1" --federated-credential-id "$2" >/dev/null 2>&1 || true
  az ad app federated-credential create --id "$1" --parameters "{\"name\":\"$2\",\"issuer\":\"https://login.microsoftonline.com/$AZURE_TENANT_ID/v2.0\",\"subject\":\"$3\",\"audiences\":[\"api://AzureADTokenExchange\"]}" >/dev/null \
    && echo "  FIC $2 -> subject $3: ok" || echo "  FIC $2: FAILED"
}
echo "==> [postprovision] Creating worker-SP federated credentials..."
create_fic "$DIAGNOSE_SP_APP_ID" "diagnose-sandbox-mi" "$DIAGNOSE_MI_PRINCIPAL_ID"
create_fic "$ACTION_SP_APP_ID"   "action-sandbox-mi"   "$ACTION_MI_PRINCIPAL_ID"

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

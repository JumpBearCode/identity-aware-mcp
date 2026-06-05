#!/bin/sh
set -e

# Log in as this worker's Service Principal — every `az` call uses this identity.
# Tenant-level login: this worker holds only data-plane roles (e.g. Storage Blob
# Data Reader), no subscription/control-plane access, so --allow-no-subscriptions
# is required or `az login` exits non-zero with "No subscriptions found".
az login --service-principal \
  -u "$AZURE_CLIENT_ID" \
  -p "$AZURE_CLIENT_SECRET" \
  --tenant "$AZURE_TENANT_ID" \
  --allow-no-subscriptions \
  --output none

echo "diagnose-worker logged in as SP $AZURE_CLIENT_ID"
exec uvicorn worker:app --host 0.0.0.0 --port "${PORT:-9001}"

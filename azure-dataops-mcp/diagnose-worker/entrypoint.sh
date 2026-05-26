#!/bin/sh
set -e

# Log in as this worker's Service Principal — every `az` call uses this identity.
az login --service-principal \
  -u "$AZURE_CLIENT_ID" \
  -p "$AZURE_CLIENT_SECRET" \
  --tenant "$AZURE_TENANT_ID" \
  --output none

echo "diagnose-worker logged in as SP $AZURE_CLIENT_ID"
exec uvicorn worker:app --host 0.0.0.0 --port "${PORT:-9001}"

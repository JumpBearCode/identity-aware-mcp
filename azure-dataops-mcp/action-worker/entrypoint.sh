#!/bin/sh
set -e

az login --service-principal \
  -u "$AZURE_CLIENT_ID" \
  -p "$AZURE_CLIENT_SECRET" \
  --tenant "$AZURE_TENANT_ID" \
  --output none

echo "action-worker logged in as SP $AZURE_CLIENT_ID"
exec uvicorn worker:app --host 0.0.0.0 --port "${PORT:-9002}"

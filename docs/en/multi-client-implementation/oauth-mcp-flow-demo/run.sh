#!/usr/bin/env bash
# Start the teaching app. Must listen on 8080 (Entra callback whitelist is http://localhost:8080/callback）。
set -euo pipefail
cd "$(dirname "$0")"

if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  Port 8080 is already in use (possibly by VS Code / Claude Code MCP callback)."
  echo "    Free up port 8080 first, otherwise the Live login callback will not return."
  exit 1
fi

echo "▶ Open browser: http://localhost:8080"
exec python3 server.py
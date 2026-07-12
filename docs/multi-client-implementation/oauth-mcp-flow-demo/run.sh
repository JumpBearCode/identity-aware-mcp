#!/usr/bin/env bash
# 启动教学 App。必须监听 8080（Entra 回调白名单是 http://localhost:8080/callback）。
set -euo pipefail
cd "$(dirname "$0")"

if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  8080 端口已被占用（可能是 VS Code / Claude Code 的 MCP 回调）。"
  echo "    先腾出 8080 再跑，否则 Live 登录回调会落不回来。"
  exit 1
fi

echo "▶ 打开浏览器： http://localhost:8080"
exec python3 server.py

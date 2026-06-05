"""action-worker: scoped bash executor.

Runs write-capable `az cli` / shell commands under the action Service Principal.
There is intentionally NO approval gate here:

  - Human-in-the-loop approval is the MCP *client's* responsibility. Every
    interactive client (VS Code, Claude Code, Cursor, Gemini CLI, ...) prompts
    before a tool call by default. The server cannot portably render or enforce a
    per-client approval UI, so it doesn't try — there is no standard, cross-client
    way to declare "this tool needs approval" anyway.
  - The MCP server signals risk to clients via tool annotations
    (destructiveHint on action_bash); see src/mcp-server/main.py.
  - The real, non-bypassable safety boundary is this worker's Service Principal
    RBAC scope plus audit logging — not a click.
"""

import asyncio
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("action-worker")

app = FastAPI()
PORT = int(os.environ.get("PORT", "9002"))


class ExecRequest(BaseModel):
    command: str


@app.post("/exec")
async def exec_command(req: ExecRequest):
    logger.info("exec: %s", req.command)
    proc = await asyncio.create_subprocess_shell(
        req.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}

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
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", str(64 * 1024)))

TRUNCATE_HINT = (
    "\n\n[output truncated at {n} bytes] Narrow it at the source — "
    "`az ... --query <JMESPath> -o tsv` or `| jq '<filter>'`. If you need the "
    "full result, redirect to a file (`az ... > /tmp/out.json`) and read it in "
    "chunks with jq/grep."
)


class ExecRequest(BaseModel):
    command: str
    timeout: float  # required; the MCP server is the single source of truth


def _cap(raw: bytes) -> tuple[str, bool]:
    if len(raw) <= MAX_OUTPUT_BYTES:
        return raw.decode(errors="replace"), False
    return raw[:MAX_OUTPUT_BYTES].decode(errors="replace"), True


@app.post("/exec")
async def exec_command(req: ExecRequest):
    logger.info("exec: %s", req.command)
    proc = await asyncio.create_subprocess_shell(
        req.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=req.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": f"command timed out after {req.timeout:.0f}s and was killed",
            "truncated": False,
        }

    stdout_text, t1 = _cap(stdout)
    stderr_text, t2 = _cap(stderr)
    if t1 or t2:
        stdout_text += TRUNCATE_HINT.format(n=MAX_OUTPUT_BYTES)
    return {
        "exit_code": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "truncated": t1 or t2,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}

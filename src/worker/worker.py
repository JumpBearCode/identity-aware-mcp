"""worker: scoped bash executor shared by the diagnose and action workers.

Both workers run this exact same program. The ONLY thing that differentiates them
is identity and Azure RBAC — injected via env (the Service Principal credentials)
and configured on the Azure side, not in code. That is the whole point: the
execution substrate is generic; the boundary is the worker's SP + RBAC.

  - diagnose-worker -> read-only SP    (Reader-style RBAC)
  - action-worker   -> write-scoped SP (Contributor-style RBAC)

There is intentionally NO human-approval gate here. Human-in-the-loop approval is
the MCP *client's* responsibility (interactive clients prompt before a tool call
by default); the MCP server signals risk via tool annotations. The real,
non-bypassable safety boundary is this worker's SP RBAC plus audit logging.
"""

import asyncio
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

WORKER_NAME = os.environ.get("WORKER_NAME", "worker")
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", str(64 * 1024)))

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger(WORKER_NAME)

app = FastAPI()

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

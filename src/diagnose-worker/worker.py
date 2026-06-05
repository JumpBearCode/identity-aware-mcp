"""diagnose-worker: read-only bash executor. RBAC of the diagnose-sp is the safety boundary."""

import asyncio
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("diagnose-worker")

app = FastAPI()
PORT = int(os.environ.get("PORT", "9001"))


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

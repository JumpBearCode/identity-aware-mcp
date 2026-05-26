"""action-worker: gated bash executor. Each call is held for human approval
via a file-based hook (the simplest possible substitute for a real UI hook).

Approval mechanism:
  - Pending request written to /tmp/pending/<id>.json
  - Operator approves by `touch /tmp/pending/<id>.approve` (or .reject)
  - For local dev, simply tail the container logs and use `docker exec`.

A production version would push to Slack / Teams / pager and resolve via webhook.
"""

import asyncio
import json
import logging
import os
import pathlib
import uuid

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
logger = logging.getLogger("action-worker")

app = FastAPI()
PORT = int(os.environ.get("PORT", "9002"))
PENDING_DIR = pathlib.Path("/tmp/pending")
PENDING_DIR.mkdir(parents=True, exist_ok=True)
APPROVAL_TIMEOUT_SEC = int(os.environ.get("APPROVAL_TIMEOUT_SEC", "300"))


class ExecRequest(BaseModel):
    command: str


async def _await_approval(req_id: str) -> str:
    approve = PENDING_DIR / f"{req_id}.approve"
    reject = PENDING_DIR / f"{req_id}.reject"
    for _ in range(APPROVAL_TIMEOUT_SEC):
        if approve.exists():
            return "approved"
        if reject.exists():
            return "rejected"
        await asyncio.sleep(1)
    return "timeout"


@app.post("/exec")
async def exec_command(req: ExecRequest):
    req_id = uuid.uuid4().hex[:8]
    payload = {"id": req_id, "command": req.command}
    (PENDING_DIR / f"{req_id}.json").write_text(json.dumps(payload, indent=2))

    logger.warning(
        "APPROVAL NEEDED  id=%s  command=%s\n"
        "  Approve: docker exec action-worker touch /tmp/pending/%s.approve\n"
        "  Reject:  docker exec action-worker touch /tmp/pending/%s.reject",
        req_id,
        req.command,
        req_id,
        req_id,
    )

    verdict = await _await_approval(req_id)
    if verdict != "approved":
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": f"Action {verdict} by approval hook.",
        }

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

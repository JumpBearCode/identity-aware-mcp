"""Execution backend abstraction.

`main.py` no longer talks to a worker URL directly. Both backends — the local
Docker workers and the cloud ACA sandboxes — sit behind one `Executor`
interface so the tool wiring is identical regardless of where the command runs.

  - `LocalDockerExecutor` (here) — POSTs to the existing diagnose/action worker
    containers. No Session concept; one shared container per group. This keeps
    today's behaviour byte-for-byte.
  - `SandboxManager` (sandbox_manager.py, Phase 3) — the ACA backend. It also
    implements this `Executor` protocol, adding Session-sticky routing,
    passwordless FIC login, Blob persistence, and sandbox lifecycle.

Pick the backend with `EXECUTOR=local|aca` (see `make_executor`).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, runtime_checkable

import httpx

logger = logging.getLogger("dataops-mcp.executor")

Group = Literal["diagnose", "action"]


@dataclass(frozen=True)
class ExecResult:
    """Result of running a command, aligned with the worker's JSON shape.

    `exit_code` is `None` when the command timed out and was killed.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_worker_json(cls, data: dict) -> "ExecResult":
        return cls(
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            truncated=bool(data.get("truncated", False)),
        )


@dataclass(frozen=True)
class SessionCtx:
    """Everything an Executor needs to route + persist a single tool call.

    `group` is the read/write boundary (diagnose = read-only, action = write).
    The routing key for ACA stickiness is `(user_oid, session_id, group)`;
    `conversation_id` only scopes Blob output directories, never routing.
    """

    user_oid: str | None
    session_id: str | None
    conversation_id: str | None
    group: Group


@runtime_checkable
class Executor(Protocol):
    """Where a tool call actually runs."""

    async def exec(self, ctx: SessionCtx, command: str) -> ExecResult: ...

    async def aclose(self) -> None: ...


class LocalDockerExecutor:
    """Executor backed by the two long-running worker containers.

    Behaviourally identical to the original `main.py:_exec_on_worker`: it picks
    the diagnose or action worker URL by `ctx.group` and POSTs `/exec`. There is
    no per-Session isolation here — the local stack is a single shared container
    per group, which is exactly today's contract.
    """

    def __init__(self, diagnose_url: str, action_url: str, timeout: float):
        self._urls: dict[Group, str] = {
            "diagnose": diagnose_url,
            "action": action_url,
        }
        self._timeout = timeout

    async def exec(self, ctx: SessionCtx, command: str) -> ExecResult:
        worker_url = self._urls[ctx.group]
        # httpx waits MCP_EXEC_TIMEOUT; the worker kills the subprocess 10s
        # earlier so a timeout comes back as a structured result, not a
        # ReadTimeout exception.
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                f"{worker_url}/exec",
                json={"command": command, "timeout": self._timeout - 10},
            )
            r.raise_for_status()
            return ExecResult.from_worker_json(r.json())

    async def aclose(self) -> None:  # nothing persistent to release
        return None


def make_executor() -> Executor:
    """Build the configured backend from the environment.

    `EXECUTOR=local` (default) → `LocalDockerExecutor`.
    `EXECUTOR=aca`             → `SandboxManager` (imported lazily so the local
                                  path never needs the Azure SDK installed).
    """
    import os

    backend = os.environ.get("EXECUTOR", "local").lower()
    if backend == "aca":
        from sandbox_manager import SandboxManager  # lazy: heavy Azure deps

        logger.info("executor backend: aca (SandboxManager)")
        return SandboxManager.from_env()

    logger.info("executor backend: local (LocalDockerExecutor)")
    return LocalDockerExecutor(
        diagnose_url=os.environ.get("DIAGNOSE_WORKER_URL", "http://diagnose-worker:9001"),
        action_url=os.environ.get("ACTION_WORKER_URL", "http://action-worker:9002"),
        timeout=float(os.environ.get("MCP_EXEC_TIMEOUT", "120")),
    )

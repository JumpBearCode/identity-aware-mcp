"""Blob persistence for per-User / per-Session / per-Conversation workspaces.

Layout in the `mcp-workspaces` container:

    {user_oid}/{session_id}/{conversation_id}/...

`session_id` already carries a `_timestamp` suffix (see session.py), so the
tree is self-documenting about when each Session happened.

Primary mechanism: the SandboxManager mounts this container into each sandbox
as a BYO blob volume (auth = the sandbox group's managed identity), so files
that bash writes under the workspace persist automatically while the sandbox
stays stateless. This module provides the path layout plus a thin
azure-storage-blob helper used server-side (and as the read/write fallback if
volume prefixes turn out too coarse — open item §7.3 of the migration doc).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("dataops-mcp.blob")


class WorkspaceLayout:
    """The single source of truth for workspace paths (no leading slash)."""

    @staticmethod
    def session_prefix(user_oid: str | None, session_id: str | None) -> str:
        return f"{user_oid or 'anon'}/{session_id or 'nosession'}"

    @staticmethod
    def conversation_prefix(
        user_oid: str | None, session_id: str | None, conversation_id: str | None
    ) -> str:
        base = WorkspaceLayout.session_prefix(user_oid, session_id)
        return f"{base}/{conversation_id or 'default'}"


class BlobWorkspace:
    """Server-side helper over the workspace container (read/list/write)."""

    def __init__(self, account_url: str, container: str, credential):
        from azure.storage.blob.aio import BlobServiceClient

        self._svc = BlobServiceClient(account_url=account_url, credential=credential)
        self._container = container

    @classmethod
    def from_env(cls, credential) -> "BlobWorkspace | None":
        import os

        account = os.environ.get("STORAGE_ACCOUNT")
        container = os.environ.get("BLOB_CONTAINER", "mcp-workspaces")
        if not account:
            return None
        return cls(f"https://{account}.blob.core.windows.net", container, credential)

    async def upload(self, path: str, data: bytes, *, overwrite: bool = True) -> None:
        client = self._svc.get_blob_client(self._container, path)
        await client.upload_blob(data, overwrite=overwrite)

    async def download(self, path: str) -> bytes:
        client = self._svc.get_blob_client(self._container, path)
        stream = await client.download_blob()
        return await stream.readall()

    async def list(self, prefix: str) -> list[str]:
        container = self._svc.get_container_client(self._container)
        return [b.name async for b in container.list_blobs(name_starts_with=prefix)]

    async def aclose(self) -> None:
        await self._svc.close()

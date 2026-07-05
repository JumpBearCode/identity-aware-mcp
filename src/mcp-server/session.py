"""Session / Conversation derivation.

The migration's four-layer model:

    User (Entra oid)
      └─ Session            one work period; 30-min sliding TTL; routing lives here
           └─ Conversation  one question; many per Session
                └─ Tool Call diagnose_bash / action_bash

- `user_oid` comes from the verified JWT.
- `session_id` is a sliding 30-min window *per user*: keep calling and you keep
  the same Session (even across Conversations); go quiet past the window and the
  next call mints a new one (`{base}_{unix_ts}` — the timestamp suffix makes the
  Blob layout self-documenting about when the Session happened). The window is
  the routing key's lifetime, so it doubles as the Session-over signal.
- `conversation_id` only scopes Blob output directories; it never enters the
  routing key. MCP doesn't natively carry a "conversation" id, so we take the
  best stable signal available from the transport.

The reliable-source-vs-TTL-heuristic question is an open item in the plan; this
uses the transport id as the Session *base* when present and the TTL window for
lifetime, which is robust regardless of how stable the transport id is.
"""

from __future__ import annotations

import time

from cache import UserSessionCache


class SessionResolver:
    """Resolve `(session_id, conversation_id)` for a tool call."""

    def __init__(self, user_session_cache: UserSessionCache):
        self._c = user_session_cache

    async def resolve(
        self,
        oid: str | None,
        transport_session_id: str | None,
        request_id: str | None,
    ) -> tuple[str | None, str | None]:
        # Conversation: scopes Blob dirs only. Prefer the transport session id
        # (stable across a conversation), fall back to the per-request id.
        conversation_id = transport_session_id or request_id

        if oid is None:
            # No identity to anchor a window — fall back to the transport id.
            return (transport_session_id or "anon"), conversation_id

        existing = await self._c.get(oid)  # also slides the window on hit
        if existing:
            return existing, conversation_id

        base = transport_session_id or oid.replace("-", "")[:12]
        session_id = f"{base}_{int(time.time())}"
        await self._c.set(oid, session_id)
        return session_id, conversation_id

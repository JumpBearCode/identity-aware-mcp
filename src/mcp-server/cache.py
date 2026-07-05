"""Cache abstraction for the MCP server.

Two layers, on purpose:

1. CacheBackend  — a generic, TTL-aware key/value store. InMemoryBackend today;
   swap in a RedisBackend (same interface) once you deploy a Redis pod under K8s,
   without touching any caller.
2. Typed views  — thin wrappers over a backend, one per use case (GroupCache for
   now). To cache MORE things later, add another typed view here that shares the
   SAME backend (one Redis for everything) — don't grow the backend interface.

Values handed to the backend are kept JSON-serializable (e.g. sets are stored as
lists) so InMemory and Redis behave identically.
"""

import json
from typing import Any, Protocol

from cachetools import TTLCache


class CacheBackend(Protocol):
    """Generic TTL-aware key/value store. TTL is fixed at construction."""

    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...


class InMemoryBackend:
    """Per-process TTL cache. Single replica only — each pod keeps its own copy,
    so eviction timing and hit rates are not shared across pods."""

    def __init__(self, ttl: int, maxsize: int = 10_000):
        self._c: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)

    async def get(self, key: str) -> Any | None:
        return self._c.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._c[key] = value

    async def delete(self, key: str) -> None:
        self._c.pop(key, None)


class RedisBackend:
    """Shared cache over Redis — the same two ops, so any typed view works
    unchanged on it. `ttl=None` means no expiry (e.g. the user profile cache);
    an integer TTL (seconds) is re-applied on every `set`, which is how the
    session views get their sliding window (set-on-read refreshes the clock).

    Values are JSON so InMemory and Redis behave identically (sets <-> lists).
    """

    def __init__(self, client, ttl: int | None, prefix: str = "mcp"):
        self._r, self._ttl, self._p = client, ttl, prefix

    def _k(self, key: str) -> str:
        return f"{self._p}:{key}"

    async def get(self, key: str) -> Any | None:
        raw = await self._r.get(self._k(key))
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any) -> None:
        data = json.dumps(value)
        if self._ttl is None:
            await self._r.set(self._k(key), data)
        else:
            await self._r.set(self._k(key), data, ex=self._ttl)

    async def delete(self, key: str) -> None:
        await self._r.delete(self._k(key))


def make_redis_client(url: str):
    """Async Redis client from a redis:// URL (decode_responses for JSON text).

    Fail-fast timeouts: without them a single command blocks indefinitely if the
    host is unreachable, which surfaces to MCP clients as an opaque "Timeout
    connecting to server" hang. With them an unreachable Redis errors in ~5s and
    callers can degrade gracefully.
    """
    from redis.asyncio import from_url

    return from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        health_check_interval=30,
    )


class GroupCache:
    """Typed view: oid -> set of group IDs the user belongs to.

    Keys are namespaced ("groups:") so multiple typed views can share one backend
    (one Redis) without colliding. Sets are stored as lists for JSON-safety.
    """

    def __init__(self, backend: CacheBackend):
        self._b = backend

    async def get(self, oid: str) -> set[str] | None:
        value = await self._b.get(f"groups:{oid}")
        return set(value) if value is not None else None

    async def set(self, oid: str, groups: set[str]) -> None:
        await self._b.set(f"groups:{oid}", list(groups))


class SessionSandboxCache:
    """Typed view: `(oid, session_id, group) -> sandbox_id`.

    This is the Session-stickiness source *and* the Session lifetime signal.
    Back it with a backend whose TTL is the Session window (30 min); `get`
    re-sets the value so an active Session keeps sliding. When the key finally
    expires, the Session is over and the reaper deletes the orphaned sandbox.

    A per-sandbox `bootstrapped:` flag records that first-run FIC login + profile
    restore already happened, so re-routing to a live sandbox skips bootstrap.
    """

    def __init__(self, backend: CacheBackend):
        self._b = backend

    @staticmethod
    def _key(oid: str | None, session_id: str | None, group: str) -> str:
        return f"session:{oid}:{session_id}:{group}"

    async def get(self, oid, session_id, group: str) -> str | None:
        key = self._key(oid, session_id, group)
        sandbox_id = await self._b.get(key)
        if sandbox_id is not None:
            await self._b.set(key, sandbox_id)  # sliding-window refresh
        return sandbox_id

    async def peek(self, oid, session_id, group: str) -> str | None:
        """Read without refreshing the window — for the reaper's liveness check."""
        return await self._b.get(self._key(oid, session_id, group))

    async def set(self, oid, session_id, group: str, sandbox_id: str) -> None:
        await self._b.set(self._key(oid, session_id, group), sandbox_id)

    async def delete(self, oid, session_id, group: str) -> None:
        await self._b.delete(self._key(oid, session_id, group))

    async def is_bootstrapped(self, sandbox_id: str) -> bool:
        return bool(await self._b.get(f"bootstrapped:{sandbox_id}"))

    async def mark_bootstrapped(self, sandbox_id: str) -> None:
        await self._b.set(f"bootstrapped:{sandbox_id}", True)


class UserProfileCache:
    """Typed view: `oid -> {subscription_id, tenant_id, default_rg?}`.

    Only durable profile metadata — never tokens. Written on first login,
    restored into every fresh sandbox so the user's `az` context survives the
    sandbox being stateless. Back it with a long/None TTL backend.
    """

    def __init__(self, backend: CacheBackend):
        self._b = backend

    async def get(self, oid: str) -> dict | None:
        return await self._b.get(f"profile:{oid}")

    async def set(self, oid: str, profile: dict) -> None:
        await self._b.set(f"profile:{oid}", profile)


class UserSessionCache:
    """Typed view: `oid -> current session_id`, with a sliding Session-window TTL.

    Drives Session derivation: while a user keeps calling within the window the
    same `session_id` is reused (across many Conversations); after the window
    lapses the next call mints a new Session. `get` refreshes the window.
    """

    def __init__(self, backend: CacheBackend):
        self._b = backend

    async def get(self, oid: str) -> str | None:
        session_id = await self._b.get(f"usersession:{oid}")
        if session_id is not None:
            await self._b.set(f"usersession:{oid}", session_id)  # slide
        return session_id

    async def set(self, oid: str, session_id: str) -> None:
        await self._b.set(f"usersession:{oid}", session_id)

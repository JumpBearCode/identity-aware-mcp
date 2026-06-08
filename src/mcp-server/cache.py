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

from typing import Any, Protocol

from cachetools import TTLCache


class CacheBackend(Protocol):
    """Generic TTL-aware key/value store. TTL is fixed at construction."""

    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any) -> None: ...


class InMemoryBackend:
    """Per-process TTL cache. Single replica only — each pod keeps its own copy,
    so eviction timing and hit rates are not shared across pods."""

    def __init__(self, ttl: int, maxsize: int = 10_000):
        self._c: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)

    async def get(self, key: str) -> Any | None:
        return self._c.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._c[key] = value


# When you deploy Redis (e.g. a Redis pod under K8s), implement the same two
# methods so all pods share one cache. Sketch:
#
#     import json
#     class RedisBackend:
#         def __init__(self, client, ttl: int, prefix: str = "mcp"):
#             self._r, self._ttl, self._p = client, ttl, prefix
#         async def get(self, key):
#             raw = await self._r.get(f"{self._p}:{key}")
#             return json.loads(raw) if raw is not None else None
#         async def set(self, key, value):
#             await self._r.set(f"{self._p}:{key}", json.dumps(value), ex=self._ttl)
#
# Then in main.py:  group_cache = GroupCache(RedisBackend(redis_client, ttl=300))


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

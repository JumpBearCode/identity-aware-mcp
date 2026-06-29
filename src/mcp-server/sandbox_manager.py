"""SandboxManager — the ACA execution backend (implements Executor).

This is the control brain for the cloud path. It turns "one tool call" into
"run this command in the correct, running, already-logged-in sandbox", and owns
those sandboxes' whole lifecycle. Five jobs (see §3.4 of the migration doc):

1. Hold the SDK clients — one data-plane `SandboxGroupClient` per group, built
   from `DefaultAzureCredential` (the MCP app's managed identity in the cloud).
2. Session-sticky routing — Redis maps `(oid, session_id, group)` to a
   `sandbox_id`; a hit reuses it (`get_sandbox_client` + `ensure_running`), a
   miss creates one.
3. First-run bootstrap (once per sandbox) — passwordless FIC `az login` as the
   group's worker SP, then restore the user's `az` profile.
4. Execute — `SandboxClient.exec`, refresh the Session TTL, cap the output.
5. Lifecycle — delete a Session's sandboxes on end; auto-suspend / auto-delete
   as the idle fallback. (Background reaper lands in Phase 6; blob volume in 5.)
"""

from __future__ import annotations

import asyncio
import logging
import os

from azure.core.exceptions import ResourceNotFoundError

from cache import (
    SessionSandboxCache,
    UserProfileCache,
    make_redis_client,
    RedisBackend,
)
from executor import ExecResult, Group, SessionCtx

logger = logging.getLogger("dataops-mcp.sandbox")

MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", str(64 * 1024)))
TRUNCATE_HINT = (
    "\n\n[output truncated at {n} bytes] Narrow it at the source — "
    "`az ... --query <JMESPath> -o tsv` or `| jq '<filter>'`. If you need the "
    "full result, redirect to a file (`az ... > /tmp/out.json`) and read it in "
    "chunks with jq/grep."
)


def _cap(text: str) -> tuple[str, bool]:
    raw = text.encode(errors="replace")
    if len(raw) <= MAX_OUTPUT_BYTES:
        return text, False
    return raw[:MAX_OUTPUT_BYTES].decode(errors="replace"), True


def _label_safe(value: str | None) -> str:
    """Reduce an id to a label-safe token."""
    if not value:
        return "none"
    return "".join(c if c.isalnum() else "-" for c in value)[:63]


class SandboxManager:
    """ACA implementation of the Executor protocol."""

    def __init__(
        self,
        *,
        credential,
        subscription_id: str,
        resource_group: str,
        region: str,
        tenant_id: str,
        group_names: dict[Group, str],
        sp_app_ids: dict[Group, str],
        sessions: SessionSandboxCache,
        profiles: UserProfileCache,
        default_subscription: str | None,
        disk_id: str | None,
        disk_image: str | None,
        disk_public: str,
        cpu: str,
        memory: str,
        auto_suspend_seconds: int,
        auto_delete_seconds: int,
        redis_client=None,
    ):
        self._cred = credential
        self._subscription_id = subscription_id
        self._resource_group = resource_group
        self._region = region
        self._tenant_id = tenant_id
        self._group_names = group_names
        self._sp_app_ids = sp_app_ids
        self._sessions = sessions
        self._profiles = profiles
        self._default_subscription = default_subscription or subscription_id
        self._disk_id = disk_id
        self._disk_image = disk_image
        self._disk_public = disk_public
        self._cpu = cpu
        self._memory = memory
        self._auto_suspend_seconds = auto_suspend_seconds
        self._auto_delete_seconds = auto_delete_seconds
        self._redis = redis_client

        self._group_clients: dict[Group, object] = {}
        self._built_disk_ids: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ build
    @classmethod
    def from_env(cls) -> "SandboxManager":
        from azure.identity.aio import DefaultAzureCredential

        redis_url = os.environ["REDIS_URL"]
        redis_client = make_redis_client(redis_url)
        session_ttl = int(os.environ.get("MCP_SESSION_TTL", "1800"))
        sessions = SessionSandboxCache(RedisBackend(redis_client, ttl=session_ttl))
        profiles = UserProfileCache(RedisBackend(redis_client, ttl=None))

        return cls(
            credential=DefaultAzureCredential(),
            subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
            resource_group=os.environ["ACA_RESOURCE_GROUP"],
            region=os.environ["ACA_REGION"],
            tenant_id=os.environ["AZURE_TENANT_ID"],
            group_names={
                "diagnose": os.environ["DIAGNOSE_SANDBOX_GROUP"],
                "action": os.environ["ACTION_SANDBOX_GROUP"],
            },
            sp_app_ids={
                "diagnose": os.environ["DIAGNOSE_SP_APP_ID"],
                "action": os.environ["ACTION_SP_APP_ID"],
            },
            sessions=sessions,
            profiles=profiles,
            default_subscription=os.environ.get("AZURE_SUBSCRIPTION_ID"),
            disk_id=os.environ.get("SANDBOX_DISK_ID") or None,
            disk_image=os.environ.get("SANDBOX_DISK_IMAGE") or None,
            disk_public=os.environ.get("SANDBOX_DISK", "ubuntu"),
            cpu=os.environ.get("SANDBOX_CPU", "1000m"),
            memory=os.environ.get("SANDBOX_MEMORY", "2048Mi"),
            auto_suspend_seconds=int(os.environ.get("SANDBOX_AUTO_SUSPEND_SECONDS", "300")),
            auto_delete_seconds=int(os.environ.get("SANDBOX_AUTO_DELETE_SECONDS", "3600")),
            redis_client=redis_client,
        )

    def _group_client(self, group: Group):
        client = self._group_clients.get(group)
        if client is None:
            from azure.containerapps.sandbox import endpoint_for_region
            from azure.containerapps.sandbox.aio import SandboxGroupClient

            client = SandboxGroupClient(
                endpoint_for_region(self._region),
                self._cred,
                subscription_id=self._subscription_id,
                resource_group=self._resource_group,
                sandbox_group=self._group_names[group],
            )
            self._group_clients[group] = client
        return client

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # -------------------------------------------------------------- routing
    async def get_or_create(self, ctx: SessionCtx):
        """Return a running, bootstrapped SandboxClient for this routing key."""
        group = ctx.group
        gclient = self._group_client(group)

        # Serialize create-or-reuse per routing key so two concurrent calls in
        # the same Session don't each spin up a sandbox.
        async with self._lock(f"{ctx.user_oid}:{ctx.session_id}:{group}"):
            sandbox_id = await self._sessions.get(ctx.user_oid, ctx.session_id, group)
            if sandbox_id is not None:
                client = gclient.get_sandbox_client(sandbox_id)
                try:
                    await client.ensure_running()
                    logger.info("session hit: reuse sandbox %s (%s)", sandbox_id, group)
                    return client
                except ResourceNotFoundError:
                    logger.info("stale session sandbox %s gone; recreating", sandbox_id)
                    await self._sessions.delete(ctx.user_oid, ctx.session_id, group)

            client = await self._create_sandbox(ctx, gclient, group)
            await self._sessions.set(ctx.user_oid, ctx.session_id, group, client.sandbox_id)
            if not await self._sessions.is_bootstrapped(client.sandbox_id):
                await self._bootstrap(client, ctx, group)
                await self._sessions.mark_bootstrapped(client.sandbox_id)
            return client

    async def _create_sandbox(self, ctx: SessionCtx, gclient, group: Group):
        disk_kwargs = await self._resolve_disk(gclient, group)
        labels = {
            "user": _label_safe(ctx.user_oid),
            "session": _label_safe(ctx.session_id),
            "group": group,
        }
        environment = {
            "SP_APP_ID": self._sp_app_ids[group],
            "AZURE_TENANT_ID": self._tenant_id,
            "AZURE_SUBSCRIPTION_ID": await self._user_subscription(ctx.user_oid),
        }
        logger.info("session miss: creating %s sandbox (%s)", group, disk_kwargs)
        poller = await gclient.begin_create_sandbox(
            labels=labels,
            environment=environment,
            cpu=self._cpu,
            memory=self._memory,
            auto_suspend_seconds=self._auto_suspend_seconds,
            **disk_kwargs,
        )
        client = await poller.result()
        await self._apply_idle_autodelete(client)
        return client

    async def _resolve_disk(self, gclient, group: Group) -> dict:
        """Pick the sandbox source: prebuilt disk id > built-from-image > public."""
        if self._disk_id:
            return {"disk_id": self._disk_id}
        if self._disk_image:
            return {"disk_id": await self._ensure_disk_image(gclient, group)}
        return {"disk": self._disk_public}

    async def _ensure_disk_image(self, gclient, group: Group) -> str:
        group_name = self._group_names[group]
        async with self._lock(f"diskimage:{group_name}"):
            cached = self._built_disk_ids.get(group_name)
            if cached:
                return cached
            logger.info("building disk image for %s from %s", group_name, self._disk_image)
            poller = await gclient.begin_create_disk_image(self._disk_image, name="mcp-sandbox")
            image = await poller.result()
            self._built_disk_ids[group_name] = image.id
            return image.id

    async def _apply_idle_autodelete(self, client) -> None:
        """1-hour idle auto-delete fallback so orphaned sandboxes self-reclaim."""
        try:
            from azure.containerapps.sandbox import AutoDeletePolicy, LifecyclePolicy

            await client.set_lifecycle_policy(
                LifecyclePolicy(
                    auto_delete=AutoDeletePolicy(
                        enabled=True, delete_interval_seconds=self._auto_delete_seconds
                    )
                )
            )
        except Exception as e:  # non-fatal: Session-level reaper is the main path
            logger.warning("could not set auto-delete on %s: %s", client.sandbox_id, e)

    # ------------------------------------------------------------- bootstrap
    async def _bootstrap(self, client, ctx: SessionCtx, group: Group) -> None:
        """Passwordless FIC login as the worker SP, then restore user context."""
        logger.info("bootstrapping sandbox %s (%s)", client.sandbox_id, group)
        result = await client.exec("bash /opt/bootstrap.sh")
        if result.exit_code != 0:
            logger.error(
                "bootstrap failed on %s: rc=%s stderr=%s",
                client.sandbox_id, result.exit_code, result.stderr[:2000],
            )
            raise RuntimeError(f"sandbox bootstrap failed (rc={result.exit_code})")
        logger.info("bootstrap ok on %s: %s", client.sandbox_id, result.stdout.strip()[:200])

    async def _user_subscription(self, oid: str | None) -> str:
        if oid is not None:
            profile = await self._profiles.get(oid)
            if profile and profile.get("subscription_id"):
                return profile["subscription_id"]
        return self._default_subscription

    # ----------------------------------------------------------------- exec
    async def exec(self, ctx: SessionCtx, command: str) -> ExecResult:
        client = await self.get_or_create(ctx)
        result = await client.exec(command)
        stdout, t1 = _cap(result.stdout or "")
        stderr, t2 = _cap(result.stderr or "")
        if t1 or t2:
            stdout += TRUNCATE_HINT.format(n=MAX_OUTPUT_BYTES)
        return ExecResult(
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=t1 or t2,
        )

    # ------------------------------------------------------------ lifecycle
    async def end_session(self, oid: str | None, session_id: str | None) -> None:
        """Delete both of a Session's sandboxes and clear its routing keys."""
        for group in ("diagnose", "action"):
            sandbox_id = await self._sessions.get(oid, session_id, group)  # type: ignore[arg-type]
            if sandbox_id is None:
                continue
            try:
                gclient = self._group_client(group)  # type: ignore[arg-type]
                await gclient.begin_delete_sandbox(sandbox_id)
                logger.info("ended session: deleted %s sandbox %s", group, sandbox_id)
            except ResourceNotFoundError:
                pass
            finally:
                await self._sessions.delete(oid, session_id, group)  # type: ignore[arg-type]

    async def aclose(self) -> None:
        for client in self._group_clients.values():
            try:
                await client.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            await self._cred.close()
        except Exception:
            pass
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass

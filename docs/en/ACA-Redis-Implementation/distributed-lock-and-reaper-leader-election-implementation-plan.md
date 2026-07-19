# Distributed Lock & Reaper Leader Election: Implementation Plan (Plain English)

Companion design doc: [MCP-Horizontal-Scaling-Distributed-Lock-and-Reaper-Leader-Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md).
That doc covers "why and the principles"; **this doc only covers "exactly which lines of code to change, how to set parameters, and how to roll out in steps"**, and tries to use plain language.

Files involved: `src/mcp-server/sandbox_manager.py`, `cache.py`, `main.py`, `provisioning/aca/modules/mcp-app.bicep` (line numbers anchored at current commit `0b0706d`).

---

## 0. In a Nutshell: What This Document Is For

Later, if we scale the service from "1 instance" to "several instances", two minor issues will pop up. We'll fix **two pieces of code** in advance to plug those holes. **The changes are small and are disabled by default, so they don't affect anything now.**

---

## 1. Let's Clarify a Few Terms First (Otherwise You Won't Follow)

We'll use one analogy throughout: **You're running a customer service center.**

### 1.1 Replica = Customer Service Agent

Right now there is only **1 agent** (1 instance) answering all calls. In the future, when calls increase, you'll hire **N agents** (N instances) to answer them together — they all do the exact same job, but more people means more calls can be handled. This is "**horizontal scaling**", and in the config it means changing `maxReplicas` from 1 to 3 (`mcp-app.bicep:121`).

⚠️ **Key point: The same customer might get agent A on one call and agent B on the next.** Who answers is random (load balancing). This is the root of all the trouble that follows.

### 1.2 Sandbox = A Dedicated Workstation Set Up for the Customer

The first time a customer calls in, the agent needs to **set up a dedicated workstation for them** (create a sandbox). This action is **slow** (seconds to tens of seconds) and **once it's set up, it can't be taken back**. After it's set up, all subsequent work for that customer happens on this workstation.

- **Setting up the workstation** = `_create_sandbox` in `get_or_create` (`sandbox_manager.py:244`), slow.
- **After the workstation is set up, it needs to be "booted and logged in"** once (`az login`) = `_bootstrap` (`:358`).
- **Which customer → which workstation** is recorded in a **shared registry** (Redis).

### 1.3 Routing Key = The Customer's ID Badge

In the registry, each workstation is associated with an **ID badge**. The ID badge = `(user_oid, session, group)` combined.

- Same ID badge → same workstation (reuse).
- Different group (diagnose / action) → different ID badge → different workstation (this is correct, read and write should be separated).

### 1.4 The Problem to Prevent: The Same Customer Gets Two Workstations Set Up

**Trouble scenario** (Design doc §3.3): A customer makes two calls almost simultaneously (e.g., two parallel `diagnose_bash` calls in one AI turn), one goes to agent A, the other to agent B.

1. A checks the registry: no workstation for this customer → I'll set one up.
2. B checks the registry at the same time: also no workstation (A hasn't finished setting up and written it yet) → I'll set one up too.
3. Result: **Two workstations are set up, but only one is registered. The other is unclaimed = orphan, wasting money.**

What we need to prevent is this: **For the same ID badge, only one person is allowed to "set up a workstation" at any given time.**

### 1.5 Two Types of "Simultaneous", Requiring Two Different Locks

"Preventing two workstations from being set up at the same time" essentially means adding a **lock**: whoever wants to set one up must acquire the lock first, set it up, then release the lock. But "simultaneous" comes in two types:

| Type of "Simultaneous" | What Blocks It | Analogy |
|---|---|---|
| **Within the same agent**, two concurrent calls (two coroutines in the same process) | `asyncio.Lock` (already in the code) | Agent A remembers internally "I'm already handling this customer" |
| **Between different agents** (A and B, different processes) | **Redis lock** (to be added) | A note on a **whiteboard everyone can see** saying "I'm working on this, don't touch" |

**Why isn't the existing `asyncio.Lock` enough?** Because it's just a **mental note** for agent A; agent B can't see it at all. A and B each think independently, and they'll still each set up a workstation. So, for cross-agent coordination, we must rely on a **shared** thing — Redis (the whiteboard). This is the "distributed lock".

> ⚠️ Note: Today, with only 1 agent, all calls are within that one agent. `asyncio.Lock` is completely sufficient; **there won't be a single orphan**. The Redis lock is only needed "when you hire a 2nd agent".

### 1.6 So Why Not Just Use the Redis Lock and Throw Away `asyncio.Lock`?

Because **for concurrency within the same agent, using `asyncio.Lock` is free (pure memory); using Redis requires a network round trip**. Imagine 10 concurrent calls from the same customer within agent A:

- **Using only Redis lock**: All 10 coroutines **go to Redis to compete for the lock**, and 9 of them are stuck doing repeated network round trips waiting.
- **Using both locks together (this plan)**: The 10 coroutines first queue up on the local `asyncio.Lock` (free), and **only the first one in line** goes to Redis to acquire the lock and block other agents. The pressure on Redis is reduced by an order of magnitude.

There's another, more important point — **if Redis goes down**: `asyncio.Lock` is still there, so correctness within the same agent is preserved; if we only had the Redis lock, its failure would mean no lock at all. That's why it's called "**two lines of defense**": the cheap and reliable one (`asyncio`) handles the bulk of the work, and the expensive and potentially faulty one (Redis) only covers the "cross-agent" scenario that the first one can't reach.

**Conclusion: Two locks with a division of labor — `asyncio.Lock` handles "same agent", Redis lock handles "cross-agent". The former is free and reliable, no reason to discard it.**

### 1.7 Fast Path / Slow Path / Lock-Free (These Terms Are the Easiest to Get Confused By)

"**Fast path / slow path**" is a general programming term: **most cases take a fast and simple route (fast path), and only a few cases take a slow and complex route (slow path).**

Applied here:

- **Slow path** = The customer's first time, **needs a new workstation**. Slow, and must be locked (to prevent setting up two).
- **Fast path** = The workstation was set up long ago, it's in the registry, **just look it up and use it**. Fast.

**Key point: The vast majority of calls are on the fast path.** For the 2nd, 3rd, 4th… calls within the same session, the workstation was set up the first time, and everything after is "check registry → found → use directly". **Only the first time for each session** goes through the slow path to set up the workstation.

"**Lock-free fast path**" = **"Using an existing workstation directly" doesn't require acquiring a lock at all**. Because two people using the same existing workstation at the same time is not a problem; the lock is only to prevent "setting up **two** workstations simultaneously", not to prevent "**using** one simultaneously".

Later, §8 will explain: this "lock-free fast path" sounds like it could speed things up, but in our case, it would introduce new bugs, and the overhead it saves isn't worth it. So, **it's not implemented by default**. Just know what the term means for now.

---

## 2. What the Current Code Does and What It's Missing

`get_or_create` (`sandbox_manager.py:191`) currently does one thing: **takes the ID badge, checks the registry for a workstation, uses it if found, sets one up if not.** Simplified version:

```python
async with self._lock(ID Badge):        # ← Only asyncio.Lock (only blocks same agent)
    workstation = check_registry(ID Badge)
    if workstation exists:
        ensure booted → return reuse            # Fast path
    workstation = create_new_one()               # Slow path (slow, no timeout)
    write_registry(ID Badge → workstation)
    boot_and_login(az login)
    return
```

**Missing three things (all only become problems with "multiple agents"):**

1. Only `asyncio.Lock`, **cannot block cross-agent** double creation → orphans.
2. The workstation creation step **has no timeout**, if it gets stuck, it holds the lock forever.
3. **The reaper for recycling workstations runs on every agent** (`_reaper_loop` `:432`), N agents scan N times redundantly, wasteful.

---

## 3. The Three Things We Need to Do (Overview)

| # | Plain English | **Actual Code Changes** (All in `sandbox_manager.py`) | Details |
|---|---|---|---|
| **①** | Add a "cross-agent" lock: before creating a workstation, acquire a lock on Redis; other agents see it and wait | **New** `_dlock()` context manager; inside `get_or_create`, wrap the existing `self._lock(key)` **with another layer** `async with self._dlock(key)` | §4.2 / §4.3 |
| **②** | Add a timer for workstation creation: give up and release the lock if it takes too long | In `get_or_create`, wrap `_create_sandbox(...)` with `asyncio.wait_for(..., create_timeout)`; add a `try/except` to revoke the session key on failure | §4.3 / §8 |
| **③** | Let only one agent do the recycling: elect a "duty officer" to scan for orphans, the rest stand by | Modify `_reaper_loop`: at the start of each round, `SET mcp:reaper:leader NX EX` to become the leader; non-leaders skip; **new** `_try_become_reaper()` / `_resign_reaper()` methods + `_RELEASE_LUA` constant. `reap_orphans` itself is unchanged | §4.4 |
| **Common** | Several switches and timeout knobs | `__init__` **adds 5 new parameters** (`distributed_lock` / `lock_ttl` / `lock_wait` / `create_timeout` / `reaper_lease`), `from_env` reads the corresponding 5 environment variables; add `import contextlib` / `import uuid` at the top of the file | §4.1 |

**Scope of changes nailed down in one sentence:** All concentrated in **`sandbox_manager.py` one file** — **add 3 private methods** (`_dlock`, `_try_become_reaper`, `_resign_reaper`) + **modify 2 methods** (`get_or_create` adds two layers of locks + timer, `_reaper_loop` adds leader election) + add 5 parameters. **`reap_orphans`, `cache.py`, `main.py` don't need changes** (bicep only changes `maxReplicas` + turns on the switch in PR-4).

All three things rely on the **same Redis trick** (`SET key val NX` = "only occupy if unoccupied" + Lua verification for release), just with different keys and semantics (Design doc §7): ①'s key is `lock:<ID Badge>`, ③'s key is `reaper:leader`.

And **everything is behind a master switch, disabled by default**:

- Switch off (now) = code behavior **identical to today**, only `asyncio.Lock`.
- Switch on (in the future when you hire a 2nd agent) = all three things take effect.

The switch is called `SANDBOX_DISTRIBUTED_LOCK` (default `0`). **It's the gate that gets opened together with `maxReplicas` when going from 1 agent to multiple agents.**

> Regarding **watchdog (automatic lease renewal)**: Design doc §10.2 determines that implementing it now would be "premature optimization", **this plan does not implement it**, using a "sufficiently generous fixed timeout" instead. We'll revisit if actual measurements show that workstation creation is really slow.

---

## 4. Making the Changes: `sandbox_manager.py`

> ✅ **The changes in this section have been implemented (PR-1 instrumentation + PR-2 + PR-3, right here on the `fix-redis` branch).** The code blocks below are explanatory versions (some use Chinese placeholders to show the structure); the actual implementation is in `src/mcp-server/sandbox_manager.py`, with behavior consistent with what's described here. Everything is behind `SANDBOX_DISTRIBUTED_LOCK`, disabled by default.

### 4.1 First, Add a Few Switches and Timeout Parameters

At the end of `__init__` (`:65`) and in `from_env` (`:126`), add these (all have defaults, so it works without configuration):

```python
# __init__ new parameters
distributed_lock: bool = False,   # Master switch. Off = only asyncio.Lock (today's behavior)
lock_ttl: int = 60,               # How long the Redis sticky note lasts (seconds), must cover worst-case "create workstation + boot" time
lock_wait: float = 45.0,          # Max wait time when unable to acquire the lock
create_timeout: float = 30.0,     # Timer for creating a workstation: give up if exceeded
reaper_lease: int = 90,           # Duty officer term (seconds)
```

```python
# In from_env, read the corresponding environment variables
distributed_lock=os.environ.get("SANDBOX_DISTRIBUTED_LOCK", "0") == "1",
lock_ttl=int(os.environ.get("SANDBOX_LOCK_TTL", "60")),
lock_wait=float(os.environ.get("SANDBOX_LOCK_WAIT", "45")),
create_timeout=float(os.environ.get("SANDBOX_CREATE_TIMEOUT", "30")),
reaper_lease=int(os.environ.get("SANDBOX_REAPER_LEASE", "90")),
```

> ✅ **The second values above have been calibrated through actual measurements (see §5.1), they are no longer placeholders.** Actual cold start end-to-end is ~4s (worst case ~8s), so `create_timeout=30`, `lock_ttl=60`, `lock_wait=45` all have a "4-7x margin over the measured worst case", which is very safe. **Watchdog is confirmed not needed** (critical section ~8s, far from the 60s TTL).

### 4.2 Add a "Cross-Agent" Lock: `_dlock`

Add it right next to the existing `_lock` (`:183`). Core idea: **if you can stick the sticky note, stick it; if you can't stick it / the whiteboard (Redis) is broken, just let it through. Never let the lock cause a user request to hang or fail.**

```python
import contextlib   # Add at the top of the file; asyncio is already imported

@contextlib.asynccontextmanager
async def _dlock(self, key: str):
    """Cross-agent lock, layered beneath asyncio.Lock. If it can't be stuck / Redis is down, let it through (degrade)."""
    if not self._dlock_enabled or self._redis is None:
        yield                                   # Switch off / no Redis → this lock is a no-op
        return
    lock = self._redis.lock(
        f"lock:{key}",
        timeout=self._lock_ttl,                 # Sticky note TTL, auto-removed when expired (prevents deadlock)
        blocking=True,
        blocking_timeout=self._lock_wait,       # Max wait time when unable to stick the note
    )
    got = False
    try:
        got = await lock.acquire()
        if not got:
            logger.warning("dlock: %s lock wait timeout, degrading and letting through", key)
    except Exception as e:                       # Redis connection failure, etc.
        logger.warning("dlock: lock acquisition error (%s), degrading to local lock only", e)
    try:
        yield
    finally:
        if got:
            try:
                await lock.release()             # redis-py internally uses a script to verify "is this my sticky note" before removing, safe
            except Exception as e:
                logger.warning("dlock: failed to remove sticky note (%s)", e)
```

This uses redis-py's built-in `Lock` (Design doc §4.5: don't reinvent the wheel). **"Letting through if it can't be stuck" is intentional** — this lock is a "cost-saving lock", not a "correctness lock" (Design doc §4.8): even if a double creation slips through occasionally, the extra orphan is handled by the reaper, so there's no correctness issue.

> Small note: Our Redis client has `decode_responses` enabled (`cache.py:89`). redis-py `Lock`'s acquire/release work fine; **just don't use its `lock.owned()` / `lock.extend()`** (those two would misjudge). We don't use them, so it's fine.

### 4.3 Integrate This Lock into `get_or_create`

**Minimal change: just nest `_dlock` inside the existing `asyncio.Lock` layer, and add a timer for "create workstation". Every other line and their order remain unchanged.**

```python
async def get_or_create(self, ctx):
    group = ctx.group
    gclient = self._group_client(group)
    key = f"{ctx.user_oid}:{ctx.session_id}:{group}"

    async with self._lock(key):            # Outer layer: blocks same agent (free)
        async with self._dlock(key):       # Inner layer: blocks cross-agent (§4.2, no-op when switch is off)
            workstation = check_registry(...)
            if workstation exists:
                ensure booted → return reuse
            # Wrap "create workstation" with a timer: give up on timeout, release locks (both layers auto-release)
            workstation = await asyncio.wait_for(
                self._create_sandbox(ctx, gclient, group),
                timeout=self._create_timeout,
            )
            try:
                write_registry(ID Badge → workstation)     # Order must not change: write registry first
                write_reverse_index(workstation → ID Badge)   # For the reaper
                if not booted:
                    boot_and_login()
                    mark_as_booted()
                return workstation
            except Exception:
                # New: if boot/write to DB fails, revoke the registry entry to avoid leaving a "bad workstation" for the next person.
                # Keep the reverse index so the reaper can quickly recycle this broken workstation (see §8).
                revoke_registry(...)
                raise
```

There are really only three changes:
1. **Wrap with an extra `async with self._dlock(key)`** — cross-agent mutual exclusion (no effect when switch is off).
2. **Wrap `_create_sandbox` with `asyncio.wait_for(..., create_timeout)`** — timer for workstation creation.
3. **Add a `try/except` to revoke the registry entry** — a quick fix for an existing minor issue (explained in §8).

> The Chinese placeholders above are for clarity of structure. In the actual code, replace "check_registry", "write_registry" with the original `self._sessions.get/set`, `self._index.set`, `self._bootstrap` lines (they don't need a single character changed).

### 4.4 Let Only One Agent Do the Recycling: Duty Officer Election

Currently, every agent scans independently (`_reaper_loop` `:432`). Change it to: **at the start of each round, everyone competes for a "I'm on duty today" sticky note. The one who gets it does the scan; the rest skip.** If they can't get it / Redis is down, fall back to "everyone scans" (redundant scanning is safe, just wasteful).

```python
# Add at the top of the file: only remove the sticky note if it's ours (prevent accidentally removing someone else's)
_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end")
_NO_LEASE = ""   # Sentinel: should scan, but didn't get the sticky note (no need to remove)

async def _reaper_loop(self):
    while True:
        await asyncio.sleep(self._reaper_interval)     # Default every 300s
        try:
            token = await self._try_become_reaper()    # Try to become duty officer; None if failed
            if token is None:
                continue                               # Someone else is on duty → skip this round
            try:
                await self.reap_orphans()              # Only the duty officer actually scans
            finally:
                await self._resign_reaper(token)       # Remove the sticky note after scanning
        except Exception as e:
            logger.warning("reaper pass failed: %s", e)

async def _try_become_reaper(self):
    if not self._dlock_enabled or self._redis is None:
        return _NO_LEASE                               # Switch off / no Redis → scan as usual (idempotent)
    token = uuid.uuid4().hex
    try:
        got = await self._redis.set("mcp:reaper:leader", token,
                                    nx=True, ex=self._reaper_lease)
    except Exception as e:
        logger.warning("reaper election error (%s), scanning as usual", e)
        return _NO_LEASE
    return token if got else None

async def _resign_reaper(self, token):
    if not token or self._redis is None:
        return
    try:
        await self._redis.eval(_RELEASE_LUA, 1, "mcp:reaper:leader", token)
    except Exception as e:
        logger.warning("reaper handover failed (%s)", e)
```

Add `import uuid` at the top of the file. **`reap_orphans` (`:440`) itself doesn't change a single line.** The duty officer term `reaper_lease` just needs to be longer than the worst-case time for one scan round; if it does expire, at most two agents will both scan in the same round, which is redundant but safe. If the duty officer crashes, the sticky note auto-expires after 90 seconds, and someone else will naturally take over in the next round.

---

## 5. How to Set Parameters: Measure First, Then Tune (The Easiest First Step)

The second values in §4.1 are placeholders. **What they should actually be depends on "how long it actually takes to create a workstation". So the first step is not to write the lock, but to measure.**

Add timestamps before and after `_create_sandbox` (`:244`) and `_bootstrap` (`:358`):

```python
import time
t0 = time.monotonic()
client = await poller.result()
logger.info("timing: create workstation %s took %.2fs (%s)", client.sandbox_id, time.monotonic()-t0, group)
```

Run it a few dozen times and see how long each of the three scenarios takes: **cold start first creation / cache hit reuse (should be sub-second) / brand new image first creation (will be slow, but one-time)**. After measuring, fill in the seconds according to this rule (Design doc §4.9):

```
Actual workstation creation time  <  create_timeout (workstation creation timer)  <  lock_wait (max lock wait)
and  lock_ttl (sticky note duration)  >  create_timeout + boot time
```

> Design doc §10.1 estimates that cold start is actually **second-level** (microVM starts in seconds, images are pre-built, boot is just a few APIs). If actual measurements confirm it's second-level, the current placeholder values are already generously sufficient, **and watchdog is indeed unnecessary**.

### 5.1 Actual Measurement Results (2026-07-04, Current Single-Instance Deployment) ✅

The instrumentation above has been added to `sandbox_manager.py` and **actually run** (measurement script in §5.2). On the current **single-instance** `dataops-aca-rg` (westus2), for the `diagnose` / `action` groups, 5 "fake users" (different routing keys) were created serially, each reused once, totaling 10 workstations. **All were deleted after measurement, 0 leaks.** Unit: seconds.

| Segment | Meaning | Median | Worst (p95≈max) | Notes |
|---|---|---|---|---|
| **A `disk`** | Parse disk/image | **~0** (cache hit) | 0.09 | Only paid once **per replica first time** for `list_disk_images` (image already Ready, reused directly, **no build triggered**); after that, in-process cache, ≈0 |
| **B `vol`** | Create/confirm volume | **~0** (cache hit) | 0.09 | Same, first confirmation (volume already exists, 409 swallowed), after that cache ≈0 |
| **C `vm`** ⭐ | **Create microVM itself** | **~1.1** | **4.70** | The only segment with variance. Mostly ~0.9–1.2s, occasional spikes to 2.3–4.7s |
| **D `autodel`** | Set auto-delete policy | 0.13 | 0.22 | Stable, small overhead |
| **E `bootstrap`** ⭐ | **FIC `az login`** | **~2.7** | 3.42 | Very stable, the largest and most predictable chunk |
| **wall (end-to-end miss)** | A+B+C+D+E+DB write | **~4.0** | **8.34** | One full "cold" creation from start to finish |
| **hit (reuse)** | `ensure_running` | **0.08** | 0.10 | Hit path takes almost no time |

**Four conclusions:**

1. **Cold start is second-level, not minute-level** — median ~4s, worst ~8.3s, **confirming Design doc §10.1**.
2. **bootstrap (E, ~2.7s) is heavier and more stable than VM creation (C, ~1s)**; the real jitter is in C (microVM scheduling).
3. **A/B steady state ≈ 0** (only the first time per replica costs ~0.09s), **no image build encountered** (image already Ready → reused). The "90-second timer hitting a minute-level build" landmine from §8 **only triggers for brand new groups / new image tags**; in steady state, it's always reuse, so `create_timeout` is unaffected.
4. **hit path ~0.08s** — confirming that "the vast majority of calls are fast, locking the hot path makes almost no difference", so §5 (optional fast path) is indeed unnecessary.

**Based on this, replace the placeholder values in §4.1 with the calibrated values:**

| Parameter | Old Placeholder | Calibrated Value | Rationale |
|---|---|---|---|
| `create_timeout` | 90 | **30** | Worst "create VM segment" (A+B+C+D) ~5s → 6× margin |
| `lock_ttl` | 180 | **60** | Worst critical section (create + boot) ~8.3s → 7× margin; if lock holder crashes, others wait at most 60s |
| `lock_wait` | 120 | **45** | ≥ one worst-case "create + boot", so the waiter can get the reuse; and `> create_timeout` |
| watchdog | TBD | **Not implemented** | Critical section ~8s, far from 60s TTL, renewal is completely unnecessary |

> ⚠️ These are numbers from a **single-instance, serial, pre-warmed image** scenario, which is an **optimistic lower bound**. With multiple replicas competing for ARM, or cross-region cold starts, C (create VM) will be more jittery — hence the 4-7× margin. When doing multi-replica stress testing in PR-4, re-run the same script to see the tail of C before finalizing.

### 5.2 Measurement Script (Reproducible)

> 📌 **Branch note:** The PR-1 timing instrumentation is already in `sandbox_manager.py` on **this branch `fix-redis`** along with the feature. The two **measurement scaffolding** files (harness + unit test) below are only on the `measure-sandbox-timing` branch in `src/mcp-server/tests/` — they, along with the formal unit tests for PR-2/PR-3, will be added later during **end-to-end (e2e) testing** (see §7).

Two files (on the `measure-sandbox-timing` branch in `src/mcp-server/tests/`):

- **`test_timing_instrumentation.py`** — Pure mock unit tests (no Azure interaction, no cost), verifying that each segment emits the correct structured `timing` log. Runnable in CI: `pytest src/mcp-server/tests/`.
- **`measure_create_timeline.py`** — **The actual measurement script that hits ACA** (creates real sandboxes, costs money), which is the source of the table above. Usage:

  ```bash
  # Requires az login + environment with azure-containerapps-sandbox / azure-identity installed
  python src/mcp-server/tests/measure_create_timeline.py --n 5 --group diagnose
  ```

  **Why it doesn't need OAuth/OBO login**: OBO is only used at the MCP **front door** (`main.py`: validates user JWT → OBO → Graph to check groups), and its output is just a `user_oid` string. The sandbox lifecycle we're measuring (`get_or_create → _create_sandbox → _bootstrap`) authenticates to ACA using the **application's own** `DefaultAzureCredential`, **independent of the user token**. So "different users" = just feed different `(oid, session)` routing keys, **no actual login needed**. The script also bypasses Redis (uses an in-memory cache instead, because the production Redis is an internal FQDN unreachable from the local machine), and **cleans up after each run by diffing** (compares the sandbox list before and after, deletes the ones added during the run, preventing leaks).

---

## 6. Rollout Steps (4 Small PRs, Each Reversible)

| PR | What It Does | Status | Will It Change Current Behavior? |
|---|---|---|---|
| **PR-1** | Add timestamps to measure time (§5) + measurement script + unit tests, results in §5.1 | ✅ **Completed** | No, only adds logs |
| **PR-2** | Add timer for workstation creation (`wait_for`) + revoke registry on failure (§4.3 changes 2, 3) | ✅ **Completed** (this batch) | Almost none, just makes error handling cleaner |
| **PR-3** | Add `_dlock` + duty officer election, all behind a switch (disabled by default, §4.1–4.4) | ✅ **Completed** (this batch) | **No** (switch off by default = today's behavior) |
| **PR-4** | bicep `maxReplicas>1` + turn on the switch + stress testing | ⏳ **Future work** | Yes, this is when multi-agent truly starts |

> **Progress & Branches (2026-07-04):**
> - **Main code changes are all on this branch `fix-redis`**: PR-1 instrumentation + PR-2 (timer + rollback) + PR-3 (`_dlock` + duty officer). With all `from_env` defaults, the behavior is **byte-for-byte identical to today** (switch off by default).
> - **The `measure-sandbox-timing` branch only retains the "measurement conclusions"**: the measurement harness (`measure_create_timeline.py`) + PR-1 unit tests + measurement report, for reproducibility; those tests, along with the formal unit tests for PR-2/PR-3, **will be added later together with end-to-end (e2e) testing** (§7).
> - Manual smoke verification done: with the switch on, `_dlock` correctly acquires/releases (TTL=60 / lock wait=45), the duty officer `SET NX EX` acquires leadership + Lua handover, the second contender gets `None` and skips, and with the switch off, `_dlock` doesn't touch Redis at all.
> - PR-4 is left for when we actually scale to multiple replicas in the future.

**Rule: `maxReplicas>1` and turning on `SANDBOX_DISTRIBUTED_LOCK` must be in the same PR (i.e., PR-4).** Adding agents without turning on the switch = risk of orphans; turning on the switch without adding agents = paying a tiny bit of Redis round trips for nothing. Don't separate them (Design doc §10).

`main.py` **doesn't need any changes** (`SandboxManager.from_env` already has the Redis client, `:130`).

---

## 7. Tests to Be Added (TODO — Not Written in This Batch, Declaration List)

> ⚠️ **Current status:** PR-1's `test_timing_instrumentation.py` (5 tests, passed) is on the `measure-sandbox-timing` branch; the PR-2/PR-3 code on this branch (`fix-redis`) has only undergone **manual smoke verification** (see §6 progress note), **formal unit tests haven't been written yet**. The plan is to **add them later together with end-to-end (e2e) testing**, the list below is what needs to be written.

**PR-2 (Timer + Rollback) — Add to `tests/test_get_or_create.py` (new file):**

- [ ] `_create_sandbox` hangs > `create_timeout` → `get_or_create` raises `asyncio.TimeoutError`, **both lock layers are released** (the same key can be entered again afterward).
- [ ] bootstrap fails (`_bootstrap` raises an error) → before raising, `sessions.delete(key)` is called once, **the reverse index `index` is not deleted** (left for the reaper).
- [ ] bootstrap succeeds → session key is written, `mark_bootstrapped` is called, client is returned (regression, ensure rollback doesn't break it).
- [ ] After `create_timeout` triggers, the ARM side might still complete → add a comment noting "orphan is handled by the reaper" (cannot be asserted in a unit test, just note it).

**PR-3 (`_dlock` + Duty Officer) — Add to `tests/test_dlock_reaper.py` (new file, using `fakeredis` or a stub):**

- [ ] Switch **off** / `redis is None` → `_dlock` lets through directly, **doesn't touch Redis at all**; `_try_become_reaper` returns `_NO_LEASE` (scans as usual).
- [ ] Switch **on**, normal → `_dlock` calls `lock()` with the correct `timeout=lock_ttl` / `blocking_timeout=lock_wait`, acquire→yield→release each once.
- [ ] Switch on, Redis `acquire` **raises an exception** → it's swallowed, lets through, **doesn't raise** (degrades, request doesn't fail).
- [ ] Switch on, `acquire` returns `False` (lock wait timeout) → logs a warning then lets through (degrades).
- [ ] Duty officer election: first `SET NX EX` succeeds and gets a token; second gets `None` → `continue` to skip this round.
- [ ] `_resign_reaper`: only releases using **its own token** via Lua (feed it someone else's token, assert it doesn't delete by mistake); `_NO_LEASE` / `redis is None` is a no-op.

**PR-4 (Multi-Replica Stress Test, Real Environment) — Reuse the `measure_create_timeline.py` approach, concurrent `--n` version:**

- [ ] N concurrent calls with the same routing key (across ≥2 replicas) → **only one sandbox is created**, the rest reuse it (count via `list_sandboxes` labels).
- [ ] Two replicas' reapers trigger at the same time → in a given round, **only one** replica logs `reaping orphan`, the other replica's `_try_become_reaper→None` skips.
- [ ] During stress testing, pull the Redis plug for 5 seconds → tool calls **don't fail** (degrade), and after recovery, the lock/leader election self-heals.

---

## 8. A Pitfall + Why We're Not Doing a "Seemingly Faster Optimization"

**Why did we add a `try/except` to revoke the registry entry in §4.3?** The existing code has a minor issue (unrelated to locks): if "boot and login" fails, but the registry has already been written (registry is written first, boot happens second), the next time the same customer comes, they will **reuse this broken, un-booted workstation**, and commands will keep failing. Furthermore, the reaper won't recycle it (because the registry still shows it as "alive"). Adding the `try/except` to revoke the registry on failure means the next attempt will create a new one, and the reaper can also clean up the broken workstation. It's a quick fix.

**Why not implement the "lock-free fast path" (the one from §1.7)?** The idea is: "using an existing workstation directly" doesn't need a lock; only creating a new one does. This would save the overhead of acquiring a lock every time. It sounds good, but in our case, it introduces two new bugs:

1. **Using a workstation that hasn't been booted**: The current code writes to the registry first, then boots. A lock-free fast path would find the workstation in the registry and use it immediately, potentially getting one that hasn't completed `az login` yet → failure.
2. **Reaper accidentally deletes it**: To fix the issue above, if we move the "write to registry" step to after booting, then during the boot process, the workstation isn't in the registry. The reaper would scan it and delete the **workstation that's still being created** as an orphan.

These two requirements conflict. To satisfy both, we'd need to introduce a "being created" state, which adds a whole new level of complexity. In a **low-frequency** scenario like human-machine interaction, the overhead of "acquiring a lock every time" is negligible. So, **it's not implemented by default**. If performance testing ever shows it's a bottleneck, we can add the guarded version from Design doc §5.2.

---

## Appendix: Summary of New Environment Variables

| Variable | Default | Plain English |
|---|---|---|
| `SANDBOX_DISTRIBUTED_LOCK` | `0` (off) | Master switch. Set to `1` together with `maxReplicas>1` when scaling to multiple agents |
| `SANDBOX_CREATE_TIMEOUT` | `30` | Timer for creating one workstation (seconds), give up if exceeded |
| `SANDBOX_LOCK_TTL` | `60` | How long the Redis sticky note lasts (seconds) |
| `SANDBOX_LOCK_WAIT` | `45` | Max wait time when unable to acquire the lock (seconds) |
| `SANDBOX_REAPER_LEASE` | `90` | Duty officer's term for recycling (seconds) |

> The second values have been calibrated according to §5.1 measurements (cold start measured ~4s / worst ~8s, with a 4-7× margin).

---

*Related:* [MCP-Horizontal-Scaling-Distributed-Lock-and-Reaper-Leader-Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md) (Principles) · [MCP-User-Isolation-and-Redis-Design.md](../MCP-user-isolation-comparison-and-redis-design.md) · [ACA-Sandbox-Migration-Plan.md](migrating-identity-aware-mcp-to-aca-sandboxes-plan.md)
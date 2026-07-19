# Distributed Primitives · Database · Orchestration: Three Galleries of the Same Concurrency Control

You asked a crucial question while reading [MCP-Horizontal Scaling-Distributed Lock and Reaper Leader Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md):

> *"This set of distributed locks, optimistic locks, reapers, and also master-slave, master-master… sounds just like PostgreSQL, right?"*

**Yes, and this is a cognitive leap worth highlighting.** These things **were neither invented by Redis nor by databases** — they are more fundamental theories of **concurrency control + distributed systems**. If it feels "like writing a database," it's because **databases are precisely the gallery where most people first encounter these concepts**.

This document lays out three galleries side by side:

- **General Primitives**: The concepts themselves (locks, replication, leader election, reconciliation…).
- **PostgreSQL**: What they look like in a relational database (you're likely already familiar).
- **This Project (Redis + ARM sandbox)**: What they look like in this architecture.

After reading, you'll be able to answer: which parts are universal truths that "look the same regardless of the system name"; where the three **truly diverge**; and why this project's skeleton is **half database, half Kubernetes**.

Prerequisites: [MCP-Horizontal Scaling-Distributed Lock and Reaper Leader Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md), [Redis-Basic Syntax Introduction.md](redis-basic-syntax-introduction.md).

---

## TL;DR

- **Locks, optimistic/pessimistic, master-slave/master-master, leader election** are **general concurrency control primitives**. PostgreSQL has near 1:1 counterparts — so "it looks like a database" is **genuinely true**.
- However, there are **three key points of divergence** that define why this project is **not just "CRUD running on a database"**:
  1. **Lock release model differs**: Redis relies on **TTL / lease**, Postgres advisory lock relies on **connection liveness**.
  2. **What is being protected is an "external, non-rollbackable side effect" (creating a sandbox), not a row of data** — ACID breaks at the database boundary, forcing a retreat to **best-effort locks + idempotency + post-hoc reconciliation (reaper)**. This is the world of **saga / eventual consistency**, not ACID.
  3. **Redis is being used here precisely as a coordination database**; you **could replace it with Postgres**; choosing Redis is "choosing the right tool," not a "capability gap."
- One-sentence categorization: **Locks / master-slave = the database side; reaper / "non-rollbackable sandbox creation" = the orchestration (orchestration / Kubernetes controller) side.**

---

## 0. Why "Everything Looks the Same": Primitives Precede Products

First, establish a mental model. The concepts below **existed in operating system, database, and distributed systems textbooks long before** Redis / Postgres / Kubernetes — they are just different **implementations**:

```
        General Primitives (Theory Layer)
   ┌────────────┬─────────────┬──────────────┐
   │  Mutex Lock │  Replication │ Leader Election│ ……
   └─────┬──────┴──────┬──────┴───────┬──────┘
         │             │              │
   ┌─────▼─────┐ ┌─────▼─────┐ ┌──────▼──────┐
   │PostgreSQL │ │  Redis    │ │ Kubernetes  │  (Respective Galleries/Implementations)
   │advisory   │ │ SET NX    │ │ Lease Object│
   │lock       │ │           │ │             │
   └───────────┘ └───────────┘ └─────────────┘
```

So when you manually implement a distributed lock with `SET NX` in Redis, you are essentially **re-implementing at the application layer** something databases have long provided. **"This is the same as a database" is not a coincidence, it's inevitable** — they are solving the same problem.

---

## 1. Big Comparison Table

| General Primitive | This Project (Redis + ARM) | PostgreSQL | Same? |
|---|---|---|---|
| **Named Mutex** | Distributed lock `SET lock:key NX PX` (§4.5) | `pg_try_advisory_lock(key)` | Concept same; **release mechanism differs** (see §3) |
| **Pessimistic Lock** (lock then act) | Lock entire "check—create—write" section | `SELECT ... FOR UPDATE` (row lock) | Same |
| **Optimistic Concurrency Control** | claim + poll (§4.7); or could use `WATCH/MULTI/EXEC` | `version` column / `SERIALIZABLE` isolation | Same, textbook level (see §2) |
| **Atomic "write only if not exists"** | `SET NX` | `INSERT ... ON CONFLICT DO NOTHING` | Same |
| **Check-then-act Race Condition** | Two requests both read miss, each creates a sandbox | lost update / phantom | **Same problem** (see §4) |
| **Replication · Master-Slave** | Redis primary + replica | streaming replication | Same, even the failover data loss window is the same (see §5) |
| **Replication · Master-Master** | active-active (Redis Enterprise CRDT only) | BDR / Citus / logical replication conflict resolution | **Equally hard, for the same reasons** |
| **Leader Election** | `SET reaper:leader NX EX` + lease renewal (§6.1) | `pg_try_advisory_lock` to elect single worker | Same; also same as K8s `Lease` |
| **Background Reclamation / GC** | reaper scans for orphan sandboxes | `pg_cron` to clean expired rows + autovacuum to clean dead tuples | Concept similar, **different objects** (see §6) |
| **Automatic Expiration TTL** | Redis native `EXPIRE` | **No native TTL**, must write own cleanup task | Redis has it natively, Postgres needs manual implementation |

Below, we pick the most worth-deep-diving ones and expand on them individually.

---

## 2. Pessimistic Lock vs Optimistic Lock — This is Pure Database Theory

You used the term "optimistic lock," and you used it **very accurately**. This pair of concepts is the core of concurrency control and has nothing to do with Redis:

- **Pessimistic Lock**: "I assume conflict will happen, so **lock first, then act**." Others must wait for me.
- **Optimistic Lock**: "I assume conflict usually won't happen, so **act first, check at commit time** if someone cut in line; if so, retry." No one waits; only retry on failure.

Here's what they look like in the three galleries:

**Pessimistic Lock:**
```sql
-- PostgreSQL: Lock this row; others' SELECT FOR UPDATE will block
BEGIN;
SELECT sandbox_id FROM sessions WHERE key = 'oid:sid:group' FOR UPDATE;
-- ……make decisions within the lock……
COMMIT;  -- Release
```
```python
# This Project: Lock the entire "check—create—write" section (conceptually equivalent to FOR UPDATE)
async with self._dlock(key):          # SET lock:key NX PX
    ...  # check—create—write
```

**Optimistic Lock:**
```sql
-- PostgreSQL: No lock; rely on version column to detect if someone else modified during write-back
UPDATE sessions SET sandbox_id = :new, version = version + 1
WHERE key = :k AND version = :expected;   -- 0 rows affected = someone else changed it first → retry
```
```
# Redis itself has a native optimistic lock: WATCH/MULTI/EXEC
WATCH session:key          # Watch it
val = GET session:key      # Read
MULTI                       # Begin transaction
SET session:key <new>       # Intended write
EXEC                        # If key was modified by someone else during this → entire transaction aborted, returns nil → retry
```

The **claim + poll** in §4.7 is a variant of the optimistic approach: instead of holding a lock for minutes, it does **one atomic claim** (`SET NX`, equivalent to `INSERT ... ON CONFLICT DO NOTHING`). The one who claims it builds; those who don't poll and wait for the result. **The advantage is not blocking other replicas for long; the disadvantage is a more complex state machine** (if the claimer crashes midway, timeout recovery is needed) — this is exactly the same kind of "need to write retry logic for optimistic locks" annoyance found in databases.

> Memory hook: **Pessimistic = `FOR UPDATE` / hold lock; Optimistic = version column / `WATCH` / `ON CONFLICT` + retry.** This project defaults to pessimistic (distributed lock); claim+poll is the optimistic alternative.

---

## 3. Named Lock: Distributed Lock ↔ Advisory Lock, and That Critical Release Difference

Redis's `SET lock:key NX` and Postgres's `pg_try_advisory_lock` are **the most similar pair** — both are locks **"not tied to any specific row of data, purely based on a name"**, specifically designed to serialize a critical section across multiple connections/clients (as opposed to `SELECT FOR UPDATE` which is tied to specific rows).

But the **release/fault cleanup model is fundamentally different**, and this difference shapes all design decisions for Redis locks:

| | Redis Distributed Lock | Postgres Advisory Lock |
|---|---|---|
| Who "prevents deadlock" | **TTL (`PX`)**: If the lock holder crashes, the lock disappears automatically upon expiry | **Connection liveness**: If the lock holder's connection drops, the server **automatically** releases it |
| Is TTL needed? | **Mandatory** (Redis doesn't track "is the lock holder's connection dead?") | **Not needed** (session-level lock follows the connection; txn-level lock follows `COMMIT`/`ROLLBACK`) |
| Is a token needed to prevent accidental deletion? | **Yes** (§4.3: after timeout, could delete someone else's lock) | No (server manages by owner) |
| Survives failover? | No (async replication loses locks, §4.8) | **No, and more thoroughly**: advisory locks are **in-memory, not written to WAL**; on failover **all** advisory locks evaporate instantly |

**Core sentence: Redis locks rely on "lease + time"; Postgres locks rely on "connection (session) lifecycle."** This is why Redis locks cannot do without `PX`, tokens, and watchdog lease renewal (§4.4), while Postgres locks need none of these — the server handles it for them.

> Interestingly: **Both are unsafe during failover** (see §5), because locks are **ephemeral state**. For absolute safety across failures, you need a **fencing token** (monotonically increasing number + resource-side validation), which goes beyond the capability of a single lock primitive.

---

## 4. Check-then-act Race Condition ↔ Database Isolation Levels

The scenario in §3 (the other document) where "two concurrent requests both read miss, so both go to create a sandbox" — this has a formal name in databases: **lost update / phantom read**, a classic problem that **isolation levels** are designed to solve.

Databases offer you three weapons:

| Approach | How to write in Postgres | Corresponding in this project |
|---|---|---|
| **Raise isolation level** | `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE` — on conflict, one side gets error `40001` to retry | (No direct equivalent; roughly "let the database detect the race for you") |
| **Unique constraint as fallback** | `INSERT ... ON CONFLICT DO NOTHING` — only one of two concurrent inserts succeeds | `SET session:key NX` (claim, §4.7) |
| **Explicit lock** | `SELECT ... FOR UPDATE` | Distributed lock (§4.5) |

**If the sandbox state were a row in Postgres**, you could even **use no locks at all**, just rely on `SERIALIZABLE` or a unique constraint to let the database block the race for you. On conflict, it would automatically `ROLLBACK`, clean and simple. **This project cannot do that** — the reason is §7② below: the critical action "create sandbox" is not in the database at all and cannot be rolled back. This is precisely where the divergence begins.

---

## 5. Replication: Master-Slave / Master-Master — Even the "Pitfalls" Are Identical

**Master-Slave (primary + read-only replica)**: Both use **asynchronous replication**, both have the same failover data loss window:

```
Redis:    primary returns OK → hasn't replicated to replica yet → primary crashes → replica takes over (missing the last few writes)
Postgres: COMMIT returns   → WAL hasn't streamed to standby yet → primary crashes → standby takes over (missing the last few writes)
```

**The solution is the same, and the trade-off is the same**: switch to **synchronous replication** (Redis `WAIT` / Postgres `synchronous_commit` + `synchronous_standby_names` quorum), trading **latency** for **no data loss** — there's no free lunch; both choose a point on this curve.

**Master-Master (multi-master)**: Both are **difficult and rarely used, and for the exact same reasons** — two masters writing to the same key simultaneously creates a **write conflict**, requiring a resolution mechanism (last-write-wins / CRDT / application-level resolution):

| | Redis | PostgreSQL |
|---|---|---|
| Native support | ❌ Not supported by default; active-active relies on **Redis Enterprise CRDT** | ❌ Core doesn't support it; relies on **BDR / Citus / logical replication** conflict handling |
| Why is it hard | Concurrent write conflicts cannot be automatically "correctly" merged; only a compromise strategy can be chosen | Same as left |

> Remember: **"Master-slave" is easy and cheap everywhere; "master-master" is difficult and expensive everywhere** — this is not a shortcoming of any particular product, but the **inherent difficulty of distributed writes**.

---

## 6. Reclamation: What Does the Reaper Actually Resemble — From Database to Orchestration

The reaper is the concept **least like a pure database** among the three. Breaking it down is interesting:

- **Like `pg_cron` scheduled cleanup**: The reaper scans every 300s to delete expired items, roughly equivalent to `DELETE FROM sessions WHERE expires_at < now()` run on a timer. **Note**: Postgres has **no native TTL**; you must write your own expiration cleanup; Redis's `EXPIRE` is native — so the 30-minute sliding window in the routing table is a single `SET ... EX` in Redis, but would require a cron job in Postgres.
- **Like autovacuum (partially)**: Postgres's autovacuum reclaims dead tuples generated by MVCC in the background. It's also a "background GC" and is conceptually similar to the reaper — but it cleans **garbage internal to the database**.
- **But what it truly resembles is the Kubernetes controller's reconcile loop** ← Key point.

What the reaper does, in essence, is: **"Compare the actual state of the external world (sandboxes that really exist in ARM) with the desired state recorded in my source of truth (Redis reverse index); delete any extras."** This is precisely the **level-triggered reconciliation** of a K8s controller:

| Kubernetes controller | This project's reaper |
|---|---|
| desired state (spec in etcd) | source of truth (Redis reverse index `_index`) |
| actual state (real pods in the cluster) | actual (ARM `list_sandboxes()`) |
| reconcile: delete extras, add missing, idempotent, runs repeatedly | reap_orphans: actual > desired → delete, idempotent, runs every 300s |
| leader election uses `coordination.k8s.io/Lease` | reaper leader uses `SET reaper:leader NX EX` (§6.1) |

**The last line is particularly elegant**: The K8s `Lease` object contains `holderIdentity` + `renewTime` + `leaseDurationSeconds` — **exactly the same thing as your `SET reaper:leader token NX EX` + lease renewal**. Leader election is **the same primitive in three different skins** across Redis, Postgres advisory lock, and K8s Lease.

> So the reaper's positioning is: **"A database won't delete an orphan VM for you, because that's not its data."** The reaper stands at the boundary between database and orchestration, leaning towards the orchestration side.

---

## 7. Three Key Points of Divergence: Why It's "Not Just a Database"

Everything above was about "sameness." Now, **three real differences** — and these three points are precisely the **root cause** for the existence of this entire set of lock + reaper + idempotency:

### ① Lock Release Model: Lease vs Connection
Already expanded in §3. **Redis relies on TTL/time; Postgres relies on connection lifecycle.** This dictates that Redis locks must carry a token + PX + watchdog, while Postgres locks delegate all of this to the server.

### ② What is Protected is an "External, Non-rollbackable Side Effect," Not a Row of Data ← The Deepest Point

Database transactions can `ROLLBACK` because they protect **data they manage themselves**. But the core action within this project's critical section — `create_sandbox()` — is **creating a real VM in ARM, about which the database knows nothing and over which it has no rollback capability**. You cannot "roll back a sandbox that has been running for 3 minutes."

```
Database world (ACID):        BEGIN → modify data → conflict? → ROLLBACK, as if nothing happened
This project's world (saga):  Create VM → conflict? → VM already exists, can't go back → only "compensate": delete it later (reaper)
```

**Precisely because the side effect is outside the database boundary, ACID breaks down**, and you are forced into another methodology:

- **Best-effort locks** (block what you can; §4.8 already stated it's just an "efficiency lock");
- **Idempotency** (`_ensure_volume` swallows `409 AlreadyExists`, repeated creation doesn't error);
- **Post-hoc reconciliation / compensation (reaper)** (orphans that slip through the net are found and deleted later).

The formal name for this set of techniques is the **saga pattern / compensating transaction / eventual consistency**. **In one sentence: Databases give you ACID, but ACID only covers their own data; stepping outside the database boundary to touch external resources means entering the world of sagas.**

### ③ Redis Here "Is Being Used Precisely as a Coordination Database" — It Could Be Replaced with Postgres

Here's an interesting twist: Redis in this architecture **plays the role of a database** (storing routing tables, acting as a lock, providing TTL). So you **could absolutely replace it with Postgres**:

| What this project does with Redis | How to do it with Postgres |
|---|---|
| Distributed lock `SET NX` | `pg_try_advisory_lock` |
| Routing table + 30min TTL | A `sessions` table + `expires_at` column |
| Reaper scans for expiration | `pg_cron` scheduled `DELETE WHERE expires_at < now()` |
| Reaper leader election | `pg_try_advisory_lock` to elect a single worker |

**Both would work.** Choosing Redis is not because Postgres can't do it, but because Redis is **born for "short-lived, needs TTL, needs speed, single atomic `SET NX`" coordination state**; Postgres is for "persistent, relational, needs transactions" data. **This is "choosing the right tool," not a "capability gap."** If this state were meant to live alongside business data in Postgres anyway, using advisory locks would actually save you one component.

---

## 8. Categorization: Which Parts Belong to "the Database Side" and Which to "the Orchestration Side"

Flatten all concepts into a single ownership table to wrap up the entire document:

| Concept | Belongs to Which Side | Mental Anchor |
|---|---|---|
| Pessimistic Lock / Optimistic Lock | **Database** | `FOR UPDATE` / version column |
| Distributed Lock | **Database** (coordination layer) | advisory lock |
| Check-then-act Race Condition, Isolation Levels | **Database** | lost update / SERIALIZABLE |
| Master-Slave / Master-Master Replication, Failover Window | **Database** (also general distributed systems) | streaming replication |
| TTL / Expiration Reclamation | Fence-sitting (Redis native / Postgres manual) | `EXPIRE` vs `pg_cron` |
| **Reaper Orphan Reconciliation** | **Orchestration** | K8s reconcile loop |
| **Leader Election** | Fence-sitting (another use of locks) | Redis Lease = K8s Lease |
| **"Non-rollbackable Sandbox Creation" → Saga / Compensation** | **Orchestration** | Compensating transaction, eventual consistency |

> **The true shape of this project: half is "concurrency control running on Redis" (the database side), half is "coordinating external resources that the database cannot manage" (the orchestration side).** Your familiar Postgres knowledge can be **directly transferred** to the left half; the right half (reaper, idempotency, saga) needs to be understood with the **Kubernetes controller / cloud orchestration** mental model.

---

## 9. One-Sentence Summary

> What you see — **locks, optimistic/pessimistic, master-slave/master-master, leader election** — are **general concurrency control and distributed system primitives**. Databases are just their most famous gallery — so "it looks like PostgreSQL" is **genuinely true**, and you can confidently transfer your database intuition.
> But the real skeleton of this project is **"using a data store (Redis) to coordinate a set of external resources (ARM sandboxes) that the database cannot manage"** — once side effects cross the database boundary, **ACID breaks down, saga takes over**, and thus we have the reaper, idempotency, and best-effort locks. **This half is more like a Kubernetes controller than CRUD running on Postgres.**

---

*Related documents:* [MCP-Horizontal Scaling-Distributed Lock and Reaper Leader Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md) · [Redis-Basic Syntax Introduction.md](redis-basic-syntax-introduction.md) · [MCP-User Isolation and Redis Design.md](../MCP-user-isolation-comparison-and-redis-design.md) · [ACA-Sandbox-Migration-Plan.md](migrating-identity-aware-mcp-to-aca-sandboxes-plan.md)
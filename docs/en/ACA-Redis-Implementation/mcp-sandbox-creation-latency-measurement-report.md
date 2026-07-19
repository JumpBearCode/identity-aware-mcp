# MCP Sandbox Creation Latency: Measurement Report (Single Instance / Current Deployment)

Related documents: [MCP-Distributed Lock and Reaper Leader Election-Implementation Plan.md](distributed-lock-and-reaper-leader-election-implementation-plan.md) (§5.1 is a condensed version of this report), [MCP-Horizontal Scaling-Distributed Lock and Reaper Leader Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md) (principles).

> **In a nutshell:** Creating a sandbox from scratch takes a **median of ~4 seconds, worst case ~8.3 seconds**, and a cache hit takes **~0.08 seconds**. Cold start is **seconds, not minutes**, confirming the assessment in §10.1 of the principles document; accordingly, the three distributed lock timeouts are set to `create_timeout=30 / lock_ttl=60 / lock_wait=45`, and it is confirmed that **a watchdog is unnecessary**.

The code instrumentation and measurement scripts are not on this branch; they are on the **`measure-sandbox-timing`** branch (`src/mcp-server/tests/`). This report only documents the methodology and conclusions.

---

## 1. Why Measure

The values for the three distributed lock timeouts (`create_timeout` / `lock_ttl` / `lock_wait`) in the implementation plan depend entirely on **how long it actually takes to create a sandbox**. Without actual measurements, you can only guess. So before writing the lock, this step is necessary: add timestamps to the `_create_sandbox` / `_bootstrap` / reuse paths, run it for real, get the distribution, and then set the parameters. This is the highest-ROI first step in the entire roadmap (as also outlined in §10 of the principles document).

## 2. How It Was Measured (Method)

**The creation chain was split into five segments A–E for instrumentation** (`sandbox_manager.py`):

| Segment | Location | Meaning |
|---|---|---|
| A `disk` | `_resolve_disk` | Resolve disk/image (if a Ready image is hit, this is just a `list_disk_images` call) |
| B `vol` | `_workspace_volumes` | Create/confirm blob volume |
| C `vm` | `begin_create_sandbox` + `poller.result()` | **Create the microVM itself** |
| D `autodel` | `_apply_idle_autodelete` | Set auto-delete policy |
| E `bootstrap` | `_bootstrap`'s `exec("bash /opt/bootstrap.sh")` | **FIC `az login` + restore profile** |
| hit | `ensure_running` on the reuse path | Hit an existing sandbox |

**"Fake users", no OAuth/OBO.** This is the key reason this measurement could be done at low cost:

> OBO only exists at the MCP **front door** (`main.py`: validate user JWT → OBO → Graph check group membership); its output is just a `user_oid` string. The sandbox lifecycle we want to measure (`get_or_create → _create_sandbox → _bootstrap`) authenticates to ACA using the **application's own** `DefaultAzureCredential`, **completely unrelated to the user token**.
>
> Therefore, "simulating different users" = directly constructing different `SessionCtx(oid, session, group)` objects and feeding them into `get_or_create`, **no real login required**. The measurement script uses `probe-<run>-<i>` to create N different routing keys, equivalent to N users each hitting the system for the first time.

**Bypassing Redis.** The production Redis is an ACA internal FQDN (`redis://…internal…:6379`), unreachable from the local machine. The script uses `InMemoryBackend` as a substitute for the session/profile/index cache — this does not affect creation latency (creation goes through ARM, not Redis), and it also allows a second call with the same key to actually hit the cache, enabling measurement of the hit path.

**Creating real resources, cleaning up each time.** The script creates real sandboxes directly on the currently deployed `dataops-aca-diagnose` / `-action` groups, using the same deployment image `mcp-sandbox:latest`. Before running, the script snapshots existing sandboxes; after running, it diffs to find all newly created ones and deletes them, and checks for leaks — **all 10 sandboxes in this run were deleted cleanly, 0 leaks**.

## 3. Environment

| Item | Value |
|---|---|
| Subscription / RG / Region | `ee5f77a1…` / `dataops-aca-rg` / `westus2` |
| Replicas | **1** (`maxReplicas=1`, currently single instance) |
| Image | `dataopsacaacrvyq3trlvkn4za.azurecr.io/mcp-sandbox:latest`, both groups already have a **Ready** disk image → **creation uses reuse, no build triggered** |
| Sample | diagnose 5 + action 5 = **10 total**, serial; each sandbox is reused once after creation |
| Date | 2026-07-04 |

## 4. Raw Data (one row per "user", seconds)

**diagnose (n=5):**

| User | wall (end-to-end) | vm (create VM) | bootstrap | hit |
|---|---|---|---|---|
| 0 | 4.08 | 1.19 | 2.59 | 0.099 |
| 1 | 3.68 | 0.92 | 2.64 | 0.080 |
| 2 | 5.28 | 2.35 | 2.72 | 0.080 |
| 3 | 3.68 | 0.95 | 2.63 | 0.080 |
| 4 | 3.93 | 1.03 | 2.69 | 0.082 |

**action (n=5):**

| User | wall (end-to-end) | vm (create VM) | bootstrap | hit |
|---|---|---|---|---|
| 0 | 7.18 | 3.84 | 3.02 | 0.080 |
| 1 | 8.34 | 4.70 | 3.42 | 0.079 |
| 2 | 3.69 | 0.92 | 2.66 | 0.086 |
| 3 | 3.70 | 0.97 | 2.55 | 0.087 |
| 4 | 4.12 | 1.24 | 2.77 | 0.080 |

> A `disk` / B `vol` / D `autodel` are not in the table above because they are very small: A and B only incur a cost (~0.09s for list / volume confirmation) **on the first sandbox of each process**, after which the in-process cache makes them ≈ 0; D is stable at 0.11–0.22s. There is also one data point for the "first sandbox in a fresh process" (cold process + first list + first volume): disk 0.090 / vol 0.098 / vm 3.086 / autodel 0.134 / bootstrap 2.861 / wall 6.27.

## 5. Segment Statistics (10 sandboxes combined, seconds)

| Segment | Median | Min | Worst (≈max) |
|---|---|---|---|
| A `disk` | ~0 (cached) | 0 | 0.09 |
| B `vol` | ~0 (cached) | 0 | 0.09 |
| **C `vm`** ⭐ | **1.11** | 0.92 | **4.70** |
| D `autodel` | 0.13 | 0.11 | 0.22 |
| **E `bootstrap`** ⭐ | **2.68** | 2.55 | 3.42 |
| **wall (end-to-end)** | **4.00** | 3.68 | **8.34** |
| hit (reuse) | 0.08 | 0.079 | 0.10 |

## 6. Conclusions

1. **Cold start is seconds, not minutes.** End-to-end median is ~4s, worst case ~8.3s. **The prediction in §10.1 of the principles document is confirmed** — the "~5min" placeholder used in earlier documents was just an order-of-magnitude estimate for reasoning about race conditions; the actual speed is much faster.
2. **Bootstrap (E, ~2.7s) is heavier and more stable than VM creation (C, ~1s).** The real variability is in C (microVM scheduling, 0.9→4.7s jitter); E is very stable (2.5–3.4s).
3. **A/B steady state ≈ 0, no image build encountered.** The image was already Ready → reuse, only paying the ~0.09s list/volume confirmation on the first sandbox per process. The "30-second timer colliding with a minute-long image build" risk described in §8 of the implementation plan **only triggers on a brand-new group / new image tag**; steady-state creation always uses reuse, so `create_timeout` is unaffected by it.
4. **Hit path is ~0.08s.** This confirms that "the vast majority of tool calls are very fast, and locking the hot path hardly matters," and also confirms that the "lock-free fast path" optimization mentioned in the implementation plan is indeed non-essential.

## 7. Parameters Set Accordingly

| Parameter | Value | Rationale |
|---|---|---|
| `create_timeout` | **30s** | Worst-case "create VM segment" (A+B+C+D) ~5s → ~6× margin |
| `lock_ttl` | **60s** | Worst-case critical section (create + boot) ~8.3s → ~7× margin; if the lock holder crashes, others wait at most 60s |
| `lock_wait` (blocking_timeout) | **45s** | ≥ one worst-case "create + boot", allowing the waiter to potentially reuse; also satisfies `> create_timeout` |
| **watchdog (lease renewal)** | **Not implemented** | Critical section ~8s, far from the 60s TTL; lease renewal is completely unnecessary (§4.1 of implementation plan / §10.2 of principles document) |

The ordering satisfies the iron rule (§4.9 of principles document): `actual creation time < create_timeout(30) < lock_wait(45)`, and `lock_ttl(60) > create_timeout + bootstrap`.

## 8. Limitations / Caveats

- **This data is from a single instance, serial execution, with a pre-warmed image — it is an optimistic lower bound.** With multiple replicas contending for ARM, or landing on a cold zone, the tail of C (create VM) will be longer. The 4–7× margin is intended to cover this.
- **Image build time was not measured** (because a Ready image already existed, so no build was triggered). Builds are one-time, minute-level, and amortizable; it is recommended to maintain the current practice of "pre-building the image during deployment" so that `_ensure_disk_image` always takes the reuse path.
- **Final parameter tuning is deferred to PR-4.** When load testing with multiple replicas online, re-run the same script to observe the distribution of C under concurrency before finalizing the values.

## 9. Reproduction

The code (instrumentation + script) is on the `measure-sandbox-timing` branch:

```bash
# Unit tests (pure mock, no Azure interaction, no cost)
pytest src/mcp-server/tests/test_timing_instrumentation.py

# Real measurement (requires az login + install azure-containerapps-sandbox / azure-identity; creates real sandboxes and cleans up automatically)
python src/mcp-server/tests/measure_create_timeline.py --n 5 --group diagnose
python src/mcp-server/tests/measure_create_timeline.py --n 5 --group action
```

No script is needed in production — the instrumentation is already in `sandbox_manager.py`. Just read the cloud logs:

```bash
az containerapp logs show -n dataops-aca-mcp -g dataops-aca-rg --tail 500 | grep 'timing phase'
```

---

*Related:* [MCP-Distributed Lock and Reaper Leader Election-Implementation Plan.md](distributed-lock-and-reaper-leader-election-implementation-plan.md) · [MCP-Horizontal Scaling-Distributed Lock and Reaper Leader Election.md](mcp-server-horizontal-scaling-distributed-lock-and-reaper-leader-election.md) · [ACA-Sandbox-Migration Plan.md](migrating-identity-aware-mcp-to-aca-sandboxes-plan.md)
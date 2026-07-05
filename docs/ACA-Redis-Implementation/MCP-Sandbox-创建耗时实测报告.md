# MCP Sandbox 创建耗时:实测报告(单机 / 当前部署)

配套文档:[MCP-分布式锁与Reaper选主-实现方案.md](MCP-分布式锁与Reaper选主-实现方案.md)(§5.1 是本报告的浓缩版)、[MCP-水平扩展-分布式锁与Reaper选主.md](MCP-水平扩展-分布式锁与Reaper选主.md)(原理篇)。

> **一句话:** 建一台 sandbox 从头到尾**中位 ~4 秒、最坏 ~8.3 秒**,复用命中 **~0.08 秒**。冷启动是**秒级不是分钟级**,坐实了原理篇 §10.1 的判断;据此把分布式锁的三个超时定为 `create_timeout=30 / lock_ttl=60 / lock_wait=45`,并确认 **watchdog 不用做**。

代码打点与测量脚本不在本分支,在 **`measure-sandbox-timing`** 分支(`src/mcp-server/tests/`)。本报告只记录方法与结论。

---

## 1. 为什么测

实现方案里分布式锁的三个超时(`create_timeout` / `lock_ttl` / `lock_wait`)该填多少,**完全取决于"建一台 sandbox 实际多久"**。没有实测就只能瞎填。所以在写锁之前,先做这一步:给 `_create_sandbox` / `_bootstrap` / 复用路径打时间戳,真跑一遍,拿到分布,再定参数。这是整个 roadmap 里性价比最高的第一步(原理篇 §10 也是这么排的)。

## 2. 怎么测的(方法)

**把创建链拆成 A–E 五段打点**(`sandbox_manager.py`):

| 段 | 位置 | 含义 |
|---|---|---|
| A `disk` | `_resolve_disk` | 解析磁盘/镜像(命中已 Ready 镜像则只是一次 `list_disk_images`) |
| B `vol` | `_workspace_volumes` | 建/确认 blob volume |
| C `vm` | `begin_create_sandbox` + `poller.result()` | **建 microVM 本体** |
| D `autodel` | `_apply_idle_autodelete` | 设 auto-delete 策略 |
| E `bootstrap` | `_bootstrap` 的 `exec("bash /opt/bootstrap.sh")` | **FIC `az login` + 恢复 profile** |
| hit | 复用分支的 `ensure_running` | 命中已有 sandbox |

**"假用户",不走 OAuth/OBO。** 这是关键,也是本次能低成本实测的原因:

> OBO 只存在于 MCP **前门**(`main.py`:验用户 JWT → OBO → Graph 查组成员),它产出的只是一个 `user_oid` 字符串。而我们要测的 sandbox 生命周期(`get_or_create → _create_sandbox → _bootstrap`)是用**应用自己的** `DefaultAzureCredential` 去认证 ACA 的,**跟用户 token 完全无关**。
>
> 所以"模拟不同用户"= 直接构造不同的 `SessionCtx(oid, session, group)` 喂进 `get_or_create`,**不需要任何真实登录**。测量脚本用 `probe-<run>-<i>` 造 N 个不同 routing key,等价于 N 个用户各自首次触碰。

**绕开 Redis。** 线上 Redis 是 ACA 内网 FQDN(`redis://…internal…:6379`),本机连不上。脚本用 `InMemoryBackend` 顶替 session/profile/index 缓存 —— 不影响创建耗时(创建走 ARM,不走 Redis),还顺便让"第二次同 key 调用"真命中,能测到 hit 路径。

**打真资源、每次清理。** 直接对**当前部署的** `dataops-aca-diagnose` / `-action` 两组建真 sandbox;用部署同款镜像 `mcp-sandbox:latest`。脚本跑前快照已有 sandbox,跑后 diff 出本次新增的全部删除,并复查泄漏 —— **本次 10 台全部删净,0 泄漏**。

## 3. 环境

| 项 | 值 |
|---|---|
| 订阅 / RG / 区域 | `ee5f77a1…` / `dataops-aca-rg` / `westus2` |
| 副本数 | **1**(`maxReplicas=1`,当前单实例) |
| 镜像 | `dataopsacaacrvyq3trlvkn4za.azurecr.io/mcp-sandbox:latest`,两组均已有 **Ready** 磁盘镜像 → **create 走复用,未触发 build** |
| 样本 | diagnose 5 + action 5 = **10 台**,串行;每台建完再复用一次 |
| 日期 | 2026-07-04 |

## 4. 原始数据(每个"用户"一行,秒)

**diagnose(n=5):**

| 用户 | wall(端到端) | vm(建VM) | bootstrap | hit |
|---|---|---|---|---|
| 0 | 4.08 | 1.19 | 2.59 | 0.099 |
| 1 | 3.68 | 0.92 | 2.64 | 0.080 |
| 2 | 5.28 | 2.35 | 2.72 | 0.080 |
| 3 | 3.68 | 0.95 | 2.63 | 0.080 |
| 4 | 3.93 | 1.03 | 2.69 | 0.082 |

**action(n=5):**

| 用户 | wall(端到端) | vm(建VM) | bootstrap | hit |
|---|---|---|---|---|
| 0 | 7.18 | 3.84 | 3.02 | 0.080 |
| 1 | 8.34 | 4.70 | 3.42 | 0.079 |
| 2 | 3.69 | 0.92 | 2.66 | 0.086 |
| 3 | 3.70 | 0.97 | 2.55 | 0.087 |
| 4 | 4.12 | 1.24 | 2.77 | 0.080 |

> A `disk` / B `vol` / D `autodel` 没进上表,因为很小:A、B 只有**每个进程第一次**付一次(~0.09s 的 list / volume 确认),之后进程内缓存 ≈ 0;D 稳定在 0.11–0.22s。另有一个"全新进程首台"数据点(冷进程 + 首次 list + 首次 volume):disk 0.090 / vol 0.098 / vm 3.086 / autodel 0.134 / bootstrap 2.861 / wall 6.27。

## 5. 分段统计(10 台合并,秒)

| 段 | 中位数 | 最小 | 最坏(≈max) |
|---|---|---|---|
| A `disk` | ~0(缓存) | 0 | 0.09 |
| B `vol` | ~0(缓存) | 0 | 0.09 |
| **C `vm`** ⭐ | **1.11** | 0.92 | **4.70** |
| D `autodel` | 0.13 | 0.11 | 0.22 |
| **E `bootstrap`** ⭐ | **2.68** | 2.55 | 3.42 |
| **wall(端到端)** | **4.00** | 3.68 | **8.34** |
| hit(复用) | 0.08 | 0.079 | 0.10 |

## 6. 结论

1. **冷启动是秒级,不是分钟级。** 端到端中位 ~4s、最坏 ~8.3s。**原理篇 §10.1 的预判被坐实** —— 之前文档里用的 "~5min" 只是推导竞态用的占位量级,实际快得多。
2. **bootstrap(E,~2.7s)比建 VM(C,~1s)更重、也更稳。** 真正有波动的是 C(microVM 调度,0.9→4.7s 抖动);E 很稳(2.5–3.4s)。
3. **A/B 稳态 ≈ 0,没碰到镜像 build。** 镜像已 Ready → 复用,只有每进程第一次付 ~0.09s 的 list/volume 确认。实现方案 §8 那个"30 秒闹钟撞上分钟级镜像 build"的雷,**只在全新组 / 新镜像 tag 时才会响**;稳态创建永远走复用,`create_timeout` 不受它影响。
4. **hit 路径 ~0.08s。** 印证"绝大多数 tool call 都很快、锁不锁热路径几乎无所谓",也说明实现方案里那个"无锁快路径"优化确实非必需。

## 7. 据此定的参数

| 参数 | 取值 | 依据 |
|---|---|---|
| `create_timeout` | **30s** | 最坏"建 VM 段"(A+B+C+D)~5s → 约 6× 余量 |
| `lock_ttl` | **60s** | 最坏临界区(建+开机)~8.3s → 约 7× 余量;持锁者崩了别人最多空等 60s |
| `lock_wait`(blocking_timeout) | **45s** | ≥ 一次"建+开机"最坏,让等锁者能等到复用;且满足 `> create_timeout` |
| **watchdog(续租)** | **不做** | 临界区 ~8s,离 60s 的 TTL 远得很,续租毫无必要(实现方案 §4.1 / 原理篇 §10.2) |

排序满足铁律(原理篇 §4.9):`建实际耗时 < create_timeout(30) < lock_wait(45)`,且 `lock_ttl(60) > create_timeout + bootstrap`。

## 8. 局限 / 注意

- **这是单机、串行、镜像已预热下的数,是乐观下限。** 多副本并发抢 ARM、或落到冷区,C(建 VM)的尾巴会更长。留 4–7× 余量就是为这个。
- **未测镜像 build 的真实耗时**(因为已有 Ready 镜像,没触发)。build 是一次性、分钟级、可摊销;建议保持"部署期预建镜像"的现状,让 `_ensure_disk_image` 永远走复用。
- **参数定稿留到 PR-4。** 多副本上线压测时用同一脚本复测,把 C 在并发下的分布看清再最终敲定。

## 9. 复现

代码(打点 + 脚本)在 `measure-sandbox-timing` 分支:

```bash
# 单元测试(纯 mock,不碰 Azure、不花钱)
pytest src/mcp-server/tests/test_timing_instrumentation.py

# 真实测量(需 az login + 装 azure-containerapps-sandbox / azure-identity;会建真 sandbox 并自动清理)
python src/mcp-server/tests/measure_create_timeline.py --n 5 --group diagnose
python src/mcp-server/tests/measure_create_timeline.py --n 5 --group action
```

生产上无需脚本 —— 打点已进 `sandbox_manager.py`,直接读云日志即可:

```bash
az containerapp logs show -n dataops-aca-mcp -g dataops-aca-rg --tail 500 | grep 'timing phase'
```

---

*关联:* [MCP-分布式锁与Reaper选主-实现方案.md](MCP-分布式锁与Reaper选主-实现方案.md) · [MCP-水平扩展-分布式锁与Reaper选主.md](MCP-水平扩展-分布式锁与Reaper选主.md) · [ACA-Sandbox-迁移方案.md](ACA-Sandbox-迁移方案.md)

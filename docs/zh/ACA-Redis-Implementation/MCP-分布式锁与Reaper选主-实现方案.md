# 分布式锁与 Reaper 选主:落地实现方案(大白话版)

配套设计篇:[MCP-水平扩展-分布式锁与Reaper选主.md](MCP-水平扩展-分布式锁与Reaper选主.md)。
那篇讲"为什么、原理";**这篇只讲"具体改哪几行代码、参数怎么定、怎么分步上线"**,并且尽量说人话。

涉及文件:`src/mcp-server/sandbox_manager.py`、`cache.py`、`main.py`、`provisioning/aca/modules/mcp-app.bicep`(行号锚在当前 commit `0b0706d`)。

---

## 0. 一句话:这份文档要干嘛

以后如果把服务从"1 个实例"扩到"好几个实例",会冒出两个小毛病。我们提前把**两段代码**改好,把毛病堵上。**改动很小,而且默认关着,不影响现在。**

---

## 1. 先把几个词讲清楚(不然后面看不懂)

全程用一个比喻:**你在开一个客服中心。**

### 1.1 副本(replica)= 客服坐席

现在只有 **1 个坐席**(1 个实例)接所有电话。将来电话多了,你会雇 **N 个坐席**(N 个实例)一起接——他们干的活一模一样,只是人多能接更多电话。这就是"**水平扩展**",在配置里就是 `maxReplicas` 从 1 调到 3(`mcp-app.bicep:121`)。

⚠️ **关键:同一个客户,这通电话可能接到坐席 A,下一通可能接到坐席 B。** 谁接是随机分配的(负载均衡)。这是后面所有麻烦的根源。

### 1.2 sandbox = 给客户专门搭的工作台

客户第一次打进来,坐席要**给他搭一个专属工作台**(建 sandbox)。这个动作**慢**(几秒~几十秒)、而且**搭了就收不回**。搭好之后,这个客户后续的活都在这张台子上干。

- **搭台子** = `get_or_create` 里的 `_create_sandbox`(`sandbox_manager.py:244`),慢。
- **台子搭好后要"开机登录"**一次(`az login`)= `_bootstrap`(`:358`)。
- 客户是谁 → 用哪张台子,记在一个**共享的登记本**里(Redis)。

### 1.3 routing key = 客户的身份牌

登记本上,每张台子挂在一个**身份牌**下面。身份牌 = `(用户oid, session, group)` 这三样拼起来。

- 同一个身份牌 → 同一张台子(复用)。
- 换个 group(diagnose / action)→ 换个身份牌 → 换张台子(这是对的,读写要分开)。

### 1.4 要防的那个毛病:同一个客户被搭了两张台子

**麻烦场景**(设计篇 §3.3):客户几乎同时打了两通电话(比如 AI 一轮里并行发了两个 `diagnose_bash`),一通给坐席 A、一通给坐席 B。

1. A 查登记本:这客户没台子 → 我来搭一张。
2. B 同时查登记本:也没台子(A 还没搭完写上去)→ 我也搭一张。
3. 结果:**搭了两张台子,只有一张被登记,另一张没人认领 = 孤儿(orphan)**,白花钱。

我们要防的就是这个:**同一个身份牌,同一时刻只许一个人在"搭台子"。**

### 1.5 两种"同时",要两把不同的锁

"防止同时搭两张台子",本质是加一把**锁**:谁要搭,先拿锁,拿到才能搭,搭完放锁。但"同时"分两种:

| 哪种"同时" | 谁挡得住 | 打个比方 |
|---|---|---|
| **同一个坐席**内,两个电话并发(同一进程内两个协程) | `asyncio.Lock`(代码里现有的) | 坐席 A 自己心里记着"这客户我在弄了" |
| **不同坐席之间**(A 和 B,不同进程) | **Redis 锁**(要新加的) | 在**大家都能看的白板**上贴张"我在弄了,别动" |

**为什么现有的 `asyncio.Lock` 不够?** 因为它只是坐席 A **自己脑子里**的备忘,坐席 B 根本看不见。A、B 各想各的,照样一人搭一张。所以跨坐席必须靠一个**公共的**东西——Redis(白板),这就是"分布式锁"。

> ⚠️ 注意:今天只有 1 个坐席,所有电话都在这一个坐席里,`asyncio.Lock` 已经完全够用,**一张孤儿都不会有**。Redis 锁是"等你雇了第 2 个坐席"才需要的。

### 1.6 那为什么不干脆只用 Redis 锁,把 `asyncio.Lock` 扔了?

因为**同坐席内的并发,用 `asyncio.Lock` 挡是免费的(纯内存);用 Redis 挡要走一趟网络**。设想坐席 A 里同一个客户并发来了 10 通电话:

- **只用 Redis 锁**:10 个协程**全都**跑去 Redis 抢锁,9 个在那儿反复网络往返干等。
- **两把锁一起(本方案)**:10 个先在本地 `asyncio.Lock` 排队(免费),**只有排头 1 个**去 Redis 抢锁挡别的坐席。Redis 的压力少一个数量级。

还有更重要的一点——**Redis 万一挂了**:`asyncio.Lock` 还在,同坐席内的正确性不丢;要是只有 Redis 锁,它一挂就彻底没锁了。所以叫"**两道防线**":便宜可靠的那道(asyncio)先兜大头,贵且可能坏的那道(Redis)只补它够不着的"跨坐席"。

**结论:两把锁分工——`asyncio.Lock` 管"同坐席",Redis 锁管"跨坐席"。前者免费又可靠,没理由扔。**

### 1.7 快路径 / 慢路径 / lock-free(这组词最容易懵)

"**快路径 / 慢路径**"是编程里的通用说法:**大多数情况走一条又快又简单的路(快路径),只有少数情况走又慢又复杂的路(慢路径)。**

放到这里:

- **慢路径** = 客户第一次来,**要新搭台子**。慢,且必须加锁(防搭两张)。
- **快路径** = 台子早搭好了,登记本里有,**直接查出来用**。快。

**重点:绝大多数电话都是快路径。** 同一个 session 里第 2、3、4… 次调用,台子第一次就搭好了,后面全是"查登记本 → 有 → 直接用"。**只有每个 session 的第一次**才走慢路径搭台子。

"**lock-free 快路径**"(无锁快路径)= **"直接用现成台子"这件事根本不用抢锁**。因为两个人同时用同一张现成台子,没任何问题;锁只是为了防"同时**搭**两张",不是防"同时**用**一张"。

后面 §8 会讲:这个"无锁快路径"听着能提速,但在我们这儿会引新 bug,而且省的那点开销根本不值,所以**默认不做**。先知道这个词是啥意思就行。

---

## 2. 现在的代码在做什么,缺什么

`get_or_create`(`sandbox_manager.py:191`)现在就干一件事:**拿身份牌去登记本查台子,有就用,没有就搭。** 简化版:

```python
async with self._lock(身份牌):        # ← 只有 asyncio.Lock(只挡同坐席)
    台子 = 查登记本(身份牌)
    if 台子存在:
        确保开机 → 返回复用            # 快路径
    台子 = 搭一张新的()               # 慢路径(慢、无超时)
    写登记本(身份牌 → 台子)
    开机登录(az login)
    返回
```

**缺三样(都只在"多坐席"时才是问题):**

1. 只有 `asyncio.Lock`,**挡不住跨坐席**双搭 → 孤儿。
2. 搭台子那步**没有超时**,万一卡住会一直占着锁。
3. **回收台子的 reaper,每个坐席各跑一个**(`_reaper_loop` `:432`),N 个坐席就重复扫 N 遍,浪费。

---

## 3. 我们要做的三件事(总览)

| # | 大白话 | **实际动的代码**(全在 `sandbox_manager.py`) | 详见 |
|---|---|---|---|
| **①** | 加一把"跨坐席的锁":搭台子前先在 Redis 上抢锁,别的坐席看到就等 | **新增** `_dlock()` 上下文管理器;在 `get_or_create` 里原来那层 `self._lock(key)` **内层再套一层** `async with self._dlock(key)` | §4.2 / §4.3 |
| **②** | 给搭台子加个闹钟:搭太久就放弃、放锁 | 在 `get_or_create` 里把 `_create_sandbox(...)` 用 `asyncio.wait_for(..., create_timeout)` 包起来;再加个 `try/except` 在失败时撤销 session key | §4.3 / §8 |
| **③** | 让回收只由一个坐席干:选个"值日生"扫孤儿,其余歇着 | 改 `_reaper_loop`:每轮先 `SET mcp:reaper:leader NX EX` 抢主,非主跳过;**新增** `_try_become_reaper()` / `_resign_reaper()` 两个方法 + `_RELEASE_LUA` 常量。`reap_orphans` 本身不动 | §4.4 |
| **公共** | 几个开关和超时旋钮 | `__init__` **新增 5 个参数**(`distributed_lock` / `lock_ttl` / `lock_wait` / `create_timeout` / `reaper_lease`),`from_env` 读对应 5 个环境变量;文件顶部补 `import contextlib` / `import uuid` | §4.1 |

**一句话钉死改动范围:** 全部集中在 **`sandbox_manager.py` 一个文件** —— **新增 3 个私有方法**(`_dlock`、`_try_become_reaper`、`_resign_reaper`)+ **改 2 个方法**(`get_or_create` 加两层锁+闹钟、`_reaper_loop` 加抢主)+ 加 5 个参数。**`reap_orphans`、`cache.py`、`main.py` 都不用动**(bicep 只在 PR-4 改 `maxReplicas` + 打开关)。

三件事底层都靠**同一个 Redis 小技巧**(`SET key val NX` = "没人占才占上" + Lua 校验后释放),只是 key 和语义不同(设计篇 §7):①的 key 是 `lock:<身份牌>`,③的 key 是 `reaper:leader`。

而且**全部挂在一个总开关后面,默认关着**:

- 开关关(现在)= 代码行为**和今天一模一样**,只有 `asyncio.Lock`。
- 开关开(将来雇了第 2 个坐席时)= 三件事全生效。

开关叫 `SANDBOX_DISTRIBUTED_LOCK`(默认 `0`)。**它就是"从 1 个坐席变多个坐席"时,和 `maxReplicas` 一起打开的那个闸。**

> 关于 **watchdog(自动续租)**:设计篇 §10.2 判定现在做它是"过早优化",**本方案不做**,用一个"给得足够宽的固定超时"代替。等以后实测发现搭台子真的很慢再说。

---

## 4. 动手改:`sandbox_manager.py`

> ✅ **这一节的改动已经落地(PR-1 打点 + PR-2 + PR-3,就在本分支 `fix-redis`)。** 下面的代码块是讲解版(部分用中文占位看结构);真实实现以 `src/mcp-server/sandbox_manager.py` 为准,行为与这里一致。全部挂在 `SANDBOX_DISTRIBUTED_LOCK` 后,默认关。

### 4.1 先加几个开关和超时参数

`__init__`(`:65`)末尾、`from_env`(`:126`)里,加这几个(都有默认值,不配也能跑):

```python
# __init__ 新增参数
distributed_lock: bool = False,   # 总开关。关=只用 asyncio.Lock(今天的行为)
lock_ttl: int = 60,               # Redis 便利贴最多贴多久(秒),要盖过"搭台子+开机"最坏耗时
lock_wait: float = 45.0,          # 抢不到锁时,最多等多久
create_timeout: float = 30.0,     # 搭一张台子的闹钟:超过就放弃
reaper_lease: int = 90,           # 值日生任期(秒)
```

```python
# from_env 里对应读环境变量
distributed_lock=os.environ.get("SANDBOX_DISTRIBUTED_LOCK", "0") == "1",
lock_ttl=int(os.environ.get("SANDBOX_LOCK_TTL", "60")),
lock_wait=float(os.environ.get("SANDBOX_LOCK_WAIT", "45")),
create_timeout=float(os.environ.get("SANDBOX_CREATE_TIMEOUT", "30")),
reaper_lease=int(os.environ.get("SANDBOX_REAPER_LEASE", "90")),
```

> ✅ **上面这几个秒数已经实测校准过了(见 §5.1),不再是占位值。** 实测冷启动端到端 ~4s(最坏 ~8s),所以 `create_timeout=30`、`lock_ttl=60`、`lock_wait=45` 都是"实测最坏值的 4~7 倍余量",很安全。**watchdog 确认不做**(临界区 ~8s,离 60s 的 TTL 远得很)。

### 4.2 加一把"跨坐席的锁":`_dlock`

紧挨现有 `_lock`(`:183`)加一个。核心思想:**能贴上便利贴就贴,贴不上/白板(Redis)坏了就直接放行,绝不因为锁把用户的请求卡死或搞失败。**

```python
import contextlib   # 文件顶部补上;asyncio 已经 import 了

@contextlib.asynccontextmanager
async def _dlock(self, key: str):
    """跨坐席的锁,叠在 asyncio.Lock 之下。贴不上/Redis 坏了就放行(降级)。"""
    if not self._dlock_enabled or self._redis is None:
        yield                                   # 开关关 / 没 Redis → 等于没这把锁
        return
    lock = self._redis.lock(
        f"lock:{key}",
        timeout=self._lock_ttl,                 # 便利贴 TTL,到点自动撕(防死锁)
        blocking=True,
        blocking_timeout=self._lock_wait,       # 贴不上时最多干等多久
    )
    got = False
    try:
        got = await lock.acquire()
        if not got:
            logger.warning("dlock: %s 等锁超时,降级放行", key)
    except Exception as e:                       # Redis 连不上等
        logger.warning("dlock: 抢锁出错(%s),降级只用本地锁", e)
    try:
        yield
    finally:
        if got:
            try:
                await lock.release()             # redis-py 内部用脚本校验"是不是我的便利贴"才撕,安全
            except Exception as e:
                logger.warning("dlock: 撕便利贴失败(%s)", e)
```

这里用的是 redis-py 自带的 `Lock`(设计篇 §4.5:别自己造轮子)。**"贴不上就放行"是故意的**——这把锁是"省钱锁"不是"正确性锁"(设计篇 §4.8):就算偶尔漏过一次双搭,多出来的孤儿有 reaper 兜底,不会出正确性问题。

> 小注:我们的 Redis client 开了 `decode_responses`(`cache.py:89`)。redis-py `Lock` 的 acquire/release 没问题;**只是别用它的 `lock.owned()` / `lock.extend()`**(那俩会误判)。我们没用到,无碍。

### 4.3 把这把锁套进 `get_or_create`

**改动极小:就是在原来那层 `asyncio.Lock` 里,再套一层 `_dlock`,并给"搭台子"加个闹钟。其余每一行、顺序,全都不动。**

```python
async def get_or_create(self, ctx):
    group = ctx.group
    gclient = self._group_client(group)
    key = f"{ctx.user_oid}:{ctx.session_id}:{group}"

    async with self._lock(key):            # 外层:挡同坐席(免费)
        async with self._dlock(key):       # 内层:挡跨坐席(§4.2,开关关时是空操作)
            台子 = 查登记本(...)
            if 台子存在:
                确保开机 → 返回复用
            # 给"搭台子"套个闹钟:超时就放弃、放锁(两层锁自动释放)
            台子 = await asyncio.wait_for(
                self._create_sandbox(ctx, gclient, group),
                timeout=self._create_timeout,
            )
            try:
                写登记本(身份牌 → 台子)     # 顺序不能变:先写登记本
                写反向索引(台子 → 身份牌)   # 给 reaper 用的
                if 没开过机:
                    开机登录()
                    标记已开机()
                返回 台子
            except Exception:
                # 新增:开机/写库失败,就把登记撤掉,别留个"坏台子"坑下一个人。
                # 反向索引留着,让 reaper 快点把这台废台子回收(见 §8)。
                撤销登记(...)
                raise
```

真正改的就三处:
1. **多包一层 `async with self._dlock(key)`** —— 跨坐席互斥(开关关时无效果)。
2. **`_create_sandbox` 外面套 `asyncio.wait_for(..., create_timeout)`** —— 搭台子的闹钟。
3. **加个 `try/except` 撤销登记** —— 顺手修一个既有小坑(§8 解释)。

> 上面用中文占位是为了看清结构,真实代码里把"查登记本""写登记本"换回原来的 `self._sessions.get/set`、`self._index.set`、`self._bootstrap` 那几行即可(它们一个字都不用改)。

### 4.4 让回收(reaper)只由一个坐席干:值日生选举

现在每个坐席各扫各的(`_reaper_loop` `:432`)。改成:**每轮开始,大家抢一张"今天我值日"的便利贴,抢到的那个才扫,其余跳过。** 抢不到/Redis 坏了就退回"各扫各的"(反正重复扫也不出错,只是浪费)。

```python
# 文件顶部加:只有"是我贴的便利贴"才撕(防误撕别人的)
_RELEASE_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] "
                "then return redis.call('del', KEYS[1]) else return 0 end")
_NO_LEASE = ""   # 哨兵:该扫,但没占便利贴(不用撕)

async def _reaper_loop(self):
    while True:
        await asyncio.sleep(self._reaper_interval)     # 默认每 300s 一轮
        try:
            token = await self._try_become_reaper()    # 抢值日生;抢不到 → None
            if token is None:
                continue                               # 别人值日 → 本轮跳过
            try:
                await self.reap_orphans()              # 只有值日生真扫
            finally:
                await self._resign_reaper(token)       # 扫完把便利贴撕了
        except Exception as e:
            logger.warning("reaper pass failed: %s", e)

async def _try_become_reaper(self):
    if not self._dlock_enabled or self._redis is None:
        return _NO_LEASE                               # 开关关/没Redis → 照扫(幂等)
    token = uuid.uuid4().hex
    try:
        got = await self._redis.set("mcp:reaper:leader", token,
                                    nx=True, ex=self._reaper_lease)
    except Exception as e:
        logger.warning("reaper 选举出错(%s),照扫", e)
        return _NO_LEASE
    return token if got else None

async def _resign_reaper(self, token):
    if not token or self._redis is None:
        return
    try:
        await self._redis.eval(_RELEASE_LUA, 1, "mcp:reaper:leader", token)
    except Exception as e:
        logger.warning("reaper 交班失败(%s)", e)
```

文件顶部补 `import uuid`。**`reap_orphans`(`:440`)本身一行不改。** 值日生任期 `reaper_lease` 只要比"扫一轮最坏耗时"长就行;真超了,顶多两个坐席同轮都扫一遍,重复但安全。值日生中途崩了,便利贴 90 秒后自动过期,下一轮别人自然顶上。

---

## 5. 参数怎么定:先测,再拧(最省事的第一步)

§4.1 那几个秒数是占位的。**真正该填多少,取决于"搭一张台子实际多久"。所以第一步不是写锁,是先测。**

在 `_create_sandbox`(`:244`)和 `_bootstrap`(`:358`)前后打个时间戳:

```python
import time
t0 = time.monotonic()
client = await poller.result()
logger.info("timing: 搭台子 %s 用了 %.2fs (%s)", client.sandbox_id, time.monotonic()-t0, group)
```

跑几十次,看三种情况各多久:**冷启动首搭 / 命中复用(应该亚秒)/ 全新镜像第一台(会慢,但一次性)**。测完把秒数按这个规矩填(设计篇 §4.9):

```
搭台子实际耗时  <  create_timeout(搭台子闹钟)  <  lock_wait(等锁上限)
且  lock_ttl(便利贴时长)  >  create_timeout + 开机时间
```

> 设计篇 §10.1 估计冷启动其实是**秒级**(microVM 秒级起、镜像预建好、开机就几个 API)。如果实测确实秒级,现在填的占位值已经很宽松够用,**watchdog 也确实不用做**。

### 5.1 实测结果(2026-07-04,当前单机部署)✅

已经把上面的打点加进 `sandbox_manager.py` 并**真跑了一遍**(测量脚本见 §5.2)。在**当前单实例**的 `dataops-aca-rg`(westus2)上,对 `diagnose` / `action` 两组各造 5 个"假用户"(不同 routing key)串行建 sandbox,每个再复用一次,共 10 台,**全部实测完即删,0 泄漏**。单位:秒。

| 段 | 含义 | 中位数 | 最坏(p95≈max) | 说明 |
|---|---|---|---|---|
| **A `disk`** | 解析磁盘/镜像 | **~0**(缓存命中) | 0.09 | 只有**每副本第一次**付一次 `list_disk_images`(镜像已 Ready,直接复用,**没触发 build**);之后进程内缓存,≈0 |
| **B `vol`** | 建/确认 volume | **~0**(缓存命中) | 0.09 | 同上,首次确认(volume 已存在,409 被吞),之后缓存 ≈0 |
| **C `vm`** ⭐ | **建 microVM 本体** | **~1.1** | **4.70** | 唯一有波动的一段。多数 ~0.9–1.2s,偶发跳到 2.3–4.7s |
| **D `autodel`** | 设 auto-delete 策略 | 0.13 | 0.22 | 稳定的小开销 |
| **E `bootstrap`** ⭐ | **FIC `az login`** | **~2.7** | 3.42 | 很稳,是最大且最可预测的一块 |
| **wall(端到端 miss)** | A+B+C+D+E+写库 | **~4.0** | **8.34** | 一次"冷"建从头到尾 |
| **hit(复用)** | `ensure_running` | **0.08** | 0.10 | 命中路径几乎不花时间 |

**四条结论:**

1. **冷启动是秒级,不是分钟级** —— 中位 ~4s、最坏 ~8.3s,**坐实设计篇 §10.1**。
2. **bootstrap(E,~2.7s)比建 VM(C,~1s)更重也更稳**;真正抖的是 C(microVM 调度)。
3. **A/B 稳态 ≈ 0**(只有每副本第一次付 ~0.09s),**没碰到镜像 build**(镜像已 Ready → 复用)。§8 那个"90 秒闹钟撞上分钟级 build"的雷,**只在全新组/新镜像 tag 时才会响**;稳态永远走复用,`create_timeout` 不受影响。
4. **hit 路径 ~0.08s** —— 印证"绝大多数调用很快,锁不锁热路径几乎无所谓",§5(可选快路径)确实非必需。

**据此把 §4.1 的占位值换成实测校准值:**

| 参数 | 旧占位 | 实测校准 | 依据 |
|---|---|---|---|
| `create_timeout` | 90 | **30** | 最坏"建 VM 段"(A+B+C+D)~5s → 6× 余量 |
| `lock_ttl` | 180 | **60** | 最坏临界区(建+开机)~8.3s → 7× 余量;持锁者崩了别人最多等 60s |
| `lock_wait` | 120 | **45** | ≥ 一次"建+开机"最坏,让等锁者能等到复用;且 `> create_timeout` |
| watchdog | 待定 | **不做** | 临界区 ~8s,离 60s TTL 远得很,续租毫无必要 |

> ⚠️ 这是**单机、串行、镜像已预热**下的数,是**乐观下限**。多副本并发抢 ARM、或跨区冷区,C(建 VM)会更抖 —— 所以留了 4~7× 余量。PR-4 多副本压测时用同一脚本复测,把 C 的尾巴看清再定稿。

### 5.2 测量脚本(可复现)

> 📌 **分支说明:** PR-1 的 timing 打点已随功能一起在**本分支 `fix-redis`** 的 `sandbox_manager.py` 里。下面这两个**测量脚手架**文件(harness + 单测)**只在 `measure-sandbox-timing` 分支**的 `src/mcp-server/tests/` —— 它们连同 PR-2/PR-3 的正式单测,留待日后**端到端(e2e)测试**时一起补(见 §7)。

两个文件(在 `measure-sandbox-timing` 分支的 `src/mcp-server/tests/`):

- **`test_timing_instrumentation.py`** —— 纯 mock 单元测试(不碰 Azure、不花钱),验证每段都emit了正确的结构化 `timing` 日志。CI 可跑:`pytest src/mcp-server/tests/`。
- **`measure_create_timeline.py`** —— **真打 ACA 的测量脚本**(会建真 sandbox、花钱),就是上面这张表的来源。用法:

  ```bash
  # 需要 az login + 装了 azure-containerapps-sandbox / azure-identity 的环境
  python src/mcp-server/tests/measure_create_timeline.py --n 5 --group diagnose
  ```

  **为什么它不需要走 OAuth/OBO 登录**:OBO 只在 MCP **前门**(`main.py`:验用户 JWT → OBO → Graph 查组),它产出的只是一个 `user_oid` 字符串。而我们要测的 sandbox 生命周期(`get_or_create → _create_sandbox → _bootstrap`)是用**应用自己的** `DefaultAzureCredential` 认证 ACA 的,**跟用户 token 无关**。所以"不同用户"= 直接喂不同的 `(oid, session)` routing key,**不用真登录**。脚本还绕开了 Redis(内存缓存代替,因为线上 Redis 是内网 FQDN,本机连不上),并**每次跑完 diff 清理**(比对前后 sandbox 列表,删掉本次新增的,防泄漏)。

---

## 6. 分几步上线(4 个小 PR,每步能单独回滚)

| PR | 干什么 | 状态 | 会改变现在的行为吗 |
|---|---|---|---|
| **PR-1** | 打时间戳测耗时(§5) + 测量脚本 + 单测,结果见 §5.1 | ✅ **已完成** | 不会,只加日志 |
| **PR-2** | 给搭台子加闹钟(`wait_for`)+ 失败撤销登记(§4.3 改动 2、3) | ✅ **已完成**(本次) | 几乎不会,只让出错时更干净 |
| **PR-3** | 加 `_dlock` + 值日生选举,全挂在开关后(默认关,§4.1–4.4) | ✅ **已完成**(本次) | **不会**(开关默认关 = 今天的行为) |
| **PR-4** | bicep `maxReplicas>1` + 打开开关 + 压测 | ⏳ **未来再做** | 会,这才真正开始多坐席 |

> **进度与分支(2026-07-04):**
> - **主代码改动都在本分支 `fix-redis`**:PR-1 打点 + PR-2(闹钟+回滚)+ PR-3(`_dlock`+值日生),`from_env` 全默认值下**行为与今天逐字节一致**(开关默认关)。
> - **`measure-sandbox-timing` 分支只保留"测量结论"**:测量 harness(`measure_create_timeline.py`)+ PR-1 单测 + 实测报告,供复现;那几个测试连同 PR-2/PR-3 的正式单测,**日后与端到端(e2e)测试一起补**(§7)。
> - 手动 smoke 验证过:开关开时 `_dlock` 正确 acquire/release(TTL=60 / 等锁=45)、值日生 `SET NX EX` 抢主 + Lua 交班、第二个竞争者拿 None 跳过、开关关时 `_dlock` 完全不碰 Redis。
> - PR-4 留待将来真扩多副本时做。

**纪律:`maxReplicas>1` 和打开 `SANDBOX_DISTRIBUTED_LOCK` 必须在同一个 PR(即 PR-4)。** 只加坐席不开开关 = 有孤儿风险;只开开关不加坐席 = 白付一点点 Redis 往返。别拆开(设计篇 §10)。

`main.py` **完全不用改**(`SandboxManager.from_env` 自己就有 Redis client,`:130`)。

---

## 7. 待补的测试(TODO — 本次未写,声明清单)

> ⚠️ **现状:** PR-1 的 `test_timing_instrumentation.py`(5 个,已过)在 `measure-sandbox-timing` 分支;本分支(`fix-redis`)的 PR-2/PR-3 代码只做了**手动 smoke 验证**(见 §6 进度note),**正式单元测试还没写**。计划**日后与端到端(e2e)测试一起补**,下面是清单,照着写。

**PR-2(闹钟 + 回滚)—— 补进 `tests/test_get_or_create.py`(新文件):**

- [ ] `_create_sandbox` 挂起 > `create_timeout` → `get_or_create` 抛 `asyncio.TimeoutError`,**两层锁都释放**(之后同 key 还能再进)。
- [ ] bootstrap 失败(`_bootstrap` 抛错)→ 抛错前 `sessions.delete(key)` 被调用一次、**反向索引 `index` 不被删**(留给 reaper)。
- [ ] bootstrap 成功 → session key 写入、`mark_bootstrapped` 被调、返回 client(回归,别被回滚误伤)。
- [ ] `create_timeout` 触发后,ARM 侧可能仍建成 → 记录一条注释说明"孤儿靠 reaper 兜"(无法在单测断言,注明即可)。

**PR-3(`_dlock` + 值日生)—— 补进 `tests/test_dlock_reaper.py`(新文件,用 `fakeredis` 或 stub):**

- [ ] 开关**关** / `redis is None` → `_dlock` 直接放行,**完全不碰 Redis**;`_try_become_reaper` 返回 `_NO_LEASE`(照扫)。
- [ ] 开关**开**、正常 → `_dlock` 用正确的 `timeout=lock_ttl` / `blocking_timeout=lock_wait` 调 `lock()`,acquire→yield→release 各一次。
- [ ] 开关开、Redis `acquire` **抛异常** → 吞掉、放行、**不 raise**(降级,请求不失败)。
- [ ] 开关开、`acquire` 返回 `False`(等锁超时)→ 记 warning 后放行(降级)。
- [ ] 值日生选举:第一个 `SET NX EX` 成功拿 token;第二个拿 `None` → 本轮 `continue` 跳过。
- [ ] `_resign_reaper`:只用**自己的 token** 走 Lua 释放(灌一个别人的 token,断言不误删);`_NO_LEASE` / `redis is None` 时是 no-op。

**PR-4(多坐席压测,真环境)—— 复用 `measure_create_timeline.py` 思路,`--n` 并发版:**

- [ ] 同一 routing key 并发 N 个调用(跨 ≥2 副本)→ **只建出一台 sandbox**,其余复用(`list_sandboxes` labels 计数)。
- [ ] 两副本同时到点回收 → 某一轮**只有一个**副本打出 `reaping orphan`,另一副本本轮 `_try_become_reaper→None` 跳过。
- [ ] 压测中途拔 Redis 5 秒 → tool call **不失败**(降级),恢复后锁/选主自愈。

---

## 8. 一个坑 + 为什么不做"看起来更快的优化"

**为什么 §4.3 里加了个 `try/except` 撤销登记?** 现有代码有个小毛病(和锁无关):如果"开机登录"失败,但登记本已经写了(先写登记、后开机),下次同一个客户会**复用到这台没登录的坏台子**,命令一直失败,而且 reaper 还不回收它(因为登记本里它还"活着")。加个 try/except 在失败时撤销登记,下次就会重搭,reaper 也能把废台子收走。顺手修掉。

**为什么不做"无锁快路径"(§1.7 那个)?** 想法是:"直接用现成台子"不加锁,只有要搭新台子才加锁,能省掉每次抢锁的开销。听着很美,但在我们这儿会引两个新 bug:

1. **用到没开机的台子**:现在代码是"先写登记、后开机"。无锁快路径一查到登记就用,可能拿到还没 `az login` 的台子 → 失败。
2. **reaper 误删**:若为了堵上面那条,把"写登记"挪到开机之后,那开机期间登记本里没它,reaper 扫到就把**正在搭的台子**当孤儿删了。

两个要求互相打架,得再引个"搭建中"状态才能同时满足,复杂度上一个台阶。而在人机交互这种**低频**场景,"每次先抢个锁"的开销根本不值一提。所以**默认不做**,真压出瓶颈了再按设计篇 §5.2 的带守卫版本加。

---

## 附:新增环境变量一览

| 变量 | 默认 | 大白话 |
|---|---|---|
| `SANDBOX_DISTRIBUTED_LOCK` | `0`(关) | 总开关。扩到多坐席时,和 `maxReplicas>1` 一起置 `1` |
| `SANDBOX_CREATE_TIMEOUT` | `30` | 搭一张台子的闹钟(秒),超了放弃 |
| `SANDBOX_LOCK_TTL` | `60` | Redis 便利贴最多贴多久(秒) |
| `SANDBOX_LOCK_WAIT` | `45` | 抢不到锁最多等多久(秒) |
| `SANDBOX_REAPER_LEASE` | `90` | 回收值日生的任期(秒) |

> 秒数已按 §5.1 实测校准(冷启动实测 ~4s / 最坏 ~8s,留了 4~7× 余量)。

---

*关联:* [MCP-水平扩展-分布式锁与Reaper选主.md](MCP-水平扩展-分布式锁与Reaper选主.md)(原理篇) · [MCP-用户隔离与Redis设计.md](MCP-用户隔离与Redis设计.md) · [ACA-Sandbox-迁移方案.md](ACA-Sandbox-迁移方案.md)

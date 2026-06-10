# MCP 用户隔离方案对比与 Redis 设计

本文回答三个问题:

1. 多用户共享 worker 造成的互相干扰(`~/.azure` 污染只是其中一种)有哪些通道,各隔离方案分别能挡住什么——含 comparison table;
2. "能不能在 pod level host 不同的 request?"——能,怎么做、代价是什么;
3. Redis 该怎么设计:在现有 `groups:{oid}` 缓存(见 `src/mcp-server/cache.py` 与 [MCP-鉴权-缓存与凭据演进.md](MCP-鉴权-缓存与凭据演进.md))之上,还应该存什么、**不**该存什么;以及 worker 容器当前的几个必修问题。

---

## 1. 干扰通道全景:污染不只 `~/.azure`

共享一个常驻 worker pod、裸 `create_subprocess_shell` 执行任意 bash 时,用户之间的干扰分两类:

**A 类:正确性串扰(良性并发也会撞)**

| 通道 | 表现 |
|---|---|
| `azureProfile.json`(active subscription) | 用户 A `az account set` 后,用户 B 的命令落到 A 的 subscription |
| `az configure --defaults` | A 设了 `group=rg-A location=eastus`,B 静默继承 |
| `az cloud set` / active tenant | 切 cloud / tenant 影响所有人 |
| `az extension add` | 装进共享 config dir 的 `cliextensions/`,影响所有人 |
| 固定路径临时文件 | worker 的 TRUNCATE_HINT 自己教 agent 写 `az ... > /tmp/out.json`(`worker.py` 的 `TRUNCATE_HINT`)——固定路径,并发即互相覆盖/误读 |

**B 类:安全串扰(威胁模型含被 prompt-injection 污染的 agent 时)**

| 通道 | 表现 |
|---|---|
| `/proc` 窥探 | `ps aux` 看到别人命令行里的 SAS token / 密码;`cat /proc/<pid>/environ` 读出别人进程的 env(包括 `AZURE_CLIENT_SECRET`) |
| 共享文件系统 | 读别人的输出文件、改 `$HOME`、投毒共享脚本 |
| 后台进程残留 | `nohup ... &` 逃过 `proc.communicate()`,跨请求驻留 |
| 共享网络 namespace | 反向 shell、本地端口互访、隧道 |
| IMDS / SA token | 上 workload identity 后,pod 内任意 bash 都能直接 mint token,绕过 server 端一切逻辑 |
| 资源耗尽 | 写爆 `/tmp`、fork bomb 打满共享 cgroup |

**关键判断:`AZURE_CONFIG_DIR` 一类技巧只覆盖 A 类;B 类必须靠 OS namespace 或 pod 边界。**

---

## 2. 候选隔离方案

### 方案 A:现状 —— 共享常驻 pod + 裸 subprocess

无隔离。A、B 两类全开。仅在"用户彼此完全可信 + 串行使用"下勉强可用。

### 方案 B:per-request `AZURE_CONFIG_DIR`(+ Redis 持久化用户上下文)

每次 exec 设 `AZURE_CONFIG_DIR=/tmp/cfg/{request_id}`,用户的 subscription/defaults 落在私有目录;绑定关系存 Redis(见 §5.2),跨 pod 可重建。

- 解决:A 类全部(config、defaults、cloud、extension;临时文件需配合私有 cwd)。
- 不解决:B 类全部。
- 两个实现要点:
  1. **新 config dir 里没有登录态**——必须 per-request 注入身份:从 base dir 拷贝 SP 登录态(profile + token cache,毫秒级),或 workload identity 下 `az login --federated-token`。否则 az 直接报 "Please run az login"。
  2. 隔离 config dir = 放弃共享 token cache(同一 SP 本可复用 token)。拷贝 base dir 顺带把 token cache 也带上,可缓解。

### 方案 C:B + namespace 沙箱(bwrap / unshare),常驻 pod

worker 常驻,但每次 exec 用 `bubblewrap`(或 `unshare`)套进全新的 PID / mount / IPC namespace:私有 `/proc`、私有 tmpfs `/tmp`、只读 rootfs、non-root、cgroup limit,`--die-with-parent` 保证超时连根杀整棵进程树。启动开销 **~10–30ms**。

```bash
bwrap \
  --unshare-pid --unshare-ipc --unshare-uts \
  --proc /proc --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /lib /lib \
  --bind /tmp/cfg/$REQ /home/worker/.azure \
  --die-with-parent --new-session --uid 1000 \
  bash -c "$command"
```

- 解决:A 类全部 + B 类的 `/proc` 窥探、文件串扰、后台残留、fork bomb。
- 不解决:共享内核(内核 0day 可逃逸)、共享网络 ns(可加 `--unshare-net`,但 az 需要出网,得配 slirp 或保持共享 + NetworkPolicy 锁 egress)、IMDS/SA token 对沙箱内仍可达(az 要干活就必须能拿 token,这条本质上不属于"用户隔离",见 §4 末尾)。
- **部署 caveat**:在非特权容器里创建 namespace 默认被 Docker/containerd 的 seccomp profile 拦(`unshare`/`clone` 受限)。K8s 下需要给 worker pod 配 securityContext / 自定义 seccompProfile 放行;这是上线前要验证的点,不是写了 bwrap 就能跑。

### 方案 D:pod-per-request(ephemeral Job/Pod)——"pod level host 不同 request"之一

**直接回答:能。** MCP server 收到 tool call 后通过 K8s API 起一个一次性 Pod(绑定对应 SP 的 workload identity),执行完即销毁。这就是"每个 request 一个 pod"。

- 解决:A、B 两类**全部**(每次全新内核 namespace 全家桶、全新文件系统、独立网络 ns、独立 cgroup);审计天然一对一;配 gVisor/Kata RuntimeClass 连内核面都隔。
- 代价:**冷启动 2–10s**(调度 + 容器启动 + 登录);K8s API 权限给到 mcp-server(它从"只会发 HTTP"变成"能创建 pod",控制平面权限变大,需用专门的 namespace + 最小 RBAC 圈住);纯按量计费,无闲置成本。

### 方案 E:warm pool(预热池)—— D 的低延迟变体

预热 2–3 个已登录的 pod 挂在池里。请求来了**抓现成热 pod(毫秒级)→ 执行 → 移出轮转异步销毁 → 后台补一个新的**。冷启动仍然发生,但**不在用户关键路径上**。闲时(下班/周末)可 scale-to-zero,只有空闲后的第一个请求吃一次冷启动。

- 隔离强度 = D(每个请求拿到的都是没人用过的 pod)。
- 代价:常驻 2–3 个小 pod 的闲置成本 + 一个池管理器(自己写控制循环或用现成 operator),是 D 之上最主要的复杂度增量。

### 方案 F:pod-per-user(每用户常驻 pod,sticky 路由)——"pod level host"之二

按 user(oid)起常驻 pod,session affinity 把同一用户路由回同一 pod。

- 用户**之间**隔离 = pod 级,干净;且用户的 az 状态天然驻留,无需重建。
- 但:① 成本随用户数线性涨,闲置浪费大(你自己说了"穷酸团队"不可行);② **同一用户的并发请求仍共享 pod**,A 类串扰在单用户多 agent 场景还在;③ 撤权/回收逻辑要自己管。

### 修饰符 G:gVisor / Kata RuntimeClass

不是独立方案,是加在 D/E/F 上的内核面加固:把"共享内核"这条残余风险也压掉。多租户互不信任时才值得,本团队现阶段不必。

---

## 3. Comparison Table

### 3.1 能力矩阵(挡得住 ✅ / 部分 ⚠️ / 挡不住 ❌)

| 干扰通道 | A 现状 | B config-dir | C +bwrap | D pod/request | E warm pool | F pod/user |
|---|---|---|---|---|---|---|
| subscription / defaults / cloud / extension 污染 | ❌ | ✅ | ✅ | ✅ | ✅ | ⚠️ 单用户并发仍撞 |
| `/tmp` 等共享文件串扰 | ❌ | ⚠️ 仅 config dir | ✅ 私有 tmpfs | ✅ | ✅ | ⚠️ 用户间✅ 用户内❌ |
| `/proc` 窥探(偷 argv/env) | ❌ | ❌ | ✅ 新 PID ns | ✅ | ✅ | ⚠️ 同上 |
| 后台进程残留 / 超时杀不净 | ❌ | ❌ | ✅ die-with-parent | ✅ pod 销毁 | ✅ | ❌ pod 常驻 |
| fork bomb / 资源耗尽 | ❌ | ❌ | ✅ cgroup | ✅ pod limit | ✅ | ⚠️ 影响限于该用户 |
| 网络 ns 隔离 | ❌ | ❌ | ⚠️ 可选但麻烦 | ✅ | ✅ | ✅ 用户间 |
| 内核 0day 逃逸 | ❌ | ❌ | ❌ 共享内核 | ⚠️ +gVisor→✅ | ⚠️ 同左 | ⚠️ 同左 |
| IMDS / SA token 可达 | ❌ | ❌ | ❌ | ❌* | ❌* | ❌* |

\* 所有用户共用同一 worker SP,"沙箱内能拿 SP token"是**设计使然**(az 要干活),不是用户隔离问题——它属于凭据生命周期问题,解法是 workload identity + 短 TTL token + 审计(见鉴权文档 §4),不要指望任何隔离方案"顺手"解决它。

### 3.2 成本 / 延迟 / 复杂度

| 维度 | A | B | C | D | E | F |
|---|---|---|---|---|---|---|
| 每次调用额外延迟 | 0 | ~0(拷 config 毫秒级) | +10–30ms | **+2–10s** | ~ms(池命中) | 首次冷,后续 0 |
| 闲置成本 | 1 pod/种类 | 同 A | 同 A | **≈0**(纯按量) | 2–3 个热备 pod | **∝ 用户数** |
| 运维复杂度 | 低 | 低(+Redis 读写) | 中(seccomp/securityContext 验证) | 中(K8s API + RBAC) | 中高(池管理器) | 中(sticky + 回收) |
| 多副本一致性 | — | 需 Redis 重建(§5.2) | 同 B | 天然(无状态) | 天然 | sticky 路由解决 |
| 审计对应关系 | 弱 | 弱 | 中(per-exec) | **强(pod=request)** | 强 | 中 |

### 3.3 推荐组合(按 worker 种类拆开,而不是全局选一个)

| worker | 频率/风险 | 推荐 | 理由 |
|---|---|---|---|
| **diagnose** | 高频、只读、低危 | **C(常驻 + bwrap)+ B(per-request config dir)** | 延迟 ~0,成本一个 pod;读操作配 namespace 隔离绰绰有余 |
| **action** | 低频、写、高危 | **D(ephemeral pod)或 E(热备 1 个)** | 写操作没人在乎多等几秒,换全量隔离 + pod=request 的审计;穷酸版先 D,嫌慢再加 1 个热备升级成 E |

> 演进顺序:现在(compose/单 pod)先落 B+C;上 K8s 后 action 切 D;真撑到多租户再谈 G。
> F(pod-per-user)在本场景没有最佳位置:贵在常驻、又解不了单用户并发,不推荐。

---

## 4. "pod level host 不同 request" 的两个注意点

1. **B 方案是 D/E 的前置,不是对立面。** pod-per-request 意味着没有任何本地状态能活过一次请求——用户的 subscription 绑定**必须**外置(Redis),否则每个新 pod 都是失忆的。所以 §5.2 的 usercfg 设计无论选哪条路线都要做。
2. **身份绑定用 oid,不是 client 传的 request_id。** "request ID 带进 HTTP 请求、先查 Redis"有个坑:request_id 若由 client 生成/传递,等于让 client 自报身份——可伪造、可撞别人的上下文。正确做法:**MCP server 从已验签的 JWT 里取 `oid` 作为绑定键**(server 端已有,见 `main.py` 的 `UserAuthMiddleware`),request_id 仅作为 server 生成的 trace/审计关联 ID,不参与任何 lookup 授权。

---

## 5. Redis 设计

### 5.1 现状回顾(已实现,见 cache.py)

- 两层结构(`CacheBackend` 协议 + `GroupCache` 类型化视图)、key 前缀隔离、JSON-safe 值——**这个骨架是对的,加新缓存 = 加新视图,不动后端接口。**
- 已存:`groups:{oid}` → 用户所属的 KNOWN_GROUPS 子集,TTL 300s。

### 5.2 还应该存什么(按价值排序)

**① `usercfg:{oid}` — 用户执行上下文(这是状态,不是缓存)**

解决 `az account set` 的跨请求/跨 pod 持久化——即你提出的方案,但键改为 oid(§4)。

```
usercfg:{oid} = { "subscription": "...", "defaults": {"group": "...", "location": "..."} }
TTL: 7d 滑动(或不设,LRU 兜底)
```

读写协议(**只有 mcp-server 碰 Redis,worker 保持无 Redis 凭据**——数据平面保持"哑",别给执行任意 bash 的容器再塞一份基础设施凭据):

```
mcp-server (tools/call):
  usercfg = redis.get(usercfg:{oid})
  POST worker/exec { command, timeout, request_id, usercfg }

worker:
  cfg = /tmp/cfg/{request_id}        # 私有目录
  从 base 登录态拷贝 profile + token cache(毫秒级)
  若 usercfg.subscription 存在: az account set --subscription ...   # 纯本地文件写,无网络
  以 AZURE_CONFIG_DIR=cfg、最小 env 白名单跑命令
  执行后读 cfg/azureProfile.json 取 active subscription
  返回 { exit_code, stdout, stderr, context: {subscription} }

mcp-server:
  若 context.subscription 变了 → redis.set(usercfg:{oid})
```

> 用"执行后读 azureProfile.json"回捕状态,而不是 server 端正则解析 `az account set`——文件是事实来源,解析命令字符串是脆的(`az account set` 可能藏在循环、变量、子 shell 里)。

**② `audit` — 审计事件流(Redis Stream 作缓冲,不作归宿)**

当前审计只有 `logger.info` 到 stdout(`main.py:120,188`),pod 重启即丢,且 `action_bash` 只记执行**前**、不记结果——审计链不完整。

```
XADD audit MAXLEN ~ 100000 * \
  ts <server时间> oid <oid> tool action_bash request_id <id> \
  command <...> explanation <...> exit_code <rc> duration_ms <ms> truncated <bool>
```

- 每次 tool call 记**两条**(received / completed)或一条含结果的完整记录;
- 一个消费者异步搬运到 Log Analytics / blob(append-only)。**Redis 不是可靠审计存储**(默认非持久化),它在这里只是解耦缓冲——真正的 system of record 在外部。

**③ `ratelimit:{oid}:{tool}:{window}` — 限流/配额(agent 跑飞的止损阀)**

agent 死循环重试是真实风险:打满 worker、撞 Graph/ARM 限流、高危写操作连发。`INCR` + `EX` 即可:

```
diagnose_bash: 60/min/user(宽松,只防失控)
action_bash:   10/min/user(写操作没有合法理由这么密)
超限 → 返回结构化错误,提示 agent 退避
```

**④ `idem:{oid}:{sha256(command)[:16]}` — action 写操作防重放(可选)**

agent 重试语义下,同一条 `az datafactory pipeline create-run` 跑两次 = 数据重复。`SET NX EX 60`:60s 内同 user 同 command 第二次到来时**不执行**,返回 "identical write executed Ns ago; rerun intentionally? change command or wait"。注意这是**提示性护栏**(合法的"故意重跑"会被多挡一轮),不是强一致去重——别把它当正确性保证。

**⑤ `revoked:{oid}` — 即时撤权开关 + 全局熔断**

补 TTL 撤权延迟(鉴权文档 §3 已记录该问题):踢人时由管理操作 `DEL groups:{oid}` + `SET revoked:{oid} 1`;`require_action` 在查 group 缓存**之前**先查此 flag。另加一个全局 `pause:action` flag,事故期间一键冻结所有写操作。成本几乎为零,给了你"5 分钟 TTL 之外"的应急把手。

**⑥ MCP session 状态(多副本 control plane 时,方向性记录)**

streamable HTTP 的 session 有状态;mcp-server 多副本时要么 session affinity,要么 session 外置。FastMCP 对外置 session store 的支持程度**落地前需核对版本文档**,此处只记方向。

### 5.3 不该进 Redis 的(同样重要)

| 东西 | 为什么不 |
|---|---|
| OBO 换来的 Graph token / 用户 JWT | 凭据攻击面外扩;MSAL 进程内 `TokenCache` 已经够用。真要跨副本共享 token,必须加密 + 极短 TTL——默认不做 |
| SP client secret | 任何形式都不进 |
| 命令 stdout/stderr | 大、含数据平面敏感内容、复用率低;大输出走 worker 本地文件(现有 TRUNCATE_HINT 路线),不进 Redis |
| 数据平面查询结果(如 factory 列表) | agent 重查很便宜,缓存反而引入陈旧性判断负担 |

### 5.4 失效语义:Redis 挂了怎么办(按 key 分类决定 fail-open / fail-closed)

| key | Redis 不可用时 |
|---|---|
| `groups:*` | **fail-open 到源头**:当作 miss,直接打 Graph(这是缓存,源还在) |
| `usercfg:*` | 当作无绑定:全新上下文执行,并在返回里提示 agent "defaults 未恢复" |
| `ratelimit:*` | diagnose **fail-open**(可用性优先);action **fail-closed**(高危操作宁可拒绝) |
| `revoked:*` / `pause:*` | action **fail-closed**;diagnose fail-open |

### 5.5 TTL / 撤权补充

单一 `groups` 缓存 TTL=300s 对 action 偏长的问题,**优先用 §5.2-⑤ 的 revoked flag 解决应急撤权**,而不是给 action 单独搞一套更短 TTL 的缓存(双 TTL 会让同一次 Graph 查询写两份、心智成本不值)。若组织要求"常规撤权也必须 <5min 生效",再全局调低 TTL。

---

## 6. Worker 容器审查:四个必修问题

结合上面的设计,`src/worker/` 当前有四个具体问题,与隔离方案无关、单独就该修:

1. **`/exec` 完全无鉴权**(`worker.py:51`)。compose 下 worker 端口未发布到宿主机,尚可;上 K8s 后任何能到达 ClusterIP 的 pod 都能直接叫 worker 执行任意命令、**绕过 MCP server 的全部身份/group 检查**。必须:NetworkPolicy 限定 only-from mcp-server,外加共享 secret header 或 mTLS(纵深)。
2. **超时只杀 shell,不杀进程树**(`worker.py:62` 的 `proc.kill()`)。`create_subprocess_shell` 起的是 `sh -c`,`kill()` 只杀 sh,其下的 `az`(python 进程)和 `&` 后台子进程**存活并脱管**。修法:`start_new_session=True` + `os.killpg(os.getpgid(proc.pid), SIGKILL)`;上了 bwrap 后 `--die-with-parent` 天然解决。
3. **子进程继承全量 env,含 `AZURE_CLIENT_SECRET`**(entrypoint 登录后 secret 仍在环境里,`create_subprocess_shell` 默认继承)。修法:exec 时显式传最小 env 白名单 `env={"PATH":..., "HOME":..., "AZURE_CONFIG_DIR":...}`——比 entrypoint 里 `unset` 更彻底(unset 还要操心 uvicorn 进程树的继承链,白名单一处搞定)。终极解仍是 workload identity 让 secret 不存在。
4. **TRUNCATE_HINT 教 agent 写固定路径**(`worker.py:32-37` 的 `/tmp/out.json` 示例)。并发用户互相覆盖/误读。修法:提示词改成 `mktemp` 风格(`az ... > $(mktemp /tmp/out.XXXX.json)`);落了 per-request 私有 tmpfs(方案 C/D)后此问题自然消失,但提示词先改,零成本。

另外两点与 §5 的设计衔接:

- `ExecRequest` 需要扩字段:`request_id`(server 生成,审计关联)+ `usercfg`(server 从 Redis 读出后下发)。worker 自己**不**连 Redis。
- worker 返回体加 `context: {subscription}`(执行后从 `azureProfile.json` 读),供 server 回写 `usercfg:{oid}`。

---

## 7. 路线图(从穷到富)

| 阶段 | 动作 | 拿到什么 |
|---|---|---|
| **现在(compose)** | 修 §6 的 2/3/4(进程组杀、env 白名单、mktemp 提示);worker 落 per-request `AZURE_CONFIG_DIR`(方案 B,绑定键用 oid) | A 类串扰清零;secret 不再躺在子进程 env 里 |
| **+1 周** | diagnose worker 套 bwrap(方案 C);Redis pod 起来,`RedisBackend` 落地,`usercfg` / `ratelimit` / `revoked` 三类 key 上线 | B 类的 /proc、文件、后台残留挡住;撤权有应急把手;agent 跑飞有止损 |
| **上 K8s 时** | action 切 ephemeral pod(方案 D)+ workload identity 干掉两类 secret;worker 加 NetworkPolicy + mTLS(§6-1);audit stream → Log Analytics | 写操作全量隔离 + pod=request 审计;凭据泄漏面归零;审计可追溯 |
| **多租户那天** | action 加 warm pool(方案 E)+ gVisor RuntimeClass(G) | 不可信租户间的内核面隔离,且用户不吃冷启动 |

---

## 8. 一句话总结

> **config-dir 技巧管"正确性",namespace/pod 管"安全";diagnose 用零延迟的 bwrap 沙箱,action 用吃得起延迟的 ephemeral pod。Redis 里:`groups` 是缓存、`usercfg` 是状态、`audit` 是缓冲、`ratelimit`/`revoked` 是阀门——四类语义不同,失效策略也要分开定;token 和命令输出永远不进 Redis。绑定键一律用验签后的 oid,绝不用 client 自报的 request_id。**
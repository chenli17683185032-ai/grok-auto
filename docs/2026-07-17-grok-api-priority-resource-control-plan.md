# Grok API 优先与注册资源闭环计划

**日期：** 2026-07-17

**状态：** 进行中

**代码基线：** `fa7de8d8ad15cb4b4b92ac89873f2c22db0cb0df`

**实施分支：** `codex/grok-api-priority`

**生产对象：** `/home/deploy/grokcli-upstream-v1947` 的 `grokcli-2api`

## 1. 目标与边界

本轮首要目标不是提高后台账号产量，而是在 4 核 / 8 GiB 主机上把 Grok API 恢复为可预测的前台服务：注册和 Turnstile 只能使用前台 API 剩余的资源，不能再把 API 健康检查、本地请求准备或整台服务器拖入失稳区。

具体目标：

1. 自动注册固定为单并发，Turnstile 固定为单浏览器槽，后台不预取额外长任务。
2. Turnstile/Camoufox 以低于 API worker 的 CPU 调度优先级运行；现有两个 Uvicorn worker 保持不变。
3. 消除本地资源争用造成的几十秒排队；用本地处理、上游响应头、上游首 token 三段指标分别验收。
4. 验证 GrokFree 请求是否真的在同一账号上排队。只有生产并发试验能复现同账号重叠与尾延迟相关性时，才进入 Redis 在途租约/延迟感知调度，不凭感觉扩大热路径。
5. 正式切换只重建 `grokcli-2api`；PostgreSQL、Redis、Grok egress、New API、Caddy、Sub2API 和 LDXP 均不得重启。
6. API 切换或回滚中断必须小于 1 分钟，任何等待均有硬超时。

不在本轮首阶段内：

- 不增加 Uvicorn worker 数，不提高容器 CPU/内存上限，不启用 HTTP/2，不做请求对冲或重复上游生成。
- 不牺牲会话粘性来换表面吞吐；粘性对多轮上下文有业务语义。
- 不修改账号、Token、邮箱、代理订阅、计费、New API 渠道或数据库 schema。
- 不删除旧镜像、备份、Volume 或当前失败批次的审计记录。

## 2. 当前反馈基线

### 2.1 资源故障

- 2026-07-17 23:15（Asia/Shanghai）Grok 容器为 `unhealthy`，约 `241% CPU`、`4 GiB / 4 GiB`、Docker 显示 `760 PIDs`；主机 load average 为 `22.78 / 20.67 / 14.06`。
- cgroup 累计 `oom=71`、`oom_kill=22`、内存上限事件 `12,698,224` 次、CPU throttled period `363,787` 次。
- 直接测得容器内 23 个进程、398 个线程；主要负载是 2 套 Camoufox 浏览器/扩展进程、注册 Python worker 和两个 API worker。
- 生产配置为 `GROK2API_REG_CONCURRENCY=2`、`TURNSTILE_THREAD=2`、`GROK2API_WORKERS=2`，同一 4 GiB / 2.5 CPU cgroup 同时承载前台 API、自动注册和浏览器求解。
- 服务器资源邮件最近两次异常为 CPU `87.3%` 和 `83.3%`；当时内存 `60.7%`/`65.8%`、根盘约 `46.8%`，因此本轮不是磁盘超限。
- 执行 `docker pause grokcli-upstream-grokcli-2api-1` 后，Grok CPU 立即降为 0，主机 1 分钟 load 从 20+ 回落到约 2；New API 内外 `/api/status` 始终为 200。暂停保留了约 4 GiB 容器内存，这是预期行为。

### 2.2 API 时延分解

过去 24 小时生产 `[ttft]` 日志中有 76 个成功样本：

| 指标 | p50 | p90 | p95 | 最大值 | 结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| 总 TTFT | 9.83s | 31.17s | 53.55s | 117.91s | 尾延迟明显 |
| 本地处理 `local` | 170ms | 323ms | 1.12s | 62.72s | 常态较小，但资源失稳时出现灾难性排队 |
| 账号选择 `pick` | 150ms | 229ms | 290ms | 1.09s | 不是主要瓶颈 |
| 上游响应头 `up_hdr` | 1.77s | 2.24s | 2.54s | 3.56s | 相对稳定 |
| 响应头后首 token `up_tok` | 7.53s | 29.62s | 50.93s | 116.81s | 主要常态瓶颈在 GrokFree 上游生成/排队 |

附加观察：

- 76 个成功请求使用 39 个账号，14 个账号被重复使用。
- 按账号聚合后，重复账号的 `up_tok` 中位数从约 2.66s 到 49.23s，账号/上游质量差异显著。
- 依据日志时间减去 TTFT 重建请求区间，同账号重叠为 `0/76`，全局最大重叠仅 2。当前证据不支持“本地把并发都分到同一账号”是本轮慢的主因。
- 粘性请求的 TTFT 中位数约 6.84s，非粘性约 10.87s；当前不能通过关闭粘性来宣称优化。

## 3. 控制系统抽象

- **对象：** 两个 Uvicorn API worker、注册线程池、内联 Turnstile Solver、Camoufox 子进程、Redis/PostgreSQL 账号池和 GrokFree 上游。
- **控制器：** 单注册槽、单浏览器槽、零后台预取、Solver nice 优先级、容器资源上限、账号选择策略和有界部署 watchdog。
- **测量：** 主机 CPU/load/memory、cgroup `memory.events`/`cpu.stat`/线程数、Docker health、Solver pool 状态、注册终态、`local/up_hdr/up_tok/ttft`、并发成功率和账号重叠。
- **执行器：** 环境变量、安全默认值、Solver 进程调度优先级、注册批次调度、候选镜像和单服务 Compose 重建。
- **扰动：** GrokFree 上游排队、账号质量差异、浏览器断开/重建、OOM、CPU throttling、长 SSE、SSH 断开、并发生产请求和其它主机服务。
- **稳定性优先：** 先把后台负载收敛到单槽并证明 API 闭环；只有同账号在途冲突被真实复现，才增加跨 worker 调度状态。

## 4. GitHub 同类经验

1. Uvicorn 官方设置把 `workers`、`limit-concurrency` 和 TCP `backlog` 明确区分；提高 worker 不能消除 CPU 饱和，`limit-concurrency` 也会直接返回 503 而非排队。本轮保留 2 个异步 worker，不盲目增加进程：<https://github.com/encode/uvicorn/blob/7e11cc65f0642c823ef18ea01ff6b23af90aaa9e/docs/settings.md#L75-L82>、<https://github.com/encode/uvicorn/blob/7e11cc65f0642c823ef18ea01ff6b23af90aaa9e/docs/settings.md#L143-L148>。
2. Celery 对长任务建议每个 worker 只预取一个任务；长短任务混合时建议分开资源池/队列。本轮采用最小等价方案：注册单槽且额外预取为 0，避免后台长任务持续占位：<https://github.com/celery/celery/blob/d96df921e2e7bf4f520295e344b094952fe1a870/docs/userguide/optimizing.rst#L113-L142>。
3. Paperless-ngx 明确记录后台 worker/线程吃满 4 核会让交互响应变慢，并建议降低后台线程为其它任务保留算力；其默认后台 worker 也为 1：<https://github.com/paperless-ngx/paperless-ngx/blob/71557d7c648e68a74b2c9cd1d60d24e314ed4675/docs/setup.md#L636-L642>、<https://github.com/paperless-ngx/paperless-ngx/blob/71557d7c648e68a74b2c9cd1d60d24e314ed4675/src/paperless/settings/__init__.py#L672-L679>。
4. Envoy 的 least-request 直接使用 active request 数量选择上游；LiteLLM 将 least-busy 定义为选择当前进行中请求最少的部署。这是第二阶段跨 worker 账号在途调度的参考，不在没有复现时直接移植：<https://github.com/envoyproxy/envoy/blob/811e84a30d298e72c72b3e9f8353a80114107774/source/extensions/load_balancing_policies/least_request/least_request_lb.cc>、<https://github.com/BerriAI/litellm/blob/4d339648981ceb8c45df3081b388680084a2206d/litellm/types/management_endpoints/router_settings_endpoints.py>。
5. HTTPX 官方资源限制允许设置连接池和 keepalive；当前每 worker 200 连接、50 keepalive 已远高于实测并发，连接池容量不是首要瓶颈：<https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/docs/advanced/resource-limits.md>。
6. 当前 Turnstile 自愈已参考 `hmtxj/turnstile-solver-docker` 的 lifecycle/reinit 锁、槽状态和可观测反馈；本轮不重写已验证的浏览器池状态机：<https://github.com/hmtxj/turnstile-solver-docker/blob/e25dc140b70e59abcf22427af23899c04df5e693/api_solver.py>。

## 5. 第一阶段设计

### 5.1 注册与浏览器降载

- 将 `GROK2API_REG_CONCURRENCY` 的代码、Compose、启动脚本和示例默认值统一为 `1`。
- 将 `TURNSTILE_THREAD` 默认值统一为 `1`，生产 `.env` 明确设为 `1`；不再把浏览器槽默认等同于高注册并发。
- 将 `GROK2API_REG_PREFETCH_SLOTS` 默认值设为 `0`，单槽完成后才领取下一任务。
- 为内联 Solver 增加有界 `TURNSTILE_NICE`，默认 `10`，只接受 `0..19`；Camoufox 子进程继承该优先级，API worker 保持 nice 0。
- 不提高 `mem_limit=4g` 或 `cpus=2.50`，避免把局部 OOM 转为整机失稳。

### 5.2 API 前台能力

- 保持 `GROK2API_WORKERS=2`。两个 async worker、每 worker 200 个 HTTP 连接远高于当前最大实测并发 2；增加 worker 只会增加内存和调度竞争。
- 保持现有共享 `httpx.AsyncClient`、Redis 全局 RR、PostgreSQL/Redis 混合存储和会话粘性。
- 不在第一阶段修改账号缓存 TTL、连接池上限或 failover 语义。部署后用分段 TTFT 证明是否仍有本地瓶颈。

### 5.3 GrokFree 排队判定

第一阶段上线后执行两组不泄露密钥的轻量真实请求：

1. 单请求基线至少 5 次，记录 `local/up_hdr/up_tok/ttft`。
2. 5 路并发至少 2 轮，记录成功率、开始/首 token 区间、所选账号是否重叠、主机/容器资源。

只有同时满足以下条件才进入第二阶段：

- 不同请求在本地开始时间重叠；
- 两个或以上请求被选到同一账号；
- 同账号重叠样本的 `up_hdr` 或 `up_tok` 显著高于非重叠样本；
- 资源已稳定，排除 CPU/OOM 后仍能复现。

若触发第二阶段，设计为 Redis TTL 在途租约而不是永久计数：选择候选时按 `inflight -> EWMA TTFT -> 现有健康/轮询顺序` 排序；每次上游尝试建立有过期时间的租约，在响应完成、流取消、异常和 failover 的 `finally` 中释放，进程崩溃后由 TTL 自动恢复。粘性账号空闲时仍优先；粘性账号忙时是否换号必须通过上下文连续性测试后决定。

## 6. 验收指标

### 6.1 稳定性

- `grokcli-2api`、Solver、PostgreSQL、Redis、egress 全部健康；New API 内外 `/api/status` 连续 10 轮为 200。
- 新容器运行期间 `oom`/`oom_kill` 相对启动基线不增加，restart count 为 0。
- 自动注册运行时 Grok 容器内存持续低于 `3.2 GiB`，线程数目标低于 450，不能触达 4 GiB 上限。
- 主机 1 分钟 load 回落并稳定；5 分钟资源采样 CPU 不再达到 80% 告警阈值。若短时采样超过阈值，停止扩大并观察，不把瞬时值冒充稳态。

### 6.2 API 响应与并发

- `/health` 50 次、并发 10 的本地 p95 小于 250ms，失败为 0。
- 真实请求本地 `local` p95 小于 500ms，任一成功请求不得再出现大于 2s 的本地排队；若上游慢，必须能从 `up_tok` 单独看出。
- 5 路真实并发全部在 120s 有界窗口内得到首 token 或明确上游错误，不允许因本地锁/连接池无界等待。
- 对比旧基线 `local max=62.72s`；总 TTFT 只在相同上游条件下比较，不用不可控的 GrokFree 生成时间掩盖本地回归。

### 6.3 注册

- 健康快照显示注册并发 1、Solver 目标/拥有浏览器 1、driver manager 1。
- 运行 2 至 3 个小批任务，至少证明任务有界终止和账号可入池；外部邮箱/Turnstile/上游业务失败可以记录，但不能造成浏览器池死锁或 API 失联。
- 自动维护器继续能补量，不要求维持旧的约 5.5 个/分钟产量。

## 7. 测试矩阵

1. Shell 静态：`bash -n entrypoint.sh start.sh`；Compose 变量渲染和 `config --quiet`。
2. 配置：缺省值、非法/越界 `TURNSTILE_NICE`、显式覆盖值；确认不执行负 nice 或命令注入。
3. 注册：单并发、零预取、停止批次、不创建下一邮箱、终态计数。
4. Solver：现有 20 项池恢复测试全部通过；真实单槽 lazy warm、关闭、idle reclaim 和断开重建闭环。
5. API：账号选择/粘性/失败链原有测试；AST/编译、`git diff --check`。
6. 同构候选：生产镜像依赖、无业务网络的单槽 Camoufox 演练；确认 Solver 与子进程 nice 值。
7. 生产：健康、资源、单请求、5 路并发、小批注册、内外探针和严重日志。

## 8. 部署与回滚

### 8.1 部署

1. 获取专用部署锁，确认没有并发 Grok build/Compose/rsync。
2. 记录当前暂停容器 ID、镜像 ID、OOM/CPU/PID 基线、四个 Grok 服务身份和内外健康；备份 `.env`、Compose、修改源码及当前镜像摘要，不复制账号或密钥到日志。
3. 停止当前自动注册批次并有界等待在途任务归零。当前容器已暂停，若停止批次需要应用进程执行，先在隔离候选中验证，再通过重建而不是直接恢复失稳容器继续跑。
4. 旧容器保持暂停时构建版本化候选镜像；离线/同构验证通过后才切换。
5. 启动服务器端 60 秒 watchdog，只重建 `grokcli-2api`。45 秒内未 healthy，自动恢复旧 `.env`/Compose/镜像并只重建该服务。
6. 成功后执行全部验收，再更新生产标记和运维记录。

### 8.2 回滚

- 回滚恢复备份 `.env`、Compose 和修改源码，把固定旧镜像重新标记为生产镜像，并在同一个 watchdog 下只重建 `grokcli-2api`。
- PostgreSQL/Redis Volume、账号、当前代理配置和其它容器不回滚。
- 若旧镜像仍会立即资源失稳，回滚后保持自动注册关闭或并发 1；不得为了恢复旧吞吐重新启用 2/2。

## 9. 实施节点

- [x] 节点 1：暂停异常 Grok 容器，确认主站健康并完成资源/OOM/进程基线。
- [x] 节点 2：拆解 24 小时 TTFT、账号复用与请求重叠，区分本地争用和 GrokFree 上游尾延迟。
- [x] 节点 3：核对 Uvicorn、Celery、Paperless、HTTPX、Envoy/LiteLLM 与 Turnstile Solver 的 GitHub 生产经验。
- [x] 节点 4：建立完整计划、验收指标、条件分支和回滚边界。
- [x] 节点 5：先补测试/静态断言，再实现单注册槽、单浏览器槽、零预取和 Solver nice。
- [x] 节点 6：运行定向测试、全量相关回归、真实单槽 Camoufox 同构演练和差异检查。
- [x] 节点 7：提交并推送候选分支，建立生产备份和版本化镜像，执行候选验证。
- [x] 节点 8：有界切换生产，完成资源、API、5 路并发和不变性验收；最终候选只重建 API 容器，18 秒恢复健康。
- [x] 节点 9：依据真实同账号重叠反馈加入 Redis TTL 在途租约、按模型 EWMA 调度和有界陈旧快照，并完成普通/粘性并发验收。
- [x] 节点 10：功能提交已快进推送 `grok-auto/main`，唯一服务器运维手册已更新，临时 worktree、构建、探针和阶段镜像均按边界清理。

## 10. 当前结论

最终最小充分模型是“后台资源争用 + 账号池缓存击穿 + 同账号在途冲突 + GrokFree 账号速度差异”。闭环控制保持 2 个异步 worker，不用增加进程换取表面吞吐；后台注册/浏览器固定单槽，前台通过有界陈旧快照隔离后台写入，通过 Redis TTL 租约分散在途请求，再用按模型 EWMA 延迟反馈逐步偏向快账号。生产反馈已证明这套控制在 token maintainer 运行期间仍保持 `local` p95 小于 500ms，普通和粘性 5 路并发均无逻辑失败。

## 11. 实施反馈

### 11.1 节点 5

- 旧代码上的 3 组优先级契约测试先全部失败，分别覆盖注册/预取默认值、生产 Compose 变量和 Solver nice 启动。
- 实现后代码、Dockerfile、普通/生产 Compose、根入口、独立 Solver 入口和示例文档的默认值统一为注册 1、浏览器 1、预取 0、nice 10。
- `TURNSTILE_NICE` 只接受 `0..19`，显式按十进制归一化；非法值回落到 10，不允许负 nice 或把任意文本传入命令。
- 新增/既有相关测试当前为 33 项通过：API 优先契约 4 项、Turnstile 池恢复 20 项、prompt-cache/粘性 9 项。
- `bash -n`、Python 编译、Compose `config --quiet` 和 `git diff --check` 通过。本机 Docker daemon 不在线，真实进程 nice 与 Camoufox 单槽留给服务器隔离候选验证。

### 11.2 节点 6 至 8 的首次部署反馈

- 候选提交 `8638a35eb21fbd7bfde4ea63ccc73058648d2a98` 已推送至 `codex/grok-api-priority`，生产候选镜像为 `grokcli-2api:20260718-api-priority-8638a35`。
- 镜像内 24 项 API 优先/Turnstile 测试和 9 项 prompt-cache/粘性测试通过；隔离 Camoufox 候选为健康单槽，浏览器、driver manager、连接数均为 1，Solver/Camoufox 进程 nice 均为 10。
- 首次部署脚本在切换命令处失败：shell 函数 `compose` 被 `timeout` 当作外部可执行文件调用，返回 127；同一缺陷也阻断了自动回滚命令。候选代码、镜像和回归测试没有失败。
- 2026-07-18 00:30（Asia/Shanghai）已用版本化回滚镜像手动恢复 `grokcli-2api`；本地健康和外部 `/api/status` 均为 200，注册/浏览器为 1/1、预取为 0、worker 为 2，PostgreSQL、Redis 和 egress 容器身份保持不变。
- 原部署脚本已改为让 `timeout` 直接执行 `docker compose`。后续短切换复用已验证候选，只在 Compose 重建时中断 API，并继续保留 60 秒上限和旧镜像自动恢复路径。

### 11.3 节点 8 的首轮生产反馈

- 第二次切换成功，候选镜像于 2026-07-18 00:37（Asia/Shanghai）上线；只重建 `grokcli-2api`，切换耗时 19 秒。新 cgroup 的 `oom=0`、`oom_kill=0`，restart count 为 0，依赖容器身份未变。
- `/health` 50 次、并发 10 为 50/50 成功，但 p95 为 1.87 秒，未达到 250ms 指标；单路热态约 70–100ms，说明并发下存在重复深状态读取/缓存击穿，而不是网络不可达。
- 5 次单路真实 `grok-4.5` 请求全部成功，本地 `local=176–196ms`；`up_tok=148ms–14.85s`，总 TTFT 差异主要来自 GrokFree 上游。
- 第一轮 5 路真实并发全部成功并分散到 5 个账号，本地 `local=79–421ms`，上游 `up_tok=380ms–11.55s`。
- token maintainer 刷新 80 个账号后，第二轮 5 路并发的账号选择 `pick=285–722ms`、本地 `local=323–752ms`，超过 500ms 目标。当前实现会在账号写入后清空进程缓存；多个前台线程随后同时全表读取约 3,700 个账号，形成可重复的缓存击穿。
- 下一控制节点采用最小充分修正：请求路径使用有界陈旧快照承接后台刷新，并对快照重建做单飞/后台更新；健康接口避免并发重复深状态读取。另用固定会话 ID 做同账号重叠实验，只有上游排队被单独复现后才加入 Redis 在途租约。

### 11.4 同账号重叠反馈与第二阶段设计

- 先用固定会话 ID 完成单请求绑定，再发起 5 路并发；5 个请求都先命中同一粘性账号。结果为 4 个业务成功、1 个 HTTP 200 内的逻辑失败，出现 3 次 failover、多个 HTTP 200 空模型输出，最慢请求约 64 秒。
- 对照组两轮分散账号的 5 路并发均为 5/5 成功；因此“同一账号已有在途请求”与空输出/尾延迟存在可重复相关性，满足第二阶段触发条件。
- Redis 租约使用账号维度的原子 `SET NX + TTL`，不能使用无过期计数。每个请求预留一个互不重叠的有界 failover chain；一个进程只运行一个续租线程，避免每请求/每账号各起线程。
- 粘性账号空闲时仍排第一；粘性账号忙时临时使用全局轮询产生的空闲备选，但不改写原会话绑定。这样并发溢出不会永久迁移后续单路会话，且完整请求历史仍随本次请求发送。
- 正常响应、错误、客户端断流与生成器关闭均应幂等释放；进程崩溃或强杀由 TTL 回收。Redis 运行时异常采用可观测的无租约降级，不能把 Redis 短故障扩大为全部 API 不可用。
- 账号快照在后台 token 刷新时保留上一份只读快照并异步重建；首启仍同步预热。陈旧窗口只影响短时间备选集合，单个粘性账号仍做精确读取，账号禁用/上游失败仍由现有 failover 和 cooldown 收敛。

### 11.5 用户目标收敛与第二阶段验收

- 用户于 2026-07-18 明确本轮只保留两个结果目标：API 体感明显变快；API 支持多路并发访问。`/health` 深状态接口的并发延迟不再作为本阶段发布阻断项，除非它再次干扰聊天缓存。
- 聊天热路径修正包含三层：后台写账号时保留 60 秒有界只读快照并异步重建；Redis TTL 租约把并发请求分散到互不重叠的账号链；首 token 到达后写入按模型区分的 EWMA 延迟反馈。
- 非粘性请求 80% 从低 EWMA 窗口轮询、20% 全池探索；租约继续过滤已忙账号，因此快速反馈不会把并发重新集中到单个账号。Redis 失联时无租约降级，保留现有可用性。
- 发布门槛：token maintainer 刷新期间真实请求 `local` p95 小于 500ms；两轮 5 路并发均无 HTTP/SSE 逻辑失败；固定会话 5 路并发不再同时占用同一账号；反馈预热后的 TTFT 中位数低于本轮旧基线 9.83 秒，且分段日志能证明剩余等待来自上游而非本地锁。

### 11.6 最终生产闭环

- 最终功能提交为 `7a76e352e63f3b99ec34522d9485732dfc9d3a14`，镜像 `grokcli-2api:20260718-fast-concurrency-7a76e35`（ID `sha256:0ca70699a5aafdd01cb90292c6d2db6e1bd166d1634476865aadb0dbb35a57a0`）。35 项并发/缓存定向测试和 9 项 prompt-cache/粘性测试通过后才进入生产。
- 2026-07-18 02:33（Asia/Shanghai）只重建 `grokcli-2api`，18 秒恢复 healthy；PostgreSQL、Redis 和 egress 容器身份不变。最终 cgroup `oom=0`、`oom_kill=0`，restart count 为 0；压力后内存约 618MiB / 4GiB、PID 54。
- 完整 SSE 业务探针共 22/22 成功，均包含实际模型内容、finish frame 和 `[DONE]`，没有把 HTTP 200 内逻辑错误记为成功。5 次顺序请求的客户端首模型内容中位数约 1.46 秒；全部样本服务端 TTFT 中位数约 1.60 秒，显著低于旧基线 9.83 秒。
- 两轮普通 5 路并发均为 5/5 成功，每轮使用 5 个不同账号；固定会话种子后的 5 路并发也是 5/5 成功，并由原粘性账号和 4 个临时溢出账号承接。并发结束后的单路请求重新命中原粘性账号，证明溢出没有改写长期绑定。
- 22 个请求的服务端 `local` p95 约 261ms、最大 453ms，达到小于 500ms 的发布门槛；剩余 TTFT 主要是约 1.1 至 1.5 秒的上游响应头等待，而非本地锁或连接池排队。
- 验收期间 token maintainer 实际完成刷新/删除周期，第二轮并发未重现旧版缓存击穿。请求结束后 Redis `account_inflight` 租约键为 0；公网 `/api/status` 最终 10/10 为 200。
- 成功回滚目录为 `/home/deploy/grok-backups/20260718T023336-7a76e35-fast-concurrency`，旧镜像固定标签为 `grokcli-2api:rollback-20260718T023336-7a76e35-fast-concurrency`。回滚只恢复该目录中的源码和 `.env.before`，并只重建 `grokcli-2api`；不得回滚 PostgreSQL/Redis Volume、账号数据或其它依赖服务。

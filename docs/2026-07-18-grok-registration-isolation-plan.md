# Grok 注册资源硬隔离与渐进恢复计划

**日期：** 2026-07-18

**状态：** 计划已建立，尚未修改生产代码

**代码基线：** `ebd59fdf3c2cfe97ebf9237b251ad8730e1cdbe1`

**实施分支：** `codex/grok-registration-isolation`

**生产对象：** `/home/deploy/grokcli-upstream-v1947` 的 `grokcli-2api` 与拟新增 `grok-registration`

## 1. 目标与性能指标

本轮只解决两个控制目标，并按稳定性优先排序：

1. Grok API 在账号注册启动、失败、浏览器回收或注册容器 OOM 时仍保持可用，不能再与注册任务共享故障域。
2. 账号注册以极低占空比逐步恢复，只使用明确受限的剩余资源，不追求注册吞吐。

硬指标：

- API 容器不再启动 Turnstile/Camoufox，也不运行自动注册维护器。
- API 容器硬上限为 2 CPU、2 GiB、256 PIDs；注册容器硬上限为 0.5 CPU、1.5 GiB、256 PIDs。两者合计最多 2.5 CPU、3.5 GiB，低于旧单容器 2.5 CPU、4 GiB 边界，并把 OOM 隔离到注册侧。
- 注册并发固定为 1、额外预取为 0、批次大小为 1；每个尝试结束后至少休息 600 秒，首轮不提高。
- 注册活跃时 5 路真实 API 请求必须 5/5 业务成功，服务端 `local` p95 小于 500ms；外部 `/api/status` 连续 10 轮为 200。
- API 容器 `oom/oom_kill=0`、restart count 为 0；注册容器即使失败或 OOM，也不得引起 API 容器重建、健康下降或 Redis/PostgreSQL/egress 重启。

## 2. 故障复盘与根因

2026-07-17 23:15（Asia/Shanghai）的故障证据：

- `grokcli-2api` 达到约 241% CPU、4 GiB / 4 GiB、Docker 760 PIDs；主机 load average 为 `22.78 / 20.67 / 14.06`。
- cgroup 累计 `oom=71`、`oom_kill=22`、内存上限事件 `12,698,224` 次、CPU throttled period `363,787` 次。
- 容器内同时存在两套 Camoufox 浏览器/扩展进程、注册 Python worker 和两个 API worker；生产配置为注册并发 2、浏览器槽 2、API worker 2，共享同一个 4 GiB / 2.5 CPU cgroup。
- 暂停该 Grok 容器后，其 CPU 立即归零，主机 1 分钟 load 从 20+ 回落到约 2；New API 主站和数据库并未失效。磁盘仅约 47%，不是本次根因。

因果链为：自动注册大批次持续派发 -> 两套浏览器进程树并行膨胀 -> 共享 cgroup 内存触顶且 CPU/PID 饱和 -> OOM 与长时间 throttling 同时打击 API worker -> Grok API unhealthy，并把整机调度拖入失稳区。

上一轮把注册/浏览器降到 1/1、预取降到 0、浏览器 nice 调到 10 后，API 已恢复；但真实单槽注册仍测得约 2.05 GiB、220.8% CPU、230 PIDs。`nice` 只影响 CPU 调度优先级，不限制内存，也不能阻止同 cgroup OOM，因此单槽仍不是“不再复发”的充分保证。

## 3. 控制系统抽象

- **对象：** API 双 worker、账号池、注册维护器、Turnstile Solver、Camoufox、Redis/PostgreSQL、Grok egress 和宿主机 4 核 / 8 GiB 资源。
- **前台控制器：** API 独立 cgroup、2 CPU / 2 GiB / 256 PID 上限、Redis 账号租约和延迟调度。
- **后台控制器：** 注册独立 cgroup、0.5 CPU / 1.5 GiB / 256 PID 上限、单任务批次、600 秒休息、nice 10、浏览器空闲回收。
- **测量：** 两个 cgroup 的 `memory.events`、CPU、内存、PIDs、restart/health；注册 batch/inflight/终态；API `local/up_hdr/up_tok/ttft` 和业务成功率；主机 load/available memory。
- **执行器：** Compose 服务边界、环境变量、注册批次停止入口、独立 worker 进程、部署 watchdog。
- **扰动：** 浏览器内存尖峰、Turnstile/邮箱超时、上游撤销 token、真实 API 并发、注册进程 OOM、Redis 短故障、SSH 断开。
- **稳定性原则：** 注册容器可以慢、失败或停止，API 容器不能因此失败；先证明单次低占空比闭环，再考虑扩大频率。

## 4. GitHub 同类经验

1. Celery 对长任务建议每个 worker 一次只预取一个任务；长短任务混合时应拆分不同 worker 节点。本轮对应为 API 与注册分容器、注册批次 1、预取 0：<https://github.com/celery/celery/blob/d96df921e2e7bf4f520295e344b094952fe1a870/docs/userguide/optimizing.rst#L113-L142>。
2. Paperless-ngx 记录后台 worker/线程吃满 4 核会让交互请求变慢，并建议显式降低后台并发以给前台留算力：<https://github.com/paperless-ngx/paperless-ngx/blob/71557d7c648e68a74b2c9cd1d60d24e314ed4675/docs/setup.md#L636-L642>。
3. Compose 规范分别提供 `cpus`、`mem_limit` 和 `pids_limit`，它们是本轮建立独立故障域的硬执行器，而不是依赖进程自觉让路：<https://github.com/compose-spec/compose-spec/blob/master/05-services.md#cpus>、<https://github.com/compose-spec/compose-spec/blob/master/05-services.md#mem_limit>、<https://github.com/compose-spec/compose-spec/blob/master/05-services.md#pids_limit>。
4. 现有 Turnstile 池生命周期继续沿用已验证的 lazy warm、idle reclaim 和断开重建，不重写浏览器状态机：<https://github.com/hmtxj/turnstile-solver-docker/blob/e25dc140b70e59abcf22427af23899c04df5e693/api_solver.py>。

## 5. 最小充分设计

### 5.1 立即止损

- 使用现有 `stop_all_active_registrations()` 协作式停止当前批次，不停止 API、不杀数据库、不直接杀浏览器进程。
- 有界等待当前 batch/session 进入终态；再等待 lazy solver 在 180 秒内回收浏览器。
- 若协作停止超时，只停止注册相关子进程；不得暂停或停止整个 API 容器。正式切换前保持自动注册不再领取新任务。

### 5.2 API 容器

- `GROK2API_REG_AUTO_MAINTAIN=0`，`GROK2API_INLINE_SOLVER=0`，禁止 API 容器创建 Camoufox。
- 保持两个 Uvicorn worker和现有 Redis/PostgreSQL/账号调度逻辑。
- 资源上限改为 2 CPU、2 GiB、256 PIDs；这不是资源预留，空闲时仍只消耗实际使用量。
- API 不依赖注册容器健康；注册容器停止时聊天和 Responses/Anthropic 路径继续工作。

### 5.3 独立注册容器

- 新增 `registration_worker.py`，只启动注册维护器、发布心跳并处理 SIGTERM；不启动 FastAPI/Uvicorn、token maintainer 或 model health。
- 复用同一镜像和 entrypoint，在独立容器内启动单槽 Turnstile Solver；只连接共享 Redis/PostgreSQL/egress，不映射宿主机端口。
- 固定 `batch_size=1`、`concurrency=1`、`prefetch=0`、`rest=600s`、`monitor=600s`、`startup_delay=60s`、`TURNSTILE_IDLE_SEC=60`、`TURNSTILE_NICE=10`。
- 硬上限为 0.5 CPU、1.5 GiB、256 PIDs、512 MiB shm；`restart: "no"`，任何退出都保持停止，避免 OOM 后出现重启风暴。
- 停止时先标记在途注册取消，再退出；进程崩溃时依靠现有 Redis batch runner TTL 收敛，不创建第二个并发任务。

### 5.4 可观测与停止条件

- worker 每 5 秒写有 TTL 的 Redis 心跳；健康检查同时验证主循环心跳和 Solver HTTP。
- 任一条件触发立即停止注册容器并保留 API：注册 cgroup OOM、restart 增加、内存持续超过 1.35 GiB、PID 超过 220、API `local` 超过 500ms、主机 load 持续高于 4 或 available memory 低于 2 GiB。
- 首轮只证明 1 个注册尝试有界完成和浏览器回收；连续 3 个周期都稳定后才允许保持常驻，仍不提高批次、并发或 CPU/内存上限。

## 6. 测试矩阵

1. 先新增失败的隔离契约测试：API 关闭自动注册/内联 Solver；注册服务无公开端口且具有精确 CPU、内存、PID、批次和休息上限。
2. worker 单元测试：缺少共享存储时拒绝启动；启动/心跳/停止；SIGTERM 有界退出；注册线程异常导致进程失败而不是静默假健康。
3. 现有回归：API 优先、注册停止、Turnstile 池恢复、账号并发租约、prompt-cache/粘性测试。
4. 静态与配置：Python 编译、`bash -n`、Compose `config --quiet`、`git diff --check`。
5. 服务器同构候选：独立注册容器启动但先禁用真实批次，验证 0.5 CPU / 1.5 GiB / 256 PID cgroup、无宿主端口、API 容器无浏览器进程。
6. 生产闭环：API 切换后先做 5 路并发，再单独启动注册 worker；注册活跃时再做 5 路并发并连续采样资源，随后确认浏览器空闲回收。

## 7. 部署顺序

1. 获取 Grok 部署锁，确认无并发 build/Compose；记录所有相关容器身份、API 指标和 cgroup 基线。
2. 停止当前注册批次并等待浏览器回收，期间持续检查 API，不重启服务。
3. 备份 `.env`、Compose、修改源码、当前镜像 ID和依赖容器身份；构建带提交标签的不可变候选镜像。
4. 离线测试与独立候选通过后，在 60 秒 watchdog 下只重建 API 容器；新配置必须确认没有 Solver/Camoufox/注册线程。
5. API 连续健康并完成 5 路业务并发后，单独启动注册容器。先观察 1 个任务完整结束、资源回落和 600 秒 resting 状态。
6. 连续 3 个低占空比周期均通过才留下常驻；否则停止注册容器，API 保持运行，回到测量结果修正。
7. 更新本计划和唯一服务器运维手册，提交并普通 fast-forward 推送 GitHub `main`，清理临时发布工件。

## 8. 回滚

- 注册侧回滚永远优先执行 `docker compose stop grok-registration`；这不影响 API。
- API 候选失败时恢复旧镜像与源码，但环境仍强制 `GROK2API_REG_AUTO_MAINTAIN=0`、`GROK2API_INLINE_SOLVER=0`，避免回滚重新引入浏览器共享故障域。
- 只重建 `grokcli-2api`；不得重启或回滚 PostgreSQL、Redis、egress、New API、Caddy、Sub2API 或其它服务。
- 任一等待都有 60 秒硬上限；自动回滚失败时保持注册停止并进入人工恢复，不能无界卡住。

## 9. 实施节点

- [x] 节点 1：还原事故证据，确认根因是注册浏览器与 API 共用 cgroup 导致 OOM/CPU/PID 饱和。
- [x] 节点 2：复核 GitHub 长任务隔离、前台留资源和 Compose 硬限制经验。
- [x] 节点 3：建立完整计划、指标、停止条件、部署与回滚边界。
- [x] 节点 4：协作停止当前大批次并确认 API 持续可用、浏览器有界回收。
- [x] 节点 5：先补隔离/worker 测试，再实现独立注册 worker 与 Compose 资源边界。
- [x] 节点 6：完成定向回归、Compose/静态验证和服务器同构候选。
- [x] 节点 7：有界切换 API 容器，验证无浏览器、无注册线程和 5 路并发。
- [ ] 节点 8：启动低占空比注册 worker，完成至少 3 个周期的资源与 API 闭环。
- [ ] 节点 9：更新运维手册、快进合并 GitHub `main`，清理临时工件并保留最终回滚点。

## 10. 实施反馈

### 10.1 节点 4：当前批次止损

- 通过现有协作式停止入口取消 12 个仍活跃会话；当前批次从 `running/inflight=1` 收敛为 `cancelled/inflight=0`，没有停止或重建 API 容器。
- 停止期间公网 `/api/status` 5/5 为 200。Camoufox 与 Web Content 在 idle 窗口内退出，容器内存从约 2 GiB 回落到约 980 MiB、PIDs 从 200+ 回落到约 60。
- 内联 Solver forkserver 仍常驻且产生 CPU/内存开销；这不是继续注册，而是旧单容器结构的残留，节点 7 切换为 `GROK2API_INLINE_SOLVER=0` 后才会彻底消失。

### 10.2 节点 5：测试先行

- 新增 5 项初始隔离测试，旧代码表现为 1 个失败和 4 个错误：API 仍允许自动注册/内联 Solver、缺少独立注册服务和 worker 资源熔断入口。
- 实现后隔离/熔断 8 项、账号并发 11 项、Turnstile 恢复 20 项、原 API 优先 4 项，共 43 项通过；Python 编译、Shell 静态和 Compose `config --quiet` 也通过。prompt-cache 测试仅因本机缺少仓库已声明的 `python-multipart` 依赖而无法导入，将在依赖完整的候选镜像内补跑。

### 10.3 节点 6：镜像与同构 canary

- 不可变候选为 `grokcli-2api:20260718-registration-isolation-cc205a0`，镜像 ID `sha256:201c883b4111ff5593eabbc6f8fc86f9a1db54abcd5d8fec1e36e93e3a1b2cec`，revision 标签精确指向 `cc205a075e55ddba990c8afd0e080803e85f7f5e`。
- 候选镜像内 43 项 unittest 和 9 项 prompt-cache 函数测试全部通过，共 52 项；Compose 生产渲染通过。
- 目标设为 1 的无真实注册 canary 达到 healthy：0.5 CPU、1.5 GiB、256 PID 硬限制和 `restart=no` 均与设计一致，无宿主端口且只连接内部网络；空闲使用约 171 MiB、10 PIDs，cgroup `oom=0/oom_kill=0`。
- canary 启动日志确认 worker 自检为 `batch=1/concurrency=1/rest=600s`，Redis 心跳可见；停止后心跳自动清除。整个验证期间公网 `/api/status` 连续为 200，canary 随后已删除。

### 10.4 节点 7：API 独立故障域上线

- 2026-07-18 03:38（Asia/Shanghai）只重建 `grokcli-2api`，14 秒恢复 healthy，注册服务未创建；PostgreSQL、Redis 和 egress 身份不变，未触发回滚。
- API cgroup 精确为 2 CPU、2 GiB、256 PIDs；环境为 `GROK2API_INLINE_SOLVER=0`、`GROK2API_REG_AUTO_MAINTAIN=0`。进程表只有主进程和两个 API worker，没有 Solver、forkserver、Camoufox 或 Web Content。
- 空闲资源从旧结构约 980 MiB / 59 PIDs 降为约 351 MiB / 34 PIDs。5 路真实 SSE 为 5/5 业务成功并使用 5 个不同账号，服务端 `local=111–348ms`、客户端首模型内容中位数约 1.79 秒；压测后约 392 MiB / 47 PIDs，cgroup `oom=0/oom_kill=0`。
- 成功备份为 `/home/deploy/grok-backups/20260718T033823-cc205a0-registration-isolation`；旧 API 镜像已固定为同 run ID 的 rollback 标签。即使回滚，安全 Compose 仍强制关闭 API 内自动注册和内联 Solver。

### 10.5 节点 8：首轮真实注册反馈与批次契约修正

- 首轮 worker 在真实注册开始后暴露既有契约缺陷：`start_registration(count=1)` 走单会话模式，只返回 session `id`；维护器只读取 `batch_id`，因此记录空 ID并在循环中重复发起了 5 个单会话。检测后立即停止注册容器，心跳清除，容器保持 exited 且 `restart=no`；API 全程持续 200。
- 资源隔离仍按设计生效：错误扩散期间注册 cgroup 峰值约 1.13 GiB / 171 PIDs、CPU 被限制在约 50%，API 约 0.55 GiB / 53 PIDs，两个 cgroup 均 `oom=0/oom_kill=0`。浏览器活跃时 5 路真实 API 为 5/5 业务成功并使用 5 个不同账号，服务端 `local=103–206ms`。
- 修正保持后台/管理页面单会话兼容，只为自动维护器增加 `force_batch=True`，让单次尝试仍创建持久 batch id；缺失 batch id 时立即停止全部活跃会话并进入 start_error，不能继续循环派发。
- 同时修复 start_error 分支重复传入 `last_error` 的异常，并让停止全部注册直接跳过已终态历史会话，缩短 SIGTERM 清场时间。新增 5 项回归后隔离/批次测试 13 项通过，相关 unittest 合计 48 项通过；注册保持停止，等待第二候选镜像。
- 第二候选首次镜像回归发现新增测试污染验证码 provider 全局状态，导致后续 Turnstile fallback 用例失败；生产未改动。测试已改为同时恢复 `os.environ` 与 adapter 全局变量，按实际发现顺序运行的 48 项回归重新全部通过，不绕过失败结果。

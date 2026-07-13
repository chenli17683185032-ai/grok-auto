# Grok 注册流水线第三轮修复任务书

日期：2026-07-13
状态：待开发 Agent 执行，完成后由主验收 Agent复验
适用目录：`/Users/ethan/Documents/grok`

## 1. 目标与结论口径

本轮不是继续写方案，而是修复第二轮验收中已稳定复现的生产阻断问题，使 Pipeline v2 至少具备以下能力：

1. Producer 在注册成功并完成 Mint Queue 交接后立即释放注册槽位，不等待 1800 秒。
2. Mint Worker 遇到 denied、空 Token、达到最大重试次数或意外异常时不会退出。
3. 任意 Worker 崩溃后，任务可由新 Worker 安全接管；过期旧 Worker 不得覆盖新状态。
4. 注册、Device Code、浏览器审批、Token Poll、Probe 全部使用同一个 Route 出口。
5. Pending、Queue 和 Legacy Recovery 只有一个明确所有者，不重复消费，也不产生无人处理的孤儿。
6. Cookie A/B 配置真正进入生产入队路径。
7. 日志、API、诊断文件和认证文件不暴露密码、SSO、Token、YesCaptcha Key 或代理密码。
8. 所有交付文件可以从根仓库 clean clone 后完整构建和回滚。

资源口径已由用户调整：Pipeline v2 Compose 合计 `2.50 CPU` 可以接受，不再要求压回 `2.40 CPU`。必须把部署文档改成真实数值，但资源偏差不再作为验收阻断项。

## 2. 工作和交付位置

正式任务书：

```text
/Users/ethan/Documents/grok/docs/GROK_REGISTRATION_ROUND3_REMEDIATION_TASKS.md
```

源码仍在原模块内修改，不创建第二套实现：

```text
/Users/ethan/Documents/grok/registration_producer.py
/Users/ethan/Documents/grok/grok_build_adapter.py
/Users/ethan/Documents/grok/registration_controller.py
/Users/ethan/Documents/grok/registration_queue.py
/Users/ethan/Documents/grok/registration_jobs.py
/Users/ethan/Documents/grok/registration_metrics.py
/Users/ethan/Documents/grok/route_registry.py
/Users/ethan/Documents/grok/sso_to_auth_json.py
/Users/ethan/Documents/grok/model_health.py
/Users/ethan/Documents/grok/auth_store.py
/Users/ethan/Documents/grok/accounts.py
/Users/ethan/Documents/grok/cookie_bundle.py
/Users/ethan/Documents/grok/docker-compose.server.yml
/Users/ethan/Documents/grok/integrations/
```

新增回归测试统一放在：

```text
/Users/ethan/Documents/grok/tests/
```

本轮只维护一个修复报告，不再新建零散报告：

```text
/Users/ethan/Documents/grok/test-output/acceptance-fix-round3.md
```

同步更新现有部署手册：

```text
/Users/ethan/Documents/grok/SERVER_DEPLOYMENT.md
```

## 3. P0：修复 Producer 的 Pipeline v2 交接语义

当前 `mint_queued` 已表示“协议注册阶段完成，SSO 已可靠交给 Mint Queue”，但 Producer 仍只等待 `imported/error`，导致默认每批等待 1800 秒。

实现要求：

1. 为注册阶段结果定义明确的结构，例如 `imported`、`errors`、`mint_queued`、`signup_done`，不要继续用含义不清的二元 tuple。
2. `_wait_batch()` 在 `batch_status=done`、`running=0` 且 `imported + errors + mint_queued >= total` 时立即返回。
3. 默认单账号路径必须把 `mint_queued` 视为注册阶段成功交接，立即释放 Producer 槽位。
4. `mint_queued` 不能计入 `imported_lifetime`，不能当作账号已入池，也不能增加自适应成功连胜。
5. Producer 下一轮注册数量必须考虑 Queue 中尚未入池的 open jobs，避免账号池 gap 未变化时无限超量注册。
6. Legacy `PIPELINE_V2=0` 路径保持原来的 `imported/error` 语义。

必须增加测试：

```text
test_pipeline_v2_batch_handoff_returns_without_timeout
test_pipeline_v2_single_session_accepts_mint_queued
test_mint_queued_not_counted_as_imported_lifetime
test_legacy_inline_wait_semantics_unchanged
```

测试必须使用和真实管理 API 一致的响应结构，禁止通过修改断言规避 `mint_queued`。

## 4. P0：修复 Mint Worker 终局状态和异常边界

当前空 Token 会触发非法的 `mint_running -> dead_letter` 转换，异常再次逃出后终止 Worker。

实现要求：

1. 明确定义每个运行态到 `FAILED/DEAD_LETTER` 的合法路径；不能依赖非法转换触发容器重启。
2. denied、空 Token、永久错误、达到最大尝试次数都必须清租约并进入确定的终局状态。
3. 可重试错误必须回到 `mint_queued`，保留 SSO，并设置 `next_run_at`。
4. `claim_and_process_once()` 和 `run_forever()` 必须有最后一道异常隔离；单个坏 Job 不能退出整个 Worker。
5. 未知异常应记录脱敏后的错误类别，不得把异常原文中的 Token、Cookie、Key 写入数据库或日志。
6. 终局处理必须幂等；重复执行不能产生非法状态转换。

必须增加测试：

```text
test_empty_token_moves_to_terminal_without_worker_crash
test_denied_does_not_exit_worker_loop
test_retry_exhaustion_reaches_dead_letter
test_unexpected_exception_isolated_to_one_job
test_terminal_transition_clears_lease
```

## 5. P1：补全 Queue 租约、崩溃恢复和 fencing

只修复原子 Claim 不够。当前 `probe_running` 无法重领，旧 Worker 也可以在租约过期后覆盖新 Worker。

实现要求：

1. 为所有可能落库的运行态定义崩溃恢复策略，至少覆盖 `mint_running`、`token_received`、`probe_running`、`probe_passed`。
2. 因 Token 没有持久化，恢复上述状态时可以安全地重置为 `mint_running`，从 SSO 重新执行 Mint；必须记录重放次数。
3. 增加租约续期/heartbeat，长时间浏览器审批、限流等待、Token Poll 和 Probe 期间持续续租。
4. 增加 fencing token 或 lease generation。所有状态保存必须带 `job_id + lease_owner + generation` 条件；过期旧 Worker 的写入必须返回失败，不能覆盖新 Owner。
5. Worker 发现续租或 fenced save 失败后立即停止处理该 Job，不能继续 Import。
6. `enqueue()` 的 hard-limit 检查与插入必须在同一 SQLite 写事务中完成。

必须增加真实多进程测试，而不只是线程测试：

```text
test_probe_running_reclaimed_after_expired_lease
test_stale_worker_cannot_overwrite_new_owner
test_lease_heartbeat_prevents_early_reclaim
test_concurrent_enqueue_respects_hard_limit
test_multiprocess_claim_exactly_once
```

多进程压力测试最低标准：8 个进程、1000 个 Job、0 重复、0 遗漏、0 stale overwrite。

## 6. P1：把背压移到注册开始之前，并消除孤儿 Pending

实现要求：

1. Producer 在调用注册 API前读取 Queue 深度。可以直接读取共享 SQLite，但必须复用 `RegistrationQueue` API，不得自己拼 SQL。
2. `open < soft`：按正常自适应并发运行。
3. `soft <= open < hard`：强制注册并发降为 1，并按剩余 Queue 容量限制 batch size。
4. `open >= hard`：暂停新注册，只等待 Mint Worker 排空，不能先注册再抛错。
5. `requested_count` 至少取 `账号池 gap`、`batch size`、`Queue剩余容量` 三者的最小值。
6. `enqueue_after_sso()` 必须具备补偿事务：Queue 写入失败时，不能留下 `owner=mint_queue` 且无 Job 的 Pending。
7. 增加启动修复扫描：发现 `owner=mint_queue` 但没有 active Job 的文件时，自动重新入队或转交 Legacy Recovery；不得永久跳过。
8. Pending 写入、Owner 变更和恢复都必须为 `0600` 原子替换。

必须增加测试：

```text
test_producer_pauses_before_signup_at_hard_limit
test_soft_limit_forces_single_registration
test_queue_failure_leaves_no_mint_owned_orphan
test_startup_repairs_orphan_pending
```

## 7. P1：Pending 只能有一个消费者，并提供迁移/回滚

实现要求：

1. 默认 Compose 只能有一个 Legacy Pending 消费者。建议保留独立 `pending-recovery`，并将主 `registration-producer` 的恢复开关设为 `0`。
2. 即使只保留一个服务，Pending 处理仍需文件级原子 Claim，例如原子 rename 到 processing 名称或 `flock`。
3. 处理成功后删除；可重试失败原子恢复原名并写 backoff；进程崩溃后能回收过期 processing 文件。
4. `owner=mint_queue` 只能在确有 active Queue Job 时被 Legacy 跳过。
5. Pipeline v2 关闭或回滚时，已有 `owner=mint_queue` 文件必须自动迁移到 Legacy，不能永久跳过。
6. `RegistrationQueue.import_pending_json()` 必须接入真实启动/迁移入口，或者删除它并实现等价的生产迁移；不能继续只在测试中存在。

必须增加测试：

```text
test_two_recovery_processes_consume_legacy_once
test_processing_file_recovered_after_crash
test_pipeline_rollback_recovers_mint_owned_pending
test_migration_is_idempotent
```

## 8. P1：完成全链路 Route Affinity

要求同一个 `route_id` 覆盖以下全部阶段：

```text
register
SSO validation
device/code
browser approver
token poll
model probe
```

实现要求：

1. `request_device_code()` 增加显式 `proxy` 参数，并使用已有 `_open_url(..., proxy=...)`。
2. Token Poll 保持 per-request `ProxyHandler`，禁止恢复修改进程级 `os.environ`。
3. Probe 增加显式 Route Proxy。`httpx.Client` 应使用该代理并禁用不受控的环境代理继承；按项目锁定的 httpx 版本选择正确参数并添加测试。
4. Route 2 的 Device Code、Token Poll 和 Probe 均不得回落到容器全局 Route 1。
5. `ROUTE_STICKY=0` 时不得在 SSO 后擅自把一半任务分配到 Route 2；应保持 Legacy 默认出口语义。
6. 仅传输故障可在同 Route 内重新申请 Device Code；业务 denied 不跨 Route 重放。

必须增加端到端代理选择测试：

```text
test_route2_register_device_poll_probe_all_use_route2
test_route_sticky_off_keeps_legacy_route
test_no_process_environment_proxy_mutation
```

测试可 Mock 网络响应，但必须断言每一个实际网络边界收到的代理，不得只测试 `_open_url` helper。

## 9. P1：让 Cookie A/B 真正进入生产路径

实现要求：

1. `enqueue_after_sso(cookie_mode=...)` 使用 `None` 或明确 sentinel 表示“由实验解析”，不能默认用非空 `sso_only` 绕过 resolver。
2. 未显式覆盖时必须调用 `resolve_mode_for_session(session_id)`。
3. `0%/10%/25%/50%/100%` 分布和稳定性都要测试；100% 必须生成 `job.cookie_mode=auth_bundle`。
4. Sidecar 只能读取 `/app/data/cookie_bundles` 下的普通文件，必须使用 `resolve()` 做根目录包含检查并拒绝目录 symlink 绕过。
5. `extra_cookies` 必须复用同一白名单；Cookie domain 只能是 `x.ai`、`.x.ai` 或明确允许的 xAI 子域，禁止任意 domain。
6. 成功和所有终局失败均删除 Cookie Bundle；可重试任务保留到下一次尝试。
7. 增加周期性 TTL 清理器，过期文件不能依赖“下次被读取”才删除。
8. Warm Browser 继续保持默认关闭；在真实上下文隔离测试通过前不得声称已可灰度。

必须增加测试：

```text
test_pipeline_enqueue_honors_cookie_experiment
test_sidecar_rejects_bundle_outside_root
test_inline_cookie_uses_allowlist_and_xai_domain
test_terminal_failure_deletes_bundle
test_ttl_sweeper_deletes_unread_expired_bundle
```

## 10. P0：彻底移除凭据持久化、日志和API泄漏

实现要求：

1. 注册主链不得调用 `fetch_sso_token(..., save=True)`。
2. 注意 vendored 客户端当前只要传非空 `email` 也会保存文件；调用时不得传用于保存的 email/password，或者修正客户端为只有显式 `save=True` 才保存。
3. 生产默认禁止打印 RSC Body、Set-Cookie URL、完整或部分 SSO、Access Token 前缀、YesCaptcha Key、代理密码。
4. 删除无条件 RSC 诊断文件写入。若必须保留临时诊断，必须受默认关闭的独立 Flag 控制、先脱敏、文件 `0600`、目录 `0700`、有明确 TTL，并且报告中不得含内容。
5. `XConsoleAuthClient` 生产默认 `debug=False`。
6. `_compact_session()` 必须递归脱敏字符串值，而不只是删除字段名；异常文本已知可能包含 YesCaptcha Key。
7. 单 Session Getter 必须复用 `_compact_session()`，不得返回完整 Proxy、Token 前缀或原始错误。
8. 注册状态 API 不再提供 `include_auth_json=1` 的秘密导出能力；账号导出继续走已有、专门鉴权的 Auth Export 接口。
9. 对现有 `data/register_sso` 和协议 `sso_output` 提供一次性安全清理/迁移命令，但不得在单元测试中读取或打印真实内容。

必须增加测试：

```text
test_registration_never_calls_save_sso
test_session_list_and_detail_redact_nested_secrets
test_error_text_redacts_captcha_and_proxy_password
test_production_logging_contains_no_jwt_or_sso_prefix
test_raw_rsc_not_written_by_default
```

## 11. P0：统一认证文件安全写入和已有权限迁移

实现要求：

1. 建立一个统一的原子 JSON 安全写入函数，供 `write_auth_map()`、`mutate_auth_map()`、CLI `write_auth_json()`、`merge_auth_json()` 和备份路径复用。
2. 临时文件从创建时就是 `0600`，不是 `os.replace()` 后才 chmod。
3. `auth.json`、临时文件和 `auth.bak.*` 均为 `0600`，父目录为 `0700`。
4. 自动 Token 续期写回后权限必须仍为 `0600`。
5. 服务启动时对已有 `auth.json` 和备份执行幂等权限迁移；只改权限，不改 JSON 内容。
6. chmod 失败必须记录不含路径秘密的明确错误；认证写入不能静默以宽权限继续。

必须增加测试：

```text
test_write_auth_map_mode_600
test_mutate_auth_map_mode_600
test_cli_auth_writers_mode_600
test_refresh_writeback_preserves_mode_600
test_existing_auth_and_backups_permission_migration
```

所有权限测试使用临时目录，禁止读取、复制或打印真实 `data/auth.json` 内容。

## 12. P1：建立可复现 Git 和 Docker 交付边界

当前 `integrations/ruyipage` 是嵌套 Git 仓库，根仓库 clean clone 无法得到本地适配文件。

推荐方案：建立不含内层 `.git` 的最小运行时 Vendor 目录，例如：

```text
integrations/ruyipage-runtime/
```

只包含：

```text
LICENSE
pyproject.toml
ruyipage/
device_approver.py
Dockerfile.headless
必要的requirements文件
```

然后更新 Compose Build Context。原始嵌套仓库可以保留为本地参考，但不得作为生产 Build Context，也不得依赖其未提交文件。若开发 Agent拥有可提交的独立 Fork，也可以改成正式 submodule，但必须同时提交 `.gitmodules` 和包含适配代码的真实 commit；不能只添加一个裸 gitlink。

同时要求：

1. Sidecar Context 增加 `.dockerignore`，排除 `.git`、examples、tests、images、cache 和本地输出。
2. 根仓库新增核心源码、Compose、测试和文档全部进入明确的 Git 版本边界。
3. 不要暂存或提交与本轮无关的用户改动。
4. `git diff --check` 必须同时覆盖 tracked 和本轮新增文件。
5. 从临时 clean clone/worktree 验证两个 Docker Build Context 所需文件全部存在。

## 13. 指标和性能证据

当前 `first_refresh_ok`、浏览器结果等事件只有声明，没有完整生产 emit，性能报告仍为空。

实现要求：

1. 补齐 `signup_started`、`turnstile_solved`、`otp_received`、`browser_done/denied/timeout`、`first_refresh_ok` 的真实事件。
2. `first_refresh_ok` 必须能关联注册 Job/账号，但不得写邮箱或 Token 明文。
3. Producer 日志分别报告 `signup_handoff`、`mint_imported` 和 `failed`，不能把 Queue 交接当入池。
4. 本地无法证明的 6 小时数据继续标为未测，不得填写估算值冒充实测。
5. 更新 `SERVER_DEPLOYMENT.md` 的真实资源数据：默认约 `2.45 CPU / 4144 MiB`，Pipeline v2 约 `2.50 CPU / 4240 MiB`；注明用户已接受该小幅超额。

## 14. 必跑验证

开发 Agent必须在安装项目依赖的隔离环境中执行完整测试，不能再用“本机缺 httpx”作为完成状态：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -v
.venv/bin/python -m py_compile \
  registration_jobs.py registration_queue.py registration_controller.py \
  registration_metrics.py route_registry.py cookie_bundle.py \
  registration_producer.py grok_build_adapter.py sso_to_auth_json.py \
  integrations/ruyipage-runtime/device_approver.py
docker compose -f docker-compose.server.yml config --quiet
docker compose -f docker-compose.server.yml --profile pipeline-v2 config --quiet
git diff --check
```

完整 `unittest discover` 必须 0 failure、0 error、0 skip（平台特定测试除外，但必须解释）。真实 Probe 测试应导入真实 `auth.py` 和 `model_health.py`，只 Mock HTTP Transport，不得再用替身模块绕开真实签名。

如果 Docker daemon 可用，必须执行：

```bash
docker compose -f docker-compose.server.yml build grokcli-2api ruyipage-approver
docker compose -f docker-compose.server.yml --profile pipeline-v2 up -d
docker compose -f docker-compose.server.yml ps
```

然后完成无秘密的本地冒烟验证：API readiness、两个 Sidecar health、Queue handoff、一个 Mock Mint Job、容器重启后的 lease recovery。不要连接生产上游，也不要批量注册真实账号。

如果 Docker daemon 仍不可用，不得卡住等待；继续完成全部非 Docker 工作，在报告中记录精确错误，并把 Docker Build 标为唯一外部待验项，不能写成 PASS。

## 15. 报告格式

只更新以下文件：

```text
/Users/ethan/Documents/grok/test-output/acceptance-fix-round3.md
```

报告必须包含：

1. 每个 P0/P1 对应的修改文件和行号。
2. 每个历史最小复现修复前后的结果。
3. 完整测试命令、测试数量和退出码。
4. 多进程 Queue 压力测试参数及结果。
5. Compose 展开后的 CPU/内存总和。
6. Git clean-clone/build-context 验证结果。
7. Docker 构建结果或唯一明确阻塞原因。
8. 尚未验证的真实浏览器、6 小时续期和线上性能项目。

报告严禁包含邮箱密码、完整/部分 SSO、Access/Refresh Token、YesCaptcha Key、API Key、Cookie 值或代理密码。

## 16. 禁止事项

1. 不得部署服务器；服务器部署必须等主验收 Agent通过第三轮本地验收。
2. 不得修改真实账号池 JSON 内容，不得删除真实账号。
3. 不得删除真实 Pending/RSC/Auth 文件来让测试通过；只可提供独立的迁移/清理工具并在临时目录测试。
4. 不得把 Probe 默认改成 skip，或把失败改成成功来绕过验收。
5. 不得把 `mint_queued` 重新伪装成 `imported`。
6. 不得通过增大超时掩盖 Producer 等待逻辑错误。
7. 不得关闭状态机校验、Queue 容量或租约校验来消除异常。
8. 不得开启 Warm Browser、并行 Poll 或 Cookie 全量灰度作为默认值。
9. 不得提交 `.env`、`data/`、测试秘密或嵌套 `.git`。
10. 不得修改与本轮无关的现有用户改动。

## 17. 第三轮验收门槛

开发 Agent只有在以下全部满足后才能回复“已完成”：

1. Producer Pipeline v2 handoff 不超时。
2. Mint Worker 在 denied/空 Token/未知异常后仍继续处理下一 Job。
3. 运行态崩溃可恢复，stale Worker 写入被 fencing 拒绝。
4. Queue 背压发生在注册前，且不存在 mint-owned orphan Pending。
5. Legacy Pending 并发消费严格一次，回滚可恢复。
6. Route 2 六个网络阶段全部走 Route 2。
7. Cookie 实验从生产入队路径生效。
8. 默认日志、API和文件中不存在注册秘密。
9. 所有 Auth 写入和续期后权限均为 `0600`。
10. 根仓库 clean clone 可取得并构建 Sidecar 源码。
11. 完整测试 0 failure、0 error。
12. 修复报告完整且不含秘密。

开发 Agent完成后停止，不部署、不提交生产配置、不宣布线上指标提升。最终是否通过、是否进入服务器部署和灰度，由主验收 Agent决定。

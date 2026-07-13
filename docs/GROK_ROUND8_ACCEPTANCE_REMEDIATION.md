# Grok Round 8 Acceptance Remediation

日期：2026-07-13

项目目录：

```text
/Users/ethan/Documents/grok
```

当前开发分支和验收基线：

```text
branch: codex/round8-remediation
baseline commit: cbf2ad5f8e1a6bb7f89b5f83b2c109c1daba6994
```

本文件是 `cbf2ad5` 之后唯一的验收修复指令。原始完整要求仍见：

```text
/Users/ethan/Documents/grok/docs/GROK_ROUND8_REMEDIATION_TASKS.md
```

本轮不是重新规划，也不是只补测试名称。必须修复以下所有验收阻断，生成一个新的本地 commit，并由主验收 Agent 再次验收。

## 1. 交付位置和禁止事项

源码继续在现有模块原地修改。禁止创建第二套服务或复制项目树。

新增测试：

```text
/Users/ethan/Documents/grok/tests/
```

强制部署脚本：

```text
/Users/ethan/Documents/grok/scripts/server_preflight.sh
/Users/ethan/Documents/grok/scripts/smoke_server.sh
/Users/ethan/Documents/grok/scripts/rollback_server.sh
```

只更新一个开发报告：

```text
/Users/ethan/Documents/grok/test-output/acceptance-fix-round8.md
```

更新现有部署手册：

```text
/Users/ethan/Documents/grok/SERVER_DEPLOYMENT.md
```

禁止事项：

1. 不得部署云贝服务器，不得修改 NewAPI，不得操作真实号池。
2. 不得读取、打印或提交真实 Token、SSO、Cookie、密码、API Key、代理凭据或 Device Code。
3. 不得 reset、rebase、amend 或覆盖 `cbf2ad5`；在其后创建新 commit。
4. 不得删除用户现有未跟踪文件。`integrations/ruyipage/` 如属本地旧树，只加入精确 ignore，不得删除。
5. 不得通过只注入测试回调绕过生产默认路径。
6. 不得用线程测试冒充多进程测试。
7. 不得在报告中出现“未完成但 ready for acceptance”。所有 Gate 完成前不能声称 ready。

## 2. Gate A：额度和 Refresh 状态机必须真正闭环

### A1. 修复首次 Quota 周期初始化

当前错误位于 `account_pool.mark_quota_waiting()`：代码先写 `quota_waiting=True`，再计算 `already_waiting`，导致首次初始化分支永远不执行。

正确顺序：

```python
already_waiting = bool(meta.get("quota_waiting") or meta.get("disabled_for_quota"))
# 之后才能写 meta["quota_waiting"] = True
```

首次进入周期必须一次性写入：

```text
quota_cycle_id
quota_waiting_since
quota_reset_at
quota_next_probe_at
quota_limit_tokens=1000000
quota_remaining_tokens=0
quota_grace_count=0
quota_confirmation_count=0
quota_first_confirm_at=None/absent
quota_last_confirm_at=None/absent
quota_status=quota_waiting
```

没有 Reset Header 时必须保存 `now + 24h`，不能变成 30 分钟后 Probe。

同一周期重复 exhaustion：

1. 不得修改 `quota_cycle_id` 或 `quota_waiting_since`。
2. fallback `now+24h` 不得覆盖已有 Reset。
3. 只有通过明确规则校验过的权威 Reset Header 才能修正时间。
4. 不得重置已存在的 post-reset 证据。

### A2. 默认 1M Probe 必须使用服务器代理

`quota.probe_free_usage_for_creds()` 已正确使用 `/responses`，但 `trust_env=False` 且生产调用没有传代理。

要求：

1. `process_quota_probe_due()` 默认路径显式传入 `config.XAI_PROXY` 或对应 Route Proxy。
2. 不得依赖进程环境隐式代理。
3. 测试必须在生产默认路径 Mock `httpx.Client` 并断言 `proxy=http://...`。
4. `proxy=None` 只能在明确配置为直连时出现。
5. 401/403 `error_class=credential` 必须进入凭据分类器，不得被当成普通 inconclusive 无限等待。

### A3. 完成 Quota Grace 和安全清理

明确状态：

```text
active
quota_waiting
quota_probe_due
quota_grace
quota_reset_failed
terminal_cleanup_candidate
```

要求：

1. 只有 Reset 到期后的明确 `free_usage_exhausted` 才增加确认次数。
2. 网络、超时、普通 429、5xx、无法解析不增加确认。
3. 至少 3 次明确确认，且跨至少 2 个维护周期并经过配置化 Grace 时间，才能写 `quota_reset_failed`。
4. 保存 first/last confirmation 时间、证据类别和周期 ID。
5. `_cleanup_reason()` 只有在全部 Gate 满足时才能返回 `quota_reset_failed`。
6. 清理仍默认 dry-run，并保留现有二次观察窗口。
7. 真实删除失败不得清凭据或证据。
8. 成功恢复必须清除本周期所有字段，包括 cycle/grace/confirmation/first/last/reset/next probe。
9. 第二个周期必须从 0 开始。

### A4. 手工、凭据、额度、模型状态完全分开

要求：

1. `set_account_enabled(False)` 原子写 `enabled=False, manual_disabled=True`。
2. `set_account_enabled(True)` 清 `manual_disabled`，并对 `credential_suspended` 给出一致语义：要么明确清除全部 suspend 字段，要么拒绝普通 enable；禁止 `enabled=True + credential_suspended=True`。
3. Quota mark/clear 不修改手工禁用决定。
4. 普通 OpenAI/Anthropic 请求收到 `account_suspended/user_blocked/account_disabled` 时立即走 credential classifier。
5. `/responses` Probe 的 401/403 同样走 credential classifier。
6. Quota 错误分类后必须停止继续进入 model classifier。
7. 从 `_ACCOUNT_BLOCK_RE` 删除 `run out of credits/out of credits/usage_limit_reached/usage_pool_exhausted` 等额度文案，改由 Quota 证据判定。
8. Quota 错误不得创建 model block；恢复时只清 Quota 自己拥有的状态。
9. `pool_summary()` 分别统计 active/waiting/grace/reset_failed/manual/suspended/refresh_pending/refresh_terminal，waiting 不得计入 available/enabled。

### A5. 修复所有流式 Reset Headers

以下两条生产路径必须传 `resp.headers`：

```text
OpenAI stream
Anthropic stream
```

`parse_quota_reset_at()` 必须支持：

```text
epoch seconds
epoch milliseconds
relative numeric Retry-After
RFC HTTP-date Retry-After
```

使用标准库 `email.utils.parsedate_to_datetime`，不要手写日期解析。

### A6. 实现多周期 Refresh 确认

彻底停止使用“一次 invalid_grant 立即永久 `refresh_invalid`”作为清理依据。

至少实现：

```text
refresh_status
refresh_failure_count
refresh_first_failed_at
refresh_last_failed_at
refresh_next_retry_at
refresh_terminal_at
refresh_confirmed_after_expiry
```

状态要求：

1. 第一次 definitive invalid_grant -> `refresh_pending_confirmation`。
2. 未到期 Access Token 保持可用。
3. 后续维护周期按退避再次尝试。
4. 至少 3 次独立 definitive 失败，并至少一次发生在 Access Token 到期后，才能进入 `refresh_terminal`。
5. 网络/5xx 不计 definitive 次数。
6. 后续成功清除所有 Refresh 失败证据。
7. Producer 只清理 `refresh_terminal`，不得继续直接清理 legacy `refresh_invalid`。
8. legacy `refresh_invalid` 必须迁移为待重新确认状态。

### Gate A 强制测试

以下测试名必须真实存在并执行生产路径：

```text
test_first_quota_exhaustion_initializes_full_cycle
test_first_quota_exhaustion_without_header_waits_24h
test_default_quota_probe_passes_explicit_proxy
test_quota_probe_credential_error_suspends_account
test_quota_reset_failed_not_cleanup_ready_before_all_gates
test_quota_reset_failed_cleanup_candidate_after_all_gates
test_quota_recovery_clears_all_cycle_evidence
test_second_quota_cycle_starts_from_zero
test_manual_disable_survives_quota_recovery
test_manual_enable_resolves_suspend_state_atomically
test_quota_error_never_creates_model_block
test_normal_request_account_suspended_is_terminal
test_pool_summary_excludes_waiting_from_available
test_openai_stream_passes_reset_headers
test_anthropic_stream_passes_reset_headers
test_retry_after_http_date
test_first_invalid_grant_is_pending_not_terminal
test_refresh_retries_next_maintenance_cycle
test_refresh_terminal_requires_post_expiry_confirmation
test_refresh_success_clears_failure_evidence
test_legacy_refresh_invalid_requires_new_confirmation
```

## 3. Gate B：Queue 迁移、幂等和租约必须可上线

### B1. 安全创建 Active Session 唯一索引

旧数据库可能已经有重复 active Session。不得在 `_SCHEMA` 中直接创建唯一索引后才迁移。

要求：

1. 先创建/升级表和普通索引。
2. `BEGIN IMMEDIATE` 扫描重复 active `session_id`。
3. 按确定规则选 Survivor：优先合法未过期 Owner，其次恢复材料完整度，其次 `updated_at`，最后稳定 `job_id`。
4. Survivor 必须保留所有可用 `sso_ref/cookie_bundle_path/payload` 恢复信息。
5. 非 Survivor 转成明确 Terminal 状态并写 `duplicate_session_migrated`，不能删除恢复材料。
6. 去重完成后再创建部分唯一索引。
7. 迁移必须幂等，第二次启动无变化。

### B2. 修复 Enqueue 返回语义

1. `enqueue()` 必须先在事务中查同 Session active Job，再做 hard-limit 判定。
2. 重复 active Session 返回该 Active Job，不得返回最新 Terminal 历史。
3. `RegistrationController.enqueue_after_sso()` 必须使用：

```python
job = self.queue.enqueue(job)
```

4. API、Metrics、Pending Owner 都必须引用真实持久化的 `job_id`。

### B3. Heartbeat 失租后协作取消

后台 heartbeat 已能阻止最终导入，但阻塞 `token_fn` 在失租后仍继续上游动作。

要求：

1. `_LeaseHeartbeat` 失租时设置共享 `cancel_event`。
2. 将 `cancel_event/lease_guard` 传入 Device Code、Browser Approver、Token Poll、Rate-limit sleep 和 Probe。
3. 每个上游步骤和每次 sleep 前后检查取消。
4. 失租后不得再发新上游请求；当前不可取消的单个 HTTP 请求允许完成，但返回后立即停止。
5. 不得 Import，不得清理新 Owner 材料，不得遗留 heartbeat 线程。

### B4. 双 Mint 和双 Sidecar 真正作为默认 Pipeline v2 启动

1. 手册启动命令同时启动两个 Mint Worker。
2. 回滚同时停止两个 Worker并 force-recreate API/Producer/相关 Sidecar。
3. Pipeline v2 的 `.env` 示例明确启用 `PIPELINE_V2=1` 和 `ROUTE_STICKY=1`。
4. 两个并发 Job 必须分配不同 Route，并分别访问两个 Approver。
5. 修正真实资源口径为 9 services / 2.55 CPU / 4336 MiB。

### Gate B 强制测试

```text
test_existing_duplicate_active_sessions_migrate_before_unique_index
test_duplicate_migration_is_idempotent
test_duplicate_migration_preserves_recovery_material
test_duplicate_enqueue_returns_persisted_active_job
test_duplicate_enqueue_does_not_return_terminal_history
test_duplicate_enqueue_at_hard_limit_is_idempotent
test_controller_returns_persisted_job_id
test_heartbeat_loss_cancels_before_next_upstream_action
test_heartbeat_loss_never_imports
test_heartbeat_thread_exits
test_two_mint_workers_use_two_routes_and_two_approvers
test_multiprocess_claim_exactly_once_8_processes_1000_jobs
test_concurrent_enqueue_hard_limit_8_processes
```

## 4. Gate C：所有凭据和输出必须关闭

### C1. 启动权限迁移

应用开始监听前完成迁移：

```text
data/                         0700
auth.json + backups           0600
settings.json                 0600
keys.json                     0600
pending_sso/                  0700
cookie_bundles/               0700
register_sso/                 0700
上述目录内秘密文件            0600
Queue/Metrics DB              0600
```

迁移只允许修改 Mode，不读取或打印文件内容。

Settings/API Key/Pending/Cookie 的新写入必须使用 `os.open(..., 0o600)` + fsync + atomic replace，并在 replace 后 chmod。

### C2. API 和 OIDC 脱敏

1. `/health` 只返回计数和组件状态，删除 email/expires_at/auth_key。
2. 公开 `/admin/api/status` 删除 credentials_email；敏感详情只允许鉴权 Admin API。
3. OpenAI/Anthropic 响应删除 `x_grok2api_account`。
4. OIDC Session 不返回 raw `output_tail`、device_code、user_code、Token、身份邮箱或 secret URL。
5. Batch Sanitizer 增加 device_code/user_code/password/proxy credential 和 URL Query Secret。
6. Debug 只能输出状态码、hop count、错误类别；不得输出 Set-Cookie URL、JWT/SSO 前缀或 Header。

### C3. 全局 Fail-closed

1. `GROK2API_REQUIRE_API_KEY` 默认改为 `1`。
2. 本地开放必须显式 `GROK2API_REQUIRE_API_KEY=0`。
3. 损坏 Key Store 仍必须要求鉴权。
4. 损坏 `settings.json` 必须 fail closed；只有文件真正不存在时才允许首次 Setup。

### Gate C 强制测试

```text
test_startup_migrates_all_secret_permissions
test_new_settings_and_keys_are_0600
test_secret_directories_are_0700
test_health_has_no_identity
test_public_status_has_no_credentials_email
test_client_responses_have_no_internal_account
test_oidc_session_redacts_all_secrets
test_batch_redacts_device_user_code_and_query_secrets
test_debug_never_prints_set_cookie_jwt_or_sso
test_corrupt_settings_fails_closed
test_corrupt_key_store_still_requires_auth
test_api_auth_default_is_fail_closed
```

## 5. Gate D：Retention、部署脚本和文档必须完整

### D1. Retention

实现一个单一维护 Owner 周期执行：

1. Terminal Queue Job retention，默认 7 天，按批删除。
2. Metrics Event retention，默认 7 天，并支持最大行数上限。
3. Cookie Bundle/Pending 临时文件 TTL，默认 48 小时；不得删除 active Job 引用文件。
4. 每轮有最大删除数，避免长 SQLite 锁。
5. 记录上次清理时间和数量，不记录秘密。
6. Compose 所有 9 个服务增加：

```yaml
logging:
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
```

### D2. 三个强制脚本

必须创建并 `chmod +x`：

```text
scripts/server_preflight.sh
scripts/smoke_server.sh
scripts/rollback_server.sh
```

共同要求：

1. `set -euo pipefail`。
2. 所有等待有 timeout，禁止无限卡住。
3. 不打印 Secret。

Preflight 至少检查：

```text
required env existence
两套 mihomo config + syntax
external Docker network
disk/memory/CPU
bind mount existence/mode
Compose version
image architecture
database backup before migration
```

Smoke 使用合成任务验证：

```text
API readiness
无 Key 请求被拒绝
Admin 登录
两个 Sidecar health
两个 proxy route
Queue handoff
双 Mint 并发
重启/lease recovery
```

Rollback：

```text
恢复版本化镜像/配置
恢复必要数据库备份
同时停止两个 Mint Worker
force-recreate 所有读取变更环境变量的服务
确认 legacy pending 可恢复
运行有限时 smoke
```

### D3. 文档与镜像

1. 删除“三容器、单 Firefox、2.35/2.40/2.50”等旧口径。
2. 统一为 default 7 services / 2.45 CPU / 4144 MiB。
3. Pipeline v2 统一为 9 services / 2.55 CPU / 4336 MiB。
4. 启动/停止/回滚命令必须同时覆盖两个 Mint Worker。
5. mihomo 和基础镜像使用可回滚的版本 Tag 或 Digest，禁止 `latest`。
6. 文档明确执行 preflight -> backup -> up -> smoke -> observe -> rollback 顺序。

### Gate D 强制测试

```text
test_terminal_queue_retention_bounded
test_metrics_retention_bounded
test_retention_preserves_open_jobs
test_cookie_sweeper_runs_in_production_maintenance
test_all_compose_services_have_log_rotation
test_pipeline_profile_has_two_mint_workers
test_deployment_manual_starts_and_stops_both_workers
test_preflight_has_bounded_waits_and_secret_redaction
test_smoke_has_bounded_waits
test_rollback_force_recreates_affected_services
```

## 6. Gate E：Git、干净树和验证矩阵

### E1. Git 边界

1. 在 `cbf2ad5` 后创建新 commit，不 amend。
2. 添加所有源码、测试、三个脚本和更新文档。
3. `.dockerignore` 必须忽略 `.venv*`、`.venv_sys`、`test-output`、`artifacts`、`_compare_grok_register`、本地旧 `integrations/ruyipage/`。
4. `.gitignore` 精确忽略本地旧 `integrations/ruyipage/`，不得忽略交付目录 `integrations/ruyipage-runtime/`。
5. `git diff --check cbf2ad5..HEAD` 必须 exit 0。
6. 新 checkout 的 `git status --short` 必须为空。

### E2. 干净 checkout

从新 commit 创建：

```text
/tmp/grok-round8-acceptance-clean
```

在该目录中创建全新 venv、安装 `requirements.txt`，不得复用宿主 PYTHONPATH。

Compose 验证前可以从 `.env.example` 创建临时 `.env`，但报告必须写明，验证后删除；不得放真实 Secret。

### E3. 强制命令

```bash
python -m unittest discover -v
python -m unittest discover -s tests -v
python -m compileall -q .
git diff --check cbf2ad5..HEAD
docker compose -f docker-compose.server.yml config -q
docker compose -f docker-compose.server.yml --profile pipeline-v2 config -q
```

Docker daemon 可用时必须执行 build/up/smoke。不可用时只能报告准确阻断，不能声称 Docker PASS。

## 7. 报告和最终回复

更新：

```text
/Users/ethan/Documents/grok/test-output/acceptance-fix-round8.md
```

报告必须逐 Gate 给出：

```text
Gate A: PASS/FAIL + 测试
Gate B: PASS/FAIL + 测试
Gate C: PASS/FAIL + 权限 Mode（不含内容）
Gate D: PASS/FAIL + 脚本/Retention/Compose
Gate E: PASS/FAIL + commit/clean checkout/命令退出码
```

任何 Gate 为 FAIL，最终状态必须是 `not ready`，禁止写 `ready for acceptance`。

全部完成后，开发 Agent 只回复：

```text
ready for acceptance
new commit hash
report absolute path
clean checkout absolute path
full tests count and exit code
tests/ count and exit code
git diff --check exit code
compose default/profile exit codes
docker build/up/smoke result or exact blocker
remaining risks
```

最终是否通过只由主验收 Agent决定。开发 Agent不得自行批准部署。

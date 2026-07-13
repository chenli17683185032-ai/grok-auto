# Grok Registration Speed Optimization Plan

> 唯一正式计划文档。所有阶段进度、设计变更与验收结果均维护于此文件。
> 项目根：`/Users/ethan/Documents/grok`
> 生成日期：2026-07-12
> 状态：**本地实现完成，等待主验收 Agent 审查；未部署、未宣布全量上线**

---

## 1. 当前架构与问题

### 1.1 现有生产链路

```
registration-producer
  → POST /admin/api/accounts/register-email
  → grok_build_adapter._run_registration
       1) MoeMail 建邮箱
       2) YesCaptcha 解 Turnstile
       3) xconsole_client 协议注册
       4) 提取 SSO（RSC set-cookie 或 CreateSession 回退）
       5) 写入 data/pending_sso/{sid}.json (mode 0600)
       6) sso_to_auth_json.sso_to_token
            - 校验 SSO
            - OAuth Device Code
            - ruyiPage /approve（两 sidecar 轮询 + 文件锁）
            - Token Poll（串行，在浏览器之后）
       7) accounts.import_auth_payload → data/auth.json
       8) 成功则删除 pending
  → token_maintainer / model_health 后续维护与 probe
```

关键组件（`docker-compose.server.yml`）：

| 服务 | 角色 | 当前代理 | CPU 预算 |
|------|------|----------|----------|
| `grokcli-2api` | API + 注册 worker | mihomo-1 | 目标 0.40（compose 默认仍偏高，需对齐） |
| `ruyipage-approver` | Cold Firefox Device Flow | mihomo-1 | 0.80 |
| `ruyipage-approver-2` | Cold Firefox Device Flow | mihomo-2 | 0.80 |
| `grok-mihomo` / `grok-mihomo-2` | 出站代理 | — | 0.15×2 |
| `registration-producer` | 批次调度 + pending 恢复 | mihomo-1 | 0.10 |
| `pending-recovery` | 独立 pending 恢复 | mihomo-1 | 0.05 |

### 1.2 已确认的瓶颈

1. **注册与 Mint 耦合**
   `_run_registration` 在获得 SSO 后仍占用注册 worker 完成 Device Flow + Token Poll + 导入。
   协议注册吞吐被浏览器串行审批拖死。

2. **无账号级 Route Affinity**
   - 注册默认走 `GROK2API_XAI_PROXY` / `HTTP_PROXY` → mihomo-1
   - 审批 sidecar 轮询 `approver-1/2`（可跨 Route）
   - Token Poll 默认同一全局 proxy
   - IP/会话不一致会抬高 `denied` / `rate_limited` / 假成功

3. **Cold Browser 成本高**
   `device_approver.approve` 每次新建/退出 Firefox，固定 `wait(2/3)`，无状态驱动。
   两 sidecar 各有全局锁，吞吐上限 ≈ 2 个串行浏览器。

4. **Cookie 仅 SSO**
   审批只注入 `sso` / `sso-rw`，缺少注册阶段 session cookie bundle，Device Flow 成功率受限。

5. **Token Poll 与审批串行**
   `sso_to_token` 先浏览器再 poll；未并行，未在 denied 时取消 poll。

6. **观测不足**
   缺少统一 funnel 指标、route/approver 维度、pending 年龄、6h refresh 关联、New API TTFT 保护钩子。

7. **ZIP 参考实现不可直接搬**
   `artifacts/grok_register_review/grok_register_untrusted` 仅作设计参考：
   缺 `turnstilePatch`、含 `--no-sandbox`、含 Hotmail/明文账本倾向；**禁止整包复制或替换 YesCaptcha/MoeMail**。

### 1.3 参考设计（只借鉴思路）

| 主题 | ZIP 参考 | 生产落点 |
|------|----------|----------|
| Cookie 注入/域名扩展 | `browser_confirm.normalize_cookies/inject_cookies` | `cookie_bundle.py` + approver 白名单注入 |
| Browser 复用/回收 | `tab_pool` / `acquire_mint_browser` | ruyiPage Warm Browser（Context 隔离） |
| 代理粘性 | `proxyutil` thread-local pin | `route_registry` 账号级固定 |
| Mint 解耦 | `mint.py` 独立 mint | 持久化 Mint Queue |
| 错误分类 | browser_confirm 结果语义 | `registration_jobs` error class |

---

## 2. 优化目标

提高：

```
每小时最终可用账号数量
```

最终可用账号定义：

```
注册成功
+ SSO 获取成功
+ Access Token 获取成功
+ Refresh Token 存在
+ grok-4.5 轻量 Probe 成功
+ 进入 live 账号池
```

长期质量：

```
6 小时后首次 Refresh 成功
Refresh 后仍能调用 grok-4.5
没有被标记 refresh_invalid
```

相对基线目标：

| 指标 | 目标 |
|------|------|
| `valid_6h_per_hour` | ≥ 基线 +30% |
| 稳定最终有效入池 | ≥ 30/小时（伸展 35–45） |
| Device Flow 直接成功率 | ≥ 65%（理想 70–75%） |
| pending P90 | < 10 分钟；最老 < 30 分钟 |
| Grok 系统 CPU | ≤ 2.40 CPU 硬预算 |
| New API Grok TTFT | 相对基线恶化 ≤ 10–15% |

**不以**「每小时启动注册任务数」或「每小时 SSO 数」作为最终验收。

---

## 3. 非目标

- 不把注册并发直接提高到 4+
- 不新增第 3/4 个完整浏览器实例
- 不替换 YesCaptcha / MoeMail
- 不引入 Hotmail 主链
- 不运行 ZIP 的 turnstilePatch / 明文凭据账本
- 不在本地用真实 `auth.json` 做单元测试
- 不在本阶段自行部署服务器或宣布全量上线
- 不优先修改 `/Users/ethan/Desktop/云贝/服务器相关/grok_pool_status.py`（核心架构稳定后再做）

---

## 4. 服务器资源约束

主机：4 vCPU / 7.8 GiB。尽量保留约 40% CPU 空闲。

### 4.1 CPU 硬预算（合计 ≤ 2.40）

```
grokcli-2api：0.40 CPU
ruyipage-1：0.80 CPU
ruyipage-2：0.80 CPU
mihomo-1：0.15 CPU
mihomo-2：0.15 CPU
Controller/Queue/Producer：0.10 CPU
--------------------------------
合计：2.40 CPU
```

### 4.2 内存规划

```
grokcli-2api：512～768 MiB
ruyipage-1/2：各约 1.35～1.45 GiB
mihomo：各 192 MiB
Controller/Producer：128 MiB
```

### 4.3 自动保护阈值

| 条件 | 动作 |
|------|------|
| 整机 CPU >60% 持续 5 分钟 | 注册并发降为 1 |
| 整机 CPU >70% 持续 3 分钟 | 暂停新注册 |
| MemAvailable < 3.0 GiB | 暂停新注册 + 安排浏览器回收 |
| MemAvailable < 2.5 GiB | Mint 降为单 Route |
| MemAvailable < 1.8 GiB | 暂停注册与 Mint，优先推理与 Token 维护 |

---

## 5. 目标架构

```
┌────────────────────┐
│ registration_producer │  (调度入口，仍走 Admin API 或 Controller)
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐     协议注册（Route 固定 proxy）
│ Registration Worker│ ──► SSO + optional cookie_bundle
└─────────┬──────────┘
          │ enqueue (WAL SQLite)
          ▼
┌────────────────────┐
│ registration_queue │  jobs: signup → mint → probe → import
└─────────┬──────────┘
          │ lease
          ▼
┌────────────────────┐
│ Mint Worker        │  同 route_id：Device Flow + Token Poll + Probe + Import
│ (route affinity)   │
└─────────┬──────────┘
          ▼
     live pool (auth.json) + metrics + 6h refresh 关联
```

新增正式模块（均在项目根）：

| 文件 | 职责 |
|------|------|
| `route_registry.py` | Route 定义、会话固定、proxy/approver 解析 |
| `registration_jobs.py` | Job 状态机、错误分类、session_id 工具 |
| `registration_queue.py` | SQLite WAL 队列、租约、旧 pending 兼容 |
| `registration_controller.py` | Mint worker / 调度 / 背压（flag 控制） |
| `registration_metrics.py` | 结构化事件、funnel、资源采样 |
| `cookie_bundle.py` | Cookie 白名单、文件权限、TTL、脱敏 |

Feature flag 默认关闭时行为与现网一致。

---

## 6. Route Affinity 设计

### 6.1 Route 表

```
route-1:
  register_proxy  → http://grok-mihomo:7890
  approver        → http://ruyipage-approver:8765
  token_proxy     → http://grok-mihomo:7890

route-2:
  register_proxy  → http://grok-mihomo-2:7890
  approver        → http://ruyipage-approver-2:8765
  token_proxy     → http://grok-mihomo-2:7890
```

### 6.2 绑定规则

1. Job 创建时分配 `route_id`（轮询或哈希 `session_id`）。
2. 一旦获得 SSO，**禁止跨 Route 重放 Device Code**。
3. `browser_denied`（结构化业务拒绝）不得切换审批器重放同一 Device Code。
4. 仅 **transport 失败**（sidecar 不可达/畸形响应）可在同 job 内失败并新建 Device Code 重试；重试仍绑定原 `route_id`。
5. Token Poll 使用该 route 的 `token_proxy`。

### 6.3 API

```python
RouteRegistry.list_routes()
RouteRegistry.assign_route(session_id) -> route_id
RouteRegistry.get(route_id) -> Route
RouteRegistry.proxy_for(route_id, phase="register"|"token"|"browser")
RouteRegistry.approver_for(route_id)
```

---

## 7. 持久化 Registration/Mint Queue 设计

### 7.1 存储

生产：`/app/data/registration_queue.db`（0600）
测试：`test-output/registration_queue.test.db`

SQLite + WAL；任务租约（lease_owner / lease_until）。

### 7.2 表结构（逻辑）

```sql
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  route_id TEXT NOT NULL,
  state TEXT NOT NULL,
  email_hash TEXT,
  sso_ref TEXT,              -- 指向加密/权限保护的材料，非日志字段
  cookie_bundle_path TEXT,
  cookie_mode TEXT,
  error_class TEXT,
  error_code TEXT,
  attempts INTEGER DEFAULT 0,
  lease_owner TEXT,
  lease_until REAL,
  next_run_at REAL,
  created_at REAL,
  updated_at REAL,
  payload_json TEXT          -- 非 secret 元数据
);
```

### 7.3 状态机（摘要）

```
created → signup_running → sso_obtained → mint_queued
  → mint_running → browser_done|browser_denied|browser_timeout
  → token_received → probe_running → probe_passed → auth_imported
  → failed | dead_letter
```

### 7.4 兼容

- 继续双写旧 `pending_sso/*.json`（初期）。
- Worker 崩溃：lease 过期后可重新领取。
- 队列 soft limit 12 / hard limit 30（可配置）。
- 不丢 SSO：导入成功前不删除材料。

---

## 8. Cookie Bundle 设计

### 8.1 模式

| 模式 | 内容 | 默认 |
|------|------|------|
| `sso_only` | 仅 `sso` / `sso-rw` | 是（兼容现网） |
| `auth_bundle` | SSO + 注册阶段安全白名单 cookie | 实验 |

**第一版不默认加入** `cf_clearance` / `__cf_bm`（后续独立实验）。

### 8.2 白名单（auth_bundle 初版）

```
sso
sso-rw
```

可选扩展（显式 flag 后）：

```
# GROK2API_COOKIE_ALLOW_CF=1 时才允许
cf_clearance
__cf_bm
```

### 8.3 存储

生产目录：`/app/data/cookie_bundles`（0700），文件 0600。
路径防穿越；拒绝 symlink。
TTL 默认 2h；过期删除。
日志只记 `cookie_names` / `bundle_id`，永不记 value。

### 8.4 A/B

单位：`registration_session_id`
稳定哈希：

```python
bucket = sha256(f"{experiment_id}:{session_id}".encode()).digest()[0] % 10000
# percent = GROK2API_COOKIE_EXPERIMENT_PERCENT
# in_experiment if bucket < percent * 100
```

阶梯：0% → 10% → 25% → 50% → 75% → 100%。
至少 20–30 样本再扩量。

---

## 9. ruyiPage Warm Browser 设计

### 9.1 灰度

- 仅 `ruyipage-approver-2` 先开 Warm Browser。
- `ruyipage-approver-1` 保持 Cold，作为对照与快速回滚。

### 9.2 规则

- 浏览器进程复用；**每任务隔离 Context**（新 tab / 清理 storage+cookies）。
- Context 清理失败 → 回收浏览器。
- 每 `GROK2API_RUYIPAGE_RECYCLE_TASKS`（默认 10）或 `RECYCLE_SEC`（默认 5400）回收。
- 连续 2 次 timeout → 立即回收。
- 内存超阈值：任务结束后回收。
- **不增加第三个浏览器**。
- flag：`GROK2API_RUYIPAGE_WARM_BROWSER=0` 默认关闭。

### 9.3 状态驱动等待（阶段 5）

- 用 URL / 按钮 / 页面状态替代固定 sleep。
- 每 100–250ms 检查。
- 与 Token Poll 并行（见 §10）。

---

## 10. Token Poll 并行设计

当 `GROK2API_PARALLEL_TOKEN_POLL=1`：

1. 申请 Device Code 后立刻启动 Token Poll 线程/协程。
2. 同时调用 approver。
3. 严格遵守 OAuth `interval`；`slow_down` 增加间隔。
4. `denied` / 业务失败 → 取消 poll。
5. Token 成功 → 提前结束页面等待。
6. 仍绑定 route 的 token_proxy。

默认 0：保持现有「先浏览器后 poll」顺序。

---

## 11. 调度与背压

阶段 6（最后启用，`GROK2API_ADAPTIVE_SCHEDULER=1`）：

- 队列 soft/hard limit
- Route 评分与熔断
- CPU / 内存保护（§4.3）
- New API TTFT 保护钩子（只读观测 + 降并发）
- 注册并发 1/2 自动调节（不突破 2）
- 自动灰度与自动回滚钩子

阶段 0–5 只提供钩子与指标，默认不自动改行为。

---

## 12. 错误分类与重试

| error_class | 含义 | 重试策略 |
|-------------|------|----------|
| `transient_network` | 超时/连接失败 | 指数退避，同 route |
| `rate_limited` | xAI / Device Flow 限流 | 长退避，新 Device Code |
| `browser_timeout` | 审批超时 | 有限次，可触发浏览器回收 |
| `browser_denied` | 业务拒绝 | **不**跨 approver 重放同 code |
| `sso_invalid` | SSO 失效 | 不重试 mint；任务失败 |
| `probe_failed` | probe 未过 | 不入 live；可有限重试 |
| `import_failed` | auth 写入失败 | 保留材料重试 |
| `permanent` | 不可恢复 | dead_letter |

日志只记录 class/code，不记录 secret 或异常原文中的 token。

---

## 13. 指标与 A/B 实验

### 13.1 Funnel 事件

```
signup_started, turnstile_solved, otp_received, signup_complete,
sso_obtained, mint_started, browser_done, browser_denied, browser_timeout,
token_received, refresh_token_received, probe_passed, auth_imported,
first_refresh_ok
```

### 13.2 维度

```
route_id, approver_id, hour_bucket, cookie_mode,
browser_generation, producer_version, experiment_id
```

### 13.3 存储

生产：`/app/data/registration_metrics.db`
测试：`test-output/registration_metrics.test.db`
禁止写入完整邮箱 / SSO / Cookie / Token / API Key。

### 13.4 主 KPI

```
initial_valid_per_hour
valid_6h_per_hour
```

---

## 14. 敏感数据保护

- SSO / Cookie / Access / Refresh / API Key / YesCaptcha Key **永不入日志**
- JWT 正则脱敏（已有 `_SecretRedactingWriter`，继续复用并扩展）
- Cookie 文件 0600，目录 0700
- 禁止路径穿越与 symlink
- 管理员 Key 不进 URL Query
- 测试报告与 change-summary 不含 secret
- 备份不得输出到聊天

---

## 15. Feature Flags

| Flag | 默认 | 含义 |
|------|------|------|
| `GROK2API_PIPELINE_V2` | `0` | 启用新队列/控制器路径 |
| `GROK2API_ROUTE_STICKY` | `0` | 账号级 Route Affinity |
| `GROK2API_COOKIE_MODE` | `sso_only` | `sso_only` \| `auth_bundle` |
| `GROK2API_COOKIE_EXPERIMENT_PERCENT` | `0` | auth_bundle 实验百分比 |
| `GROK2API_COOKIE_ALLOW_CF` | `0` | 是否允许 CF cookie |
| `GROK2API_RUYIPAGE_WARM_BROWSER` | `0` | Warm Browser |
| `GROK2API_RUYIPAGE_RECYCLE_TASKS` | `10` | 任务数回收 |
| `GROK2API_RUYIPAGE_RECYCLE_SEC` | `5400` | 时间回收 |
| `GROK2API_PARALLEL_TOKEN_POLL` | `0` | 并行 Token Poll |
| `GROK2API_ADAPTIVE_SCHEDULER` | `0` | 自适应调度 |
| `GROK2API_REGISTRATION_QUEUE_SOFT_LIMIT` | `12` | 队列软上限 |
| `GROK2API_REGISTRATION_QUEUE_HARD_LIMIT` | `30` | 队列硬上限 |
| `GROK2API_METRICS_ENABLED` | `1` | 阶段 0 观测（只写指标，不改行为） |

**默认必须兼容当前行为。**

---

## 16. 部署与迁移

### 16.1 原则

- 可灰度、可回滚、不丢 pending、不损坏 `auth.json`
- 本地测试通过后由主验收 Agent 决定是否部署
- 部署命令必须有超时，不依赖交互

### 16.2 迁移

1. 部署代码，flags 全关 → 行为不变，仅指标库可写。
2. 打开 `METRICS` 基线采集（阶段 0）。
3. `ROUTE_STICKY=1`（阶段 1）。
4. `PIPELINE_V2=1` + 双写 pending（阶段 2）。
5. Cookie 实验 10%（阶段 3）。
6. 仅 approver-2 Warm Browser（阶段 4）。
7. 并行 Token Poll（阶段 5）。
8. 自适应调度（阶段 6）。

### 16.3 备份路径（服务器）

```
/home/deploy/grokcli-2api/deploy-backups/<timestamp>/
```

至少包含：`.env`、`docker-compose.server.yml`、关键 py、`data/auth.json`、`data/pending_sso/`。

---

## 17. 回滚方案

| 级别 | 动作 |
|------|------|
| Flag 回滚 | 将相关 `GROK2API_*` 置默认 0 / sso_only |
| 服务回滚 | 恢复上一镜像 + compose；保留 data 卷 |
| Queue | `PIPELINE_V2=0` 后回到 pending_sso 恢复路径；队列文件可保留不删 |
| Warm Browser | approver-2 设 `WARM_BROWSER=0` 或整实例回滚到 cold 镜像 |
| Cookie | `COOKIE_MODE=sso_only` + `EXPERIMENT_PERCENT=0` |

回滚后健康检查：

```bash
docker compose -f docker-compose.server.yml ps
curl -fsS http://127.0.0.1:3000/
curl -fsS http://127.0.0.1:8765/health  # 各 approver
```

---

## 18. 测试方案

### 18.1 单元

- Route 稳定分配
- 任务状态机
- SQLite 租约与崩溃恢复
- 旧 pending 兼容
- Cookie 白名单 / 权限 / TTL / 路径穿越
- 日志脱敏
- Flag 关闭时旧行为

### 18.2 集成

- 注册完成后入 Mint Queue
- Mint Worker 固定 Route
- Token Poll 固定 Proxy
- Cookie Bundle 引用读取与降级
- Probe 失败不入 live
- Worker 中断后重新领取

### 18.3 浏览器

- Cold / Warm、Context 清理、不串号、回收策略（本地 mock 为主；真浏览器在服务器灰度）

### 18.4 安全

- 路径穿越、symlink、日志泄漏、错误消息敏感值

### 18.5 命令

```bash
python3 -m py_compile registration_jobs.py registration_queue.py \
  registration_controller.py registration_metrics.py route_registry.py \
  cookie_bundle.py grok_build_adapter.py registration_producer.py \
  sso_to_auth_json.py integrations/ruyipage/device_approver.py

python3 -m unittest discover -s tests -v
python3 -m unittest test_registration_producer test_sso_to_auth_json test_grok_build_adapter -v
git diff --check
```

---

## 19. 验收标准

见任务书第十一节；摘要：

- 主 KPI：`initial_valid_per_hour` / `valid_6h_per_hour`
- 不以启动任务数验收
- 资源、New API TTFT、6h refresh、refresh_invalid 均需达标
- **开发 Agent 不得自行宣布全量上线或达到 30–45/小时**

---

## 20. 实施进度与结果

### 阶段进度

| 阶段 | 内容 | 状态 |
|------|------|------|
| 0 | 观测 metrics / 事件 | 本地已实现（默认可写测试库；生产不改行为） |
| 1 | Route Affinity | 本地已实现（`ROUTE_STICKY` 默认 0） |
| 2 | 持久化 Mint Queue | 本地已实现（`PIPELINE_V2` 默认 0；兼容 pending） |
| 3 | Cookie Bundle A/B | 本地已实现（默认 sso_only / 0%） |
| 4 | Warm Browser（approver-2） | 本地已实现（flag 默认 0） |
| 5 | 状态等待 + 并行 Token Poll | 本地已实现（flag 默认 0） |
| 6 | 自适应调度 | 钩子与阈值判定已实现；默认关闭 |

### 实际修改文件

见文末「实际修改文件」与 `test-output/change-summary.md`。

### 设计决策摘要

1. **新模块旁路集成**：不强制改死主路径；`PIPELINE_V2=0` 时现网逻辑不变。
2. **`sso_to_auth_json` 增加可选 route/cookie/parallel 参数**，默认参数保持旧签名行为。
3. **Warm Browser 实现于 ruyiPage device_approver**，用进程内复用 Firefox + 清理；非 ZIP Chromium。
4. **不复制 ZIP 依赖与 `--no-sandbox` 无说明配置**。
5. **Probe 入池**：控制器在 flag 开启时调用 `model_health.probe_model_for_creds`；失败不 import live。

---

## 实际修改文件

### 新增

- `registration_jobs.py`
- `registration_queue.py`
- `registration_controller.py`
- `registration_metrics.py`
- `route_registry.py`
- `cookie_bundle.py`
- `docs/GROK_REGISTRATION_SPEED_OPTIMIZATION_PLAN.md`
- `tests/test_route_registry.py`
- `tests/test_registration_jobs.py`
- `tests/test_registration_queue.py`
- `tests/test_registration_controller.py`
- `tests/test_registration_metrics.py`
- `tests/test_cookie_bundle.py`
- `tests/test_ruyipage_device_approver.py`
- `tests/test_pipeline_flags_compat.py`
- `test-output/change-summary.md` 及测试/安全报告

### 修改

- `grok_build_adapter.py` — 可选入队 / 指标 / route / mint_queued 批次
- `sso_to_auth_json.py` — route sticky / parallel poll / cookie mode
- `registration_producer.py` — mint worker 模式 / heartbeat 容错
- `integrations/ruyipage/device_approver.py` — Warm Browser + 状态轮询
- `docker-compose.server.yml` — API cpus 默认 0.40；mint worker profile；approver-2 warm env
- `.env.example` — 新 flags
- `SERVER_DEPLOYMENT.md` — 灰度与回滚说明

### 未修改（有意）

- `accounts.py` / `auth_store.py` 核心存储逻辑（仅被调用）
- MoeMail / YesCaptcha 集成
- `grok_pool_status.py`（状态脚本延后）
- ZIP 目录任何文件

---

## 测试结果

| 检查 | 结果 |
|------|------|
| `py_compile`（10 文件） | PASS |
| `unittest discover -s tests`（38） | PASS |
| 既有 producer/sso/adapter 测试（19） | PASS |
| `git diff --check` | PASS |
| 安全清单 | PASS（本地） |

详见：

- `test-output/unit-test-report.txt`
- `test-output/integration-test-report.txt`
- `test-output/security-check-report.txt`
- `test-output/change-summary.md`

## 已知风险

1. Warm Browser 在 Firefox/ruyiPage 上的 Context 隔离能力弱于 Chromium tab pool，需服务器灰度验证串号。
2. 注册阶段 cookie 捕获依赖 adapter 现有 `session_cookies`；字段不全时 auth_bundle 自动降级 sso_only。
3. 并行 Token Poll 与 sidecar 超时竞态需观察 `slow_down` / denied。
4. 6h refresh KPI 依赖线上时间，本地无法证明。
5. New API TTFT 在线探针未接生产。

## 未完成事项

- 服务器部署与真实流量灰度
- New API TTFT 在线采集对接（本地仅提供采样接口）
- 状态脚本 `grok_pool_status.py` 更新
- Cookie CF 类实验
- 自动灰度扩大逻辑的线上闭环

## 建议部署顺序

1. 备份 → 部署代码 flags 全默认 → 健康检查
2. 确认 metrics 写入
3. `ROUTE_STICKY=1`
4. `PIPELINE_V2=1` + mint worker profile
5. Cookie 10%
6. approver-2 Warm
7. Parallel poll
8. Adaptive（最后）

## 回滚步骤

见 §17；核心：flags 归零 + 停止 mint worker + 必要时镜像回滚 + 检查 pending_sso 与 auth.json。

## 21. 验收打回修复记录（2026-07-12 第二轮）

针对主验收 Agent 阻断项的修复：

| 阻断 | 修复 |
|------|------|
| P0 Probe `_Creds` 缺 email/user_id | 使用真实 `GrokCredentials(token,email,user_id,auth_key=None,...)`；`auto_disable=False`；`report_stats=False` |
| P1 非原子 Claim | `BEGIN IMMEDIATE` + 条件 `UPDATE ... WHERE state=? AND lease...`；rowcount 校验 |
| P1 mint_running 崩溃不可恢复 | claim 同时接受过期 lease 的 `mint_running` |
| P1 FAILED 堵容量 | capacity 终态含 `FAILED`/`DEAD_LETTER`/`AUTH_IMPORTED`；重试一律回 `mint_queued` |
| P1 Route 不全链路 | 创建 session 时分配 route + register proxy；token poll 用 `ProxyHandler` 每请求代理，不改全局 env |
| P1 双消费者 | pending `owner=mint_queue`；pending-recovery 跳过；v2 不再 inline fallback |
| P1 Cookie volume | ruyiPage 两个 sidecar 挂载 `./data:/app/data:ro`；并支持 `extra_cookies` 内联 |
| P1 Cookie 清理 | 导入成功删除 pending + cookie bundle |
| P1 A/B 分桶 | `int.from_bytes(digest[:8],'big') % 10000`；分布测试约 10% |
| P1 denied 误判 | denied/rate_limit **先于** success；`done?error=access_denied` 失败 |
| P1 凭据暴露 | `_compact_session` 去掉 password/yescaptcha/full sso/proxy secret；auth_store `chmod 0600` |
| P1 Git 边界 | 去掉 `.git/info/exclude` 对 `integrations/` 的整目录忽略；`device_approver.py` 可跟踪 |
| P2 mint_queued 当 imported | 单独 `mint_queued` 计数，不计入 imported |
| P2 CPU | mint worker 默认 0.05 CPU |

本地验证：
- tests/ 47 PASS
- legacy 19 PASS
- 8 线程 claim stress 20 jobs 0 双领
- 未部署服务器；Feature Flag 默认仍关闭


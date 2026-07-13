# Linux 服务器部署

该部署由三个容器组成：API、单实例无头 Firefox 审批器、持续注册调度器。调度器始终通过 API 发起任务，因此 `concurrency` 限制覆盖创建邮箱、注册、审批、换取 Token 和导入账号池的完整生命周期。

## 启动

```bash
cp .env.example .env
# 填写管理员密码、MoeMail、YesCaptcha、代理等密钥；不要提交 .env
docker compose -f docker-compose.server.yml config -q
docker compose -f docker-compose.server.yml up -d --build
docker compose -f docker-compose.server.yml ps
```

至少配置：

```env
GROK2API_ADMIN_PASSWORD=<strong-random-password>
GROK2API_MOEMAIL_API_KEY=<secret>
GROK2API_MOEMAIL_BASE_URL=https://...
GROK2API_MOEMAIL_DOMAIN=example.com
GROK2API_YESCAPTCHA_KEY=<secret>
GROK2API_XAI_PROXY=http://user:pass@proxy.example:port
GROK2API_REQUIRE_API_KEY=1
```

默认只监听 `127.0.0.1:3000`，供同机反代或 New API 使用。如确需监听公网，显式设置 `GROK2API_BIND_ADDRESS=0.0.0.0`，并同时配置防火墙、HTTPS 反代和 API Key。

## 资源和并发

默认资源上限合计约为 `2.35 CPU / 2.4 GiB RAM`：

- API：`1 CPU / 768 MiB`
- ruyiPage：`1.25 CPU / 1.5 GiB`，`768 MiB /dev/shm`
- 调度器：`0.1 CPU / 96 MiB`

要给整机保留约 40% 空闲资源，应满足：

```text
容器 CPU 上限总和 <= 主机逻辑 CPU × 0.60
容器内存上限总和 <= 主机可用内存 × 0.60
```

无头审批器当前串行处理浏览器任务，因此默认生产并发为 `1`。在没有完成持续压测前，不建议把注册并发调高。若主机至少 8 vCPU / 16 GiB，且代理与 xAI 没有限流，可先逐级验证 `2`，不要直接跳到更高值：

```env
GROK2API_PRODUCER_BATCH_SIZE=2
GROK2API_PRODUCER_CONCURRENCY=2
GROK2API_REG_MAX_CONCURRENCY=2
```

每次只改变一个参数，并观察至少 2 小时的成功率、`rate_limited`、内存峰值和 sidecar 队列等待时间。由于单 sidecar 内部有全局锁，提高 API 注册并发不会提高审批吞吐；若要真正提高并发，需要按副本分片审批器，而不是去掉锁。

## 24 小时运行与续期

`registration-producer` 完成一批后等待默认 45 秒再启动下一批；失败会以 30 秒起步指数退避，最大 15 分钟，避免持续撞击上游。三个容器均设置 `restart: unless-stopped` 和健康检查。

默认仅 `pending-recovery` 容器消费 legacy pending（`GROK2API_PENDING_RECOVERY=1`），主 `registration-producer` 必须为 0，避免双消费者。

注册已经取得 SSO、但 Device Flow 暂时失败时，API 会以 `0600` 权限将恢复材料保存在 `data/pending_sso/`。生产器在新批次之前串行恢复这些文件：默认跳过 120 秒内仍可能由 API 处理的文件，每轮最多恢复 2 个；成功导入账号池后才删除，失败则保留并按 2 分钟起步、最长 1 小时指数退避。日志会过滤 SSO/JWT，不会输出恢复材料。相关开关：

```env
GROK2API_PENDING_RECOVERY=0
GROK2API_PENDING_MIN_AGE_SEC=120
GROK2API_PENDING_MAX_PER_CYCLE=2
GROK2API_PENDING_RETRY_BASE_SEC=120
GROK2API_PENDING_RETRY_MAX_SEC=3600
```

`registration-producer` 必须与 API 共享 `./data:/app/data`；服务器 Compose 已包含该挂载。不要把 `data/pending_sso` 放入日志、备份公开目录或镜像层。

Access Token 的约 5 小时有效期不是账号寿命。API 内置 `token_maintainer.py`，默认提前 15 分钟进入刷新窗口，并使用 `refresh_token` 自动续期；服务器 compose 强制启用维护器。可通过 `/health` 中的 `token_maintainer.running` 和管理后台维护状态确认它在运行。

停止持续生产但保持 API：

```bash
docker compose -f docker-compose.server.yml stop registration-producer
```

## 资源口径（2026-07-13 更新）

用户已接受 Pipeline v2 小幅超额，不再要求压回 2.40 CPU。

| 模式 | 约计 CPU | 约计内存 |
|------|----------|----------|
| 默认 Compose | 2.45 CPU | 4144 MiB |
| Pipeline v2（含 mint worker 0.05） | 2.50 CPU | 4240 MiB |

API 默认 `cpus=0.40`；两 ruyiPage 各 0.80；两 mihomo 各 0.15；producer 0.10；pending-recovery 0.05；mint worker 0.05。

## Pipeline v2 灰度（注册性能优化）

完整计划见：

```text
docs/GROK_REGISTRATION_SPEED_OPTIMIZATION_PLAN.md
```

默认 **全部关闭**，行为与旧版一致。按阶段打开（每次只开一档，观察 ≥2 小时）：

```env
# 阶段0：仅观测（默认 metrics on）
GROK2API_METRICS_ENABLED=1

# 阶段1：Route Affinity
GROK2API_ROUTE_STICKY=1

# 阶段2：持久化 Mint 队列（需启动 mint worker profile）
GROK2API_PIPELINE_V2=1

# 阶段3：Cookie Bundle 实验 10%
GROK2API_COOKIE_MODE=sso_only
GROK2API_COOKIE_EXPERIMENT_PERCENT=10

# 阶段4：仅 approver-2 Warm Browser
GROK2API_RUYIPAGE_WARM_BROWSER=1

# 阶段5：并行 Token Poll
GROK2API_PARALLEL_TOKEN_POLL=1

# 阶段6：自适应调度（最后）
GROK2API_ADAPTIVE_SCHEDULER=1
```

启动 mint worker（仅 pipeline v2）：

```bash
docker compose -f docker-compose.server.yml --profile pipeline-v2 up -d registration-mint-worker
```

资源硬预算目标合计 ≤ 2.40 CPU。API 默认 cpus 已对齐为 `0.40`（可用 `GROK2API_API_CPUS` 覆盖）。

Flag 回滚：将上述开关全部置 0 / `sso_only` 并停止 mint worker：

```bash
docker compose -f docker-compose.server.yml --profile pipeline-v2 stop registration-mint-worker
```

数据路径（权限 0600/0700）：

```text
/app/data/registration_queue.db
/app/data/registration_metrics.db
/app/data/cookie_bundles/
/app/data/pending_sso/
```

**验收 KPI 不是启动注册数**，而是最终可用账号 / 小时与 6 小时 refresh 存活。未完成主验收 Agent 检查前不要全量打开 Cookie Bundle 或 Adaptive。

查看关键日志：

```bash
docker compose -f docker-compose.server.yml logs -f --tail=200 grokcli-2api registration-producer ruyipage-approver
```

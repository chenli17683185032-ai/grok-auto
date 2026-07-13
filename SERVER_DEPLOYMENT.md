# Linux 服务器部署与回滚

本手册对应 `docker-compose.server.yml`。推荐生产模式为 Pipeline v2：注册完成 SSO 后写入持久化 SQLite 队列，两台 Mint Worker 分别使用两条固定代理 Route 和两台 ruyiPage Sidecar；Access Token、额度等待、失败确认和临时文件清理由后台维护任务处理。

## 1. 拓扑与资源

资源上限按 Compose 声明统一计算：

| 模式 | 服务数 | CPU 上限 | 内存上限 |
|---|---:|---:|---:|
| 默认模式 | 7 | 2.45 CPU | 4144 MiB |
| Pipeline v2 | 9 | 2.55 CPU | 4336 MiB |

统一运维口径：默认 `7 services / 2.45 CPU / 4144 MiB`；Pipeline v2 `9 services / 2.55 CPU / 4336 MiB`。

默认模式的 7 个服务：两台 mihomo、API、两台 ruyiPage、Producer、Pending Recovery。Pipeline v2 再增加 `registration-mint-worker` 和 `registration-mint-worker-2`。

明细：

| 服务 | CPU | 内存 |
|---|---:|---:|
| grok-mihomo x2 | 0.15 x2 | 192 MiB x2 |
| grokcli-2api | 0.40 | 768 MiB |
| ruyipage-approver x2 | 0.80 x2 | 1400 MiB x2 |
| registration-producer | 0.10 | 96 MiB |
| pending-recovery | 0.05 | 96 MiB |
| registration-mint-worker x2（仅 v2） | 0.05 x2 | 96 MiB x2 |

两台 Sidecar 各自串行审批，因此 Pipeline v2 的稳定注册并发上限默认设为 `2`。提高 API 或 Producer 并发但不增加独立 Sidecar/Route，不会提高审批吞吐。资源可以小幅超额，但应持续观察 `MemAvailable`、容器 OOM、代理限流和最终可用账号/小时。

## 2. 必要配置

创建服务器专用环境文件，权限必须为 `0600`：

```bash
cp .env.example .env
chmod 600 .env
```

至少设置以下变量，不要把值写进日志、命令历史、提交或运维手册：

```env
GROK2API_ADMIN_PASSWORD=<secret>
GROK2API_MAIL_PROVIDER=yyds
GROK2API_YYDSMAIL_API_KEY=<secret>
GROK2API_YYDSMAIL_BASE_URL=https://maliapi.215.im/v1
# 可选；留空时服务端自动选择健康域名
GROK2API_YYDSMAIL_DOMAIN=
GROK2API_YESCAPTCHA_KEY=<secret>
GROK2API_REQUIRE_API_KEY=1

# Pipeline v2 必须同时开启
GROK2API_PIPELINE_V2=1
GROK2API_ROUTE_STICKY=1

GROK2API_APP_IMAGE=grokcli-2api:2026.07.13-round8
GROK2API_RUYIPAGE_IMAGE=ruyipage-headless:2026.07.13-round8
GROK2API_MIHOMO_IMAGE=metacubex/mihomo:v1.19.28
GROK2API_MIHOMO_CONFIG_DIR=/opt/new-api/mihomo
GROK2API_MIHOMO2_CONFIG_DIR=/opt/grokcli-2api/mihomo-2
GROK2API_NEW_API_NETWORK=app_yunbay-network
```

邮箱及 YesCaptcha 密钥只能保存在权限为 `0600` 的 `.env`，不要写入 Compose、请求体、命令历史、日志、提交或运维记录。`https://vip.215.im/docs` 是 YYDS 文档入口，实际 API 基址是 `https://maliapi.215.im/v1`。YYDS 邮箱约保留 24 小时，收件请求使用长轮询；服务端或反向代理的读取超时必须覆盖验证码等待窗口，单次长轮询超时不代表邮箱失效。

旧 MoeMail 部署继续受支持：设置 `GROK2API_MAIL_PROVIDER=moemail`，并配置 `GROK2API_MOEMAIL_API_KEY`、`GROK2API_MOEMAIL_BASE_URL`、`GROK2API_MOEMAIL_DOMAIN`。Preflight 会按选中的 Provider 检查对应配置；YYDS 域名不是必填项。

禁止使用 `latest`。回滚依赖版本化镜像 Tag；更严格的环境可将三项镜像配置为 Digest。

默认监听 `127.0.0.1:3000`，供同机 New API 或反向代理使用。如需公网监听，显式设置 `GROK2API_BIND_ADDRESS=0.0.0.0`，并同时启用 HTTPS、防火墙和 API Key。

## 3. 上线顺序

固定顺序为：

```text
preflight -> backup -> up -> smoke -> observe -> rollback（需要时）
```

Preflight 会检查必需变量是否存在、两套 mihomo 配置、外部 Docker Network、CPU/内存/磁盘、挂载权限、Compose、镜像架构和版本。外部 Network 不存在时会在 timeout 内创建；在任何数据库迁移前，脚本通过 SQLite backup API 将 Queue/Metrics DB 做一致性备份，并将部署环境一起保存到 `data/backups/preflight-<UTC timestamp>/`。脚本不打印变量值。

```bash
./scripts/server_preflight.sh
```

启动完整 Pipeline v2，两个 Mint Worker 必须同时启动：

```bash
docker compose --env-file .env -f docker-compose.server.yml \
  --profile pipeline-v2 up -d --build \
  grok-mihomo grok-mihomo-2 grokcli-2api \
  ruyipage-approver ruyipage-approver-2 \
  registration-producer pending-recovery \
  registration-mint-worker registration-mint-worker-2
```

执行有界 Smoke：

```bash
GROK2API_SMOKE_TIMEOUT_SEC=360 ./scripts/smoke_server.sh
```

Smoke 验证 API readiness、无 Key 拒绝、Admin 登录、双 Sidecar、双代理 Route、隔离合成 Queue handoff、双 Mint claim 和 lease recovery。所有循环和网络调用都有 timeout。

上线后至少观察 2 小时：

```bash
docker compose --env-file .env -f docker-compose.server.yml \
  --profile pipeline-v2 ps

docker compose --env-file .env -f docker-compose.server.yml \
  --profile pipeline-v2 logs -f --tail=200 \
  grokcli-2api registration-producer pending-recovery \
  registration-mint-worker registration-mint-worker-2 \
  ruyipage-approver ruyipage-approver-2
```

## 4. 停止与回滚

只停止新账号 Mint，不中断 API：

```bash
docker compose --env-file .env -f docker-compose.server.yml \
  --profile pipeline-v2 stop \
  registration-mint-worker registration-mint-worker-2
```

停止持续注册但保留 API 与 Pending Recovery：

```bash
docker compose --env-file .env -f docker-compose.server.yml \
  --profile pipeline-v2 stop registration-producer \
  registration-mint-worker registration-mint-worker-2
```

完整回滚使用 Preflight 生成的备份目录。脚本会同时停止两个 Mint Worker，恢复版本化环境/必要 DB，随后对所有读取环境变量的 API、Producer、Sidecar、Proxy 和两个 Mint Worker执行 `--force-recreate`，最后运行有界 Smoke：

```bash
GROK2API_ROLLBACK_BACKUP_DIR=/opt/grokcli-2api/data/backups/preflight-YYYYMMDDTHHMMSSZ \
  ./scripts/rollback_server.sh
```

也可显式指定旧镜像：

```bash
GROK2API_ROLLBACK_APP_IMAGE=grokcli-2api:<old-tag> \
GROK2API_ROLLBACK_RUYIPAGE_IMAGE=ruyipage-headless:<old-tag> \
GROK2API_ROLLBACK_MIHOMO_IMAGE=metacubex/mihomo:<old-tag> \
GROK2API_ROLLBACK_BACKUP_DIR=<backup-dir> \
  ./scripts/rollback_server.sh
```

## 5. 账号生命周期

### Token 自动续期

约 5 小时的 Access Token 有效期不是账号寿命。`token_maintainer.py` 会提前进入刷新窗口并使用 Refresh Token 自动续期。刷新失败采用多周期确认：

- 第一次明确 `invalid_grant` 进入 `refresh_pending_confirmation`，不是终局。
- 未过期 Access Token 继续服务。
- 网络、超时和 5xx 不增加明确失败次数。
- 至少 3 次明确失败，并至少一次发生在 Access Token 到期后，才进入 `refresh_terminal`。
- 后续成功会清空整轮失败证据。

### 1M / 24 小时额度

账号每天约 1M Token 用完后进入 `quota_waiting`，不应立即删除或反复轮询。到达 Reset 后通过真实 `/v1/responses` 小请求验证：

- 恢复成功立即回到 active。
- 普通 429、网络、超时和 5xx 只延后 Probe，不计失败证据。
- 只有明确 `free_usage_exhausted` 才增加确认。
- 至少 3 次确认、跨至少 2 个维护周期并经过 Grace，才进入 `quota_reset_failed`。
- 清理账号仍默认 Dry Run，并且还有 Producer 二次观察窗口。

### 手工禁用与凭据封禁

`manual_disabled`、`credential_suspended`、Quota 和 model block 相互独立。Quota 恢复不会重新启用手工禁用账号；手工启用会原子清除 credential suspend，但不会擅自清除 Quota 或 model block。

## 6. Retention 与数据路径

主 `registration-producer` 是 Retention Owner，并使用跨进程文件锁避免重复维护：

```text
Terminal Queue Job: 7 days
Metrics Event: 7 days + 200000 row cap
Cookie Bundle / Pending SSO: 48 hours
Single cleanup batch: 200 rows/files
```

Active Job 引用的 SSO/Cookie 文件永远不被 Sweeper 删除。每轮结果写入 `data/retention_status.json`，只记录时间和数量，不记录秘密。

关键路径：

```text
/app/data/auth.json                         0600
/app/data/settings.json                     0600
/app/data/keys.json                         0600
/app/data/registration_queue.db             0600
/app/data/registration_metrics.db           0600
/app/data/pending_sso/                       0700
/app/data/cookie_bundles/                    0700
```

所有 9 个 Compose 服务均使用 Docker `json-file` 日志轮转：`max-size=10m`、`max-file=3`。

## 7. 并发调整

初始生产值：

```env
GROK2API_PRODUCER_BATCH_SIZE=2
GROK2API_PRODUCER_CONCURRENCY=2
GROK2API_REG_MAX_CONCURRENCY=2
```

每次只改变一个变量，并观察最终可用账号/小时、Mint Queue oldest age、`rate_limited`、refresh 存活率、内存峰值和两个 Sidecar 的利用率。不要以“启动注册数”作为吞吐指标。

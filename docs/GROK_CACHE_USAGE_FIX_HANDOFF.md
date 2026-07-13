# Grok 缓存用量透传修复交接方案

更新时间：2026-07-13（Asia/Shanghai）

## 1. 固定位置

### 计划方案

本文件是本次工作的唯一执行方案：

```text
/Users/ethan/Documents/grok/docs/GROK_CACHE_USAGE_FIX_HANDOFF.md
```

不要另建第二份计划，不要把旧聊天内容当成最终要求。如实现过程中发现本计划与生产运行态冲突，先在产物报告中记录证据，停止扩大修改范围，交回验收者判断。

### 实现工作区

所有代码修改必须在已经创建的隔离 worktree 中完成：

```text
/Users/ethan/Documents/grok-cache-usage-fix
```

对应分支：

```text
codex/grok-cache-usage-fix
```

不要修改原工作区 `/Users/ethan/Documents/grok` 中的源代码。原工作区包含其他尚未提交的云贝/Grok 工作，禁止 reset、clean、checkout 覆盖或回滚。

### 生成文件

代码文件放在实现工作区的正常源码位置。预计只允许改动或新增：

```text
/Users/ethan/Documents/grok-cache-usage-fix/app.py
/Users/ethan/Documents/grok-cache-usage-fix/test_usage_cache.py
```

诊断脚本、脱敏原始用量、测试报告和差异报告统一放在：

```text
/Users/ethan/Documents/grok-cache-usage-fix/artifacts/grok_cache_usage_fix/
```

建议产物名称：

```text
probe_raw_usage.py
raw_usage_before.json
raw_usage_after.json
test-report.txt
changed-files.txt
implementation-summary.md
```

产物目录不得包含 Access Token、Refresh Token、API Key、邮箱、账号 ID、请求正文、完整响应正文、Cookie、SSH 私钥或数据库密码。原始 `usage` JSON 可以保留，但必须删除所有非用量字段。

## 2. 问题结论与已知证据

云贝生产 New API 的 Grok 调用链为：

```text
客户端 -> 云贝 New API -> 渠道 41（Grok 专用）
       -> http://grokcli-2api:3000 -> cli-chat-proxy.grok.com/v1
```

已完成的只读检查：

- 生产 New API 和 PostgreSQL 均 healthy。
- 2026-07-12 04:58 至 2026-07-13 00:47 共 249 次 Grok 消费日志。
- `cache_tokens > 0` 为 0 次。
- 其中 238 次使用了上游返回的输入/输出用量，只有 11 次使用 New API 本地 token 估算。
- 最大单次输入为 144,336 tokens，因此不能简单归因于提示词低于缓存阈值。
- 云贝 New API 已支持读取 `usage.prompt_tokens_details.cached_tokens`，无需修改 New API 前端或后端。
- 生产 Grok 容器中的 `/app/app.py` 与本地 `app.py` 一致。
- `app.py::_normalize_usage()` 当前会重建 `usage`，仅返回 `prompt_tokens`、`completion_tokens`、`total_tokens`，所有缓存明细都会被丢弃。

因此确定存在一个中转层字段丢失缺陷；但在抓取中转归一化之前的原始 `usage` 前，不能断言 Grok Build 上游是否实际返回缓存命中量。

## 3. 修复目标

成功标准必须同时满足：

1. 上游提供标准缓存字段时，中转返回给 New API 的最终非流式响应和流式终止帧都保留缓存值。
2. 上游使用兼容别名时，只在有明确数值证据的情况下规范到 `prompt_tokens_details.cached_tokens`。
3. 不根据提示词长度、重复前缀、账号轮询或历史消息自行估算缓存。
4. 原有输入、输出、总量和 fallback 逻辑保持不变。
5. 不从 `prompt_tokens` 中减去缓存 token。OpenAI 语义下 `prompt_tokens` 通常包含缓存部分，New API 会自行按缓存明细拆分结算。
6. 上游完全不提供缓存字段时，结果仍然是缓存 0/缺失，不伪造命中。
7. 所有新增测试通过，原有测试不回归。

## 4. 执行步骤

### 步骤 A：抓取归一化前的原始 usage

在修改代码前，创建脱敏探针：

```text
/Users/ethan/Documents/grok-cache-usage-fix/artifacts/grok_cache_usage_fix/probe_raw_usage.py
```

探针要求：

- 在生产 `grokcli-2api-grokcli-2api-1` 容器内运行，复用容器已有代码和凭据读取逻辑。
- 固定同一个健康账号、同一个 `grok-4.5` 模型。
- 连续发送两次完全相同的稳定长前缀，前缀至少约 2,000 tokens，输出限制在 8 至 16 tokens。
- 请求必须包含 `stream_options.include_usage=true`。
- 直接请求 Grok Build 上游，绕过 `_normalize_usage()`，只收集最终 `usage` 对象。
- 输出中只允许出现请求序号、HTTP 状态、`usage` 的字段名和数值。
- 不打印账号邮箱、账号 key、Bearer Token、请求正文、回答正文或响应头中的身份信息。
- 将两次脱敏结果保存为 `raw_usage_before.json`。

生产连接只以以下文档为准：

```text
/Users/ethan/Desktop/云贝/服务器相关/yunbay-new-api-vps-连接信息.md
```

如果原始上游两次都没有任何缓存字段，仍继续完成“字段不丢失”的代码修复；报告必须明确说明“修复可透传字段，但当前 Grok Build 上游未报告缓存，生产后台仍可能显示 0”。

### 步骤 B：实现最小缓存字段规范化

修改 `app.py::_normalize_usage()`，原则如下：

1. 返回类型从 `dict[str, int]` 调整为 `dict[str, Any]`。
2. 继续以现有逻辑计算 `prompt_tokens`、`completion_tokens`、`total_tokens`，不要重写计数算法。
3. 对以下标准明细对象执行浅复制并透传，避免引用上游对象后再原地修改：

```text
prompt_tokens_details
completion_tokens_details
input_tokens_details
```

4. 最终是 OpenAI Chat Completions 响应，因此 New API 的规范入口必须是：

```json
{
  "usage": {
    "prompt_tokens_details": {
      "cached_tokens": 123
    }
  }
}
```

5. 缓存读取候选按“标准优先、明确正值优先”的原则处理：

```text
prompt_tokens_details.cached_tokens
input_tokens_details.cached_tokens
prompt_cache_hit_tokens
cached_tokens
cache_read_input_tokens
```

如果多个候选冲突：

- 优先选择第一个合法正整数；
- 多个正整数冲突时按上面的顺序选择；
- 没有正数但存在明确的 0 时，可以保留 0；
- 布尔值不视为整数；
- 负数、浮点字符串、对象、数组和不可转换值不得写入缓存字段。

6. 如果上游已经提供 `prompt_tokens_details`，在其浅复制上补充规范缓存值，保留其中其他字段，例如 `text_tokens`、`audio_tokens`、`image_tokens`。
7. 如果只提供 `input_tokens_details`，保留原字段，同时复制为 `prompt_tokens_details`，使 New API Chat Completions 计费链能读取。
8. 只有顶层兼容字段存在时，创建最小的 `prompt_tokens_details`。
9. `cached_creation_tokens` 只透传已有标准明细值。除非步骤 A 的真实上游证据显示了明确等价字段，否则不要自行把其他字段映射成缓存创建量。
10. 不修改 New API 仓库；它已经支持标准缓存字段。
11. 不修改 `anthropic_compat.py`，除非测试证明 `/v1/messages` 路径也经过相同字段丢失且与本次云贝 Grok 渠道直接相关。若确需修改，先在 `implementation-summary.md` 写明证据和最小范围，等待验收者决定，不得自行扩大提交。

推荐把“读取合法非负整数”和“选择缓存候选”实现为一到两个短小私有 helper；不要引入类、配置项或通用用量框架。

### 步骤 C：新增定向测试

新增：

```text
/Users/ethan/Documents/grok-cache-usage-fix/test_usage_cache.py
```

至少覆盖：

1. 标准 `prompt_tokens_details.cached_tokens` 被保留。
2. `prompt_tokens_details` 中除缓存外的明细字段被保留。
3. 只有 `input_tokens_details.cached_tokens` 时，会生成规范的 `prompt_tokens_details.cached_tokens`，并保留原 `input_tokens_details`。
4. 顶层 `prompt_cache_hit_tokens` 能规范化。
5. 顶层 `cached_tokens` 能规范化。
6. `cache_read_input_tokens` 只有在没有更高优先级正值时才采用。
7. 标准位置为 0、兼容位置为正数时采用明确正值。
8. 多个正值冲突时遵循固定优先级。
9. 负数、布尔值、无效字符串不会变成缓存 token。
10. 原有 prompt/completion/total 读取不变。
11. prompt 或 completion 缺失时原 fallback 行为不变。
12. `_sse_chunk()` 生成的流式终止帧包含嵌套缓存明细。
13. 上游不提供缓存字段时，不生成虚假正缓存值。

测试必须断言具体 JSON 结构，不能只断言函数没有抛异常。

### 步骤 D：本地验证与自审

在隔离 worktree 运行：

```bash
cd /Users/ethan/Documents/grok-cache-usage-fix
python -m pytest -q test_usage_cache.py
python -m pytest -q test_history_compact.py test_tool_stream_emit.py test_usage_cache.py
python -m py_compile app.py test_usage_cache.py
git diff --check
git status --short
```

把完整但不含 secret 的摘要写入：

```text
artifacts/grok_cache_usage_fix/test-report.txt
artifacts/grok_cache_usage_fix/changed-files.txt
artifacts/grok_cache_usage_fix/implementation-summary.md
```

`changed-files.txt` 必须证明代码提交范围仅包含预期文件。诊断产物可以保持未跟踪，不要把生产探针输出提交到 Git。

### 步骤 E：提交并交回验收

在 `codex/grok-cache-usage-fix` 分支创建一个聚焦提交，提交内容只允许包括：

```text
app.py
test_usage_cache.py
```

不要提交 `artifacts/`，不要合并到 `main`，不要 push，不要部署生产，不要重启任何服务器容器。

交回信息必须包含：

- 分支名；
- 提交 hash；
- 修改文件列表；
- 测试命令与通过数量；
- `raw_usage_before.json` 的字段级结论；
- 是否确认 Grok Build 上游实际报告缓存；
- 所有产物的绝对路径；
- 仍然存在的风险或不确定性。

## 5. 禁止事项

- 禁止伪造、估算或按重复前缀推算缓存 token。
- 禁止为了让后台“出现缓存”而写死正数。
- 禁止修改云贝 New API 前端展示或数据库历史日志。
- 禁止回填过去 249 条 Grok 日志。
- 禁止把 `prompt_tokens` 减去 `cached_tokens`。
- 禁止输出或保存任何 secret、账号身份或请求正文。
- 禁止修改 `/Users/ethan/Documents/grok` 原工作区中的现有未提交文件。
- 禁止执行 `git reset --hard`、`git clean -fd`、`rsync --delete`。
- 禁止直接部署生产或重启容器。
- 禁止新增大型抽象、第三方依赖或与缓存无关的重构。

## 6. 最终验收责任

最终验收由当前主 Agent 完成，不由实施 Agent 自行宣布完成。

主 Agent 收到提交后将执行：

1. 审查 `main...codex/grok-cache-usage-fix` 的完整 diff。
2. 检查是否存在 secret、越界修改和计费语义错误。
3. 在干净环境重新运行定向测试与现有回归测试。
4. 复核原始上游 `usage` 证据，确认实现覆盖真实字段而非猜测。
5. 将通过验收的最小改动整合到 `/Users/ethan/Documents/grok`。
6. 部署前在服务器创建带时间戳的 `app.py` 备份。
7. 构建并重启 Grok API 容器，所有等待命令设置明确超时，保证不会无限卡住；允许的服务短暂重启控制在 1 分钟内。
8. 验证 Grok API、New API 和数据库日志链路；分别记录“字段已透传”和“实际上游是否命中缓存”。
9. 部署完成后，把备份路径、部署命令摘要、容器健康状态、测试结果和运行态结论追加到现有文件：

```text
/Users/ethan/Desktop/云贝/服务器相关/yunbay-new-api-vps-连接信息.md
```

不为本次部署另建新的运维记录文件。

只有主 Agent 完成上述复核、部署验证和运维记录后，本任务才算最终验收通过。

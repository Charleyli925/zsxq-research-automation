# PR 2 — 建立单一状态核心与可迁移数据模型

- 状态：规划中
- 顺序：2 / 5
- 前置依赖：PR 1 已合并，状态语义和现网部署真源已经固定
- 合并要求：必须在 PR 3、PR 4 之前合并

## 目标

建立不依赖 OpenClaw、Shell 全局变量或多份 JSON 拼接的 pipeline 状态核心。SQLite 成为下载、提取、摘要、发布、通知和调度的唯一运行状态真源；现有 JSON/JSONL 通过只读 importer 迁移，之后只作为兼容导出和人工查看材料。

本 PR 只建立模型、事务、迁移和查询能力，不切换任何生产任务到新状态机。

## 事实与根因

### 已确认事实

1. 当前下载链至少使用以下状态：
   - `state/zsxq_foreign_reports_state.json`
   - `state/zsxq_domestic_cicc_reports_state.json`
   - `state/zsxq_autodownload_runs/*.json`
   - task-local `run_status.json`、`last_result.json`
   - `notification_state.json`、`notification_outbox.json`、`notification_messages.jsonl`
2. 当前摘要链至少使用以下状态：
   - `watch_state.json`
   - `pending_batch.json`
   - `run_status.json`、`last_result.json`
   - `stage_retry_ledger.json`
   - `notification_outbox.json`、`notification_messages.jsonl`
   - `publish_records.jsonl`
   - `quarantine.json`
   - `text_cache/`、`summary_cache/`
3. `ResearchLibrary/state/processed_files.sqlite` 当前是资料库索引，不是调度和重试真源。现有文档也明确要求不能仅依赖该库判断运行是否完成。
4. `last_result.json` 会被后续 no-op 检查覆盖，不能独立证明更早批次是否成功；`run_status.json` 可能停留在 waiting，而 retry ledger 已没有可执行项。
5. 发布已经具有 `remote_written → success` 的事务雏形，但状态存放在 JSONL；进程状态、文件阶段、发布状态和通知状态无法在一个事务里对账。
6. 文件身份同时使用 path、filename、normalized filename、file ID、PDF SHA256 和 text cache key。不同脚本各自决定优先级，迁移前必须固定统一 identity contract。

### 当前调用链与状态断点

```text
download wrapper
  -> JSON checkpoint + run manifest + task result
  -> archive PDF + processed_files.sqlite

digest scanner
  -> watch_state.json + pending_batch.json
  -> text_cache / summary_cache
  -> stage_retry_ledger.json
  -> publish_records.jsonl
  -> notification_outbox.json
```

任一阶段崩溃时，需要跨多个文件推断“远端是否已写入、摘要是否可复用、文件是否应该 ack、通知是否应该重发”。这正是状态假健康、重复判断和恢复脚本不断膨胀的根因。

## 修改清单

### `pyproject.toml`

- 启用 `src/` package discovery。
- 增加命令入口：

  ```text
  zsxq-pipeline = zsxq_pipeline.cli:main
  ```

- 继续使用 Python 标准库 `sqlite3`、`tomllib` 和 `fcntl`；本 PR 不增加数据库框架。

### `src/zsxq_pipeline/__init__.py`

- 定义 package version 和数据库 schema version。
- 不导入有浏览器、模型或飞书副作用的模块。

### `src/zsxq_pipeline/config.py`

- 定义结构化 pipeline 配置读取和校验。
- 区分：源码配置、runtime 路径、source 配置、飞书目标、模型参数。
- 拒绝未知关键字段和相对 runtime 根目录逃逸。
- 不读取现有 shell `config.env` 作为长期接口；legacy importer 可以读取明确允许的字段。

### `src/zsxq_pipeline/schema.py`

- 以显式 migration version 创建以下核心表：
  - `schema_migrations`
  - `runs`
  - `source_windows`
  - `documents`
  - `artifacts`
  - `stage_attempts`
  - `publications`
  - `notification_outbox`
  - `leases`
- 建立唯一约束：
  - source document：`source + source_file_id`
  - content：`pdf_sha256`
  - stage output：`document_id + stage + workflow_version`
  - summary：`pdf_sha256 + extractor_version + prompt_version + model`
  - publication：`summary_sha256 + target + partition_key`
  - notification：`idempotency_key`
- migration 只能向前、可重复执行；未知的更高 schema version 必须拒绝打开。

### `src/zsxq_pipeline/state.py`

- 提供短事务 API，不允许业务模块直接拼 SQL。
- 关键符号：
  - `PipelineState.open()`
  - `PipelineState.migrate()`
  - `register_source_window()`
  - `upsert_document()`
  - `record_artifact()`
  - `claim_due_stage()`
  - `complete_stage()`
  - `fail_stage()`
  - `record_remote_write()`
  - `complete_publication()`
  - `enqueue_notification()`
  - `derive_health()`
- 使用 `BEGIN IMMEDIATE` 完成 claim，确保两个进程不能同时领取同一阶段。
- 所有时间以带时区 ISO 8601 和 UTC epoch 双字段持久化；展示层再转换为 Asia/Shanghai。

### `src/zsxq_pipeline/model.py`

- 用 enum/dataclass 固定阶段和状态：

  ```text
  queued, running, succeeded, retry_wait,
  blocked_auth, blocked_release, quarantined
  ```

- 固定错误类别：`transient`、`auth`、`release_contract`、`content`、`invariant`。
- 禁止用自由文本决定是否重试；自由文本只作为诊断详情。

### `src/zsxq_pipeline/legacy_import.py`

- 只读解析现有下载和 digest JSON/JSONL/SQLite 索引。
- 先生成 import plan，包含：文件数、各阶段数、冲突、孤儿记录、远端写入但未成功记录、无法识别的路径。
- 默认不创建或修改数据库；显式 `--apply` 后只写新的 `pipeline.sqlite3`。
- import plan 带源文件 SHA256；apply 前重新验证，避免扫描后源状态已经变化。
- 导入过程幂等，重复 apply 不生成重复 document/publication/outbox。

### `src/zsxq_pipeline/status.py`

- 从 SQLite 推导 `healthy / degraded / blocked`，不能由最后一次命令退出码直接决定。
- 输出每个 source、stage 的 queued、running、retry_wait、blocked、quarantined 数量，以及最早可执行时间。
- blocked 项不得呈现为 waiting。

### `src/zsxq_pipeline/cli.py`

- 本 PR 只提供无外部副作用的命令：
  - `db migrate`
  - `legacy plan`
  - `legacy apply`
  - `status --json`
  - `doctor --state-only`
- `download`、`process`、`tick` 子命令由后续 PR 实现。

### `config/examples/pipeline.example.toml`

- 提供不含真实 ID、路径或凭据的配置结构。
- source、schedule、model、publish target 使用稳定逻辑名，不能把 `.openclaw` 目录写成架构要求。

### 新增测试

- `tests/test_pipeline_schema.py`
- `tests/test_pipeline_state.py`
- `tests/test_pipeline_legacy_import.py`
- `tests/test_pipeline_status.py`
- `tests/test_pipeline_cli.py`

测试覆盖 migration、唯一约束、并发 claim、崩溃后 lease 恢复、状态推导、导入幂等、源文件变化拒绝 apply 和敏感字段不进入日志。

### 文档

- `docs/architecture.md`：补充新状态模型，但标记生产尚未切换。
- `docs/source-of-truth.md`：定义 SQLite、artifact、Git release、remote Feishu 各自真源。
- `docs/project_conventions.md`：规定业务模块不得自行写 JSON 状态或裸 SQL。

## 边界条件

### 必须保持不变

- PDF 和摘要文件继续作为长期可读 artifact；数据库只保存身份、状态、hash 和路径，不把全文塞进数据库。
- `ResearchLibrary` 和 Obsidian 的现有目录结构保持不变。
- source window checkpoint 只能在计划内候选全部 downloaded、already satisfied 或 deterministically blocked 后推进。
- `remote_written` 和 `success` 必须是两个不同发布状态。
- quarantine 不得等同于成功发布，但可以结束自动重试。
- legacy import 永远不修改旧 JSON、JSONL、SQLite 索引或缓存文件。

### 明确不改

- 不调用知识星球、Playwright、Codex、OpenClaw 或 `lark-cli`。
- 不切换现有 wrapper、cron 或 LaunchAgent。
- 不处理真实 526 条积压，只用脱敏 fixture 验证 importer。
- 不删除任何旧状态文件或兼容字段。
- 不决定最终调度时间和 catch-up 策略；PR 5 负责。

## 验收标准

### 测试命令

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest \
  tests/test_pipeline_schema.py \
  tests/test_pipeline_state.py \
  tests/test_pipeline_legacy_import.py \
  tests/test_pipeline_status.py \
  tests/test_pipeline_cli.py -q
python3 -m pytest -q
python3 scripts/check_repository_hygiene.py
```

预期结果：全部退出 0；无测试访问真实 runtime 或网络。

### 关键回归场景

1. 对空库连续执行 migration 两次，schema 和数据均不变化。
2. 打开比代码更高的 schema version，明确拒绝，不能自动降级。
3. 两个连接同时 claim 同一 stage，只有一个成功。
4. `remote_written` 后模拟进程退出，重开数据库后 publication 仍等待 verify，不回到 create。
5. 526 条 `retry_exhausted` fixture 导入后全部是 blocked，不是 retry_wait。
6. 同一 PDF 不同路径但 SHA256 一致时，不生成两个 content identity；source document 记录仍分别保留。
7. 同一路径内容发生变化时，不能静默复用旧 artifact。
8. legacy plan 与 apply 之间源文件变化，apply 拒绝。
9. outbox 相同 idempotency key 重复 enqueue 只保留一条。
10. `status --json` 输出稳定 schema，blocked 数量不会被 healthy 覆盖。

## 停止条件

出现以下任一情况，暂停并汇报：

- PR 1 最终状态语义与本 PR 的 enum 不一致。
- 无法为 source file、PDF content、summary 或 publication 建立稳定唯一键。
- legacy 数据存在无法区分的远端重复发布，必须访问飞书才能决定导入状态。
- 需要修改 ResearchLibrary 实体文件才能完成状态导入。
- SQLite 文件位于不支持可靠锁语义的网络盘或同步盘。
- 业务需要跨多机并发，单机 SQLite 假设不成立。
- 新 package 必须引入 ORM、消息队列或服务端数据库才能完成当前范围。
- PR 2 开始触碰真实 scheduler、浏览器或模型调用。

## 未决风险

- `pdf_sha256` 在部分 legacy 项中可能缺失，需要读取本地 PDF 补算；该操作成本和失败比例尚未测量。
- 同一内容多 source、多文件名的产品语义需要在 importer fixture 上确认。
- `processed_files.sqlite` 现有 schema 与新 `documents/artifacts` 的映射尚未做全量冲突统计。
- SQLite WAL 在本机 APFS 上预计可用，但正式 runtime 路径和备份策略仍需验证。
- 状态库增长、vacuum 和长期归档策略尚未通过真实 500+ 文件规模基准。
- legacy JSON 中的自由文本错误无法全部可靠分类；未知项必须导入为 blocked/invariant，而不是猜测 transient。

## 与其他 PR 的关系

- PR 2 必须基于 PR 1 合并后的 main。
- PR 3 和 PR 4 都必须以本 PR 的 schema、状态 enum、identity key 和 claim API 为唯一接口。
- PR 3 与 PR 4 可在本 PR 合并后并行开发；禁止各自再定义第二套状态文件。
- PR 5 依赖本 PR 的 schedules、leases 和 outbox 表完成统一 tick。

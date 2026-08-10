# PR 5 — 统一调度、发布安装与最终切换

- 状态：规划中
- 顺序：5 / 5
- 前置依赖：PR 1–PR 4 已合并；PR 3 下载 canary 和 PR 4 摘要/飞书 canary 均通过
- 合并要求：严格最后合并；不得与其他 PR 并行切换生产

## 目标

用一个 launchd one-shot job 和一个 `zsxq-pipeline tick` 统一外资下载、中金下载、文本提取、摘要、发布和通知。调度器只负责发现“到期工作”并从 SQLite claim；每个阶段保持独立恢复。正式切换后停用旧两个下载 LaunchAgent 和 digest crontab，活跃运行代码与目录不再依赖 OpenClaw。

## 事实与根因

### 已确认事实

1. 当前外资和中金下载由两个 LaunchAgent 分别在固定时刻执行；digest 由 `*/10 * * * *` crontab 执行。
2. 三个任务使用各自 task directory、PID/lock、日志、status/result 和 notification outbox。
3. 下载与摘要本应是解耦阶段，但当前 digest 通过扫描目录、静默窗口和下载 task PID 推断上游是否完成。
4. macOS 睡眠、重启或错过固定时间后，scheduler 本身不会提供业务时间窗补偿；恢复依赖下次运行读取 checkpoint。
5. 旧 `mkdir`/PID lock 曾出现任务已死但长期 busy 的历史问题。单进程崩溃后不应留下需要猜测 owner 的锁。
6. 当前 installer 已要求 clean detached release，但运行 task dir 仍通过多个软链接指向 release 文件；digest 在 PR 1 前甚至不在同一部署 manifest 中。
7. PR 2 将提供单一 SQLite 状态；PR 3、PR 4 将使下载和处理阶段可由同一个 Python CLI 调用。因此本 PR 不需要再保留三套 scheduler。

### 目标调用链

```text
launchd StartInterval=300 + RunAtLoad
  -> versioned release/current/bin/zsxq-pipeline tick
  -> acquire process flock
  -> SQLite transaction: register due source windows
  -> claim due download/extract/summary/publish/notification work
  -> bounded work budget
  -> export status snapshot
  -> exit
```

### 根因

旧架构把“什么时候触发”和“业务阶段是否可继续”分散在 launchd、cron、PID、quiet window、目录 mtime 和多份 JSON 中。任何一处状态过期都可能表现为没有执行、长期 waiting 或重复补跑。统一 tick 后，scheduler 只依据 durable schedule/checkpoint/stage state 工作，不再通过另一个任务的进程文件推断业务真相。

## 修改清单

### `src/zsxq_pipeline/scheduler.py`

- 从配置读取 source logical name、时区和计划时刻。
- 每次 tick 计算从上次 durable schedule cursor 到当前时间的 due slots。
- 多个错过 slot 不逐个重复下载；对每个 source 合并为一个 catch-up window：`last_successful_check_at → now`。
- 只有 catch-up job 已持久化后才推进 schedule cursor。
- 机器长时间关机后仍由业务 checkpoint 覆盖缺失区间；设置最大回看范围时必须显式报告截断，不能静默跳过。

### `src/zsxq_pipeline/worker.py`

- 实现 `tick` 的有界工作循环：
  1. enqueue due source windows
  2. claim/download
  3. claim/extract
  4. claim/summary
  5. claim/publish
  6. drain notification outbox
- 每次 tick 有最大墙钟时间和每阶段配额；到期后安全退出，剩余工作由下次 tick 继续。
- 下游失败不阻塞上游：Codex 不可用仍可下载，Lark 不可用仍可提取和摘要。
- stage claim 使用 PR 2 的事务 API；不扫描 PID 文件判断其他阶段是否活跃。

### `src/zsxq_pipeline/lock.py`

- 使用 runtime-root 下的 `fcntl.flock` 防止 launchd 和人工 tick 重叠。
- 文件描述符关闭或进程死亡自动释放，不实现 stale PID 猜测。
- 未获得锁时返回明确 `busy`，不修改任何业务状态。

### `src/zsxq_pipeline/cli.py`

- 补齐最终命令：
  - `tick`
  - `run-stage`
  - `status`
  - `doctor`
  - `retry plan/apply`
  - `outbox drain`
- 人工命令与 launchd 使用同一 state/lock/配置，不存在旁路脚本。

### `config/examples/pipeline.example.toml`

- 固定外资、中金 schedule、Asia/Shanghai 时区、tick budget、worker count、runtime root 和外部 CLI 路径结构。
- 真实 chat ID、认证和本机路径继续位于 Git 外配置。

### `deploy/launchd/zsxq-pipeline.plist.template`

- 一个 user LaunchAgent：
  - `RunAtLoad=true`
  - `StartInterval=300`
  - one-shot，不使用 `KeepAlive` 循环
  - 显式 HOME、PATH、runtime config 和 release entrypoint
  - stdout/stderr 写到统一 runtime logs
- 不把固定 source 时刻硬编码在 plist；业务 schedule 位于 pipeline 配置和数据库。

### 部署 installer

将现有 `deploy/install_local_runtime.sh` 收敛为单 pipeline installer，或者用等价 Python installer 替代；最终行为必须包括：

- 只接受 clean detached tag/SHA。
- 安装到：

  ```text
  ~/Library/Application Support/zsxq-research-automation/releases/<git-sha>/
  ```

- `current` 通过原子 symlink 切换。
- 写 deployment manifest：SHA、schema version、Python、Playwright、Codex、lark-cli、CFT capability、配置 hash。
- 激活前运行 `doctor`；失败则不切 `current`、不 reload launchd。
- 检查旧三个任务和新 pipeline 均 idle 后才切换。
- 备份旧 plist/crontab 行和 task wrapper，提供显式 rollback 命令。
- 不复制或打印凭据。

### Runtime 迁移

- 新 runtime 根目录：

  ```text
  ~/Library/Application Support/zsxq-research-automation/
  ```

- 通过 PR 2 importer 导入旧状态；artifact 保持原路径，不批量移动 PDF、summary 或 Obsidian 文件。
- CFT 和 Lark profile 先以显式配置引用旧路径完成 canary；只有重新认证和 smoke 通过后，才能另行迁移路径。
- 路径名称中仍出现 `.openclaw` 不等于运行时依赖，但最终 doctor 应把它报告为 migration debt。

### 删除旧活跃入口

在新 pipeline soak 通过后，从仓库活跃路径删除：

- `openclaw_tasks/zsxq_download/` scheduler wrappers
- `openclaw_tasks/zsxq_pdf_digest/` cron wrappers
- 两个旧 download launchd templates
- 只服务旧 wrapper 的 result/status兼容代码

历史设计说明可以保留，但必须放到明确的 archive/history 文档，不得出现在新安装说明中。

### `scripts/check_repository_hygiene.py`

- 增加活跃代码禁止引用规则：
  - `openclaw agent`
  - 旧 task runtime entrypoint
  - `codex exec` 出现在 download 模块
  - `@playwright/mcp@latest`
  - 生产代码从开发仓库绝对路径运行

### 测试

新增：

- `tests/test_pipeline_scheduler.py`
- `tests/test_pipeline_worker.py`
- `tests/test_pipeline_lock.py`
- `tests/test_pipeline_tick.py`
- `tests/test_pipeline_installation.py`
- `tests/test_pipeline_cutover.py`

覆盖 missed slot、sleep/wake catch-up、同一 source coalescing、stage isolation、时间预算、安全锁、install/rollback、旧 scheduler 检测和 outbox 独立排空。

### 文档

- 重写 `README.md` 的运行入口。
- 更新 `docs/architecture.md`、`docs/project_flow_zh.md`、`docs/deployment.md`、`docs/runtime-recovery.md`、`docs/source-of-truth.md`。
- 新增正式 cutover runbook，逐步列出 pause、snapshot、import、doctor、activate、canary、soak、retire、rollback。

## 边界条件

### 必须保持不变

- 外资和中金的逻辑运行时刻、关键词、source config 和 checkpoint 连续性保持不变。
- 机器错过时刻后必须从最后成功 checkpoint 补到当前，而不是把“没有触发”当作空窗口。
- 每个 stage 的 artifact 和幂等键保持有效；调度切换不能重新下载、重新摘要或重复发布已完成项。
- 下载、摘要、发布、通知故障互相隔离。
- 正式部署继续要求 clean detached release、空闲检查和人工 release 决策。
- rollback 只切换代码/调度入口，不回滚或覆盖已经成功写入的业务数据。

### 明确不改

- 不移动或删除 ResearchLibrary、Obsidian、PDF、summary artifact。
- 不改变模型、prompt、关键词、飞书目标或通知文案。
- 不把 pipeline 改造成常驻 HTTP 服务、消息队列或多机系统。
- 不启用 launchd `KeepAlive` 无限重启。
- 不自动删除旧 runtime；只停用并只读保留到 soak 完成。
- 不自动更新 Codex、lark-cli、Playwright 或 CFT。
- 不在 PR 合并时自动部署生产。

## 验收标准

### 测试命令

```bash
python3 -m pytest \
  tests/test_pipeline_scheduler.py \
  tests/test_pipeline_worker.py \
  tests/test_pipeline_lock.py \
  tests/test_pipeline_tick.py \
  tests/test_pipeline_installation.py \
  tests/test_pipeline_cutover.py -q
python3 -m pytest -q
python3 scripts/check_repository_hygiene.py
plutil -lint deploy/launchd/zsxq-pipeline.plist.template
```

预期结果：全部退出 0；CI 不访问生产 runtime 或网络。

### 关键回归场景

1. 机器跨过两个外资 slot 后恢复：只创建一个 catch-up window，起点是最后成功 checkpoint。
2. tick 在 download 后被 SIGKILL：flock 自动释放；下次 tick 从数据库续跑。
3. 两个手动 tick 同时启动：只有一个 claim 工作，另一个返回 busy。
4. Codex 不可用：download 和 extract 继续，summary blocked/retry_wait，不影响 source checkpoint。
5. Lark 不可用：summary artifact 保留，publication/outbox 等待，不重新摘要。
6. notification outbox 到期但没有新下载：tick 仍独立发送。
7. 安装 doctor 失败：`current` 和现有 launchd 不变化。
8. 切换后回滚：新产生的数据库和 artifact 保留，旧代码不重复处理成功项。
9. DST/时区输入异常：配置预检拒绝，不能静默用系统默认时区。
10. 旧 cron 或旧 LaunchAgent 仍启用：cutover preflight 拒绝双跑。
11. source window、summary、publication 和 notification 分别注入失败，最终 health 与 stage 数量一致。

### 生产 cutover 验收

1. 备份旧 task state、plist、crontab 和部署 manifest；记录 hash，不修改 artifact。
2. 确认三个旧任务 idle。
3. legacy import 先 plan，再人工核对冲突和数量后 apply。
4. 新 release `doctor` 全部通过。
5. 激活新 LaunchAgent，同时停用旧两个 LaunchAgent 和 digest cron；禁止重叠窗口。
6. 手动执行一次 `tick --budget-seconds <small>`，只处理 canary 工作。
7. 外资、中金各通过至少 4 个真实计划窗口，总计至少 8 个窗口。
8. 至少执行一次进程强杀恢复、一次 Codex unavailable、一次 Lark unavailable 演练。
9. 确认无漏下载、无重复 PDF、无重复摘要、无重复飞书文档、无重复通知。
10. soak 通过后才把旧 runtime 标记 read-only archive；删除另开明确审批，不在本 PR 自动执行。

## 停止条件

出现以下任一情况，暂停并汇报：

- PR 3 或 PR 4 尚未完成真实 canary。
- legacy import 仍有无法解释的 document/publication 冲突。
- 新旧 scheduler 无法在一个明确时间点原子切换，存在双跑窗口。
- launchd 后台身份无法访问 CFT、Codex auth、Lark auth 或 runtime 目录。
- schedule catch-up 必须改变业务窗口或会造成 source API 不可承受的回看。
- rollback 需要覆盖数据库、删除远端文档或撤回通知。
- 发现除三个已知任务外还有生产入口会触发同一下载/摘要链。
- 需要引入常驻服务、外部数据库或消息队列才能保证单机正确性。
- 计划文件之外出现新的生产目录、凭据或 scheduler 需要迁移。

## 未决风险

- launchd 在不同 sleep/wake、用户未登录和系统升级场景下的触发行为仍需真实 soak；业务 checkpoint 可以补偿，但不能保证关机期间实时执行。
- 知识星球 source 可能限制长时间回看；最大 catch-up 窗口需要基于真实 API 行为确定。
- 旧 task state 与新 SQLite 在 cutover 瞬间的最后增量需要通过 idle gate 和最终 hash 固定。
- `releases/<sha>/current` 的磁盘清理策略尚未确定；清理必须保留当前和至少一个已验证 rollback release。
- CFT/Lark profile 最终迁移可能需要人工重新登录和 macOS 权限，不应成为主切换的隐式前提。
- 八个窗口只能证明短期稳定性，不能证明第三方网络或 DOM 永不变化；长期目标是失败可见、数据不丢和自动续跑。

## 与其他 PR 的关系

- PR 5 严格依赖 PR 1 的同 SHA/空闲部署原则、PR 2 的 SQLite 状态、PR 3 的确定性下载和 PR 4 的直连摘要/飞书。
- PR 5 不与任何前序 PR 并行合并或生产部署。
- 如果 PR 3、PR 4 并行开发，必须先串行合并并完成联合回归，再从最新 main 创建 PR 5。
- PR 5 合并不等于自动发布；release、部署、cutover 和旧链退役是独立人工门禁。

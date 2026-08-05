# OpenClaw 定时任务接入说明（知识星球外资研报）

## 1. 目标

在 OpenClaw 里创建一个定时任务，让 ClawBot 每次按以下顺序执行：

1. OpenClaw 定时触发 Codex（推荐）或直接执行任务 Prompt（备选）。
2. Codex 读取本地配置与状态文件。
3. Codex 使用 Playwright MCP 控制专用 Chrome for Testing（固定可执行文件 + 持久化 profile）。
4. Codex 在知识星球 `前沿信息收录` 的 `外资研报` 页面筛选并下载新文件。
5. Codex 调用本地归档脚本，把下载文件转存到批次目录并更新状态文件。

## 2. 前置条件

1. Mac 开机时任务才能执行；休眠或断电不会丢失检查点，下次登录后会由 LaunchAgent 补触发。
2. Chrome for Testing 可启动，并使用固定 profile 目录（首次需人工登录一次知识星球）。
3. OpenClaw 运行环境可访问以下目录：
   - `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload`
   - `${AUTOMATION_ROOT}`
   - `${DOWNLOADS_DIR}`
4. OpenClaw 任务可用工具至少包含：
   - Shell/Command 执行（用于触发 Codex）
   - 本地文件读写
5. 本机 Codex CLI 可执行文件存在：
   - `/Applications/Codex.app/Contents/Resources/codex`
6. 建议安装 Chrome for Testing，可执行文件默认路径：
   - `/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`

## 3. 关键文件

1. 任务模板 Prompt：
   `${AUTOMATION_ROOT}/prompts/openclaw_task_template.md`
2. 临时关注主题（可随时改）：
   `${AUTOMATION_ROOT}/prompts/openclaw_runtime_prompt.md`
3. 关键词规则（长期规则）：
   `${AUTOMATION_ROOT}/config/local/interest_keywords.json`
4. 任务状态（自动更新）：
   `${INVESTMENT_REPORTS_RUNTIME_DIR}/state/zsxq_foreign_reports_state.json`
5. 归档脚本（自动调用）：
   `${AUTOMATION_ROOT}/scripts/finalize_download_batch.py`
6. 下载归档根目录：
   `${DOWNLOADS_DIR}/ZSXQ-外资研报`
7. 推荐调度脚本（OpenClaw 直接调用）：
   `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/run.cron-safe.sh`
8. 推荐调度 Prompt（给 Codex 用）：
   `${AUTOMATION_ROOT}/prompts/openclaw_scheduler_prompt.md`
9. 运行日志目录：
   - OpenClaw 侧：`${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/cron.log`
   - Codex 侧：`${INVESTMENT_REPORTS_RUNTIME_DIR}/logs`
10. OpenClaw 侧状态回传文件：
   - `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/last_result.json`
   - `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/last_result.md`
11. 显式时间窗覆盖文件（补抓/回放用）：
   - `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/time_window_override.json`

## 4. 在 OpenClaw 新建任务（推荐模式：OpenClaw 触发 Codex）

1. 新建 Task，建议名称：`ZSXQ-外资研报定时下载`。
2. Workspace/CWD 设为：
   `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload`
3. 任务类型选 `Command/Shell`（或等价能力）。
4. 命令填：

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/run.cron-safe.sh
```

5. 调度设置为每天四次（本地时区 Asia/Shanghai）：
   - 08:00
   - 12:00
   - 16:00
   - 20:30
6. 超时建议至少 `30-45` 分钟（避免高峰期页面慢导致中断）。
7. 若平台支持重试，建议失败重试 `1` 次，间隔 `3-5` 分钟。

## 5. 备选模式（OpenClaw 直接跑 Prompt）

如果你的 OpenClaw 不支持命令任务，再用这个模式：

1. Task Prompt 使用：
   `${AUTOMATION_ROOT}/prompts/openclaw_scheduler_prompt.md` 的完整内容。
2. 需要在同一个任务里确保可调用 Playwright MCP + Shell。
3. 调度时间同推荐模式。

## 6. 首次上线前测试（强烈建议）

1. 先手动启动一次任务，让脚本自动拉起专用 Chrome for Testing。
2. 在该专用浏览器里人工登录知识星球一次（后续复用同一 profile）。
3. 再在 OpenClaw 里手动触发一次任务（Run now）。
4. 检查 OpenClaw 侧日志与回传文件：
   - `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/cron.log`
   - `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/last_result.md`
5. 检查 Codex 侧日志目录是否生成新文件：
   - `${INVESTMENT_REPORTS_RUNTIME_DIR}/logs/run_*.log`
6. 检查任务输出是否包含：
   - archived folder path
   - downloaded filenames
   - skipped filenames
7. 检查状态文件是否推进：
   - `last_successful_check_at`
   - `last_batch_dir`
   - `last_batch_file_count`
8. 检查归档目录是否产生新批次文件夹，命名应为：
   `YYYY-MM-DD_HH-MM-SS__to__YYYY-MM-DD_HH-MM-SS`

## 7. 日常维护方式

1. 临时改关注方向（建议）：
   直接改 `${AUTOMATION_ROOT}/prompts/openclaw_runtime_prompt.md`。
2. 长期改关键词规则：
   修改 `${AUTOMATION_ROOT}/config/local/interest_keywords.json`。
3. 不要手动修改状态文件，除非你明确要“回放历史时间窗”。

## 8. 补抓模式（OpenClaw 指定时间范围）

除定时模式外，可通过 OpenClaw 传入明确时间窗：

1. 编辑 `${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/time_window_override.json`：
   - `enabled=true`
   - `window_start=<ISO8601>`
   - `window_end=<ISO8601>`
   - `apply_once=true`（建议）
2. 手动触发一次任务（或由外部流程触发 `run.cron-safe.sh`）。
3. `run.sh` 会自动传递 `ZSXQ_WINDOW_START/ZSXQ_WINDOW_END` 给 Codex。
4. Codex 本次运行会按显式窗口筛选，并调用：
   - `python3 scripts/finalize_download_batch.py --window-start ... --window-end ...`
5. 若执行成功且 `apply_once=true`，脚本自动回写 `enabled=false`。

## 9. 常见阻塞与处理

1. 阻塞：专用 Chrome for Testing 未启动/不可连接，或知识星球登录失效。
   处理：检查专用浏览器是否可启动；必要时重新登录后再触发一次。
2. 阻塞：下载后页面卡在详情层。
   处理：按固定绕路 `查看原主题 -> 返回 前沿信息收录 -> 外资研报`。
3. 阻塞：任务执行了但没有新文件。
   处理：先看状态时间窗是否已经覆盖该时段，再看标题是否命中关键词。
4. 阻塞：同一时间触发两次任务。
   处理：脚本已内置并发锁，第二个任务会自动跳过并输出 `another run is in progress`。
5. 阻塞：电脑重启后任务没有立即执行。
   处理：确认用户 LaunchAgent 同时配置 `RunAtLoad=true` 和原有的
   `StartCalendarInterval`。它会在用户登录后补跑一次；未完成窗口依靠
   checkpoint 和 manifest 重新对账，而不是依赖错过的调度时刻。

## 10. 安全边界

1. 只允许处理 `外资研报` 页面内容。
2. 只下载匹配关键词的 PDF。
3. 已归档同名文件自动跳过，避免重复。
4. 状态推进只在脚本执行后写入，防止时间窗错位。

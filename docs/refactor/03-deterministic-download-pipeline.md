# PR 3 — 用确定性 Python 下载链替换 Codex 编排

- 状态：已实现，待合并与真实 canary
- 顺序：3 / 5
- 前置依赖：PR 2 已合并
- 并行关系：可与 PR 4 并行开发；合并时必须串行并在后合并者上 rebase

## 目标

把下载链从“Shell → Codex Agent → 动态 Playwright MCP → Python helper”缩短为“Python pipeline → Playwright/CDP → 归档事务”。下载阶段不再依赖 OpenAI/Codex 网络、Agent 判断、MCP 或 `npx @latest`，同时保持现有 immutable scan plan、内容保护判定、归档和 checkpoint 语义。

## 事实与根因

### 已确认事实

1. `scripts/run_zsxq_task_via_codex.sh` 当前负责锁、CFT 启动、登录预检、冻结时间窗和最终 reconciliation，但真正下载时会执行 `codex exec`。
2. 该 Codex 调用动态注入：

   ```text
   npx -y @playwright/mcp@latest --cdp-endpoint http://127.0.0.1:9223
   ```

   这引入 Codex transport、模型配置、npm、MCP 启动和版本漂移五类与业务下载无关的失败点。
3. 传给 Codex 的 runtime prompt 要求它严格执行三个已经存在的确定性脚本：
   - `scan_zsxq_download_candidates.py`
   - `download_zsxq_plan_file.py`
   - `finalize_download_batch.py`
4. `scripts/download_zsxq_plan_file.py` 已经直接使用 `playwright.sync_api` 和 `chromium.connect_over_cdp()`；它校验候选必须属于 immutable plan，等待异步页面状态，确认持续内容保护，`save_as` 后验证 PDF header，再原子移动到 staging。
5. `scripts/finalize_download_batch.py` 已经实现计划内归档、SHA256/文件名去重、run manifest 聚合和“仅完整 reconciliation 才推进 checkpoint”。
6. `scripts/zsxq_preflight.py` 已经直接连接 CFT/CDP 并区分 login、site、browser 状态。
7. 因此 Agent 目前主要是在解释 prompt、逐个调用既有脚本和打印一行机器结果；它不是下载能力的唯一实现。
8. 当前 launcher 只对“cloud requirements timeout”执行一次特定重试；TLS EOF、Codex transport 断开或 MCP 安装失败仍会让下载在浏览器健康时整体失败。

### 当前调用链

```text
launchd
  -> openclaw_tasks/zsxq_download/run.cron-safe.sh
  -> openclaw_tasks/zsxq_download/run.sh
  -> scripts/run_zsxq_*_task_via_codex.sh
  -> ensure CFT + zsxq_preflight.py
  -> codex exec
  -> npx @playwright/mcp@latest
  -> scan_zsxq_download_candidates.py
  -> download_zsxq_plan_file.py × N
  -> finalize_download_batch.py × N + final reconciliation
```

### 根因

下载任务是一个可编码、可测试的有限状态过程，却把 Agent 放在控制平面。Agent/MCP 并没有增加必要能力，反而让确定性业务受模型、传输和动态工具版本影响；每个文件重新连接 helper 也带来额外启动成本。

## 修改清单

### `src/zsxq_pipeline/browser.py`

- 从现有 launcher 抽取 CFT 启动、CDP readiness、keepalive page 和登录预检。
- `BrowserSession` 在一轮下载中只建立一次 Playwright/CDP 连接并复用 page/context。
- 只删除确认无存活 owner 的 CFT singleton 文件；不清理 cookie、storage 或用户 profile。
- 返回结构化错误，不直接写 pipeline 状态。

### `src/zsxq_pipeline/download.py`

- 新增 `DownloadPipeline.run(source, window)`：
  1. 在 SQLite 事务中冻结 source window。
  2. 调用扫描器生成 immutable plan 和 plan hash。
  3. 对 plan 中每个唯一 file identity 顺序执行下载。
  4. 每个文件下载后立即校验并归档。
  5. 最后执行完整 reconciliation。
  6. 只有全部候选 downloaded、already satisfied 或 deterministically blocked 才提交 checkpoint。
- 浏览器下载并发固定为 1；本 PR 不用并发换吞吐。
- transient navigation 只做有界重试；`source_content_protected` 不重试、不绕过。
- 将每个文件阶段写入 PR 2 的 `stage_attempts/artifacts`，不新增 task-local JSON 真源。

### `scripts/scan_zsxq_download_candidates.py`

- 保留 CLI 兼容入口，把纯扫描逻辑提取为可导入函数。
- 输出 schema 增加显式 version 和 plan hash。
- 保持 API-first、DOM fallback 和关键词筛选规则不变。

### `scripts/download_zsxq_plan_file.py`

- 保留单文件 CLI 作为诊断工具。
- 抽出接受已有 page/session 的纯执行函数，供 `DownloadPipeline` 复用连接。
- 保持 exact plan membership、异步权限等待、PDF 校验和 staging collision 语义。

### `scripts/finalize_download_batch.py`

- 提取 archive/reconcile 函数供 package 调用。
- 将 manifest 结果映射到 PR 2 的 document/artifact/source_window 事务。
- CLI 兼容输出保留一个 release 周期；状态写入由 pipeline 统一负责。

### `scripts/zsxq_preflight.py`

- 将 browser/site/login 检查复用到 `BrowserSession.doctor()`。
- CLI 继续存在，用于人工诊断。

### `src/zsxq_pipeline/download_result.py`

- 用 manifest 和数据库事实生成 canonical result。
- 新字段使用 `process_exit_code`；兼容导出暂时保留 `codex_exit_code`，值等于 process exit code，并标记 deprecated。
- 删除从 Agent 自由文本猜测成功数量的决策路径。

### `openclaw_tasks/zsxq_download/run.sh`

- 保持当前 task-local config、日志和通知兼容层，但改为调用确定性 Python download command。
- 将 `CODEX_SCRIPT_PATH` 替换为 `DOWNLOAD_RUNNER_PATH`。
- 外层 wrapper 只负责兼容输入/输出和调用现有通知策略，不再导出 `ZSXQ_OUTER_RUNNER_ACTIVE` 给 Agent。

### `openclaw_tasks/zsxq_download/config.foreign.env.example`、`config.domestic.env.example`

- 删除模型、thinking、Codex timeout 和 MCP 相关配置。
- 增加 Python runner、source logical name、job config、keyword config 和 CDP endpoint。
- 暂时保留现有 CFT profile 路径；目录迁移属于 PR 5。

### `deploy/install_local_runtime.sh`

- download deployment env 改写 `DOWNLOAD_RUNNER_PATH`，不再写 Codex launcher 路径。
- 记录 Python、Playwright 和 browser capability preflight 结果。

### 删除或退役旧 Agent 编排文件

在所有引用清理完成后删除：

- `scripts/run_zsxq_task_via_codex.sh`
- `scripts/run_zsxq_domestic_cicc_task_via_codex.sh`
- `prompts/openclaw_scheduler_prompt.md`
- `prompts/openclaw_domestic_cicc_scheduler_prompt.md`
- 只服务于下载 Agent 的 task template/runtime prompt

如果 `scripts/update_zsxq_focus.py` 仍需要 runtime prompt 作为人工可读镜像，则必须先把它改为只更新结构化 focus 配置，不能留下第二个下载控制契约。

### `scripts/zsxq_autodownload_result.py`

- 删除 cloud requirements、Codex self-kill 和 Agent marker 作为新链路决策依据。
- 保留旧 result 读取兼容函数，明确限定为 legacy import/render。

### `scripts/git_workflow.py`

- 将原 Codex launcher 的 focused test mapping 改到 `src/zsxq_pipeline/download.py`、browser、scanner、downloader 和 finalizer。

### 测试调整

- 新增 `tests/test_pipeline_browser.py`、`tests/test_pipeline_download.py`、`tests/test_pipeline_download_result.py`、`tests/test_zsxq_preflight.py`。
- 扩展：
  - `tests/test_scan_zsxq_download_candidates.py`
  - `tests/test_download_zsxq_plan_file.py`
  - `tests/test_finalize_download_batch.py`
  - `tests/test_local_runtime_deployment.py`
  - `tests/test_zsxq_notification_policy.py`
- 删除以 prompt 文本和 Codex stub 为唯一对象的测试，替换为状态机、plan 和 manifest 合同测试。

### 文档

- 更新 `README.md`、`docs/architecture.md`、`docs/project_flow_zh.md`、`docs/deployment.md`。
- 将 `docs/openclaw_task_setup_zh.md` 标记为历史或删除；活跃文档不得再称 OpenClaw/Codex 为下载执行者。

## 边界条件

### 必须保持不变

- source window 冻结和 checkpoint 推进规则不变。
- 候选只能来自 immutable scan plan；不能下载 plan 外文件。
- 关键词、标签、source 配置和去重规则不变。
- `source_content_protected` 必须 fail closed，不能尝试绕过内容保护。
- CFT 登录态、端口 9223 和浏览器 profile 继续复用。
- 每个下载文件必须验证为非空 PDF，并在归档前完成原子 staging rename。
- 同名候选、内容重复、已归档重复和部分下载的现有 reconciliation 语义保持不变。
- 下载结果仍能驱动现有低噪声飞书通知策略。

### 明确不改

- 不修改摘要、OCR、飞书文档发布或通知内容。
- 不迁移 runtime 根目录或 CFT profile。
- 不统一 scheduler；仍由现有两个 LaunchAgent 触发，PR 5 再收敛。
- 不增加浏览器并行下载。
- 不把 source API 改成非官方抓取或绕过权限的请求。
- 不在 CI 或 PR 中执行真实知识星球下载。

## 验收标准

### 测试命令

```bash
python3 -m pytest \
  tests/test_pipeline_browser.py \
  tests/test_pipeline_download.py \
  tests/test_pipeline_download_result.py \
  tests/test_scan_zsxq_download_candidates.py \
  tests/test_download_zsxq_plan_file.py \
  tests/test_finalize_download_batch.py \
  tests/test_zsxq_download_launcher_runtime.py \
  tests/test_zsxq_notification_policy.py \
  tests/test_local_runtime_deployment.py -q
python3 -m pytest -q
python3 scripts/check_repository_hygiene.py
```

代码扫描：

```bash
rg -n 'codex exec|@playwright/mcp|npx.*playwright|run_zsxq_.*_via_codex' \
  src scripts openclaw_tasks deploy
```

预期结果：活跃下载路径零匹配；仅迁移说明或历史文档可以出现，并应有明确 historical 标记。

### 关键回归场景

1. 扫描得到 0 个更新：成功推进到冻结窗口结束，不报失败。
2. 有更新但无关键词命中：保持现有 no-download reason。
3. 计划 4 个文件，下载 3 个后进程终止：checkpoint 不推进；重启只补缺失项。
4. 一个文件持续内容保护：记录 deterministically blocked，不反复点击或重试。
5. 同一物理文件对应多个同名 plan row：只归档一次，所有对应候选正确 satisfied。
6. staging 已存在同名但不同内容：拒绝覆盖并给出明确状态。
7. CFT 进程存在但 `/json/version` 不可用：blocked_browser，不进入扫描。
8. 登录页可见：blocked_auth，不推进 checkpoint。
9. 模拟 OpenAI、Codex、npm 均不存在：下载单元测试和离线 runner preflight 仍可运行。
10. notification 失败：下载结果已经提交，不能重新扫描或重新下载。

### 合并后的 canary

- 先对外资和中金各运行一次 `--plan-only`，与旧 scanner 在同一冻结窗口比较 plan hash、数量和候选 identity。
- 差异为 0 后，只放行一个真实 source window。
- 核对 archive manifest、SQLite stage、旧兼容 result 和通知策略，再放行另一个 source。
- 本 PR 不自动执行 canary 或生产切换。

## 停止条件

出现以下任一情况，暂停并汇报：

- 真实下载需要 Agent 临场做出当前 helper 未编码的页面判断。
- 同一 scan plan 在新旧执行器中产生不同候选集合且无法由时间边界解释。
- 复用一个 CDP session 会改变下载权限、页面状态或 save behavior。
- 需要改变关键词、source API、内容保护或 checkpoint 语义才能完成替换。
- 需要新增浏览器扩展、抓包、cookie 导出或绕过登录。
- `zsxq_autodownload_result` 的外部消费者依赖未记录的 Codex 专用字段。
- PR 3 必须修改 PR 4 所有的摘要/飞书模块接口。
- 删除 prompt 前发现仍有独立人工流程把它当真源。

## 未决风险

- 当前 Playwright helper 是逐文件连接；单 session 复用在长批次中的内存和页面稳定性尚未基准测试。
- API-first 到 DOM fallback 的真实切换频率和差异率尚未量化。
- 知识星球页面结构变化仍可能让 deterministic selector 失效；新架构只能让失败可定位，不能保证第三方 DOM 永不变化。
- Playwright Python 版本目前是范围依赖而非完整 lock；最终 release 的版本固定策略需要与 PR 5 installer 一起完成。
- CFT profile 仍位于 `.openclaw`，这只是历史路径而非运行依赖；实际目录迁移可能要求重新登录，留到 PR 5 决策。
- 删除 Agent prompt 后，`update_zsxq_focus.py` 的产品用途可能需要单独保留一个只读报告输出，不能在未核实前直接删除。

## 与其他 PR 的关系

- 必须基于 PR 2 的 state/identity API 开发。
- 可以与 PR 4 并行开发，文件所有权原则是：PR 3 只拥有 browser/download/source window；PR 4 只拥有 extract/summary/publish/notify。
- 两个 PR 仍可能同时触碰 `pyproject.toml`、`cli.py`、installer 和架构文档，因此不能同时直接合并。建议 PR 3 先合并，PR 4 rebase 后再合并；反向也可以，但必须重新跑两边全套测试。
- PR 5 必须等本 PR 已在新状态核心上完成 canary 后才能删除旧下载 LaunchAgent。

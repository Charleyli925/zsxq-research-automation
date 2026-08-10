# PR 1 — 统一现网部署并安全恢复积压

- 状态：规划中
- 顺序：1 / 5
- 前置依赖：无
- 合并要求：必须第一个合并；不得与 PR 2–PR 5 同时切换生产

## 目标

先恢复一条可证明、可回滚的现网链路：下载、摘要、helper、提示词和发布逻辑必须来自同一个 Git SHA；配置或接口不兼容必须明确阻塞，不能被当成临时网络错误无限重试。本 PR 只止血，不引入最终的新调度器，也不移除 OpenClaw 摘要调用。

## 事实与根因

### 已确认事实

1. GitHub 仓库是源码真源，生产应从 clean、detached 的 tag/SHA 运行。`deploy/install_local_runtime.sh` 已对下载任务执行这一约束，但当前只安装外资和中金两个下载任务，没有把 `ZSXQ_pdf_digest` 纳入同一部署记录。
2. `openclaw_tasks/zsxq_pdf_digest/run.sh` 会先读取并快照 `config.env`，再根据配置中的 `HELPER_SCRIPT_PATH`、`SCANNER_SCRIPT_PATH` 等路径快照 worker 依赖。只要 `config.env` 仍指向旧仓库，就可能产生“新 worker + 旧 helper”的混合快照。
3. `openclaw_tasks/zsxq_pdf_digest/run.worker.sh` 的 `lookup_publish_recovery()` 固定调用：

   ```text
   manage_zsxq_digest_batch.py lookup-publish-recovery ... --batch-file <publish-group.batch.json>
   ```

4. 当前源码中的 `scripts/manage_zsxq_digest_batch.py` 支持 `lookup-publish-recovery --batch-file`，但 2026-08-10 的生产 `config.env` 仍通过软链接指向旧仓库；生产日志出现了精确错误：

   ```text
   manage_zsxq_digest_batch.py: error: unrecognized arguments: --batch-file ...
   ```

5. 同一错误被归类成 `publish_failed / transient_failure`，进入 5/10/20 分钟重试。2026-08-10 现场检查时，`stage_retry_ledger.json` 有 548 条记录，其中 526 条为 `retry_exhausted`，且 526 条的最终消息均为“lark-cli docs 发布失败：发布恢复记录查询失败”。
6. `run_status.json` 和 `last_result.json` 同时报告 `status=waiting`、`phase=waiting_file_retry`，但 526 条记录已经没有 `next_retry_at`，因此“仍可自动恢复”的状态表达与事实不一致。
7. 飞书文档创建、追加、正文校验和群通知已经直接使用 `lark-cli`。本次失败发生在调用 `lark-cli docs` 之前的发布恢复查询阶段，不是飞书内容、权限或网络本身导致。

### 当前调用链

```text
crontab */10
  -> ZSXQ_pdf_digest/run.cron-safe.sh
  -> ZSXQ_pdf_digest/run.sh
  -> 快照 config.env、run.worker.sh、helper、prompt
  -> run.worker.sh::publish_ready_chunks
  -> run.worker.sh::lookup_publish_recovery
  -> config.env 指定的 manage_zsxq_digest_batch.py
  -> argparse 拒绝 --batch-file
  -> record_stage_retry_for_batch
  -> retry_exhausted
  -> 后续 cron 只写 waiting_file_retry
```

### 根因

根因不是单个参数遗漏，而是部署单元不完整：下载任务有 `deployment.env` 固定到 release SHA，摘要任务没有同等级的部署覆盖层；私有运行配置同时承担业务配置和源码定位，导致旧配置可以把新入口重新指回旧仓库。重试分类又没有识别 CLI 合同不兼容，使确定性发布错误被放大成 526 条耗尽重试。

## 修改清单

### `deploy/install_local_runtime.sh`

- 新增 `--digest-task-dir`，默认指向 `${TASKS_ROOT}/ZSXQ_pdf_digest`。
- 将 digest 加入路径边界、`config.env` 存在性、任务空闲和备份检查。
- 为 digest 生成 `deployment.env`，固定以下源码路径到同一 `RELEASE_ROOT`：
  - `AUTOMATION_ROOT`
  - `HELPER_SCRIPT_PATH`
  - `SCANNER_SCRIPT_PATH`
  - `RESEARCH_LIBRARY_INDEX_SCRIPT_PATH`
  - `MARKITDOWN_SCRIPT_PATH`
  - `CLEAN_MARKDOWN_SCRIPT_PATH`
  - `OBSIDIAN_ARCHIVE_SCRIPT_PATH`
  - `OBSIDIAN_INDEX_SCRIPT_PATH`
  - `RUNTIME_GUARD_SCRIPT_PATH`
- 将 digest 的 task dir、部署 SHA 和当前调度方式写入 `.deployment/investment-reports-automation.json`。
- `--apply` 前确认三个任务均空闲；任何一个活跃都整体拒绝部署。
- 本 PR 不把 digest 从 cron 改成 launchd；只统一代码来源。

### `openclaw_tasks/zsxq_pdf_digest/run.sh`

- 在读取 task-local `config.env` 后读取 task-local `deployment.env`，并明确规定后者只能覆盖源码路径，不能包含 chat ID、认证信息或运行状态。
- 快照前校验 worker、helper、scanner、prompt 和 sidecar 均位于 `AUTOMATION_ROOT` 对应的同一 release checkout。
- 在 workflow fingerprint 中记录 `git_sha`、`release_root` 和每个依赖的相对路径；路径跨 release 时 fail closed。
- 保留当前“运行开始后使用不可变快照”的行为。

### `openclaw_tasks/zsxq_pdf_digest/run.worker.sh`

- 新增错误类型 `release_contract_mismatch`，识别 argparse 的 `unrecognized arguments`、缺少预期子命令和 helper schema/version 不匹配。
- 该错误直接写为 `blocked_release`，不进入临时重试。
- 当全部待处理文件为 `retry_exhausted` 或 `blocked_release` 且没有 `next_retry_at` 时：
  - `status=blocked`
  - `phase=blocked_release`
  - `pipeline_health=blocked`
  - `waiting_reason` 为空
- 保持已经生成的正文、摘要缓存、`remote_written` 发布记录和飞书文档不变。

### `scripts/manage_zsxq_digest_batch.py`

- 为 helper 增加机器可读的 `contract-version` 子命令；`run.sh` 快照后先校验版本和所需参数能力。
- 新增安全恢复命令 `recover-stage-retries`：
  - 默认只输出恢复计划，不写 ledger。
  - 必须给出 stage、error code、原 workflow version、目标 workflow version、错误指纹和 `--expected-count`。
  - 只有显式 `--apply` 且实际命中数等于 `--expected-count` 才原子写回。
  - 保留原始失败记录并追加 recovery audit，不删除历史。
- 不允许用“清空 ledger”作为恢复方式。

### `openclaw_tasks/zsxq_pdf_digest/config.env.example`

- 将业务配置与部署路径分组说明。
- 标记源码路径由 installer 生成的 `deployment.env` 持有，生产 `config.env` 不应再固定到某个开发仓库。
- 不加入任何真实本机路径、chat ID 或凭据。

### `tests/test_local_runtime_deployment.py`

- 增加 digest 被纳入 dry-run、apply、备份、部署记录和空闲检查的测试。
- 验证 digest `deployment.env` 只含 release 路径，不泄漏私有配置。
- 验证三个任务中任意一个活跃时 `--apply` 拒绝执行。

### `tests/test_manage_zsxq_digest_batch.py`

- 增加 contract version 测试。
- 增加恢复计划默认只读、错误指纹不符、expected count 不符、重复 apply 幂等和原子写入测试。
- 增加历史记录保留测试。

### `tests/test_zsxq_pdf_digest_run.py`

- 构造旧 helper 不支持 `--batch-file` 的回归场景，预期为一次 `blocked_release`，而不是四次临时重试。
- 增加“所有记录均 retry exhausted 时不能报告 waiting/healthy”的回归测试。
- 保留 `remote_written` 恢复后不得重复 create/append 的既有测试。

### `docs/deployment.md`、`docs/runtime-recovery.md`、`docs/source-of-truth.md`

- 写清三个任务属于同一 release 部署单元。
- 写清 `config.env`、`deployment.env`、Git SHA、runtime state 各自的所有权。
- 增加积压恢复的 preview、核对、apply、验证和回滚步骤。

## 边界条件

### 必须保持不变

- PDF、原始文本、摘要 Markdown、飞书文档和 `publish_records.jsonl` 均不得被删除或重建。
- 发布顺序继续保持“本地摘要 Markdown → 飞书文档 → 群通知”。
- `remote_written` 后进程中断，下一次运行必须先 fetch 校验，不能重复 create/append。
- `PUBLISH_LARK_CLI_AS=user` 和 `LARK_CLI_SEND_AS=bot` 的身份拆分保持不变。
- 下载任务的固定时间窗、关键词、CFT profile 和归档布局保持不变。
- installer 继续只允许 clean detached tag/SHA 正式部署。

### 明确不改

- 不移除 OpenClaw summary agent。
- 不替换 Codex 下载入口。
- 不切换 cron/launchd 调度方式。
- 不迁移 `.openclaw`、`.lark-cli`、ResearchLibrary 或 Obsidian 目录。
- 不自动部署生产、不自动重跑 526 条积压、不发送飞书消息。
- 不修改摘要提示词、模型、thinking 或并发数。

## 验收标准

### 本地测试

```bash
bash -n deploy/install_local_runtime.sh
bash -n openclaw_tasks/zsxq_pdf_digest/run.sh
bash -n openclaw_tasks/zsxq_pdf_digest/run.worker.sh
python3 -m pytest \
  tests/test_local_runtime_deployment.py \
  tests/test_manage_zsxq_digest_batch.py \
  tests/test_zsxq_pdf_digest_run.py -q
python3 scripts/check_repository_hygiene.py
```

预期结果：全部退出 0；测试不得访问真实飞书、OpenAI、知识星球或生产 runtime。

### 关键回归场景

1. 新 worker 配旧 helper：预检直接 `blocked_release`，摘要和飞书均不被调用。
2. helper 参数不兼容：只记录一次，不产生 `next_retry_at`。
3. 526 条 fixture 恢复计划：未带 `--apply` 时 ledger 字节不变。
4. `--expected-count` 不匹配：退出非 0，ledger 字节不变。
5. 成功 apply 后再次 apply：不重复生成 active entry。
6. 已有 `remote_written`：只 fetch/授权/校验，不重复写正文。
7. 全部工作不可自动重试：状态为 blocked，不是 waiting/healthy。

### 合并后的生产验收

生产验收不是本 PR 自动执行的一部分，必须在显式 release 决策后进行：

1. 从合并后的 clean detached tag/SHA 执行 installer dry-run。
2. 确认三个任务均 idle，再执行 `--apply`。
3. 运行 `ZSXQ_pdf_digest/run.sh --preflight-only --no-notify`。
4. 对真实 ledger 只生成 recovery preview，核对命中数、workflow version、错误指纹和现有 publish records。
5. 经人工确认后才允许 apply，并先放行 1 个发布分组；确认无重复文档后再分批放行剩余积压。

## 停止条件

出现以下任一情况，立即停止实现或部署并汇报：

- 生产实际调用链与上述调用链不同。
- digest 还有未发现的第二套 config/helper 来源。
- 526 条积压中存在多种根因，不能由一个错误指纹准确圈定。
- 恢复需要删除 publish record、清空 ledger 或覆盖摘要缓存。
- installer 必须修改凭据、chat ID 或浏览器 profile 才能工作。
- 改动需要切换 scheduler、模型或摘要提示词。
- 计划内文件之外出现必须修改的运行核心文件。
- 发现真实飞书远端已经写入但本地没有足够证据判断是否重复。

## 未决风险

- “526 条都可以只重试发布”目前只由 retry ledger 的 stage/message 支持；仍需逐项与 summary cache、permanent summary 和 publish records 对账。
- 旧 helper 与新 worker 是否还有除 `--batch-file` 外的其他合同差异，必须由 contract preflight 证明。
- 当前 `lark-cli` 自动化身份和 Keychain 状态可能在正式恢复时发生变化；本 PR 不把历史成功当作当前认证仍有效的证据。
- 一次放行大量积压可能触发飞书限流；需要小批量 canary 后再确定节流参数。
- PR 1 的部署扩展是过渡方案，PR 5 会用统一 pipeline installer 取代它，但在 PR 2–PR 4 开发期间仍必须保持现网可修复。

## 与其他 PR 的关系

- PR 1 必须先合并并完成代码级验收。
- PR 2 依赖本 PR 固化的错误分类、文件身份和 legacy state 语义。
- PR 3、PR 4 不得在本 PR 尚未确定生产源码真源时切换运行入口。
- PR 5 最终删除本 PR 中的过渡部署结构，但必须保留同 SHA、空闲检查和可回滚原则。

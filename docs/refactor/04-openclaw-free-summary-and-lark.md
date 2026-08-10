# PR 4 — 摘要直连 Codex，发布直连 lark-cli

- 状态：规划中
- 顺序：4 / 5
- 前置依赖：PR 2 已合并
- 并行关系：可与 PR 3 并行开发；合并时必须串行并在后合并者上 rebase

## 目标

移除摘要链对 OpenClaw binary、agent registry、agent session、worker auth 目录和 `openclaw.json` 模型绑定的运行时依赖。摘要只通过隔离、无会话、结构化输出的 `codex exec` 完成；飞书文档和通知继续直接使用 `lark-cli`，但从 5,864 行 Shell worker 中抽取成可测试的 Python adapter。

本 PR 完成后，即使 OpenClaw 未安装，已有 PDF 的提取、摘要、飞书发布和通知仍可运行。调度仍暂时使用原 digest cron wrapper，统一调度留给 PR 5。

## 事实与根因

### 已确认事实

1. `openclaw_tasks/zsxq_pdf_digest/run.worker.sh::run_agent_turn()` 通过以下命令生成摘要：

   ```text
   openclaw --no-color agent --agent <summary-agent-id> \
     --thinking <level> --timeout <seconds> --message <prompt> --json
   ```

2. 为了支持两个并发 worker，当前链路还需要：
   - `~/.openclaw/openclaw.json` 中注册 `zsxq_pdf_digest_summary_w1/w2`
   - 每个 agent 的 `auth-profiles.json`
   - agent session 目录清理
   - main agent 到 worker 的 Codex auth 同步
   - registry-aware preflight
3. 历史上出现过 worker 目录和 auth 文件存在、但 agent ID 未注册，导致所有摘要以 `Unknown agent id` 失败。这一层故障发生在飞书发布之前。
4. 当前摘要 prompt 已明确要求：只读取恢复后的正文，不读 PDF、不 OCR、不调用飞书，并输出机器可解析的 `ZSXQ_SUMMARY_JSON`。
5. 本机 `codex exec` 已提供适合直接自动化的参数：
   - `--ephemeral`
   - `--ignore-user-config`
   - `--ignore-rules`
   - `--sandbox read-only`
   - `--output-schema`
   - `--output-last-message`
6. 飞书文档发布已经直接走：
   - `lark-cli docs +create/+update/+fetch --as user`
   - `lark-cli drive ... --as user`
7. 群通知已经直接走 `lark-cli im +messages-send --as bot --idempotency-key ...`。
8. `publish_records.jsonl` 已有 `remote_written → success` 恢复语义；通知 outbox 已有幂等键和重试记录。OpenClaw 已不参与飞书发布/通知的决定性路径。

### 当前调用链

```text
cron
  -> run.cron-safe.sh
  -> run.sh snapshot
  -> run.worker.sh
  -> text extract / cache
  -> OpenClaw agent registry + session + auth sync
  -> openclaw agent --agent summary_wN
  -> parse ZSXQ_SUMMARY_JSON
  -> lark-cli docs/drive --as user
  -> lark-cli im --as bot
```

### 根因

摘要本质上是“已提取正文 → 固定 JSON schema”的无状态模型调用，却借用了完整 Agent 生命周期。OpenClaw 额外引入注册表、session、auth profile、app-server 和模型绑定等状态；这些状态既不参与文本提取，也不参与飞书写入，却可以让整批摘要在调用模型前失败。

## 修改清单

### `src/zsxq_pipeline/extract.py`

- 编排现有 MarkItDown、clean markdown、`pdftotext`、OCR 和质量门禁。
- 复用现有 extractor，不重写 OCR 算法。
- 成功 artifact 以 `pdf_sha256 + extractor_version` 缓存并写入 PR 2 的 state。
- content failure 进入 quarantined；环境或工具缺失进入 blocked_release，不按文件反复重试。

### `src/zsxq_pipeline/summary.py`

- 根据 `pdf_sha256 + extractor_version + prompt_version + model + reasoning` 构造 summary identity。
- 默认一篇 PDF 一个 summary job，最多两个并发 worker。
- worker 完成顺序可以不同，但 publication partition 必须按确定性 source/date/order 生成。
- summary 成功后先原子写 JSON/Markdown artifact，再提交数据库状态。
- publish 失败时只重新领取 publish stage，不重新提取或总结。

### `src/zsxq_pipeline/providers/codex.py`

- 使用 argv 数组直接执行 `codex exec`，禁止 `shell=True`。
- 固定调用契约：

  ```text
  codex exec
    --ephemeral
    --ignore-user-config
    --ignore-rules
    --sandbox read-only
    --model <configured-model>
    --output-schema <summary.schema.json>
    --output-last-message <result.json>
    -
  ```

- 工作目录只包含本次 batch manifest、正文输入、prompt 和输出位置；不暴露仓库写权限、MCP 或插件。
- 使用独立 process group 和硬超时；超时后 TERM，再按固定 grace KILL。
- 只接受 schema-valid final output；stdout 事件只用于诊断和 usage 统计。
- 不在运行时 fallback 到 OpenClaw 或另一个模型 provider。切换 provider 必须是显式配置和独立变更。

### `src/zsxq_pipeline/schemas/summary.schema.json`

- 固定成功/失败输出字段、handled path、title、markdown 和质量信息。
- `handled_paths` 必须与 job manifest 完全一致。
- 禁止额外字段，避免模型自由文本改变下游合同。

### `src/zsxq_pipeline/prompts/summary.md`、`summary-system.md`

- 迁移现有摘要规则。
- 删除“先输出完成通知”和 `ZSXQ_SUMMARY_JSON:` 文本前缀，改为只返回 schema JSON。
- 保持“不读 PDF、不 OCR、不调用飞书、不补外部事实”的内容边界。
- 为 prompt 内容计算版本 hash，进入 summary cache key。

### `src/zsxq_pipeline/lark.py`

- 实现 `LarkPublisher` 和 `LarkNotifier`，统一 subprocess、timeout、JSON parsing、错误脱敏和 capability preflight。
- 文档路径：
  - create/append 使用 `--as user`
  - create 后校验标题
  - write 后 `docs +fetch` 校验正文锚点
  - create 后给目标 chat view 权限
- 通知路径：
  - `im +messages-send --as bot`
  - 必须提供 idempotency key
- 机器 JSON 调用设置：

  ```text
  LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1
  LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1
  ```

- `remote_written` 在远端写成功后立即提交；标题、fetch 或权限失败时下次从 verify/grant 恢复，不能重写正文。

### `src/zsxq_pipeline/publish.py`

- 从 summary Markdown artifact 构造确定性发布分组。
- 保持当前 `DOC_GROUP_SIZE`、`DOC_GROUP_THRESHOLD` 和同日文档容量规则，先以配置迁移方式保留值。
- publication key 使用 summary hash、target、partition 和目标文档，不再依赖临时目录路径。
- 发布成功后才 ack document 的 publish stage。

### `src/zsxq_pipeline/notify.py`

- 把下载和 digest 的通知统一写入 PR 2 的 `notification_outbox`。
- outbox drain 与摘要/发布事务分离；发送失败不能撤销业务成功。
- 保持现有低噪声、supersede 和 document-before-terminal 顺序。

### `openclaw_tasks/zsxq_pdf_digest/run.sh`

- 变成兼容薄 wrapper：读取现有业务配置，调用 `zsxq-pipeline process`，导出兼容 `run_status.json/last_result.json`。
- 不再快照 OpenClaw auth、agent registry、session 或 5,864 行 worker。

### 删除 `openclaw_tasks/zsxq_pdf_digest/run.worker.sh`

- 只有在 Python pipeline 覆盖文本提取、摘要、发布、通知、状态导出和恢复测试后删除。
- `run.cron-safe.sh` 暂时保留，直到 PR 5 切换统一 LaunchAgent。

### `openclaw_tasks/zsxq_pdf_digest/config.env.example`

- 删除 `SUMMARY_AGENT_ID`、worker prefix、session reset 和 OpenClaw auth 配置。
- 改为 Codex binary、model、reasoning、timeout、worker count 和 Lark profile 配置。
- `LARKSUITE_CLI_CONFIG_DIR` 暂时允许指向现有已验证 profile；重命名/迁移在 PR 5 单独验收。

### `scripts/manage_zsxq_digest_batch.py`

- 将仍有价值的纯函数迁到 package：摘要校验、artifact persistence、publish grouping、publish key、remote recovery。
- 保留必要的 legacy importer/CLI 兼容层，不再作为主运行编排器。
- 删除对 OpenClaw result envelope 和 session metadata 的解析。

### 测试

新增：

- `tests/test_pipeline_extract.py`
- `tests/test_pipeline_codex_provider.py`
- `tests/test_pipeline_summary.py`
- `tests/test_pipeline_lark.py`
- `tests/test_pipeline_publish.py`
- `tests/test_pipeline_notify.py`
- `tests/test_pipeline_process.py`

迁移现有以下回归：

- timeout 后一次有界重试
- token/auth failure 停止继续消耗
- parallel summary、serial publish
- cache hit 不调用模型
- remote_written 不重复写
- fetch 正文缺失不标成功
- user/bot identity 拆分
- notification idempotency/supersede
- content failure 不阻塞好文件

### 文档

- 更新 `README.md`、`docs/architecture.md`、`docs/project_flow_zh.md`、`docs/deployment.md`、`docs/runtime-recovery.md`、`docs/zsxq_notification_policy.md`。
- 活跃文档不再描述 summary/publish agent；历史故障可以保留但必须标记 historical。

## 边界条件

### 必须保持不变

- 只根据提取后的正文总结，不直接把 PDF 交给模型。
- 摘要中文结构、核心结论、核心问答、禁止编造和禁止外部补充规则保持不变。
- 一篇 PDF 一个摘要 cache identity；发布失败不得重新总结。
- 两个 summary worker 可以并行，但飞书文档内报告顺序必须确定。
- 本地 summary Markdown 是发布输入真源。
- 文档由 user 身份创建/追加/授权，通知由 bot 身份发送。
- create/append 后必须 fetch 验证；远端写入与本地成功记录分两阶段。
- quarantine、永久摘要、ResearchLibrary 和 Obsidian 产物保持可读。

### 明确不改

- 不修改下载逻辑、关键词或 source window。
- 不切换最终 scheduler；digest cron 暂时保留。
- 不引入 OpenAI API 作为第二条 fallback。
- 不升级模型、不改变 reasoning；先迁移当前配置值。
- 不自动迁移或删除现有 Lark/OpenClaw credential/profile 目录。
- 不自动发布真实飞书文档或发送群消息。
- 不删除历史摘要、publish records 或 notification audit。

## 验收标准

### 测试命令

```bash
python3 -m pytest \
  tests/test_pipeline_extract.py \
  tests/test_pipeline_codex_provider.py \
  tests/test_pipeline_summary.py \
  tests/test_pipeline_lark.py \
  tests/test_pipeline_publish.py \
  tests/test_pipeline_notify.py \
  tests/test_pipeline_process.py \
  tests/test_manage_zsxq_digest_batch.py \
  tests/test_zsxq_notification_policy.py -q
python3 -m pytest -q
python3 scripts/check_repository_hygiene.py
```

静态检查：

```bash
rg -n 'openclaw agent|openclaw.json|auth-profiles.json|SUMMARY_AGENT_ID|run_agent_turn' \
  src openclaw_tasks scripts deploy
```

预期结果：活跃摘要/发布路径零匹配；legacy importer 或 historical 文档必须显式标注。

### capability preflight

```bash
codex exec --help
lark-cli docs +create --help
lark-cli docs +update --help
lark-cli docs +fetch --help
lark-cli im +messages-send --help
```

预期结果：本机已安装 CLI 提供计划使用的参数。该步骤只读，不发送消息、不创建文档。

### 关键回归场景

1. OpenClaw binary 完全不存在：cached summary 可直接发布，uncached summary 可直接调用 Codex。
2. Codex user config 含 MCP/plugin：`--ignore-user-config` 后 summary 调用不加载它们。
3. 模型输出 schema 不合法：不写 summary artifact，不进入 publish。
4. 两个 worker 一个超时：另一个仍可完成；超时文件按有界策略 retry_wait。
5. Codex auth 失效：标记 blocked_auth，一次通知，不对所有文件重复重试。
6. summary cache 命中：不启动 Codex 子进程。
7. lark create 成功、fetch 前进程退出：重启从 remote_written verify，不再 create。
8. lark fetch 正文不完整：publication 不成功、document 不 ack。
9. notification 失败：publication 保持成功，outbox 独立 retry。
10. 同一 notification idempotency key 重复执行：最多产生一条远端消息。
11. 一个坏 PDF quarantined：其他文件继续摘要和发布。
12. 现有 summary cache/publish records legacy fixture 导入后不触发重复模型调用或重复文档写入。

### 合并后的 canary

1. `--summary-only --no-notify` 对一份已有正文运行真实 Codex，核对 schema、Markdown 和 usage。
2. 对一份现有 summary cache 运行 publish dry-run/capability preflight。
3. 经明确授权后创建一个 canary 文档，执行 fetch、权限和 bot notification 验证。
4. 再处理 10 个积压项，核对无重复、顺序、缓存命中和 outbox。

本 PR 不自动执行任何真实 canary。

## 停止条件

出现以下任一情况，暂停并汇报：

- `codex exec --ignore-user-config --output-schema` 在实际版本中不能稳定返回唯一 JSON 结果。
- Codex 认证只能通过 OpenClaw 私有 agent home 使用，无法在不复制敏感凭据的情况下直接调用。
- 摘要 prompt 必须依赖 Agent 工具读取任意文件，而不能通过受控输入完成。
- Lark 本机版本参数与计划不一致，或 user/bot identity 需要扩大权限。
- 迁移会改变摘要内容结构、模型或 reasoning。
- 需要为了发布成功跳过 fetch 校验、remote_written 或 idempotency。
- Python adapter 无法表达现有同日 append/容量/授权语义。
- PR 4 必须修改 PR 3 的 browser/download 核心接口。
- 发现 publish/notification 仍有未识别的 OpenClaw fallback。

## 未决风险

- 直接 Codex CLI 与 OpenClaw Codex app-server 的认证来源是否完全一致，尚需真实 `--summary-only` smoke 证明。
- `--ignore-user-config` 是否会影响当前账户所需的 provider 配置，需要在不暴露凭据的情况下验证。
- JSON Schema 约束能减少格式漂移，但不能保证摘要事实质量；需要固定样本做语义对照。
- lark-cli 的本机版本、Keychain 降级方式和后台运行权限可能随升级变化，installer 需要固定版本/capability 证据。
- 当前 `LARKSUITE_CLI_CONFIG_DIR` 名称含 `openclaw`，它不等于运行依赖；实际迁移 profile 可能导致重新授权，不能在本 PR 顺手改名。
- 把大型 Shell worker 拆成 Python 可能暴露隐含的进度通知和 Obsidian side effect；必须以现有 tests 和真实 artifact 清单逐项对齐。

## 与其他 PR 的关系

- 必须复用 PR 2 的 state、identity、publication 和 outbox API。
- 可与 PR 3 并行开发，文件所有权原则是 PR 4 不修改 browser/download/source window。
- 与 PR 3 都可能修改 `pyproject.toml`、`cli.py`、installer 和共享文档，因此只能串行合并；后合并者必须 rebase 并运行两边完整测试。
- PR 5 只有在本 PR 证明 OpenClaw 不再是摘要/飞书运行依赖后，才能删除旧 digest cron 和历史 runtime 入口。

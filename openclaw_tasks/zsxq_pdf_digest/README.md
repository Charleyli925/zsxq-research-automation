# ZSXQ PDF 摘要任务

这个目录保留既有 cron / LaunchAgent 的兼容入口，但摘要和飞书逻辑已经在
`src/zsxq_pipeline` 中执行：

```text
run.cron-safe.sh -> run.sh -> python -m zsxq_pipeline.cli process
```

它不需要 OpenClaw binary、旧的模型注册/会话目录或 worker auth 目录。目录名是
已有 scheduler 的兼容名称，不是运行依赖。

## 目录与运行态

- 仓库主副本：`${AUTOMATION_ROOT}/openclaw_tasks/zsxq_pdf_digest`。
- 实际 cron 任务目录：通常是 `${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest`；安装器
  会把 `run.sh` 与 `run.cron-safe.sh` 链接到受审计的 release checkout。
- `config.env`：用户拥有、Git 忽略的本机业务配置。
- `pipeline.sqlite3`、`text_cache/`、`summary_cache/`、`run_status.json`、
  `last_result.json`、`last_result.md`：均写入实际任务目录，不污染仓库。

`run.sh` 只做三件事：定位实际任务目录、读取该目录的 `config.env`、在 release
源码树上启动 Python process。所有用户参数原样转交给 `zsxq-pipeline process`。

## 默认行为

- cron 每 10 分钟唤醒一次；`run.cron-safe.sh` 保留日志轮转与防重叠行为。
- 新 PDF 经静默窗口后才会处理；手工 `--file` / `--folder` 不依赖扫描。
- 每份 PDF 先提取正文并做质量门禁，模型永远不会直接读取 PDF 或执行 OCR。
- 每份 PDF 一个 summary job，最多两个 worker 并发；飞书写入始终按确定性顺序
  串行执行。
- 正文与摘要分别缓存。发布失败只恢复 publish，不会重新提取或重新调用模型。
- 本地摘要提交后会尝试写入 ResearchLibrary 的可读永久摘要；该 sidecar 失败
  会被记录，但不会丢失本地 artifact 或撤销后续 publication。
- 只有飞书文档 fetch/权限校验成功后，才尝试生成带文档 URL 的 Obsidian 阅读入口；
  Obsidian sidecar 也不会回滚已成功的文档。
- 内容型失败进入 quarantine；环境、认证或不变量失败按 durable state 的类别
  停止或等待恢复，不会把坏文件伪装成成功。

## 摘要和飞书边界

未命中缓存时，pipeline 以固定 argv 直接调用 `codex exec`：临时会话、忽略用户
配置/规则、只读 sandbox、固定 JSON Schema 和 `--output-last-message`。只接受
schema-valid 最终 JSON，随后原子写本地 JSON/Markdown artifact。

飞书文档 create/append/fetch/授权均使用 `lark-cli --as user`。远端正文一旦写入
即记录 `remote_written`；如果标题、fetch 或权限校验中断，下次只验证并补授权，
不会重复写正文。群消息使用 `lark-cli ... --as bot` 和稳定 idempotency key；
通知失败留在 outbox，不影响文档 success。

已有 `LARKSUITE_CLI_CONFIG_DIR` 的目录名即使包含 `openclaw` 也可以继续使用；
它只是已验证的 lark-cli profile 路径。本次迁移不移动、删除或复制任何 credential。

## 配置

先复制模板：

```bash
cp openclaw_tasks/zsxq_pdf_digest/config.env.example \
  "${DIGEST_TASK_DIR}/config.env"
```

常用参数：

| 组别 | 参数 |
| --- | --- |
| 输入与状态 | `RESEARCH_LIBRARY_ROOT`、`WATCH_ROOT`、`PIPELINE_DATABASE`、`STATE_FILE`、`TEXT_CACHE_DIR`、`SUMMARY_CACHE_DIR` |
| Codex | `CODEX_BIN`、`CODEX_MODEL`、`CODEX_REASONING`、`CODEX_TIMEOUT_SECONDS`、`SUMMARY_WORKER_COUNT` |
| 飞书 | `LARK_CLI_BIN`、`LARKSUITE_CLI_CONFIG_DIR`、`TARGET_CHAT_ID`、`LARK_CLI_NOTIFICATIONS`、`PUBLISH_LARK_CLI_PARENT_POSITION` |
| 分组 | `DOC_GROUP_SIZE`、`DOC_GROUP_THRESHOLD`、`MAX_FILES_PER_DOCUMENT` |
| 提取 | `EXTRACTOR_VERSION`、`LOCAL_OCR_FALLBACK_ENABLE` |

`SUMMARY_WORKER_COUNT` 最大为 2。文档身份固定为 `user`，通知身份固定为 `bot`；
`CODEX_MODEL` 必须填入当前已审批的模型名，空值应让预检失败而不是静默升级模型。
不要把这些身份改成 agent ID 或把 credential 内容写入配置文件。

## 手动操作

```bash
# 继续既有 cron-safe 入口
bash "${DIGEST_TASK_DIR}/run.cron-safe.sh"

# 只检查本地 capability，不写飞书、不发消息、不调用真实模型
bash "${DIGEST_TASK_DIR}/run.sh" --preflight-only --no-notify

# 指定一个 PDF 或文件夹
bash "${DIGEST_TASK_DIR}/run.sh" --file "/absolute/path/to/file.pdf"
bash "${DIGEST_TASK_DIR}/run.sh" --folder "/absolute/path/to/folder"

# 仅提取/摘要，不发布也不通知
bash "${DIGEST_TASK_DIR}/run.sh" --summary-only --no-notify \
  --file "/absolute/path/to/file.pdf"

# 仅本地 dry run
bash "${DIGEST_TASK_DIR}/run.sh" --dry-run --no-notify \
  --file "/absolute/path/to/file.pdf"
```

将 `${DIGEST_TASK_DIR}` 替换为实际任务目录。真实 Codex smoke、真实飞书文档和
真实群消息均需要明确单独授权；这些命令的 `--preflight-only` 不会执行外部写入。

## 本机依赖

```bash
command -v codex lark-cli pdftotext pdfinfo
command -v ocrmypdf pdftoppm tesseract  # OCR fallback 可选但建议
```

中文 OCR 可安装 `tesseract-lang`，然后以 `tesseract --list-langs` 检查
`chi_sim`。提取器会先尝试 PDF 自带文本层，只有质量不够时才用本地 OCR。

完整流程与恢复语义见 [WORKFLOW.md](WORKFLOW.md)、
[../../docs/project_flow_zh.md](../../docs/project_flow_zh.md) 和
[../../docs/runtime-recovery.md](../../docs/runtime-recovery.md)。

# ZSXQ PDF 总结任务

先记住这一条：

- 仓库里的主副本在 `${AUTOMATION_ROOT}/openclaw_tasks/zsxq_pdf_digest`
- 真正被 cron 调用的入口仍然是 `${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest`
- 现在任务目录里的代码文件会链接回仓库主副本，所以以后只改仓库这一份
- `run.sh` 会把日志、状态、缓存继续写在任务目录；`summary_prompt.md`、`summary_system_prompt.md`、`extract_pdf_text.py` 这类静态文件如果任务目录缺失，会自动回退到仓库主副本
- 这样以后仓库里新增一个提示词文件时，就不用再担心忘了手工补 symlink 导致任务跑到一半才崩
- `cron.log`、`watch_state.json`、`pending_batch.json`、`text_cache/`、`summary_cache/` 这类运行态文件，仍然留在任务目录，不污染仓库

这个任务只做一件事：
监听 `${RESEARCH_LIBRARY_ROOT}/pdfs` 里的新增 PDF，
然后先生成本地摘要文件，再把摘要写进飞书文档，最后把文档链接发回当前群。

路径口径：
- `${RESEARCH_LIBRARY_ROOT}` 是正式资料层。
- `${RESEARCH_LIBRARY_ROOT}/pdfs` 是正式 PDF 扫描入口。
- `${DOWNLOADS_DIR}` 只作为浏览器下载暂存区。
- 旧的 `${DOWNLOADS_DIR}/ZSXQ-外资研报` 不再作为总结任务的兼容扫描路径。

完整执行流程说明见：
- [WORKFLOW.md](WORKFLOW.md)

默认策略：
- cron 每 10 分钟检查一次
- 检测到新增 PDF 后，不立刻总结
- 最近一次新文件写入后，静默满 15 分钟才开始
- 如果 ZSXQ 自动下载任务还在运行，也会先等待
- 真正开始处理前，先做一次环境预检
- 默认按单文件分批（`chunk_size=1`），避免单个坏 PDF 卡死整轮
- 当本轮新增 PDF 数量 `>15` 时，改为按每 `10` 份归入一个飞书文档
- 长批次默认开启中途发布：每攒够一份飞书文档的摘要，就先创建文档并发链接，不等待整轮全部结束
- 逐份播报进度默认关闭；需要时可把 `SEND_PROGRESS_EACH_FILE` 设为 `true`
- 同一批 PDF 连续失败时会分类退避：`EOF`、超时、断流等临时网络故障按 `5/10/20` 分钟快速重试，第 4 次失败后暂停；权限、认证、配置及其他故障仍按 `30/60` 分钟重试，第 3 次失败后暂停，避免每 10 分钟死循环
- 成功提取过的正文会进入本地缓存，下次优先复用
- 成功生成过的摘要也会进入本地缓存；飞书失败时，下次可以直接复用摘要，不用重读 PDF
- 明确判成内容失败的 PDF 会进入隔离清单，不再盲重试
- 支持手动指定单个文件或整个文件夹直接总结
- 支持 `--dry-run`、`--no-notify`、`--preflight-only`

## 当前 PDF 提取链路

当前生产链路已经简化成五步：

1. **`pdftotext` 首轮探测**
   - 先快速抽一次原生文本层
   - 如果文本质量达标，直接 fastpath 进入总结

2. **文本质量门禁**
   - 不再把“有文本”误判成“有正文”
   - 重点拦截水印、重复垃圾文本、无效隐藏文本层

3. **本地 OCR fallback**
   - 当 `pdftotext` 质量不足时，自动回退到 `ocrmypdf`
   - 如果 `ocrmypdf` 失败，再回退到 `pdftoppm + tesseract`
   - `ocrmypdf` 现在只生成 sidecar 文本，不再额外落盘一份 OCR PDF，减少本机磁盘 I/O
   - `ocrmypdf` 和 `pdftoppm + tesseract` 会共用同一套 OCR 语言选择；本机装了 `chi_sim` 就会自动带上
   - 每个 chunk 默认带一次文本提取重试，专门兜底间歇性失败

4. **结构化失败分型**
   - 提取失败会明确标成 `content_failure / env_failure / transient_failure`
   - `content_failure` 进入隔离区
   - `env_failure` 视为系统环境异常，不再伪装成“PDF 本身不可读”

5. **成功缓存**
   - 成功提取的正文会按文件指纹写进 `text_cache/`
   - 同一份 PDF 再次处理时，优先直接复用缓存文本

这次链路调整的核心目标是：
- 不再把“有文本”误判成“有正文”
- 不再把水印垃圾文本直接交给 agent
- 只保留本机能稳定运行的链路
- 文本质量不足时，宁可明确失败，也不要生成看起来完成、实际没读到正文的假总结

## 目录说明

- `config.env`：任务配置
- `config.env` 里的 `SUMMARY_AGENT_ID` 指定本地摘要阶段用的轻量 OpenClaw agent
- `config.env` 里的 `SUMMARY_AGENT_THINKING` 控制摘要阶段的 thinking；默认固定为 `medium`
- `config.env` 里的 `SUMMARY_AGENT_TIMEOUT_SECONDS` 控制单次摘要调用的本地硬超时；默认 `600` 秒
- `config.env` 里的 `SUMMARY_TIMEOUT_RETRY_COUNT` 控制摘要命中超时后额外补几次新会话重试；默认 `1`
- `config.env` 里的 `RESET_AGENT_SESSION_ON_RUN` 控制每次运行开始前是否先清一次专用 agent 的主会话
- `config.env` 里的 `PUBLISH_LARK_CLI_AS=user` 固定让飞书文档由本人身份创建/追加
- `config.env` 里的 `LARK_CLI_SEND_AS=bot` 固定让群通知由机器人发送
- `summary_prompt.md`：本地摘要阶段提示词，只负责读正文并输出标准摘要 JSON
- `summary_system_prompt.md`：本地摘要阶段专用 system prompt，更短，不带飞书发布职责
- `run.sh`：稳定入口，会把 `config.env`、`run.worker.sh`、helper、scanner、提取脚本和摘要提示词一起快照后再启动
- `run.worker.sh`：真正的单次执行逻辑
- `run.cron-safe.sh`：定时入口
- `extract_pdf_text.py`：本地文本预提取脚本，负责 `pdftotext -> 文本质量门禁 -> ocrmypdf -> pdftoppm+tesseract`
- `run.sh` 自己持有并发锁，所以 cron 和手动补跑不会同时覆盖结果文件
- `run.sh` 现在不会直接跑仓库里的可变文件，而是先复制一整套临时快照再执行，避免你改 config、代码、helper 或摘要 prompt 时把一条正在运行的长任务切坏
- `run.worker.sh` 现在只在摘要 agent 调用前后清会话；飞书发布和群通知都走 `lark-cli`
- `run_status.json`：当前或最近一次运行的实时状态，重点看 `status / phase / last_heartbeat_at / current_chunk_index`
- `watch_state.json`：扫描基线
- `failure_backoff.json`：同一批文件的失败退避状态
- `last_preflight.json`：最近一次环境预检报告
- `quarantine.json`：已隔离的内容型失败文件
- `quarantine_report.md`：给人看的排障清单，按文件逐条列出结论和建议动作
- `text_cache/`：成功提取过的正文缓存
- `summary_cache/`：成功生成过的单文件摘要缓存
- `pending_batch.json`：本次新增 PDF 清单
- `last_result.json`：本次结构化结果
- `last_result.md`：本轮最终结果摘要

## 执行链路

1. `run.sh` 先快照 `config.env`、`run.worker.sh`、helper、scanner、提取脚本和摘要提示词，保证这轮执行不受后续代码改动影响
2. `run.worker.sh` 用这套快照调用本地扫描脚本，找出新增 PDF
3. 新 PDF 会先进入待处理队列，不会因为静默窗口而丢失
4. 如果没有新增 PDF，任务直接结束，只写状态文件，不发群提醒
5. 如果最近一次新文件写入还没静默满 15 分钟，任务先等待
6. 如果 ZSXQ 自动下载任务仍在运行，任务先等待
   - 这两种等待现在都会在 `last_result.json` 和 `run_status.json` 里写成 `waiting`，不再混成 `success`
   - `run.sh` 使用带 run ID、进程启动签名和 owner token 的目录锁；确认上一轮还活着时，新触发直接跳过，重启后遗留的旧 PID 不会卡死任务
7. `run.sh` 先跑一次环境预检，确认 OCR 链路、摘要模板和临时目录可用
8. 对每个 chunk，先由 `extract_pdf_text.py` 生成可读正文文本
9. 只有当正文文本存在且质量达标时，`run.sh` 才会继续进入摘要阶段
10. `run.sh` 先检查这批 PDF 有没有现成的本地摘要缓存；有就直接复用
11. 没有摘要缓存时，摘要专用 agent 会按 `summary_prompt.md` + `summary_system_prompt.md` 读取正文文本，生成本地 Markdown/JSON 摘要
   - 这里每次调用前都会先清会话，所以单个 PDF 不会背着前面几个 PDF 的上下文一起跑
   - 摘要现在带本地硬超时；单次超过 `SUMMARY_AGENT_TIMEOUT_SECONDS` 会直接终止，再按 `SUMMARY_TIMEOUT_RETRY_COUNT` 补一次新会话重试，避免一篇长尾把整轮拖太久
12. 摘要落盘成功后进入飞书发布队列；长批次会在队列攒够 `DOC_GROUP_SIZE` 后先发布已完成摘要
13. `run.sh` 用 `lark-cli docs` 以 `user` 身份创建或追加飞书文档
   - 新文档会继续校验和修正飞书文件标题，避免文件名停留在 `Untitled`
   - 文档会用 `lark-cli drive` 授权给目标群
14. 只有飞书发布成功，`run.sh` 才会确认本批次已处理，避免重复总结；内容型失败则写入隔离清单
15. `run.sh` 用 `lark-cli im` 以 `bot` 身份把文档链接发回当前群；如果用了 `--no-notify`，只写本地结果，不发群消息
16. 无论成功还是失败，`run.sh` 都会把最终摘要打印到标准输出，方便在聊天里通过 `exec ./run.sh ...` 读取真实结果

## 依赖

当前简化稳定版只依赖本机已有的：

- `pdftotext`
- `ocrmypdf`
- `pdftoppm`
- `tesseract`

可用下面命令快速检查：

```bash
command -v pdftotext ocrmypdf pdftoppm tesseract
```

补中文 OCR：

- Homebrew 的 `tesseract` 主包默认只带 `eng / osd / snum`
- 要支持中文扫描页，安装：

```bash
brew install tesseract-lang
```

- 安装后可检查：

```bash
tesseract --list-langs | rg 'chi_sim|eng'
```

- 当前脚本会自动选语言：
  - 如果本机同时有 `eng` 和 `chi_sim`，就用 `eng+chi_sim`
  - 如果没有 `chi_sim`，就自动退回 `eng`

## 唯一入口

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.cron-safe.sh
```

## 手动触发

指定单个文件：

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.sh --file "/absolute/path/to/file.pdf"
```

指定一个文件夹：

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.sh --folder "/absolute/path/to/folder"
```

只做预检，不进入业务链路：

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.sh --preflight-only
```

只跑到文本提取、本地摘要缓存检查和摘要提示词生成，不写飞书、不发群消息：

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.sh --dry-run --file "/absolute/path/to/file.pdf"
```

正常执行，但不发群消息：

```bash
bash ${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.sh --no-notify --file "/absolute/path/to/file.pdf"
```

## 关键配置

`config.env` 里当前和提取链路相关的参数：

- `TEXT_EXTRACT_MAX_CHARS`：正文文本总长度上限
- `TEXT_EXTRACT_RETRY_COUNT`：文本提取阶段的额外重试次数
- `LOCAL_OCR_FALLBACK_ENABLE`：是否启用本地 OCR fallback
- `SUMMARY_AGENT_TIMEOUT_SECONDS`：单次摘要调用的本地硬超时
- `SUMMARY_TIMEOUT_RETRY_COUNT`：摘要命中超时后的额外重试次数
- `SUMMARY_CACHE_DIR`：本地摘要缓存目录
- `PREFLIGHT_JSON`：预检报告文件
- `QUARANTINE_JSON`：隔离清单文件
- `QUARANTINE_REPORT_MD`：人工排障清单
- `TEXT_CACHE_DIR`：正文缓存目录

补充说明：
- `run.cron-safe.sh` 现在会显式补 `TMPDIR`，避免 macOS cron 环境缺少用户临时目录，导致 `ocrmypdf` / `tesseract` 在 `/tmp` 下间歇性失败
- `pdftoppm + tesseract` fallback 会自动按本机已安装语言选择 OCR 语言；装了 `chi_sim` 后，会自动切到 `eng+chi_sim`
- `run.sh` 快照会同时带上 `runtime_paths.py` 和运行锁 helper，避免临时目录中的 Python 脚本因缺少本地模块而整轮失败
- `run.cron-safe.sh` 会在 `cron.log` 达到上限时轮转，默认单文件 20 MiB、保留 3 份，避免长期运行吃满磁盘
- Obsidian 增量和全量索引都有进程组硬超时，默认 900 秒；索引卡住只记 warning，不会永久占住任务锁，也不回滚已发布结果

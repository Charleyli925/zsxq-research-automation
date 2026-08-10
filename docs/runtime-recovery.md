# 运行恢复契约

本文档说明任务因休眠、断电、进程崩溃、网络或外部服务中断后，下一次触发
怎样安全继续。

## 恢复边界

- 电脑关机期间本地任务不会执行。下载 LaunchAgent 在用户下次登录后补触发
  一次；PDF digest 在下一个十分钟检查周期继续。
- 调度器只负责再次唤醒，不负责记住业务进度。下载进度由 checkpoint、不可
  变 scan plan 和 run manifest 决定；摘要发布进度由 pipeline SQLite state、
  artifact identity 和 publication transition 决定。
- 下载窗口只有在候选文件全部对账后才推进。中断时不推进 checkpoint；已归档
  文件依靠文件指纹和 manifest 去重。
- PDF 只有在 publish `success` 或明确 content quarantine 后才被确认。未确认
  的 PDF 会在下次扫描中再次出现；已生成的正文和摘要 artifact 会被复用。
- digest 保留既有 `run.cron-safe.sh -> run.sh` 调度入口，但 `run.sh` 只读取
  本地配置并启动 Python pipeline。它不快照或清理 OpenClaw agent、session、
  registry 或 auth 文件。

## 并发与假死防护

- stage lease 用短 `BEGIN IMMEDIATE` 事务领取；运行中断后 lease 到期可被下一
  次触发安全接管。
- 兼容 `run_status.json` 的 running/heartbeat 只用于避免 cron 重叠；持久 stage
  与 publication 记录才是恢复决定依据。
- 每次触发都必须产生本次 run ID 的结果导出。如果内层在最终化前退出，不能把
  上一轮成功当作本轮成功。
- 有限任务不使用无条件 `KeepAlive`，避免登录失效或外部故障时高频重启循环。
- Codex summary 调用有独立进程组和硬超时：到期先 TERM，固定 grace 后 KILL；
  一个超时 job 不会让另一并发 summary worker 停止。extract、summary 与
  publish 的 transient failure 固定在 5 分钟后最多重试一次；第二次失败转为
  `blocked_release`，等待人工修复而不是持续消耗模型或远端写入额度。

## 中断后的预期行为

1. 用户登录 macOS 后，两个下载 LaunchAgent 因 `RunAtLoad=true` 各触发一次。
2. 共享浏览器锁只允许一条下载链进入；另一条等待或返回 `busy`。
3. 下载任务从最后已提交 checkpoint 重建时间窗口，对照 scan plan 和 manifest
   补齐缺失文件。
4. digest 的下一次 cron 检查重新发现未确认 PDF，优先命中文本和摘要缓存；缓存
   命中时不启动 Codex 子进程。
5. 若远端正文已经写入，state 中保留 `remote_written` 和 document URL；恢复只做
   标题/fetch/权限验证，绝不再 create 或 append 同一正文。
6. 群消息失败只会留在 notification outbox 中；文档 publication 保持成功，由
   后续触发独立重试。

## 健康检查

优先看结构化状态，不要只看进程是否存在：

```bash
launchctl print gui/$(id -u)/com.example.zsxq-autodownload
launchctl print gui/$(id -u)/com.example.zsxq-domestic-cicc
jq . "${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/run_status.json"
jq . "${OPENCLAW_TASKS_ROOT}/ZSXQ_国内研报_中金公司/run_status.json"
jq . "${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run_status.json"
```

将示例 label 和既有 task 根目录替换为实际值。任务目录保留历史名称并不表示
摘要 pipeline 依赖 OpenClaw。

健康运行应满足：

- `run_status.json` 的 run ID 属于本次触发，运行中的 `last_heartbeat_at` 持续
  更新；它是兼容展示，不替代 SQLite state。
- 终态的 `last_result.json` 与本次 run ID/退出码一致。
- `busy` 表示重入被阻止，不是原运行失败。
- 心跳长时间不变且 owner 进程已不存在时，下一次调度可接管；接管后仍须从
  durable state 判断已完成阶段。

`cron.log` 按大小轮转，默认单文件 20 MiB、保留 3 份。不应把无上限日志增长
误判为业务数据增长。

## 发布与积压恢复

`remote_written` 不是可删除的“半成品”。它证明一次远端写入已发生，但还未完成
标题、fetch 或权限校验。遇到发布失败时：

1. 不要清空 text/summary cache、outbox、历史摘要或 publication 记录。
2. 用本机 capability preflight 检查 Codex 和 lark-cli 参数，不触发真实写入：

   ```bash
   codex exec --help
   lark-cli docs +create --help
   lark-cli docs +update --help
   lark-cli docs +fetch --help
   lark-cli im +messages-send --help
   ```

3. 修复本机 CLI、权限或网络后，让同一 pipeline 重新领取 stage。它会从缓存或
   `remote_written` 恢复，不应重新让模型读取 PDF 或重复写飞书正文。
4. 如需真实 Codex smoke、真实飞书 canary 或批量积压恢复，先暂停 cron 并取得
   明确授权；本 PR 不自动执行这些外部动作。

认证、release-contract、invariant 和 content failure 是终态类别，不能靠无
限制定时重试解决。先修复原因或通过后续受审计的 workflow 释放。

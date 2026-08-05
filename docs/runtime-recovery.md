# 运行恢复契约

本文档回答一个问题：任务因休眠、断电、进程崩溃、网络或外部服务中断后，下一次触发怎样安全继续。

## 恢复边界

- 电脑关机期间不可能执行本地任务。下载 LaunchAgent 在用户下次登录后补触发一次；PDF 摘要在下一个十分钟检查周期继续。
- 调度器只负责再次唤醒，不负责记住业务进度。业务进度由 checkpoint、不可变 scan plan、run manifest、研究库索引和本轮 canonical result 共同确定。
- 下载窗口只有在候选文件全部对账后才推进。中断时不推进 checkpoint，下次会重扫同一窗口；已归档文件依靠文件指纹和 manifest 去重。
- PDF 摘要只确认已成功发布或已明确隔离的项。未确认 PDF 会在下次扫描中再次出现；已生成的正文和摘要缓存会被复用。

## 并发与假死防护

- 任务锁记录 run ID、owner token、PID 和进程启动时间。不只用 `ps -p <pid>` 判断，因为重启后 PID 可能被无关进程复用。
- 活锁让新触发返回 `busy`；owner 已消失或签名不匹配时，下一次触发可安全接管陈旧锁。
- 每次触发都必须产生同 run ID 的结果。如果内层在最终化前退出，外层会生成当前运行的失败结果，不允许把上一轮成功当成本轮成功。
- 有限任务不使用无条件 `KeepAlive`，避免登录失效或外部故障时变成高频重启循环。
- Codex 浏览器任务、摘要 agent 和 Obsidian 索引都有进程组硬截止；子进程卡死后会被一起终止，不会只退出父 shell 却留下占锁孤儿。

## 断电后的预期行为

1. 用户登录 macOS 后，两个下载 LaunchAgent 因 `RunAtLoad=true` 各触发一次。
2. 共享浏览器锁只允许一条下载链进入。另一条等待或返回 `busy`，后续依照它自己的日历触发再试。
3. 下载任务从最后已提交 checkpoint 重建时间窗口，对照 scan plan 和 manifest 补齐缺失文件。
4. PDF 摘要的下一次 cron 检查重新发现未确认 PDF，复用已存在缓存并继续发布。

## 健康检查

优先看结构化状态，不要只看进程是否存在：

```bash
launchctl print gui/$(id -u)/com.example.zsxq-autodownload
launchctl print gui/$(id -u)/com.example.zsxq-domestic-cicc
jq . "${OPENCLAW_TASKS_ROOT}/ZSXQ_autodownload/run_status.json"
jq . "${OPENCLAW_TASKS_ROOT}/ZSXQ_国内研报_中金公司/run_status.json"
jq . "${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run_status.json"
```

将上述示例 label 替换为通过 `deploy/install_local_runtime.sh` 安装时使用的
实际 label。

健康运行应满足：

- `run_status.json` 的 run ID 属于本次触发，运行中的 `last_heartbeat_at` 持续更新。
- 终态的 `last_result.json` 和 canonical result 具有相同 run ID 和退出码。
- `busy` 表示重入被阻止，不是原运行失败。
- 心跳长时间不变且 owner 进程已不存在时，才将其视为陈旧运行；下一次调度应自动接管。

`cron.log` 按大小轮转，默认单文件 20 MiB、保留 3 份。不应再把无上限日志增长误判为业务数据增长。

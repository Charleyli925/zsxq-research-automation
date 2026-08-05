# ZSXQ 下载任务部署入口

这两个脚本是外资研报和国内研报下载任务共用的、受 Git 管理的外层入口。真实任务目录只保存 `config.env`、状态、日志、结果和通知 outbox；不要再维护一份脱离仓库的 `run.sh`。

## 配置

分别把以下示例复制为真实任务目录中的 `config.env`，再填入本机路径和私有聊天目标：

- `config.foreign.env.example` → `ZSXQ_autodownload/config.env`
- `config.domestic.env.example` → `ZSXQ_国内研报_中金公司/config.env`

其中 `NOTIFICATION_PIPELINE` 和 `CODEX_STRUCTURED_REPORT_PATH` 必须和任务类型对应。首次安装或升级时，安装脚本会生成同目录、被 Git 忽略的 `deployment.env`，并以它覆盖代码路径到干净的发布 checkout；因此不必也不应为了升级而编辑包含私有信息的 `config.env`。

## 安装与更新

从已验证的 release checkout 执行：

```bash
bash deploy/install_local_runtime.sh --apply
```

安装脚本会先检查 checkout 是否干净、任务是否仍在运行，然后把两个下载入口链接到此目录，并渲染含 `RunAtLoad=true` 的 LaunchAgent。它不会覆盖 `config.env`、浏览器 profile、下载内容、状态或日志；被替换的旧入口会放入任务目录的 `.deployment-backups/`，可人工恢复。

若有运行中的下载任务，等它结束后再执行安装；可先用 `--dry-run` 查看将要发生的变更。

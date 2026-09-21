# 本地运行与数据管理

当前项目已安装依赖。重新安装时使用 Python 3.12+、Node.js，运行 `.venv/bin/pip install -e .` 与前端 `npm ci`。

```bash
bash scripts/start_demo.sh
```

默认后端 127.0.0.1:8000，前端 127.0.0.1:5173。前端通过 `/api` 代理访问后端；`API_PORT` 和 `UI_PORT` 可覆盖端口。关闭脚本终端会结束服务。

模型配置从根目录 `.env` 读取，进程已有环境变量优先。当前已配置 `AI_CASE_LLM_WIRE_API=responses`。结构化请求会显式声明 JSON；如果网关返回 400/422，适配器会记录安全的错误摘要并尝试 Chat Completions。密钥只供模型适配器使用；不要粘贴进日志或导出。

知识库默认为项目内 `data/runtime/catalog.db`。本轮只通过 SQLite backup API 迁移了旧临时目录中的这一个案例库；旧 `ui-*` 演示数据库留在原处。原件未删除。

```bash
.venv/bin/python scripts/backup_catalog.py
```

备份保存在 `data/runtime/backups/`，脚本验证 `PRAGMA integrity_check`。需要恢复时先停止服务，保留当前案例库副本，再用选定备份替换 catalog.db。模型密钥不在数据库中，需要另行保管 `.env`。

`scripts/daily_collection.sh` 是采集、导出、企业微信简报、备份的单次入口。当前调度设为每天本地时间 08:30 开始采集，完成后发通知；不是固定 08:30 发消息。电脑需要在执行期间可运行且联网。

macOS 后台任务直接读取 Desktop 项目曾出现 `Operation not permitted`。现在使用 `scripts/deploy_background.py` 将代码和运行依赖部署到 `~/Library/Application Support/ai-case-workbench`，再安装常驻服务、每日采集与每 5 分钟健康检查/通知重试。服务仍只监听 `127.0.0.1`，不开放公网。

部署前先确认没有进行中的采集，并停止本项目的旧 API、Vite 和常驻启动脚本。脚本不会自行终止进程；若案例库仍被打开、采集锁占用或仍有进行中记录，会拒绝迁移。

```bash
# 可以在旧服务运行期间准备依赖，缩短停机时间
.venv/bin/python scripts/deploy_background.py --prepare-only
# 停止旧服务后部署/更新，并安装 LaunchAgents
bash scripts/install_launch_agents.sh
```

首次部署保留原 `data/runtime` 到 `data/backups/runtime-before-background-*`，使用 SQLite backup API 校验后，将桌面项目的 `data/runtime` 链接到后台唯一运行数据目录。两份 `.env` 的数据目录一致、权限为 0600，旧配置和 LaunchAgent 文件保留备份。使用绝对运行路径并设置 Node/npm 的 PATH，避免后台缺少交互式 shell 环境。后续更新桌面源码或 `.env` 后，需要停止后台服务并重新部署，才能更新运行副本。

```bash
# 检查后台服务状态
launchctl print "gui/$(id -u)/com.ai-case-workbench"
launchctl print "gui/$(id -u)/com.ai-case-workbench-daily"
# 检查配置、SQLite 并创建备份，不触发采集或发送
bash scripts/daily_collection.sh --check-setup
```

通知开关和 Webhook 仅放在 `.env`，未配置时默认关闭。测试/补发已保存简报可运行 `scripts/notify_collection.py --latest --send`，不再调用模型。已确认成功的同轮简报不会重复发送；平台明确拒绝时每 5 分钟检查重试，达到 3 次停止；超时等结果未知状态需要先核实群消息，避免重复。日志和 outbox 不保存 Webhook。

按用户范围，暂不做正式测评集、账号体系、权限和审批身份。未来可在任务上下文和远程写入适配层加入 actor、resource_scope、reviewer 等身份字段，启用时必须实现真实授权校验；现在没有这些字段提供的权限保障。

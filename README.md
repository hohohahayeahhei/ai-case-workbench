# AI 案例情报工作台

面向 AI 产品经理岗位的个人项目：主动搜索别人如何使用 AI 解决工作和生活问题，抓取原文并抽取证据，由同一配置模型在独立上下文中核验和复审，再评分、入本地知识库、生成报告。当前尚无正式准确率测评。

## 使用

首次从 GitHub 克隆后，在项目根目录准备 Python 3.12+ 和 Node.js 22+：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
cd frontend
npm ci
cd ..
```

`.env.example` 默认使用 mock 模型，便于先运行离线演示。联网采集需在本地 `.env` 配置兼容模型；密钥、数据库、素材、登录状态和历史备份均不进入 Git 仓库。

```bash
bash scripts/start_demo.sh
```

打开 http://127.0.0.1:5173，默认进入「案例探索」。输入主题，点击「联网找案例」，查看进度、证据和失败原因。「发现队列」用于启动采集，「今日工作台」保留独立的 LangGraph 快照演示。

```bash
# 实时搜索；本轮总处理预算为 5，已就绪抖音作品最多占 2 条
.venv/bin/python scripts/run_collection.py --source-mode live --max-results 5
# 不重新搜索，重试已保存的待处理候选
.venv/bin/python scripts/run_collection.py --source-mode live --no-search --retry-pending
# 采集、导出并验证 SQLite 备份
bash scripts/daily_collection.sh
# 隔离临时数据进行代码回归自检
.venv/bin/python scripts/self_check.py
```

## 实际架构

```text
主控 CollectionPipeline（预算、去重、持久化、故障隔离）
  → Scout Skill → Live Source MCP.search_web → 模型服务的原生 web_search
  → Live Source MCP.fetch_page → 公开正文 + 内容哈希
  → Extract Skill → 独立模型调用，逐段摘要和原文引文
  → 程序校验 → 最多一次带具体错误的修正
  → Verify Skill → 另一独立上下文检查每条摘要是否受引文支持
  → Score Skill 提供理由 + scoring.py 六维规则计算最终分
  → Review Skill 独立复审证据与评分理由
  → SQLite 精选库 → 查询 API / 工作台 / 本地证据报告
  → 可选腾讯文档导出文件
```

这里的 Agent 是同一模型的不同职责和独立上下文，由主控顺序编排；不是并行自治进程。Skill 是实际读取的规则文件，MCP 是实际经过官方 SDK stdio 的工具调用。SQLite 是持久化主库，原始页面和时间证据是事实依据；腾讯文档只是可选导出目标。旧 LangGraph 工作流另行展示人工审核、中断恢复、版本冻结及模拟发布回执。

## 配置和数据

项目根目录 `.env` 保存模型配置，不写入日志或报告。当前服务已验证支持 Responses 和原生网页搜索；无需额外搜索密钥。使用其他兼容服务前应验证它是否支持 web_search，不能用模型凭空给出的链接冒充搜索。

默认数据在 `data/runtime/catalog.db`；可用 `AI_CASE_DATA_DIR` 改位置。`exports/` 含每次运行 Markdown/JSON，以及 `tencent-docs-pending.json` 和可导入的 TSV；`backups/` 保存通过完整性校验的数据库备份。采集日期与原文发布日期分开，不把旧文章说成今日发布。

## 文档

- `docs/completeness-audit.md`：当前审查结果与已知边界。
- `docs/discovery-pipeline.md`：实时采集与故障恢复。
- `docs/reliability-v3.md`：搜索、抓取、结构化输出和历史积压的修复与实测记录。
- `docs/tencent-docs.md`：链接写入和无人值守同步的区别。
- `docs/deployment.md`：启动、持久化和备份。
- `skills/case-{scout,extract,verify,score}/SKILL.md`：实际 Agent 规则。
- `skills/ai-case-intelligence/`：面向其他 Agent 的知识库查询约定。

正式测评集、多用户、权限与审批人身份暂不实现。停止工作台服务后，运行 `bash scripts/install_launch_agents.sh` 可部署到本机 Application Support 并安装常驻服务、每天 08:30 采集任务和每 5 分钟健康检查；保留原数据备份，桌面项目与后台共用同一案例库，日志仍可从 `data/runtime/logs/` 访问。更新代码后需重新部署才能应用到后台副本，详见 `docs/deployment.md`。
精选案例页首次打开时会自动提交一次 live 采集任务：Scout 通过 MCP 搜索公开案例，随后由 Fetch、Extract、Verify、Score Agent 逐条处理并写入本地 SQLite；页面会轮询任务进度并在任务结束后刷新精选流。已有任务会复用，超过 15 分钟无事件且确认不再持有采集锁的旧任务会标记为中断，候选可续跑。

## 证据评分与复审（v4）

当前实时采集：发现来源 → 抓取原文 → 抽取证据 → 事实核验 → 六维评分 → 独立复审 → 保存；评分失败最多修正一次，未完成显示待评分。规则及历史研究见 [评分策略](docs/scoring-policy.md)，Skill实际融合与能力边界见 [融合记录](docs/skill-integration-v4.md)。旧分迁移保留历史，可用 `.venv/bin/python scripts/regrade_catalog.py --limit 20` 从已有原文重评。

## 企业微信每日简报

本地 `.env` 设置 `AI_CASE_NOTIFICATIONS_ENABLED=true`。支持个人智能机器人（`AI_CASE_NOTIFICATION_PROVIDER=wecom_aibot`，配置 Bot ID、Secret 并绑定本人）和群消息推送（`wecom_webhook`，配置 `AI_CASE_WECOM_WEBHOOK_URL`），接入步骤见 [配置记录](docs/wecom-notification-setup.md)。凭证仅保存在后端私密配置，不要提交或贴入日志。每天 08:30 开始采集，结束后发送最多 5 条本轮新增精选案例的方法、结果和原文链接；未达标或未核验内容不会进入简报。没有新增精选时发送简短状态。

```bash
# 只预览最近一次已完成的实时采集，不发送消息
.venv/bin/python scripts/notify_collection.py --latest
# 发送该次简报；相同运行与接收目标不会重复发送
.venv/bin/python scripts/notify_collection.py --latest --send
# 验证后台配置和数据库，不采集、不发通知
bash scripts/daily_collection.sh --check-setup
```

手动 CLI、网页采集和离线自检默认不发通知；每日脚本显式启用 `--notify`。平台明确拒绝的消息每 5 分钟检查重试，最多尝试 3 次；超时或结果未知的消息保留记录，不盲目重复发送。通知状态保存在 SQLite 的 `notification_outbox` 中，通知失败不影响采集入库和备份。

## 抖音来源

侧栏「抖音采集」支持分享文本/短链/作品链接、关键词与作者发现任务、页面日期与作者观察、本地素材上传、时间段证据与现有四阶段审核。作品 ID、证据版本和素材哈希分别去重，抖音故障不阻断普通网页来源。

当前浏览器任务需要实际在本地打开页面并记录观察；程序尚无无人值守浏览器驱动。本机转写需另行配置 ffmpeg 与 Whisper 工具/模型，未配置时明确报告失败。2026-09-21 的真实搜索访问停在扫码登录，尚未证明真实视频采集成功；受控测试不计为成功案例。详见 [接入说明](docs/douyin-integration.md) 和 [验收记录](docs/douyin-validation-2026-09-21.md)。

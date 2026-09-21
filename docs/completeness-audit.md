# 项目完整性审查

审查日期：2026-09-14

## 结论

作为 AI 产品经理岗位的 Vibe Coding / 多 Agent 协作演示，项目已经形成可运行闭环：来源发现、原文抓取、证据抽取、模型核验、程序评分、去重聚类、人工审核、冻结、知识库同步、回读、发布回执和局部恢复都有对应实现。

作为个人本地使用的 MVP，可以开始演示。作为生产系统或完全开放式网页搜索产品，仍需要搜索服务、真实模型服务可达性、腾讯文档配置和定时部署环境。

## 已完成并验证

| 能力 | 实现 | 验证 |
| --- | --- | --- |
| 多 Agent 工作流 | LangGraph 研究、核验、编辑 Agent | 回归测试、审核中断和恢复测试 |
| MCP 核心概念 | Source、Evidence、Knowledge Base、Distribution MCP | Mock 和官方 stdio 协议检查 |
| 互联网候选发现 | 来源目录、RSS/Atom、文章抓取 | Feed 解析、失败隔离、原文入库测试 |
| 模型结构化抽取 | Chat Completions / Responses 双兼容 | JSON 请求、路径回退、证据定位测试 |
| 质量控制 | 数字溯源、证据门槛、评分卡、硬规则 | 对抗模型和评分测试 |
| 数据持久化 | SQLite 候选、原文、聚类、同步状态 | API 和持久化测试 |
| 可操作工作台 | 案例探索、发现队列、审核、运行时间线、评测中心 | 前端生产构建 |
| 可靠性 | 幂等、冻结版本、精确回读、发布未知状态、局部恢复 | 场景矩阵测试 |
| 自检 | 一条命令检查语法、测试、MCP、采集和前端 | `scripts/self_check.py` 通过 |

## 当前边界

1. 目前互联网入口是来源目录中的公开 RSS/Atom 和显式 Feed URL，还没有通用 Search MCP。要覆盖任意网站，需要接入搜索 API 或浏览器搜索连接器。
2. RSS 文章在抓取后会调用配置的模型做结构化抽取；如果模型不可达、输出不是 JSON 或证据无法在原文定位，候选会停在 `needs_extraction`。
3. 当前环境不能访问用户局域网模型地址，因此真实模型请求只能由用户本机执行 `scripts/check_llm.py` 验证。
4. 腾讯文档 MCP 仍需要 Token、工具名和目标文档参数；本地 SQLite 已可独立作为知识库。
5. 采集脚本已经可以被 launchd、cron 或 CI 调用，但项目没有自行安装后台常驻任务。
6. 多用户、权限、审批人身份、正式测评集和生产部署不在当前 MVP 范围内。

## 本机验收

```bash
cd "/Users/honey/Desktop/ai案例工作台"
.venv/bin/python scripts/check_llm.py
.venv/bin/python scripts/self_check.py
```

`check_llm.py` 只输出模型服务地址、模型名和结构化响应状态，不输出 API Key。

# 第六步：失败恢复与评测中心

## 局部恢复

同步回读冲突时，工作流保存的状态会被读取出来，只执行同步和回读，再继续发布。研究、核验和编辑不会被重复执行。

发布结果为 `unknown` 时，系统使用已有的外部请求 ID 查询回执。演示模式提供一个“模拟回执已确认”的恢复动作，真实连接器应替换为实际回执查询。

API：

```text
POST /api/runs/{run_id}/recover {"action":"sync"}
POST /api/runs/{run_id}/recover {"action":"publish"}
```

## 评测

```text
GET /api/evaluations/latest
```

评测使用带标签的脱敏 Fixture，比较：

- baseline：直接相信核验 Agent 的输出
- multi_agent_guarded：核验 Agent + 重复检查 + 证据字段检查 + 数字证据检查

另外提供一个故意放行所有案例的 `UnsafeModel`，用于证明护栏能拦截错误，而不是把演示结果包装成业务收益。

## 官方 MCP 连接器

LangGraph 工作流现在支持 `connector_mode=official_mcp`。该模式通过官方 MCP Python SDK 的 stdio Client 调用本地 `mcp_demo_server.py`，并使用临时状态文件保持模拟知识库和发布回执。

```bash
.venv/bin/python scripts/run_langgraph_demo.py --run-id official-001 --connector-mode official_mcp
.venv/bin/python scripts/run_langgraph_demo.py --run-id official-001 --connector-mode official_mcp --resume --approval approve
```

## 面试演示报告

运行完成或暂停后，可以通过 `GET /api/runs/{run_id}/export` 导出 Markdown 报告。报告包含运行摘要、Agent 和人工决策时间线、案例核验结论、分发回执以及冻结内容，适合现场演示后的复盘或作品集附件。

# MCP 工具契约

MCP 在本项目中是外部工具和资源的统一连接层，不承担 Agent 编排。项目同时保留进程内模拟适配层和官方 Python SDK 实现的 stdio Server：前者服务于工作流测试，后者用于协议级验证。

## Source MCP

### `search_sources`

输入：`topic`, `lookback_days`, `max_results`

输出：候选来源列表，包含标题、URL、发布日期和来源类型。

### `fetch_source`

输入：`url`

输出：原文标题、正文摘要、发布日期、作者和可引用文本片段。

### `get_source_metadata`

输入：`url`

输出：来源类型、域名、可访问状态和抓取时间。

### `search_web`

输入：搜索词和最大返回数。

输出：公开来源候选。当前 Mock/stdio Server 使用离线目录；真实环境由 RSS、搜索服务或其他明确配置的公开来源适配器实现。

## Evidence MCP

### `save_evidence_bundle`

输入：案例 ID、原文 URL、问题/方法/结果证据和局限。

输出：证据包 ID 和内容哈希。

### `get_evidence_bundle`

输入：证据包 ID。

输出：完整证据包。

### `check_duplicate_case`

输入：规范化标题、规范化 URL、案例特征。

输出：是否重复、匹配记录和判断理由。

### `score_case`

输入：已抽取并带证据的案例。

输出：版本化分项评分、总分、精选层级、硬性拦截原因和推荐理由。评分工具不负责发布。

## Knowledge Base MCP

### `append_record`

输入：冻结版本 ID、案例字段、幂等键。

输出：写入记录 ID 和写入状态。

### `read_record`

输入：记录 ID。

输出：远端记录完整字段。

### `list_records`

输入：过滤条件。

输出：已有记录列表。

## Distribution MCP

### `send_message`

输入：冻结版本 ID、渠道、目标、文案哈希。

输出：发送状态和外部请求 ID。

### `get_delivery_receipt`

输入：外部请求 ID。

输出：已确认、失败或未知。

## 权限矩阵

| 调用方 | 允许 | 禁止 |
|---|---|---|
| 研究 Agent | Source MCP、Evidence MCP 读取/保存 | Knowledge Base、Distribution |
| 核验 Agent | Evidence MCP 读取/去重 | Knowledge Base、Distribution |
| 评分 Agent | Evidence MCP.score_case | Knowledge Base、Distribution |
| 编辑 Agent | 读取 verified 案例 | 搜索、写入、发送 |
| 同步执行器 | Knowledge Base 写入/回读 | 生成或改写内容 |
| 发布执行器 | Distribution 发送/查回执 | 生成或改写内容 |

## 协议级验证

```bash
.venv/bin/python scripts/check_mcp_protocol.py
```

这个检查会启动真实 stdio Server，通过 MCP Client 完成初始化、工具发现、工具调用、Resource 发现、Prompt 发现和幂等写入验证。

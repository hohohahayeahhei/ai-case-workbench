# 技术架构

## 1. 分层原则

```text
React 工作台
  -> FastAPI API
  -> LangGraph 编排层
  -> Agent 节点 + 确定性执行器
  -> MCP Client
  -> 本地/模拟 MCP Server
  -> SQLite 与运行事件
```

LangGraph 负责流程状态、暂停恢复和节点路由；MCP 负责标准化工具和资源连接；程序代码负责 Schema 校验、去重、哈希、冻结、幂等和回读比较。

## 2. Agent 职责

### 研究 Agent

拥有来源搜索、原文读取和证据保存权限，不能写知识库或发送消息。

### 核验 Agent

拥有证据读取、重复检查和补证权限，不能修改原文或发布内容。

### 编辑 Agent

只能读取通过核验的案例并生成草稿，不能搜索新事实或调用分发工具。

## 3. LangGraph 状态

```text
START
  -> research
  -> verify
  -> revise_or_reject
  -> edit
  -> freeze
  -> human_review
  -> sync
  -> readback
  -> publish
  -> receipt
  -> END
```

每次运行使用唯一 `run_id`，每个内容版本使用唯一 `content_version_id`，每个外部动作使用唯一 `delivery_task_id`。

## 4. 关键状态

- `candidate`：已发现，等待核验
- `needs_evidence`：证据不足，等待补证
- `rejected`：核验淘汰
- `verified`：核验通过
- `draft`：已生成草稿
- `frozen`：版本已冻结
- `sync_pending` / `synced` / `sync_conflict`
- `publish_pending` / `sent` / `unknown` / `publish_failed`

## 5. 可靠性设计

- 同一 `content_version_id + channel + target` 只能存在一个有效分发任务。
- 成功写入的任务重跑时直接跳过。
- 外部写入后必须回读并精确比较。
- 冲突进入人工处理，不自动覆盖已有记录。
- 发送超时不能直接判断失败，必须查询回执。
- 每个节点保存输入摘要、输出摘要、耗时和错误信息。

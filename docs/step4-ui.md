# 第四步：工作台 UI

## 已实现页面

- 今日工作台：运行指标、审核提醒、同步和发布状态
- 案例审核：原始标题、URL、问题/方法/结果和核验理由
- 运行详情：Agent、MCP 和执行器时间线
- MCP 与连接器：四类模拟 MCP Server 和工具目录

## 启动

终端一（运行数据库写入系统临时目录）：

```bash
cd /Users/honey/Desktop/ai案例工作台
.venv/bin/uvicorn backend.app.api:app --reload --port 8000
```

终端二：

```bash
cd /Users/honey/Desktop/ai案例工作台/frontend
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。

## 演示路线

1. 选择“完整成功链路”，点击“开始运行”。
2. 看到流程在人工审核处暂停。
3. 点击“批准并继续”。
4. 打开“案例审核”查看 1 条通过和 2 条淘汰。
5. 打开“运行详情”查看 Agent 和 MCP 事件。
6. 打开“MCP 与连接器”查看工具契约。
7. 新建运行并选择“知识库回读冲突”，演示发布被阻断。

## 自检

```bash
.venv/bin/python scripts/self_check.py
```

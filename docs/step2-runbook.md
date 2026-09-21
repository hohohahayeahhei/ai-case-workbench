# 第二步运行说明

这一阶段实现了一个不依赖外部 API 的本地闭环，适合录制第一版产品演示。

## 运行成功场景

在项目根目录执行：

```bash
python3 backend/app/demo_pipeline.py --scenario happy_path
```

预期结果：

- Source MCP 返回 3 条候选
- 1 条案例通过核验
- 1 条因无出处数字被淘汰
- 1 条因规范化 URL 重复被淘汰
- 日报版本被冻结
- 知识库写入并精确回读通过
- 发布回执为 `sent`

## 运行异常场景

```bash
python3 backend/app/demo_pipeline.py --scenario sync_conflict
python3 backend/app/demo_pipeline.py --scenario publish_unknown
```

`sync_conflict` 会在知识库回读发现差异后阻断发布；`publish_unknown` 会保留 `unknown` 状态，等待查询回执。

## 运行测试

```bash
python3 -m unittest discover -s backend/tests -v
```

## 当前实现边界

- `MockMCPRegistry` 是进程内的 MCP 形状适配层，提供可发现的 Server、Tools 和 Resources。
- 它没有伪装成真实远程 MCP 协议连接，也不需要账号或 Token。
- 当前项目已经提供官方 MCP Python SDK 的 stdio Client 模式，启动时可在前端选择；Streamable HTTP 仍属于后续真实连接器扩展。
- 当前默认 Agent 是确定性 Mock Agent；配置 `openai_compatible` 后可以接入兼容 Chat Completions 的真实模型，并继续经过程序护栏和人工审核。

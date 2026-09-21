# 第三步：LangGraph 状态工作流

## 运行环境

项目使用 Python 3.12 的 `.venv`，依赖定义在 `pyproject.toml`。已安装：

- `langgraph`
- `langgraph-checkpoint-sqlite`

## 第一次运行：在人工审核处暂停

```bash
.venv/bin/python scripts/run_langgraph_demo.py --run-id demo-001
```

首次运行会完成研究、核验、编辑和冻结，然后在 `human_review` 节点返回 `__interrupt__`。

## 恢复运行

```bash
.venv/bin/python scripts/run_langgraph_demo.py --run-id demo-001 --resume --approval approve
```

恢复时必须复用相同的 `run_id`，它同时作为 LangGraph 的 `thread_id`。批准后会继续知识库同步、回读和模拟发布。

## 其他场景

```bash
.venv/bin/python scripts/run_langgraph_demo.py --run-id conflict-001 --scenario sync_conflict
.venv/bin/python scripts/run_langgraph_demo.py --run-id conflict-001 --scenario sync_conflict --resume --approval approve
```

第二条命令会在同步回读冲突后停止，最终状态为 `sync_conflict`，不会进入发布节点。

## 设计依据

LangGraph 的 `interrupt()` 会保存图状态并等待外部输入，恢复时使用同一个 `thread_id` 和 `Command(resume=...)`。本项目把人工审核放在冻结版本之后、外部写入之前，避免审核过程中内容继续变化。

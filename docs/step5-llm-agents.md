# 第五步：模型驱动 Agent 与确定性护栏

## 默认模式

项目默认使用 `mock` 模式。它会真实经过三个角色调用，但不访问外部模型服务，适合测试、录屏和公开 Demo。

## 启用 OpenAI-compatible 模型

只在本地环境变量中配置：

```bash
export AI_CASE_LLM_MODE=openai_compatible
export OPENAI_API_KEY='你的本地密钥'
export AI_CASE_LLM_MODEL='你的模型名'
export AI_CASE_LLM_BASE_URL='https://api.openai.com/v1'
# 可选：chat 或 responses；默认 chat，404/405 时自动尝试另一种路径
export AI_CASE_LLM_WIRE_API=chat
```

也可以在 UI 启动运行时选择 `llm_mode=openai_compatible`。项目不会把密钥写入 SQLite、日志或 Fixture。

命令行可使用：

```bash
.venv/bin/python scripts/run_langgraph_demo.py --llm-mode openai_compatible
```

## 护栏规则

- 研究 Agent 只能选择 MCP 返回的案例 ID。
- 核验 Agent 必须接收原文证据；程序重新检查必填证据、重复和无出处数字。
- 编辑 Agent 只能复制已通过案例中的标题、URL、问题、方法和结果。
- 模型输出不符合结构或证据字段时，程序记录 fallback 并使用安全渲染。
- 同步和发布继续使用冻结版本，不经过模型。

## 自检

```bash
.venv/bin/python scripts/self_check.py
```

新增了对抗性模型测试：即使模型故意放行无证据数字并编造标题，程序也会淘汰无证据案例并阻止编造内容进入冻结日报。

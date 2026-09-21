# 线上案例来源

这组案例来自公开的一手客户故事页面。页面内容先由人工核对，再整理成 `data/fixtures/online_cases.json`；运行时使用快照，因此即使网页临时变化，演示仍可复现。

| 案例 | 一手来源 | 进入快照的事实 | 使用限制 |
|---|---|---|---|
| Microsoft Ask Microsoft | https://www.microsoft.com/en/customers/story/26166-microsoft-microsoft-copilot-studio/ | 多个按知识范围拆分的子 Agent；页面报告最高 61% 延迟下降和最高 70% 人工处理聊天量减少 | 企业自报，口径和实验设计未完整披露 |
| Gradient Labs | https://claude.com/customers/gradient-labs | Claude 结合知识图谱处理金融客服复杂问题；页面报告 80–90% resolution rate 和最高 98% 客户满意度 | 未披露完整样本、基线和长期效果 |
| Notion Managed Agents | https://claude.com/customers/notion | 任务板触发长会话 Agent，支持跨页面和文件工作；页面标注单任务板 30+ 并发 Agent tasks | 规模和成本结果不能直接外推 |

启动页选择“线上案例快照”即可使用这组数据。快照里的 `source_url` 保留原始链接，案例审核页可以回到原文复核。

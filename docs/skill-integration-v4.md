# Khazix Skills 融合与验收

本轮没有安装或运行上游辅助脚本。阅读的是六个独立Skill；磁盘清理与本任务无关。以下是按产品目标重新实现的机制，非完整照搬。

| 借鉴 | 实际实现 | 验收位置 |
|---|---|---|
| aihot：分数/精选/热度分开，空值诚实、查询回落 | 无有效评分为null；关键词无精选回落候选并标记；hot标为近期精选；同步声明快照重置 | query_agent.py、ScoreBreakdown.jsx、query测试 |
| hv-analysis：按来源分支、证据支持结论 | 主流程保存search_plan；实践来源与官方来源交错处理避免一种来源占满额度；原文快照及逐维引文；复审指出证据缺口后回读原文 | collection_pipeline.py、quality_agent.py |
| leader：验收契约和独立抽查 | 每轮保存目标、工具边界、入选门槛、重试上限；评分与复审是两次独立模型调用 | acceptance、skill_versions、quality_evaluation |
| khazix-writer：内容价值和四层质检 | 学习价值维度；复审检查事实、归因、清晰度、读者价值；保留原文数字，不制造噱头 | case-review/SKILL.md |
| neat-freak：代码、数据、界面和说明一致 | 旧分迁移保留历史；相同评分函数供应查询、全部队列和导出；工程检查不冒充评分准确率 | score_maintenance.py、test_quality_review.py |
| storage-analyzer：可读报告 | 每条案例展示数据、判断及下一步缺口，不加入磁盘清理 | ScoreBreakdown.jsx、collection_export.py |

## 工具和能力边界

主 Agent 是Python采集编排器，调度角色与有上限的循环；不是每个角色各起一个操作系统进程。Scout和Fetcher通过实际Search/Fetch MCP读公网；抽取、事实核验、评分、复审读取原文，只输出结构化数据；只有持久化程序写SQLite。`agent_skills.py`实际装载每个角色的SKILL及评分引用文件，记录包含引用的哈希。

本轮补证循环会针对评审意见重新读已有原文，不会把别人的无关案例拼成当前案例，亦未实现任意跨站自主研究。来源可追溯性最高档需要已读取独立佐证，当前单篇模式禁止该档。后续若扩展多来源补查，应新增证据URL与关联关系、转述去重及单独可核验的材料引用。

暂不做用户排除的多用户/权限和人工测评集。结果保存在本地SQLite和网页；腾讯文档不参与当前完成条件。

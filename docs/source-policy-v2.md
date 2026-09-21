# 来源策略 v2

更新时间：2026-09-15

工作台把“来源可信度”和“案例质量”分开处理。来源层决定搜索优先级和证据要求，评分层仍然要求原文同时说明真实问题、AI 工作步骤和可观察结果。任何来源都不能直接获得质量分或跳过独立复审。

## 已加入的来源层

| 层级 | 代表来源 | 用途 | 证据规则 |
|---|---|---|---|
| 官方一手 | OpenAI News、Anthropic Newsroom、Google AI、Google DeepMind、Microsoft AI、Meta AI、GitHub Blog、Hugging Face Blog、Microsoft Research、腾讯 WorkBuddy、DeepSeek Harness、QwenPaw | 产品公告、客户实践、官方文档和示例 | 文章本身包含真实任务、步骤和结果时可进入核验 |
| 官方社交 | OpenAI、Anthropic、Google DeepMind、Qwen 官方 X | 发现新发布和案例线索 | 帖子本身通常过短，必须回到官方长文、客户案例、文档或仓库 |
| 实践者原文 | Simon Willison、Ethan Mollick | 真实个人工作流、实验过程、生产力实践 | 优先保留作者亲身经历，要求能抽出任务、方法和结果 |
| 专业资讯与分析 | MIT Technology Review AI、The Verge AI、TechCrunch AI、Latent Space | 发现行业案例、项目和作者线索 | 只作 discovery，必须追到原始公告、项目仓库、作者文章或客户原文 |

## 判断结果

- **加入**：官方站点、官方客户案例、官方文档、长期记录实际使用的实践者博客。这些来源能够提供相对稳定的原文入口，且与“别人如何用 AI 解决问题”的目标直接相关。
- **加入但降级**：官方社交账号、专业媒体和综合分析站。它们有发现价值，但无法稳定提供完整的任务过程和效果证据，因此只进入线索检索分支。
- **不作为默认来源**：无作者、无原文链接、纯转载、只讲模型能力、只有营销口号或无法访问正文的页面。它们可以被搜索器偶然发现，但不能作为案例证据。

## 运行规则

每次联网采集会执行以下搜索分支：

1. 亲历者/实践文章：发现包含步骤和结果的原文。
2. 官方一手来源：限定已配置的官方站点和官方编辑频道。
3. 官方社交线索：限定已配置的官方账号，用于发现发布，再回溯长文。
4. 实践者原文：限定长期记录 AI 使用过程的作者站点。
5. 专业资讯线索：限定专业媒体和行业分析站，只用于回溯原始来源。

搜索结果进入队列后都会保存 `source_class`、`source_role`、`evidence_policy`、`source_catalog_id` 和 `search_branch`。抓取失败、原文太短、证据不足或独立复审不通过时，案例会留在队列并显示原因。

官方来源目录依据公开入口核对：

- [OpenAI News](https://openai.com/news/)
- [Anthropic Newsroom](https://www.anthropic.com/news)
- [Google AI](https://blog.google/innovation-and-ai/technology/ai/)
- [Google DeepMind Blog](https://deepmind.google/blog/)
- [Microsoft AI](https://news.microsoft.com/source/topics/ai/)
- [AI at Meta Blog](https://ai.meta.com/blog/)
- [Simon Willison's Weblog](https://simonwillison.net/)
- [Latent Space](https://www.latent.space/)

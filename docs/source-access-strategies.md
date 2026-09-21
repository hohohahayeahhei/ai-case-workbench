# 网站来源读取策略

采集器先记录原始链接，再根据网站类型选择只读读取方式。备用地址只负责取得正文，案例引用和用户点击的“查看原文”始终保留原始 URL。

| 类型 | 识别方式 | 读取顺序 | 边界 |
| --- | --- | --- | --- |
| 普通公开网页 | 默认 | 原始 HTML/text | 仍限制公网地址、正文长度和内容类型 |
| 脚本型媒体 | OpenAI、Tom's Guide、TechRadar 等域名 | 原始 HTML → 只读文本转换 | 不绕过付费墙、登录和验证码 |
| Substack / Medium / LinkedIn | 平台域名 | 原始页面 → 只读文本转换 | 只读取公开页面 |
| Reddit | reddit.com | 原始页面 → old.reddit → 公开 JSON → 只读文本转换 | 不使用账号权限，不读取私密社区 |
| PDF | `.pdf` URL 或 PDF 响应头 | 下载公开 PDF → pypdf 提取文本 | 扫描件没有 OCR 时保留失败原因 |
| RSS / Atom | 来源目录中的 feed | 读取 feed，再回到文章原文 | feed 摘要不能直接作为完整案例证据 |

2026-09-15 的真实验证中，Reddit、Substack、Medium 和 PDF 均成功取得正文；Tom's Guide 直接读取成功。每次成功读取会保存 `fetch_strategy`、`fetch_strategy_label` 和 `fetched_url`，审核页面可看到实际采用的策略。

旧失败记录会在 `source-access-v2` 下获得一次有限重试预算，平台类来源可以重新尝试其公开表示形式。私网或本地域名解析、登录墙、验证码和明确拒绝自动访问的页面继续拦截。这些限制不能通过关闭公网校验或伪造内容解决，系统会把它们标为待处理并保留原因。

# 实时案例采集

`source_mode=live` 只处理真实搜索或公开 Feed，不注入 Fixture。`online_snapshot` / `fixture` 为独立演示模式。

## 每次运行

1. 主控记录运行 ID、主题和四份 Skill 的内容哈希，限制文章预算。
2. Scout 通过 `mcp_live_server.py` 的 `search_web`，调用模型服务原生 web_search。只有实际工具执行记录和来源引用才会成为候选，正文中的任意链接不会被当作搜索结果。
3. 按规范化 URL 去重并持久化候选。保留路径大小写；只移除跟踪参数与片段。同标题聚类保留成员列表，尚不做语义跨站合并。
4. Fetch MCP 按网站类型读取公开正文：普通网页/脚本型媒体、Reddit、Substack/Medium/LinkedIn 和 PDF 使用各自的只读策略。拒绝本地或内网文章地址，验证重定向；HTML 下载上限10 MB、RSS上限2 MB，进入模型的正文上限20,000字符。PDF、付费墙、登录页或反爬拒绝不会被隐形绕过，详见 `docs/source-access-strategies.md`。
5. Extract Agent 只抽取一个已发生的场景。代码验证引文存在、字段类型及数字，失败时附具体错误让模型修正一次。仍不合格保留候选。
6. Verify Agent 独立检查语义支持和真实应用性质；自述可入选，但必须保留归因与局限。被拒绝的案例不会因为分数高而放行。
7. Score Agent 解释亮点与局限，程序计算六维总分与硬门槛，合格记录进入查询库。
8. 保存每个阶段事件和本次报告，生成腾讯文档待写入清单。

## 失败和恢复

`completed` 表示本轮处理完成，不保证产生精选；`partial` 表示有来源失败或候选待抽取；`failed` 表示无可处理结果且有服务错误。`queued` 数量表示本轮预算以外的候选。

网络错误按来源隔离，原文已抓取而模型服务失败时会复用缓存重试。每个待处理候选最多自动尝试三轮，每轮抽取最多修正一次。已评分、已入选或被核验拒绝的 URL 不反复调用模型。文件锁避免命令行和网页同时采集。页面轮询持久化运行记录；服务重启时无存活采集锁的未完成任务标为 interrupted。

仍不提供任意网页浏览器渲染、自动登录、绕过反爬或无限重试。搜索召回与提取成功率不是经过正式测评的数据。

## 接口

- `POST /api/v1/collection/jobs`：提交后台采集，202 返回 run_id。
- `GET /api/v1/collection/jobs/{run_id}`：阶段事件与结果。
- `GET /api/v1/collection/jobs/{run_id}/report`：本地证据报告。
- `GET /api/v1/cases?source_mode=live&window=all`：实时精选库。
- `GET /api/v1/collection/tencent-preview`：待写入字段预览。

精选变化接口在内容变化时返回 `reset_required=true`，调用方必须重取完整快照。当前不伪造完整增量事件流。

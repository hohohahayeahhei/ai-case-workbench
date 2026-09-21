# 抖音内容检索接入建议

调研日期：2026-09-21。本文件是接入方案，本轮没有安装抖音爬虫、添加抖音自动采集源或调用抖音登录。

## 适合当前个人工作台的做法

建议先采用公开网页搜索发现链接，配合正常浏览器查看抖音原始页面。完整的视频证据处理链完成后，再启用批量自动入库。

可用的检索词示例：`site:douyin.com/video WorkBuddy 实测`、`site:jingxuan.douyin.com AI 自动化 工作流`。这只能覆盖搜索引擎已收录的公开内容，不能当作抖音全站搜索。此次确实发现了 2026-09-07 发布的 [WorkBuddy 工作流视频页面](https://jingxuan.douyin.com/m/video/7681900070626741514)；页面标题和介绍只能作为线索，尚未观看并核验视频，因此不是已认证的成功案例。

正常浏览器检索建议：抖音站内输入“WorkBuddy 实测”“AI 自动整理表格”“DeepSeek 工作流”等任务型关键词，优先最新、作者亲历内容；保存可见作者、原始链接、发布日期与能支持结果的内容。需要登录时由用户扫码，自动流程遇到验证码或无法访问时停止该来源，记录原因。

## 三类技术路径及实际限制

1. **官方 API。** 官方有抖音视频搜索接口 `/dy_open_api/v1/search/video/`，scope 为 `aweme.dy.video_search`，支持关键词、数量、分页和发布时间筛选。但[能力说明](https://developer.open-douyin.com/docs/resource/zh-CN/dop/ability/search-management/item-search)目前注明“该能力为实验能力，现不对外开放”。有文档不代表普通开发者可直接获得权限。该能力说明还限制缓存、下载、存储等用途，不能直接把搜索 API 视为本地视频知识库的内容授权，需要先确认应用场景和权限范围。[接口文档](https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/douyin-search-capability/aweme-dy-video-search)
2. **本人或合作作者账号授权。** `video.list` 可以获取授权账号的视频，需申请权限并获得账号授权；适合跟踪自己的账号或合作作者，不是任意全站搜索。[官方视频列表文档](https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/search-video/account-video-list)
3. **社区采集工具。** [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)声称支持抖音关键词、指定作品和作者主页，基于浏览器登录态。可学习其来源适配和去重思路，但这不是抖音官方接口，尚未在本机验证；其[许可证](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)限非商业学习用途，不能直接作为日后商用产品的默认依赖。

## 接入工作台需要补齐的能力

建议流程：剩余处理名额 → 搜索链接 → 去重 → 核实作者和发布日期 → 获取可用字幕/作者文字稿 → 必要时对有权处理的视频转写和画面文字识别 → 抽取问题/AI 方法/实际结果 → 对照视频时间点核验 → 原有评分和复审 → 本地展示。

新增字段建议：平台、作品 ID、作者、原始链接、原文发布日期及依据、视频时间点、证据类型（作者说明/字幕/语音转写/画面识别）、证据文本和获取状态。自动转写不是天然正确的原文，需要在重要数字、专有名词和效果结论处回看视频核对。

近期筛选仍沿用最近一年；点赞数只表示热度，不能直接换成质量分。案例必须说明 AI 参与了什么工作，不能把普通代码修复、产品介绍或推广标题当成 AI 成功案例。

若以后封装 MCP，可设计 `search_douyin(query, limit)` 与 `read_douyin_evidence(video_url)` 两类工具，由采集主流程传入剩余名额。MCP 只是工具调用方式，不能自动解决登录、搜索权限或视频转写的问题。

用户可能需要的操作：正常浏览器扫码登录；如果走官方授权方式，需要提供已获批应用和账号授权。来源适配、额度限制、字段映射、去重、证据展示与测试可以由开发完成。

# 微信 / 企业微信每日案例通知接入研究

核实日期：2026-09-21。本文保留首次研究时的方案与现状。后续已实现企微通知、独立投递记录、后台部署与重试入口；当前使用方式以 `README.md` 和 `docs/deployment.md` 为准，以下“建议”“尚未”等描述属于实施前研究记录。

## 推荐

第一版优先采用 **企业微信内部群「消息推送」（原群机器人）Webhook**。每天采集完成后发送一条简报，展示最多 3–5 个新增精选案例的简短中文描述和原文链接。它适合直接在聊天里阅读完整简报，也不需要为单向通知公开本地工作台或搭建回调服务器。

如果只愿使用普通微信，且接受点开通知查看正文，选择 **Server酱 Turbo 的微信服务号通道**。如果已有企业微信管理员权限，又要求普通微信直接接收简介，可考虑 **自建应用 + 微信插件**；配置比群 Webhook 多，列为第二阶段。

## 渠道比较

| 方式 | 接收位置与展示 | 准备条件 | 对本项目的判断 |
| --- | --- | --- | --- |
| 企业微信消息推送 Webhook | 企业微信内部群；文字/Markdown 可直接含多条简介和链接 | 内部群、创建消息推送的权限、Webhook 地址 | 首选，直接连接官方接口 |
| 企业微信自建应用 + 微信插件 | 应用消息；满足插件条件后可在普通微信接收 | 企业管理员配置应用、接收成员及微信插件；应用凭证和接口访问条件 | 适合已有企微环境、希望定向发给自己的情况 |
| Server酱 Turbo 微信服务号 | 普通微信；服务号卡片仅显示标题，正文点开看 | 扫码开通、绑定通道、SendKey | 最省事的个人微信备选；免费每天 5 条足够一条日报 |
| PushPlus 微信服务号 | 普通微信；普通实名用户仅标题，会员模板可展示更多正文 | 发信账号实名认证；实名有认证费，可通过会员权益完成 | 可用但不是首选；会员当前 10 元/月，完整全文直接显示仍受激活条件影响 |

企业微信官方已将「群机器人」更名为「消息推送」。目前支持内部群，外部群不支持，创建者须在管理员允许范围内。[官方设置说明](https://open.work.weixin.qq.com/help2/pc/14931)

Webhook 使用 HTTPS POST；文本上限 2048 字节、Markdown 上限 4096 字节，每个推送每分钟最多 20 条。第一版采用普通 Markdown，按 UTF-8 字节限制正文长度，默认不依赖 @ 提醒。[官方接口说明](https://developer.work.weixin.qq.com/document/path/91770)

微信插件可以接收企业自建应用消息，但需开启接收消息、应用未停用，并满足管理员及客户端的接收设置。**不能把企业微信群 Webhook 等同于普通微信群接口。** 若选自建应用同步微信，优先用 text：微工作台不支持 Markdown；开启“在微工作台中始终进入主页”还会造成微信端文本 20 字节截断。[插件接收条件](https://open.work.weixin.qq.com/help2/pc/18121)、[应用消息格式和限制](https://developer.work.weixin.qq.com/document/path/90236)

Server酱 Turbo 官方列明服务号通道的正文展示例外；不能因为购买会员就承诺普通服务号卡片展示整篇简报。Server酱³ 是独立 App，不能当作微信接收渠道。[官方通道与额度](https://sct.ftqq.com/docs/getting-started/channels/)

PushPlus 发送方须实名，会员模板显示更多信息。[实名说明](https://pushplus.plus/doc/function/verify.html)、[会员价格与功能](https://www.pushplus.plus/doc/function/vip.html)。其“激活消息”正文显示窗口在两份官方说明中分别写 24 小时和 48 小时，均有限量，失效后回到模板消息；不能依靠持续人工激活来满足无人值守日报。[正文显示说明](https://www.pushplus.plus/doc/help/showmessage.html)、[激活说明](https://www.pushplus.plus/doc/help/activation.html)。新微信 ClawBot 通道也要求周期性主动对话，不作为本项目首选。[官方说明](https://pushplus.plus/doc/channel/clawbot.html)

以上展示指聊天内消息；锁屏横幅显示多少文字取决于设备与通知设置。本次未进行手机收件实测，也未核实用户账号是否具备各平台开通条件。

## 项目现状与接入点

当前链路：每日调度 → `scripts/daily_collection.sh` → `scripts/run_collection.py` → CollectionPipeline → SQLite / 报告导出 → SQLite 备份。

- 每日入口：[daily_collection.sh](/Users/honey/Desktop/ai案例工作台/scripts/daily_collection.sh:5)。
- 推荐接入点：[run_collection.py](/Users/honey/Desktop/ai案例工作台/scripts/run_collection.py:36) 完成采集、导出、保存 run 后，关闭数据库前，持久化并尝试投递简报。
- 建议新增 `--notify`，只由每日入口显式开启，避免网页刷新、离线自检、手动重试触发重复通知。
- 案例已经包含中文 `problem.claim`、`approach.claim`、`outcome.claim` 和 `source_url`，可以直接组合摘要，第一版无需追加模型调用。
- 本轮案例来自 `result.items[].id`，再通过 `db.source_item(id)` 读取完整记录。`source_items` 没有 `run_id` 列。
- 复用现有精选门槛：真实网页/RSS 来源、核验通过、日期符合当前政策、评分达标。[现有导出门槛](/Users/honey/Desktop/ai案例工作台/backend/app/collection_export.py:18)
- `live_records()` / `tencent_rows()` 返回全库精选，不能直接作为本轮日报，否则会重复推送历史案例。
- `new_candidates` 是新线索数量，不是新增精选数量。消息中的数字由实际筛选列表计算。
- `partial` 运行也可能产生合格案例，应照常推送已入选内容，并简述还有待处理项。

建议消息规则：每天完成后合并发一条；最多 3–5 条精选，每条约 50–80 个汉字。数量少时照实展示。没有新增精选则只发一句状态。采集失败时标为失败，不能写“今日已完成”。日报日期采用 Asia/Shanghai，原文发布日期与收录日期分开。

项目默认网址 `127.0.0.1:5173` 无法在手机上访问。第一版使用案例原文链接；手机查看完整工作台属于另一个部署需求。

## 当前调度问题：实施前必须处理

已读取安装文件，并用 `launchctl print gui/501/com.ai-case-workbench-daily` 确认：每日任务已加载，时间为 08:30；已触发 7 次，最后退出码 126。`state = not running` 本身对周期任务正常，异常证据是退出码和日志。

[daily.err.log](/Users/honey/Desktop/ai案例工作台/data/runtime/logs/daily.err.log) 最近修改时间为 2026-09-21 08:30:03 CST，重复出现：

```text
getcwd: cannot access parent directories: Operation not permitted
/bin/bash: /Users/honey/Desktop/ai案例工作台/scripts/daily_collection.sh: Operation not permitted
```

`daily.out.log` 为空。这说明后台任务在读取项目阶段就失败，不能因数据库里有其他入口产生的新案例，就认为 08:30 自动采集正常。

目录 0755、脚本可读 0644，且通过 `/bin/bash 脚本` 启动；单纯给脚本添加执行位不能解释或解决这个问题。结合 Desktop 路径，推断很可能是 macOS 桌面目录隐私访问限制；尚未直接验证系统隐私授权状态。

实施时先解决后台进程访问运行目录的问题，再验证同一 launchd 路径能采集和通知。可评估把运行副本与数据放到适合后台服务的目录，或核实系统授权；本次未移动项目、修改权限或更改调度。原有 `docs/deployment.md` 的“尚未安装”描述也需随实施更新。

本机必须在任务执行时可运行并联网；若要求电脑关机时仍准点通知，需要另行部署到常开设备或服务器。

## 建议的最小实现

1. 新增 `backend/app/notifications.py`：筛选本轮精选、生成固定模板、按 UTF-8 长度裁剪、调用企微 Webhook、检查业务返回码。
2. 新增独立通知 outbox：保存 run、渠道、目标标识、冻结正文、案例 ID、发送状态、尝试次数、错误摘要与发送时间。目标标识不保存完整密钥。
3. 为 CLI 加 `--notify`，每日脚本开启；本地 `.env` 增加通知开关及 `AI_CASE_WECOM_WEBHOOK_URL`，默认关闭。
4. 提供 `--dry-run` 本地预览和单独重发入口；失败保留待发送记录，按有限重试策略处理，不重新采集。

不要复用旧 `delivery_tasks`：它属于 LangGraph 演示链，关联 `content_versions → runs`；真实采集写入的是 `collection_runs`。

用 `(run_id, channel, target_id)` 防止同轮重复提交；成功发送的案例还可按 `(case_id, channel, target_id)` 去重。网络超时可能出现平台已收、客户端未拿到回执，不能承诺严格“恰好一次”。发送失败不改变采集结果，也不能导致 shell 提前退出而跳过备份。平台业务成功不等于用户已阅读。

Webhook 地址作为密钥仅留在本地配置，不进入前端、日志、导出或报告。只推送公开案例摘要与原文链接，无需发送原文全文、模型密钥或内部日志。

实施后的有效验证：本轮筛选、跨轮去重、partial 有精选、零新增、中文超长、业务错误、超时、备份不受通知影响；离线测试通过后，再在指定接收群验证一条实际消息和下一次调度。

## 实际通知文案示例

以下根据本地已入选记录压缩，仅演示样式，不表示同一轮采集结果，也未实际发送。

> AI 案例简报 · 09/21
>
> **用 Claude 从个人 X 信息流找选题**
> 作者让 Claude 浏览已登录的信息流，结合互动数据筛出 4 条帖子，并为每条提供写作角度。
> [查看原文](https://prosperinai.substack.com/p/claude-computer-use-workflow)
>
> **用 Gemini 整理旅行书**
> 作者将旅行笔记、照片按章节交给自定义 Gem，逐章生成并人工修改，最终完成 8 章草稿。
> [查看原文](https://www.linkedin.com/pulse/ai-use-case-writing-my-little-taiwan-travel-book-45-kristi-rohtsalu-oseqc)
>
> 以上效果为作者自述；原文支持情况已由工作台核验。

## 开通路径

企微首选方案：进入目标企业的内部群 → 右上角“…” → 消息推送 → 添加 → 新建“AI 案例日报” → 获取 Webhook 地址，存入项目本地配置。旧版本菜单可能仍叫“群机器人”。[官方设置入口](https://open.work.weixin.qq.com/help2/pc/14931)

个人微信备选：在 Server酱 Turbo 扫码开通并绑定微信服务号通道，取得 SendKey，通知仍由同一简报模块生成，只替换发送适配器。[官方获取 SendKey 指引](https://sct.ftqq.com/docs/getting-started/sendkey/)

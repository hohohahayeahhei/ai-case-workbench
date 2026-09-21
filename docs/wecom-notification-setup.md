# 企业微信通知配置记录

配置日期：2026-09-21。

## 个人智能机器人接入（当前方案）

用户希望改用企业微信工作台的「智能机器人 → API 模式 → 使用长连接 → 仅个人使用」。已通过企业微信客户端查看[官方长连接文档](https://developer.work.weixin.qq.com/document/path/101463)：`aibot_send_msg` 支持无消息回调触发的主动推送，首次需用户给机器人发过消息；主动推送章节未列出回复消息的 24 小时时窗。单聊使用该消息回调中的用户标识。每个机器人同时只允许一个有效长连接。

本地代码已支持 `wecom_aibot` 通道，与群 Webhook 共用摘要筛选、发件记录、去重和重试。每天需要发送时建立短暂 WebSocket 会话，认证后发送 Markdown 并确认 ACK；本机发送和绑定共用文件锁，不需要新增常驻监听服务或公网回调地址。该接入只使用机器人私聊，无需授权邮件、文档、通讯录等扩展应用能力。

当前状态：用户已确认创建并在本机保存凭据。机器人「AI案例日报」已保存为 API / 长连接 / 仅个人使用；通过精确的一次性私聊口令完成本人绑定。桌面与后台配置均启用 `wecom_aibot`，旧 Webhook 不再使用。2026-09-21 12:10（上海时区）对已保存运行 `collect-4f30e6fe7a06` 发送了标记「接入测试」的两条真实案例简报：平台 ACK 成功、发件记录 `sent`、尝试 1 次，并在企业微信私聊中确认正文与两个原文链接均已显示。

桌面项目与后台副本的私密 `.env` 设置如下（真实值不写入文档）：

```dotenv
AI_CASE_NOTIFICATIONS_ENABLED=true
AI_CASE_NOTIFICATION_PROVIDER=wecom_aibot
AI_CASE_WECOM_BOT_ID=<机器人ID>
AI_CASE_WECOM_BOT_SECRET=<机器人密钥>
```

配置文件应为 `0600`。运行绑定命令，看到 `listening` 后，在该机器人私聊中发送同一条临时口令：

```bash
.venv/bin/python scripts/bind_wecom_aibot.py --listen --phrase '绑定日报 <一次性随机码>' --timeout 120
```

绑定脚本只接受精确口令、单聊、文本消息；仅将机器人与收件人标识写入运行目录的 `wecom-aibot-binding.json`（`0600`），不保存聊天正文或密钥。随后用本文末尾的已有案例预览与测试命令验证，无需再次采集。收到平台业务码 0 后，仍应在企业微信私聊中核对实际简报。

新增通道及原通知集成的 32 项隔离测试通过，关闭通知的配置检查与缺失机器人凭据的失败检查通过；测试运行在临时数据目录，没有写入真实案例库。代码已同步后台副本，原文件保存在 `data/backups/before-smartbot-code-20260921T040324Z`；切换前的私密配置另行备份。真实连接发现本机使用 SOCKS 代理，已在桌面及后台虚拟环境补齐 `python-socks 2.8.2`，依赖声明已同步。无需新增常驻机器人服务或重启工作台。

## 已完成的每日任务与群 Webhook 接入

- 早期 Webhook 曾写入本地 `.env`，现已切换个人智能机器人；密钥不写入文档或日志。
- 每天 08:30 开始采集，采集结束后按本轮新增精选生成一条简报，最多 5 个案例，每个含方法、结果与原文链接。
- 独立 SQLite 发件记录提供同轮及跨轮去重。平台明确拒绝后最多尝试 3 次；结果未知时保留状态，避免盲目重发。健康检查每 5 分钟执行到期重试。
- 后台部署至 `/Users/honey/Library/Application Support/ai-case-workbench`；桌面项目 `data/runtime` 链接到同一运行数据目录。原运行目录和配置已保留备份。
- API 与前端健康检查通过。每日脚本通过真实 launchd 一次性验证：配置检查及 SQLite 备份退出码 0，原先 Desktop 访问错误已消除；验证未启动额外采集。
- 隔离自检通过：174 项 Python 回归、MCP 协议、离线采集与前端构建。通知模块含 19 项测试，CLI 集成含 6 项测试。

## 旧群 Webhook 的实测结果

首次真实请求返回 `platform_error_93000`，消息未被平台接受。用户随后提供第二个地址，已同步替换桌面项目与后台副本的配置；新地址发送案例 Markdown 简报及最简 text 消息均失败。纯文本请求原始响应为 HTTP 200、`errcode: 93000`、`errmsg: invalid webhook url`，已排除简报格式导致此次失败。腾讯官方文档说明该错误表示 Webhook URL 不合法，或对应机器人已被移出群。[官方故障说明](https://docs.cloudbase.net/recipes/connect-wecom-webhook-cloud-function)

如果以后选择群消息推送，需要从目标企业微信内部群「… → 消息推送 → 对应推送 → Webhook 地址」重新复制有效地址。若推送已被删除，重新添加后获取新地址。新地址应同时更新桌面项目和后台副本 `.env`，并设置 `AI_CASE_NOTIFICATION_PROVIDER=wecom_webhook`，然后发送已保存的案例简报验收，无需再次采集。

```bash
# 预览最近已完成的实时采集
.venv/bin/python scripts/notify_collection.py --latest --test
# 用当前配置发送测试简报；同一运行/目标已有成功记录时不重复发送
.venv/bin/python scripts/notify_collection.py --latest --test --send
```

Webhook 更换后目标标识会改变，旧地址的失败记录不会影响新地址首次发送。HTTP 200 本身不代表成功，只有企业微信业务码 0 才记为 sent；sent 表示平台接受，不代表用户已阅读。

完整维护方式见 [部署说明](/Users/honey/Desktop/ai案例工作台/docs/deployment.md)。本机需要在执行时保持可运行且联网，通知时间取决于实际采集完成时间。

# 查询 Agent

当前项目新增了一个 AIHOT 风格的 `QueryAgent`。它把已核验案例包装成稳定的 v1 查询契约，供网站前端或其他 Agent 调用。

## 当前能力

- 按关键词查询案例
- 按 24 小时、7 天、30 天或全部时间筛选
- 按来源类型和分类文本筛选
- 按相关度或发布时间排序
- 通过游标分页
- 返回来源链接、证据摘要、限制条件和只读元数据
- 返回热门案例、日报和快照同步结果
- 将完整精选快照的翻页游标与同步水位分开

## API

```text
GET /api/v1/cases
GET /api/v1/hot
GET /api/v1/digests/latest
GET /api/v1/snapshot
GET /api/v1/selected/snapshot
GET /api/v1/selected/changes
GET /api/v1/health
```

示例：

```bash
curl 'http://127.0.0.1:8000/api/v1/cases?query=客服&window=all&limit=10'
curl 'http://127.0.0.1:8000/api/v1/hot?limit=5'
```

## 当前边界

查询层默认读取已核验线上快照；互联网发现入口已经提供 RSS/Atom 候选预览，但候选仍需经过文章抓取、抽取、验证和精选后才会进入正式案例库。

import EvidenceQuote from './EvidenceQuote'
import React from 'react'

export default function ScoreBreakdown({scorecard, caseId}) {
  if (!scorecard) return null
  const pending = scorecard.quality_score == null
  const rejected = scorecard.tier === "rejected"
  const labels = scorecard.dimension_labels || {}
  const max = scorecard.dimension_max || {}
  return <article className="score-breakdown">
    <div><div className="section-kicker">证据评分</div><h3>{pending ? (rejected ? '为什么未入选？' : '为什么还没有评分？') : '这个分数是怎么来的？'}</h3><p>{scorecard.reason}</p></div>
    {pending ? <p className="quality-pending">{rejected ? "未通过成功案例门槛，已保留原文与原因。" : "评分或独立复审尚未通过，保留原文等待处理。"}{scorecard.pending_reasons?.join('；')}</p> : <div className="score-bars">{Object.entries(scorecard.dimensions || {}).map(([key, value]) => {
      const judgment = scorecard.judgments?.[key]
      return <details className="score-dimension" key={key}><summary><span>{labels[key] || key}</span><b>{value}/{max[key]}</b></summary>
        <div className="score-track"><i style={{width:`${value / max[key] * 100}%`}} /></div>
        <p>{judgment?.reason}</p>
        {judgment?.evidence?.map((quote, i) => <EvidenceQuote key={i} caseId={caseId} quote={quote} explanation={judgment?.reason} />)}
        {judgment?.missing?.length > 0 && <p className="quality-gap"><strong>还缺什么：</strong>{judgment.missing.join('；')}</p>}
        <small>独立复审：{scorecard.review?.dimension_checks?.[key]?.reason || '未完成'}</small>
      </details>
    })}</div>}
    {!pending && <p className="quality-review">✓ 已通过独立复审 · {scorecard.attempts > 1 ? '经一次修正后通过' : '首轮通过'} · 事实、归因、表达、读者价值四层检查</p>}
    <details className="quality-policy"><summary>查看评分标准与借鉴来源</summary><p>来源可追溯性 15、实际问题 15、方法完整度 25、结果证据 25、可复用性 15、学习价值 5。每项按原文评 0—4 档，程序加权得到总分；70 起入选，85 起重点推荐，问题、方法、结果均至少 2 档。</p><p>缺证据、数字不受支持或复审失败时不入选。单篇来源不会被当作独立佐证；没有数字也可以有具体成果，出现百分数不自动加分。</p><p>借鉴 <a href="https://aihot.news/changelog" target="_blank" rel="noreferrer">AIHOT 公开的内容价值与一手来源原则</a>，以及 Khazix Skills 的分层质检和独立验收。以上权重为本项目制定，未找到 AIHOT 公开完整公式。</p></details>
    <small className="score-footnote">模型按原文判断档位，独立调用复审，程序计算总分并执行证据门槛。分数用于辅助筛选，尚未人工校准；不代表热度或效果已被独立复现。</small>
  </article>
}

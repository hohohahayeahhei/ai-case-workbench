import React from 'react'

export default function CollectionResultNote({job}) {
  if (!job) return null
  const active = ['running', 'queued'].includes(job.status)
  if (active && job.discovery_budget == null) return null
  const counts = job.counts || {}
  const pending = (counts.needs_extraction || 0) + (counts.needs_scoring || 0) + (counts.needs_date || 0)
  return <div className="collection-result-note" role="status">
    <strong>{active ? '本轮处理计划' : '本轮结果说明'}</strong>
    {job.new_candidates != null && <p>新增 {job.new_candidates} 条线索 · 带入历史待处理 {job.carried_over || 0} 条 · 本轮最多处理 {job.processing_budget || 12} 条</p>}
    {job.discovery_policy === 'capacity_matched' && <p>历史可重试 {job.ready_at_start || 0} 条，本轮接手 {job.carried_over || 0} 条；最多补搜 {job.discovery_budget} 条，搜索与 RSS 共用额度。{job.discovery_budget === 0 ? '本轮暂停新增发现，先处理已有案例。' : '重复或空结果不额外扩搜。'}</p>}
    {job.discovery_policy === 'backlog_first' && <p>已启用积压优先：本轮开始时有 {job.ready_at_start} 条可处理记录，减少新搜索线索并暂缓自动订阅发现，优先完成已有案例。</p>}
    {job.stop_reason === 'model_service_unavailable' && <p>模型服务连续失败，本轮已暂停后续处理。其余案例保留在队列，服务恢复后可继续。</p>}
    {job.stop_reason === 'processing_budget_reached' && <p>已达到本轮处理预算；其余记录尚未开始审核，可用“重试待处理”继续消费队列。</p>}
    {!active && <p>{job.diagnostics?.summary || `本轮处理 ${Object.values(counts).reduce((a, b) => a + b, 0)} 条：${(counts.selected || 0) + (counts.featured || 0)} 条入选，${pending} 条待抽取或评审，${counts.rejected || 0} 条内容未通过，${counts.candidate || 0} 条已核验但未达精选标准，${counts.outdated || 0} 条超出最近一年。`}</p>}
    {job.queued > 0 && <p>{active ? '当前有' : '另有'} {job.queued} 条等待处理，不计作审核失败。重试会优先处理已抓取的待复审案例，并轮流处理不同来源；日期待核实内容需要重新读取原文日期。</p>}
  </div>
}

import React from 'react'

const sources = { 'feed:published':'来源 RSS / Atom', 'legacy_feed:published':'历史 RSS 记录', 'publication_label':'原文发布标记' }
function basis(value) {
  if (sources[value]) return sources[value]
  if (value?.startsWith('html_meta:')) return '原文发布元数据'
  if (value?.startsWith('json_ld:')) return '原文结构化数据'
  if (value?.startsWith('html_time:')) return '原文时间标记'
  return value ? '来源记录' : '日期依据待补充'
}

export default function CaseDates({item, detail = false}) {
  return <div className="case-dates">
    <span title={`日期依据：${basis(item.published_at_source)}；${item.published_at_evidence || ''}`}>原文发布日期：<strong>{item.published_at || '待确认'}</strong></span>
    <span>收录：{(item.collected_at || item.discovered_at)?.slice(0,10) || '未记录'}</span>
    {item.date_status === 'outdated' && <span className="date-warning">超出最近一年</span>}
    {item.date_status === 'future' && <span className="date-warning">日期晚于今天，待核实</span>}
    {detail && <small>日期依据：{basis(item.published_at_source)}。发布日期用于判断新旧，不代表案例实际发生时间；{item.updated_at ? `页面更新于 ${item.updated_at}，` : ''}更新时间和收录时间不替代发布日期。</small>}
  </div>
}

import EvidenceQuote from './EvidenceQuote'
import CaseDates from './CaseDates'
import ScoreBreakdown from './ScoreBreakdown'
import CollectionResultNote from './CollectionResultNote'
import React, {useEffect, useState} from 'react'

const API = import.meta.env.VITE_API_BASE_URL || '/api'
const labels = {queued:'排队中', running:'运行中', completed:'完成', partial:'部分完成', failed:'失败', interrupted:'已中断', selected:'入选', featured:'精选', candidate:'候选', needs_scoring:'待评分或复审', rejected:'未通过', needs_extraction:'待抽取或重试', needs_date:'日期待核实', outdated:'超出一年'}
const sourceRoleLabel = value => value === 'primary' ? '一手证据' : value === 'discovery' ? '发现线索' : '需回溯原文'
const statusTabs = [{key:'all', label:'全部'}, {key:'selected', label:'已入选'}, {key:'candidate', label:'候选'}, {key:'needs_scoring', label:'待评分'}, {key:'needs_extraction', label:'待抽取'}, {key:'needs_date', label:'日期待核实'}, {key:'outdated', label:'超出一年'}, {key:'rejected', label:'未通过'}]
function normalizedStatus(item) {
  if (item.status === 'featured' || item.status === 'selected') return 'selected'
  return item.status || 'unknown'
}

async function read(path, options) {
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) throw new Error(`请求失败（${response.status}），请检查后端服务`)
  return response.json()
}

export default function DiscoveryQueue() {
  const [items, setItems] = useState([])
  const [query, setQuery] = useState('人们亲自使用 AI 工具解决工作和生活问题的真实经验，包含具体步骤和实际结果')
  const [job, setJob] = useState(null)
  const [history, setHistory] = useState([])
  const [statusTab, setStatusTab] = useState('all')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const runningJob = history.find(run => run.source_mode === 'live' && ['queued', 'running'].includes(run.status))
  const active = submitting || Boolean(runningJob) || ['queued', 'running'].includes(job?.status)

  async function refresh() {
    const [queue, runs] = await Promise.all([read('/v1/collection/items?limit=500'), read('/v1/collection/jobs')])
    setItems(queue.items.filter(item => ['web_search', 'rss'].includes(item.discovery_mode)))
    setHistory(runs.items)
    setJob(current => {
      const running = runs.items.find(run => run.source_mode === 'live' && ['queued', 'running'].includes(run.status))
      return running || runs.items.find(run => run.run_id === current?.run_id) || current || runs.items.find(run => run.source_mode === 'live') || null
    })
    return runs.items
  }
  useEffect(() => { refresh().catch(e => setError(e.message)) }, [])
  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return
    let canceled = false
    let timer
    async function poll() {
      try {
        const next = await read(`/v1/collection/jobs/${job.run_id}`)
        if (canceled) return
        setJob(next)
        setError('')
        await refresh()
        if (['queued', 'running'].includes(next.status)) timer = setTimeout(poll, 3000)
      } catch(e) {
        if (!canceled) { setError(`${e.message}；已提交的任务可在连接恢复后继续查看`); timer = setTimeout(poll, 5000) }
      }
    }
    timer = setTimeout(poll, 1000)
    return () => { canceled = true; clearTimeout(timer) }
  }, [job?.run_id, job?.status])
  useEffect(() => {
    if (active) return
    const timer = setInterval(() => refresh().catch(() => {}), 5000)
    return () => clearInterval(timer)
  }, [active])

  async function collect(retry = false) {
    setSubmitting(true); setError('')
    try {
      setJob(await read('/v1/collection/jobs', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({source_mode:'live', query, max_results:12, search_web:!retry, retry_pending:true, include_catalog:true, force:true})}))
    } catch(e) { setError(e.message) }
    finally { setSubmitting(false) }
  }
  const counts = items.reduce((result, item) => {
    const key = normalizedStatus(item)
    result[key] = (result[key] || 0) + 1
    result.all = (result.all || 0) + 1
    return result
  }, {})
  const visibleItems = statusTab === 'all' ? items : items.filter(item => normalizedStatus(item) === statusTab)
  return <section className="panel wide">
    <div className="panel-title"><div><h3>从网上发现真实 AI 用法</h3><span className="muted">搜索原帖 → 抽取证据 → 核验 → 评分 → 独立复审 → 入库</span></div></div>
    <form className="explore-search" onSubmit={e => {e.preventDefault(); collect()}}>
      <input aria-label="搜索主题" value={query} onChange={e => setQuery(e.target.value)} maxLength={500}/>
      <button className="primary" disabled={active || !query.trim()}>{active ? '采集中…' : '联网找案例'}</button>
      <button type="button" className="text-button" disabled={active} onClick={() => collect(true)}>重试待处理</button>
    </form>
    <p className="muted">只采集最近一年发布的案例，每次最多处理 12 篇。原文发布日期与收录时间分开显示；超期或日期待核实的历史内容保留在对应分类。</p>
    {error && <p role="alert" className="error">{error}</p>}
    {job && <div className="collection-summary">
      <strong>{labels[job.status] || job.status}</strong> · 新增 {job.new_candidates ?? job.discovered ?? 0} 条 · 入选 {(job.counts?.selected || 0) + (job.counts?.featured || 0)} 条 · 跳过 {job.skipped || 0} 条
      <p className="muted">{job.events?.at(-1)?.agent} · {job.events?.at(-1)?.action} {job.queued ? ` · 另有 ${job.queued} 条等待处理` : ''}</p>
      <CollectionResultNote job={job} />
      {job.error && <p>{job.error}</p>}
      {job.feed_errors?.map((e, i) => <p className="error" key={i}>{e.url}：{e.error}</p>)}
      {!active && <a href={`${API}/v1/collection/jobs/${job.run_id}/report`} target="_blank" rel="noreferrer">查看本次证据报告 ↗</a>}
      <details><summary>查看 Agent 执行记录</summary>{job.events?.map((e, i) => <p className="muted" key={i}>{new Date(e.at).toLocaleTimeString()} · {e.agent} · {e.action} · {labels[e.status] || e.status}</p>)}</details>
    </div>}
    <div className="queue-status-tabs" role="tablist" aria-label="案例状态筛选">{statusTabs.map(tab => <button key={tab.key} role="tab" aria-selected={statusTab === tab.key} className={statusTab === tab.key ? 'queue-status-tab active' : 'queue-status-tab'} onClick={() => setStatusTab(tab.key)}>{tab.label}<b>{counts[tab.key] || 0}</b></button>)}</div>
    {items.length === 0 && <p className="muted">尚无实时采集案例，输入主题开始搜索。</p>}
    {items.length > 0 && visibleItems.length === 0 && <p className="muted queue-empty">这个状态下暂时没有案例。</p>}
    {visibleItems.map(item => <article className="queue-row" key={item.source_item_id}>
      <div><span className="case-id">{labels[item.status] || item.status} · {item.source_name || item.source_type || '公开网页'} · {sourceRoleLabel(item.source_role)}{item.fetch_strategy_label ? ` · ${item.fetch_strategy_label}` : ''}</span>
        <h3>{item.title_original}</h3><CaseDates item={item} detail /><a href={item.source_url} target="_blank" rel="noreferrer">查看原文 ↗</a>
        <p><strong>{item.diagnostic?.label || '处理说明'}：</strong>{item.diagnostic?.reason || item.verification?.reason || item.extraction?.reason || '已发现，等待抓取原文'}</p>
        {item.retry_state === 'cooldown' && <p className="muted">下次可重试：{new Date(item.next_retry_at).toLocaleString('zh-CN')}</p>}
        {item.retry_state === 'exhausted' && <p className="muted">已达三次尝试上限，暂不重复请求此来源。</p>}
        {item.problem && <details><summary>问题、方法、结果和原文依据</summary>{[['problem','问题'],['approach','方法'],['outcome','结果']].map(([key,label]) => <div key={key}><p><strong>{label}：</strong>{item[key]?.claim}</p><EvidenceQuote caseId={item.case_id || item.source_item_id} quote={item[key]?.evidence} explanation={item[key]?.claim} /></div>)}<p>局限：{item.limitations?.join('；') || '未标注'}</p></details>}
        {item.scorecard && <details className="queue-quality"><summary>评分依据、复审与待补信息</summary><ScoreBreakdown caseId={item.case_id || item.source_item_id} scorecard={item.scorecard}/></details>}
      </div><div className="queue-score">{item.scorecard?.quality_score != null ? <><strong>{item.scorecard.quality_score}</strong><small>质量分 / 100</small></> : <small>{labels[item.status]}</small>}</div>
    </article>)}
    <details><summary>历史采集（{history.length}）</summary>{history.map(run => <button className="history-row" key={run.run_id} onClick={() => setJob(run)} disabled={active}><span>{run.run_id} · {run.source_mode}</span><span>{labels[run.status]}</span></button>)}</details>
  </section>
}

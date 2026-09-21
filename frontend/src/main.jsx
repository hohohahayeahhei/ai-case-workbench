import EvidenceQuote from './EvidenceQuote'
import CaseDates from './CaseDates'
import ScoreBreakdown from './ScoreBreakdown'
import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ArrowLeft, ArrowUpRight, Bot, CalendarDays, CheckCircle2, ChevronRight, CircleAlert, ExternalLink, Flame, History as HistoryIcon, LayoutList, Search, ShieldCheck, SlidersHorizontal, Sparkles, Star, Zap } from 'lucide-react'
import './discovery.css'
import './evaluation.css'
import './styles.css'
import DiscoveryQueue from './DiscoveryQueue'
import CollectionResultNote from './CollectionResultNote'
import DouyinPanel from './DouyinPanel'

const API = import.meta.env.VITE_API_BASE_URL || '/api'

function Status({ value }) {
  const labels = { running: '运行中', queued: '排队中', partial: '部分完成', interrupted: '已中断', awaiting_review: '待审核', publishing: '发布中', blocked: '已阻断', completed: '已完成', failed: '运行失败', needs_evidence: '待补证', needs_attention: '待处理', pending: '待执行', synced: '已同步', sent: '已发送', unknown: '结果未知', rejected: '已淘汰', verified: '已通过', selected: '已入选', featured: '精选', candidate: '候选', needs_scoring: '待评分', needs_extraction: '待抽取', needs_date: '日期待核实', outdated: '超出一年' }
  return <span className={`status status-${value}`}>{labels[value] || value}</span>
}

function sourceRoleLabel(source) {
  if (source?.source_role === 'primary') return '一手证据'
  if (source?.source_role === 'discovery') return '发现线索'
  return '需回溯原文'
}

function SourceBadge({source}) {
  if (!source) return null
  return <span className={`source-badge ${source.source_role === 'primary' ? 'primary' : 'discovery'}`} title={source.why || source.evidence_policy}>
    {source.source_name || source.name || '公开网页'} · {sourceRoleLabel(source)}
  </span>
}

function App() {
  const [run, setRun] = useState(null)
  const [topic, setTopic] = useState('AI 产品案例')
  const [scenario, setScenario] = useState('happy_path')
  const [connectorMode, setConnectorMode] = useState('mock')
  const [llmMode, setLlmMode] = useState('mock')
  const [datasetMode, setDatasetMode] = useState('fixture')
  const [knowledgeMode, setKnowledgeMode] = useState('local_sqlite')
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState('explore')
  const [selectedCase, setSelectedCase] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch(`${API}/config`).then(response => response.ok ? response.json() : null).then(config => {
      if (config?.llm_mode === 'openai_compatible' && config.llm_configured) setLlmMode('openai_compatible')
    }).catch(() => {})
  }, [])

  useEffect(() => {
    const savedRunId = window.localStorage.getItem('ai-case-workbench.currentRun')
    if (savedRunId) openRun(savedRunId)
  }, [])

  useEffect(() => {
    if (run?.run?.run_id) window.localStorage.setItem('ai-case-workbench.currentRun', run.run.run_id)
  }, [run?.run?.run_id])

  async function start() {
    setLoading(true); setError('')
    try {
      const response = await fetch(`${API}/runs`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({topic, scenario, connector_mode: connectorMode, llm_mode: llmMode, dataset_mode: datasetMode, knowledge_mode: knowledgeMode}) })
      if (!response.ok) throw new Error(await response.text())
      const payload = await response.json(); setRun(payload); setTab('overview')
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }

  async function review(decision) {
    if (!run) return
    setLoading(true); setError('')
    try {
      const response = await fetch(`${API}/runs/${run.run.run_id}/review`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision}) })
      if (!response.ok) throw new Error(await response.text())
      setRun(await response.json())
    } catch (e) { setError(e.message) } finally { setLoading(false) }
  }

  async function exportReport() {
    if (!run) return
    const response = await fetch(`${API}/runs/${run.run.run_id}/export`)
    if (!response.ok) { setError('导出演示报告失败'); return }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = `${run.run.run_id}-report.md`; link.click()
    URL.revokeObjectURL(url)
  }

  async function loadHistory() {
    const response = await fetch(`${API}/runs`)
    if (response.ok) setHistory((await response.json()).runs)
  }

  async function openRun(runId) {
    setError('')
    const response = await fetch(`${API}/runs/${runId}`)
    if (!response.ok) { setError('无法加载历史运行'); return }
    const payload = await response.json()
    if (payload.run?.status === 'failed') {
      setRun(null)
      setTab('explore')
      setError('')
      return
    }
    setRun(payload); setTab('overview')
  }

  const pageTitle = tab === 'overview' ? '每日采集' : tab === 'explore' ? '精选案例' : tab === 'case-detail' ? '案例讲解' : tab === 'discovery' ? '发现队列' : tab === 'douyin' ? '抖音案例' : tab === 'cases' ? '案例审核' : tab === 'events' ? '运行详情' : tab === 'tools' ? 'MCP 与连接器' : '评测中心'

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Sparkles size={18} /></div><div><strong>案例情报</strong><small>AI CASES</small></div></div>
      <div className="nav-label">内容</div>
      <NavButton icon={<Sparkles size={16} />} label="精选案例" active={tab === 'explore'} onClick={() => setTab('explore')} />
      <NavButton icon={<LayoutList size={16} />} label="全部案例" active={tab === 'discovery'} onClick={() => setTab('discovery')} />
      <NavButton icon={<ExternalLink size={16} />} label="抖音案例" active={tab === 'douyin'} onClick={() => setTab('douyin')} />
      <NavButton icon={<CalendarDays size={16} />} label="每日采集" active={tab === 'overview'} onClick={() => { setRun(null); setTab('overview') }} />
      <div className="nav-label nav-label-spaced">工作流</div>
      <NavButton icon={<Bot size={16} />} label="Agent 运行" active={tab === 'events'} onClick={() => setTab('events')} />
      <NavButton icon={<ShieldCheck size={16} />} label="审核与证据" active={tab === 'cases'} onClick={() => setTab('cases')} />
      <NavButton icon={<Zap size={16} />} label="MCP 连接器" active={tab === 'tools'} onClick={() => setTab('tools')} />
      <NavButton icon={<HistoryIcon size={16} />} label="评测中心" active={tab === 'evaluation'} onClick={() => setTab('evaluation')} />
      <div className="sidebar-foot"><span className="dot"></span><div>本地 SQLite 知识库</div><small>来源可追溯 · 可选腾讯文档导出</small></div>
    </aside>
    <main className="main">
      <nav className="mobile-nav" aria-label="案例工作台导航">{[["explore","精选案例"],["discovery","全部案例"],["douyin","抖音案例"],["overview","每日采集"]].map(([key,label]) => <button key={key} aria-current={tab === key ? "page" : undefined} onClick={() => {if(key === "overview") setRun(null); setTab(key)}}>{label}</button>)}</nav>
      <header className="topbar"><div><div className="breadcrumb">内容 <ChevronRight size={13} /> <span>{pageTitle}</span></div><h1>{pageTitle}</h1></div><div className="header-actions"><span className="read-only"><span className="dot"></span>本地只读知识库</span><button className="icon-button" title="筛选"><SlidersHorizontal size={17} /></button></div></header>
      {!run && tab === 'overview' && <DailyCollection onOpenRun={openRun} />}
      {error && <div className="error">{error}</div>}
      {run && tab === 'overview' && <Overview run={run} onReview={review} loading={loading} onNew={() => setRun(null)} onRunUpdated={setRun} onExport={exportReport} />}
      {tab === 'explore' && <Explore onOpenCase={item => { setSelectedCase(item); setTab('case-detail') }} />}
      {tab === 'case-detail' && selectedCase && <CaseDetail item={selectedCase} onBack={() => { setSelectedCase(null); setTab('explore') }} />}
      {tab === 'discovery' && <DiscoveryQueue />}
      {tab === 'douyin' && <DouyinPanel />}
      {tab === 'cases' && <Cases />}
      {tab === 'events' && <AgentRuns />}
      {tab === 'tools' && <Tools />}
      {tab === 'evaluation' && <Evaluation />}
    </main>
  </div>
}

function NavButton({icon, label, active, onClick}) { return <button className={active ? 'nav active' : 'nav'} onClick={onClick}>{icon}<span>{label}</span>{active && <span className="nav-active-dot" />}</button> }

function DailyCollection({onOpenRun}) {
  const [jobs, setJobs] = useState([])
  const [job, setJob] = useState(null)
  const [query, setQuery] = useState('真实用户如何使用 AI 工具解决工作和生活问题，包含具体步骤和实际结果')
  const [sources, setSources] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const active = ['queued', 'running'].includes(job?.status)
  const runningJob = jobs.find(item => ['queued', 'running'].includes(item.status))
  const collectionRunning = active || Boolean(runningJob)
  const runningEvent = (runningJob || job)?.events?.at(-1)
  const stages = [{key:'scout', label:'发现来源', note:'MCP 搜索公开网页和 RSS'}, {key:'fetcher', label:'抓取原文', note:'读取原文并保存快照'}, {key:'extractor', label:'抽取案例', note:'提取问题、方法、结果'}, {key:'verifier', label:'独立核验', note:'检查证据、数字和口径'}, {key:'scorer', label:'证据评分', note:'按原文判断六维档位'}, {key:'reviewer', label:'复审与修正', note:'逐维复核，最多一次修正'}, {key:'curator', label:'保存案例', note:'统一评分与入选状态'}]
  const latestEvent = job?.events?.at(-1)
  const stageIndex = stages.findIndex(stage => stage.key === latestEvent?.agent)
  async function refresh() {
    const response = await fetch(`${API}/v1/collection/jobs`)
    if (!response.ok) throw new Error('无法读取采集任务')
    const data = await response.json()
    const liveJobs = (data.items || []).filter(item => item.source_mode === 'live')
    setJobs(liveJobs)
    setError('')
    setJob(current => {
      if (current) return liveJobs.find(item => item.run_id === current.run_id) || current
      return liveJobs[0] || null
    })
  }
  useEffect(() => {
    refresh().catch(e => setError(e.message))
    fetch(`${API}/v1/sources`).then(response => response.ok ? response.json() : null).then(data => setSources(data?.items || [])).catch(() => {})
  }, [])
  useEffect(() => {
    const timer = setInterval(() => refresh().catch(e => setError(e.message)), collectionRunning ? 4000 : 10000)
    return () => clearInterval(timer)
  }, [job?.run_id, job?.status, collectionRunning])
  async function collect(retry = false) {
    setBusy(true); setError('')
    try {
      const response = await fetch(`${API}/v1/collection/jobs`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source_mode:'live', query, max_results:12, search_web:!retry, retry_pending:true, include_catalog:true, force:true})})
      if (!response.ok) throw new Error('启动采集失败')
      setJob(await response.json())
      await refresh()
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }
  const counts = job?.counts || {}
  return <section className="daily-page">
    <section className="daily-intro"><div><div className="eyebrow">DAILY CASE COLLECTION</div><h2>每天把网上真实的 AI 用法带回案例库</h2><p>每日采集只纳入最近一年发布的案例，先核对原文日期，再抽取事实、核验和评分。日期待确认和超期内容单独保留。</p></div><div className="daily-status"><span className={`status-dot ${collectionRunning ? 'active' : ''}`}></span><strong>{collectionRunning ? 'Agent 正在工作' : '采集服务已就绪'}</strong><small>{collectionRunning ? `${runningEvent?.agent || 'orchestrator'} · ${runningEvent?.action || '处理中'}` : '可手动启动采集；自动定时需本机定时服务正常运行'}</small></div></section>
    <section className="source-panel"><div><div className="section-kicker">SOURCE WATCHLIST</div><h3>正在关注的信息源</h3><p>官方站点可作为一手证据；官方社交、实践者、媒体和分析站用于发现线索，入选前必须回到原文核验。</p></div><div className="source-pills">{sources.map(source => <span key={source.source_id} className={`source-pill ${source.source_role === 'primary' ? 'official' : ''}`} title={source.why}><b>{source.name}</b><small>{sourceRoleLabel(source)}</small></span>)}</div></section>
    <section className="daily-command"><div><div className="section-kicker">启动一次采集</div><h3>告诉 Agent 你想找什么</h3><p>每轮最多处理 12 条，先接手可重试的历史案例，再按剩余名额补搜；搜索和 RSS 共用额度。时间范围为滚动最近一年。原文发布日期缺失、超期或证据不足时，不进入精选。</p></div><div className="daily-form"><input aria-label="采集主题" value={query} maxLength={500} onChange={e => setQuery(e.target.value)} /><button className="primary" disabled={busy || collectionRunning || !query.trim()} onClick={() => collect(false)}>{collectionRunning ? '采集中…' : busy ? '启动中…' : '开始联网采集'}</button><button className="secondary" disabled={busy || collectionRunning} onClick={() => collect(true)}>重试待处理</button></div></section>
    {error && <div className="error">{error}</div>}
    <section className="daily-flow panel"><div className="panel-title"><div><h3>采集流程</h3><span className="muted">查看 Agent 阶段和搜索、抓取调用记录</span></div>{job && <Status value={job.status} />}</div><div className="daily-stage-list">{stages.map((stage, index) => <div className={`daily-stage ${active && index === stageIndex ? 'current' : ''} ${job?.events?.some(event => event.agent === stage.key && event.status !== 'running') ? 'done' : ''}`} key={stage.key}><div className="stage-number">{job?.events?.some(event => event.agent === stage.key && event.status !== 'running') ? '✓' : String(index + 1).padStart(2, '0')}</div><div><strong>{stage.label}</strong><small>{stage.note}</small></div>{index < stages.length - 1 && <span className="stage-line" />}</div>)}</div></section>
    <section className="daily-metrics"><div className="daily-metric"><span>本轮新增</span><strong>{job?.new_candidates ?? job?.discovered ?? 0}</strong><small>新收录线索，不含历史积压</small></div><div className="daily-metric"><span>本轮入选</span><strong className="green">{(counts.selected || 0) + (counts.featured || 0)}</strong><small>进入实时精选流</small></div><div className="daily-metric"><span>待处理</span><strong>{job?.queued || 0}</strong><small>等待抓取或重试</small></div><div className="daily-metric"><span>本轮已处理</span><strong>{job?.processed_count ?? Object.values(counts).reduce((a,b) => a+b, 0)}</strong><small>上限 {job?.processing_budget || 12} 条，含历史重试</small></div></section>
    <CollectionResultNote job={job} /><section className="daily-history panel"><div className="panel-title"><div><h3>最近采集任务</h3><span className="muted">失败和待补证案例也会保留原因</span></div></div>{jobs.length === 0 ? <p className="muted">还没有联网采集任务</p> : jobs.slice(0, 8).map(item => <button className={`daily-job ${item.run_id === job?.run_id ? 'selected' : ''}`} key={item.run_id} onClick={() => setJob(item)}><span><strong>{item.run_id}</strong><small>{item.new_candidates ?? item.discovered ?? 0} 条新增 · {(item.counts?.selected || 0) + (item.counts?.featured || 0)} 条入选</small></span><Status value={item.status} /></button>)}</section>
  </section>
}

function Overview({run, onReview, loading, onNew, onRunUpdated, onExport}) {
  const counts = run.counts || {}
  return <>
    <div className="run-head"><div><span className="muted">运行 {run.run.run_id}</span><h2>{run.run.topic}</h2></div><div className="run-actions"><Status value={run.run.status} /><button className="secondary" onClick={onExport}>导出报告</button><button className="secondary" onClick={onNew}>新建演示</button></div></div>
    <div className="metrics"><Metric title="候选案例" value={(counts.verified || 0) + (counts.rejected || 0)} detail="Source MCP 返回" /><Metric title="通过核验" value={counts.verified || 0} detail="可进入日报" good /><Metric title="已淘汰" value={counts.rejected || 0} detail="有明确原因" /><Metric title="运行阶段" value={run.interrupted ? '审核' : run.run.status === 'completed' ? '完成' : '处理'} detail="LangGraph 状态" /></div>
    {run.interrupted && <div className="review-banner"><div><div className="eyebrow">HUMAN REVIEW REQUIRED</div><h3>日报版本已冻结，等待你的审核</h3><p>审核通过后才会执行知识库同步和消息发布。</p></div><div className="review-buttons"><button className="primary" disabled={loading} onClick={() => onReview('approve')}>批准并继续</button><button className="secondary danger-text" disabled={loading} onClick={() => onReview('reject')}>驳回</button></div></div>}
    {run.run.status === 'blocked' && <RecoveryBanner run={run} action="sync" label="知识库同步发生冲突" description="只重试同步、回读和后续发布，不重跑前面的 Agent。" onRecovered={onRunUpdated} />}
    {run.run.status === 'needs_attention' && <RecoveryBanner run={run} action="publish" label="发布结果未知" description="查询已有外部请求的回执，确认后再结束任务。" onRecovered={onRunUpdated} />}
    <div className="grid-two"><section className="panel"><div className="panel-title"><h3>分发状态</h3><span className="muted">冻结版本后执行</span></div><div className="delivery"><Delivery label="日报版本" status={run.content_version?.status || 'pending'} note={run.content_version?.content_hash ? `SHA-256 ${run.content_version.content_hash.slice(0, 12)}…` : '尚未生成'} /><Delivery label="知识库" status={run.deliveries?.find(x => x.channel === 'knowledge_base')?.status || (run.interrupted ? 'pending' : 'pending')} note="写入后精确回读" /><Delivery label="消息渠道" status={run.deliveries?.find(x => x.channel === 'demo_channel')?.status || 'pending'} note="保存外部回执" /></div></section><section className="panel"><div className="panel-title"><h3>最近事件</h3><button className="text-button" onClick={() => {}}>查看全部 →</button></div><div className="mini-events">{run.events.slice(-5).map((e, index) => <div className="mini-event" key={`${e.event_id || e.at || 'event'}-${e.actor || e.agent || ''}-${e.action || ''}-${index}`}><span className={`event-dot ${e.status}`}></span><div><strong>{e.actor || e.agent}</strong><p>{e.message || e.action}</p></div></div>)}</div></section></div>
  </>
}

function RecoveryBanner({run, action, label, description, onRecovered}) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  async function recover() {
    setBusy(true)
    const response = await fetch(`${API}/runs/${run.run.run_id}/recover`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action})})
    if (response.ok) {
      const updated = await response.json()
      setMessage('恢复完成')
      onRecovered(updated)
    } else {
      setMessage('恢复失败，请查看运行详情')
    }
    setBusy(false)
  }
  return <div className="review-banner recovery-banner"><div><div className="eyebrow">RECOVERY AVAILABLE</div><h3>{label}</h3><p>{description}</p>{message && <p>{message}</p>}</div><button className="primary" disabled={busy} onClick={recover}>{busy ? '处理中…' : action === 'sync' ? '局部恢复同步' : '查询发布回执'}</button></div>
}

function Metric({title, value, detail, good}) { return <div className="metric"><span>{title}</span><strong className={good ? 'green' : ''}>{value}</strong><small>{detail}</small></div> }
function History({runs, onRefresh, onOpen}) { useEffect(() => { onRefresh() }, []); return <section className="panel history-panel"><div className="panel-title"><div><h3>历史运行</h3><span className="muted">从本地 SQLite 恢复，可继续审核或查看报告</span></div><button className="text-button" onClick={onRefresh}>刷新</button></div>{runs.length === 0 ? <p className="muted">还没有历史运行</p> : runs.slice(0, 8).map(item => <button className="history-row" key={item.run_id} onClick={() => onOpen(item.run_id)}><span><strong>{item.topic}</strong><small>{item.run_id} · {item.connector_mode === 'official_mcp' ? '官方 MCP' : 'Mock MCP'}</small></span><Status value={item.status} /></button>)}</section> }
function Delivery({label, status, note}) { return <div className="delivery-row"><div className="delivery-icon">{status === 'sent' || status === 'synced' ? '✓' : status === 'sync_conflict' ? '!' : '·'}</div><div><strong>{label}</strong><small>{note}</small></div><Status value={status} /></div> }
function Cases() {
  const [items, setItems] = useState([])
  const [filter, setFilter] = useState('all')
  const [error, setError] = useState('')
  const statusTabs = [['all','全部'],['selected','已入选'],['candidate','候选'],['needs_scoring','待评分'],['needs_extraction','待抽取'],['needs_date','日期待核实'],['outdated','超出一年'],['rejected','未通过']]
  const normalized = item => ['selected','featured'].includes(item.status) ? 'selected' : item.status
  async function refresh() {
    const response = await fetch(`${API}/v1/collection/items?limit=500`)
    if (!response.ok) throw new Error('无法加载审核队列')
    setItems(((await response.json()).items || []).filter(item => ['web_search', 'rss'].includes(item.discovery_mode)))
  }
  useEffect(() => { refresh().catch(e => setError(e.message)); const timer = setInterval(() => refresh().catch(() => {}), 5000); return () => clearInterval(timer) }, [])
  const counts = items.reduce((out, item) => { const key = normalized(item); out.all = (out.all || 0) + 1; out[key] = (out[key] || 0) + 1; return out }, {})
  const visible = filter === 'all' ? items : items.filter(item => normalized(item) === filter)
  return <section className="review-page">
    <section className="review-hero"><div><div className="eyebrow">审核与证据</div><h2>每条案例都要有证据，才会进入精选</h2><p>审核中心把采集队列按状态拆开。你可以先看哪些已通过，再集中处理待抽取、待评分和未通过的原因。</p></div><ShieldCheck size={38} /></section>
    <div className="review-status-tabs">{statusTabs.map(([key, label]) => <button key={key} className={filter === key ? 'review-status active' : 'review-status'} onClick={() => setFilter(key)}>{label}<strong>{counts[key] || 0}</strong></button>)}</div>
    {error && <div className="error">{error}</div>}
    {visible.length === 0 ? <section className="panel review-empty"><ShieldCheck size={22} /><p>这个状态下暂时没有案例。</p></section> : <section className="review-list">{visible.map(item => <ReviewItem key={item.source_item_id} item={item} />)}</section>}
  </section>
}

function ReviewItem({item}) {
  const statusLabels = {selected:'已入选', featured:'精选', candidate:'候选', needs_scoring:'待评分', needs_extraction:'待抽取', needs_date:'日期待核实', outdated:'超出一年', rejected:'未通过'}
  const evidence = item.problem && item.approach && item.outcome
  return <article className="review-item"><div className="review-item-head"><div><span className="case-id">{statusLabels[item.status] || item.status} · {item.source_name || '公开网页'} · {item.source_role === 'primary' ? '一手证据' : item.source_role === 'discovery' ? '发现线索' : '需回溯原文'}{item.fetch_strategy_label ? ` · ${item.fetch_strategy_label}` : ''}</span><h3>{item.title_original}</h3><CaseDates item={item} /><a href={item.source_url} target="_blank" rel="noreferrer">查看原文 ↗</a></div><div className="review-score"><strong>{item.scorecard?.quality_score ?? '—'}</strong><small>{item.scorecard ? '质量分' : '尚未评分'}</small></div></div><div className="review-reason"><b>{item.diagnostic?.label || (item.verification?.decision === 'pass' ? '核验通过' : item.verification?.decision === 'reject' ? '核验未通过' : item.extraction?.extraction_status === 'fetch_failed' ? '原文抓取失败' : '等待处理')}</b><p>{item.diagnostic?.reason || item.verification?.reason || item.extraction?.reason || item.scorecard?.reason || '等待抓取原文并抽取问题、方法和结果。'}</p></div>{item.scorecard && <details className="queue-quality"><summary>评分与独立复审记录</summary><ScoreBreakdown caseId={item.case_id || item.id || item.source_item_id} scorecard={item.scorecard}/></details>}{evidence && <details><summary>查看问题、方法、结果证据</summary><div className="review-evidence-grid">{[['problem','问题'],['approach','方法'],['outcome','结果']].map(([key,label]) => <div key={key}><label>{label}</label><p>{item[key].claim}</p><EvidenceQuote caseId={item.case_id || item.source_item_id} quote={item[key].evidence} explanation={item[key].claim} /></div>)}</div>{item.limitations?.length > 0 && <p className="muted">局限：{item.limitations.join('；')}</p>}</details>}</article>
}

function AgentRuns() {
  const [jobs, setJobs] = useState([])
  const [selected, setSelected] = useState(null)
  const [error, setError] = useState('')
  async function refresh() {
    const response = await fetch(`${API}/v1/collection/jobs`)
    if (!response.ok) throw new Error('无法加载 Agent 运行记录')
    const next = ((await response.json()).items || []).filter(item => item.source_mode === 'live')
    setJobs(next); setSelected(current => current ? next.find(item => item.run_id === current.run_id) || current : next[0] || null)
  }
  useEffect(() => { refresh().catch(e => setError(e.message)); const timer = setInterval(() => refresh().catch(() => {}), 4000); return () => clearInterval(timer) }, [])
  const stageNames = {orchestrator:'编排', scout:'搜索', fetcher:'抓取', extractor:'抽取', verifier:'核验', scorer:'评分', reviewer:'复审', curator:'入库'}
  const events = selected?.events || []
  const latest = events.at(-1)
  return <section className="agent-page"><section className="agent-hero"><div><div className="eyebrow">AGENT OPERATIONS</div><h2>看见每个 Agent 正在做什么</h2><p>这里记录联网采集的真实运行。搜索 Agent 只发现候选，抓取、抽取、核验、评分和复审分别留下事件，最后由入库 Agent 保存结果。</p></div><Bot size={40} /></section>{error && <div className="error">{error}</div>}{jobs.length === 0 ? <section className="panel review-empty"><Bot size={22} /><p>还没有采集任务。去“每日采集”启动一次联网采集。</p></section> : <div className="agent-layout"><section className="panel agent-jobs"><div className="panel-title"><div><h3>采集任务</h3><span className="muted">按时间倒序保存</span></div></div>{jobs.map(job => <button className={selected?.run_id === job.run_id ? 'agent-job active' : 'agent-job'} key={job.run_id} onClick={() => setSelected(job)}><span><strong>{job.run_id}</strong><small>{job.new_candidates ?? job.discovered ?? 0} 条新增 · {job.processed_count ?? Object.values(job.counts || {}).reduce((a,b) => a+b, 0)} 条已处理</small></span><Status value={job.status} /></button>)}</section><section className="panel agent-detail"><div className="panel-title"><div><h3>{selected?.run_id}</h3><span className="muted">{selected?.query || '无主题'}</span></div>{selected && <Status value={selected.status} />}</div><div className="agent-metrics"><Metric title="本轮新增" value={selected?.new_candidates ?? selected?.discovered ?? 0} detail="不含历史待处理" /><Metric title="已入选" value={(selected?.counts?.selected || 0) + (selected?.counts?.featured || 0)} detail="通过质量门槛" good /><Metric title="待处理" value={selected?.queued || 0} detail="等待重试" /><Metric title="当前阶段" value={stageNames[latest?.agent] || '完成'} detail={latest?.action || '暂无事件'} /></div><CollectionResultNote job={selected} /><div className="agent-stage-strip">{['scout','fetcher','extractor','verifier','scorer','reviewer','curator'].map((key, index) => <div className={latest && events.findIndex(e => e.agent === key) >= 0 && events.findIndex(e => e.agent === key) <= events.length - 1 ? 'agent-stage done' : 'agent-stage'} key={key}><span>{String(index + 1).padStart(2,'0')}</span><b>{stageNames[key]}</b></div>)}</div><div className="timeline">{events.length === 0 ? <p className="muted">该任务没有事件记录。</p> : events.slice().reverse().map((e, index) => <div className="timeline-row" key={e.event_id || `${selected.run_id}:${events.length - 1 - index}`}><div className="timeline-time">{new Date(e.at || e.created_at).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'})}</div><div className={`event-dot ${e.status}`}></div><div className="timeline-content"><div><strong>{stageNames[e.agent] || e.agent}</strong><span className="event-type">{e.action}</span></div><p>{e.status === 'completed' ? '阶段完成' : e.status === 'running' ? '正在执行' : e.status}</p>{e.error && <p>{e.error}</p>}{e.issues?.length > 0 && <p>{e.issues.join('；')}</p>}{e.metadata && Object.keys(e.metadata).length > 0 && <code>{JSON.stringify(e.metadata)}</code>}</div></div>)}</div></section></div>}</section>
}
function Tools() { const [catalog, setCatalog] = useState(null); useEffect(() => { fetch(`${API}/tools`).then(r => r.json()).then(setCatalog) }, []); return <section className="panel wide"><div className="panel-title"><div><h3>MCP 与连接器</h3><span className="muted">{catalog?.mode === 'official_mcp' ? '官方 MCP SDK stdio Client' : '本地模拟 Server'}</span></div><span className="status status-synced">已连接</span></div><div className="tool-grid">{catalog && Object.entries(catalog.servers).map(([server, tools]) => <div className="tool-server" key={server}><div className="server-head"><span className="server-dot"></span><strong>{server} MCP</strong><small>{tools.length} tools</small></div>{tools.map(tool => <div className="tool" key={tool.name}><code>{tool.name}</code><p>{tool.description}</p></div>)}</div>)}</div></section> }
function Evaluation() { const [report, setReport] = useState(null); useEffect(() => { fetch(`${API}/evaluations/latest`).then(r => r.json()).then(setReport) }, []); if (!report) return <section className="panel wide"><p className="muted">加载评测数据…</p></section>; const normal = report.dataset, adversarial = report.adversarial; return <section className="panel wide"><div className="panel-title"><div><h3>评测中心</h3><span className="muted">脱敏 Fixture · {normal.dataset_size} 条人工标签样本</span></div><span className="status status-synced">离线可复现</span></div><div className="eval-note">比较直接相信模型输出，与加入重复检查、证据检查和数字溯源护栏后的结果。对抗模型故意放行所有案例，用来验证护栏。</div><div className="eval-grid"><EvalMetric label="正常 Mock · baseline" value={`${Math.round(normal.baseline.accuracy * 100)}%`} /><EvalMetric label="正常 Mock · guarded" value={`${Math.round(normal.multi_agent_guarded.accuracy * 100)}%`} good /><EvalMetric label="对抗模型 · baseline" value={`${Math.round(adversarial.baseline.accuracy * 100)}%`} /><EvalMetric label="对抗模型 · guarded" value={`${Math.round(adversarial.multi_agent_guarded.accuracy * 100)}%`} good /></div><div className="eval-table">{adversarial.rows.map(row => <div className="eval-row" key={row.case_id}><code>{row.case_id}</code><span>期望：{row.expected}</span><span>baseline：{row.baseline}</span><span className={row.guarded === row.expected ? 'good-text' : 'bad-text'}>guarded：{row.guarded}</span><small>{row.guarded_reason}</small></div>)}</div></section> }
function Explore({onOpenCase}) {
  const [query, setQuery] = useState('')
  const [payload, setPayload] = useState(null)
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState('全部')
  const [collection, setCollection] = useState(null)
  const autoStarted = useRef(false)
  const searchToken = useRef(0)
  const categoryParams = {'多 Agent':'多 Agent', '工作流':'工作流', '产品设计':'产品设计', '个人效率':'个人效率'}
  async function search(event) {
    event?.preventDefault(); const token = ++searchToken.current; setLoading(true)
    try {
      const params = new URLSearchParams({query, window:'1y', limit:'20', source_mode:'live'})
      if (categoryParams[filter]) params.set('category', categoryParams[filter])
      const response = await fetch(`${API}/v1/cases?${params}`)
      if (response.ok) {
        const next = await response.json()
        if (token === searchToken.current) setPayload(next)
      }
    } finally { if (token === searchToken.current) setLoading(false) }
  }
  async function ensureCollection() {
    if (autoStarted.current) return
    autoStarted.current = true
    try {
      const historyResponse = await fetch(`${API}/v1/collection/jobs`)
      const history = historyResponse.ok ? await historyResponse.json() : {items: []}
      const active = history.items?.find(item => ['queued', 'running'].includes(item.status) && item.source_mode === 'live')
      if (active) {
        setCollection(active)
        return
      }
      const sessionKey = 'ai-case-workbench.auto-collection-at'
      const lastAutoStart = Number(window.sessionStorage.getItem(sessionKey) || 0)
      if (Date.now() - lastAutoStart < 30 * 60 * 1000) return
      const response = await fetch(`${API}/v1/collection/jobs`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        source_mode:'live', query:'真实用户如何使用 AI 工具解决工作和生活问题，包含具体步骤、工具和实际结果', max_results:12, search_web:true, retry_pending:true, include_catalog:true
      })})
      if (response.ok) {
        window.sessionStorage.setItem(sessionKey, String(Date.now()))
        setCollection(await response.json())
      }
    } catch { /* 精选流仍然可以读取已有本地数据 */ }
  }
  useEffect(() => { search() }, [filter])
  useEffect(() => { ensureCollection() }, [])
  useEffect(() => {
    if (!collection?.run_id || !['queued', 'running'].includes(collection.status)) return
    let canceled = false
    let timer
    async function poll() {
      try {
        const response = await fetch(`${API}/v1/collection/jobs/${collection.run_id}`)
        if (canceled) return
        if (!response.ok) throw new Error('暂时无法读取采集进度')
        const next = await response.json()
        setCollection(next)
        if (['queued', 'running'].includes(next.status)) timer = setTimeout(poll, 4000)
        else search()
      } catch { if (!canceled) timer = setTimeout(poll, 6000) }
    }
    timer = setTimeout(poll, 1200)
    return () => { canceled = true; clearTimeout(timer) }
  }, [collection?.run_id, collection?.status])
  const items = payload?.items || []
  const latestEvent = collection?.events?.at(-1)
  const stageLabels = {scout:'联网搜索', fetcher:'抓取原文', extractor:'抽取证据', verifier:'核验证据', scorer:'证据评分', reviewer:'独立复审与修正', curator:'写入知识库', orchestrator:'整理候选'}
  const stageText = latestEvent ? `${stageLabels[latestEvent.agent] || latestEvent.agent} · ${latestEvent.action}` : '准备启动'
  return <>
    <section className="explore-hero">
      <div className="eyebrow">CURATED AI USE CASES</div>
      <h2>看看别人怎样把 AI 用进真实工作</h2>
      <p>从最近一年发布的原文中发现可复用工作流，核对发布日期并经过证据核验和评分，进入你的个人案例库。</p>
      <div className="hero-chips"><span><CalendarDays size={14} /> 最近一年 · 按原文发布日期</span><span><CheckCircle2 size={14} /> 原文可回溯</span><span><ShieldCheck size={14} /> 证据核验</span><span><Bot size={14} /> Agent 自动采集</span></div>
    </section>
    <section className="explore-toolbar">
      <div className="filter-tabs">{['全部','多 Agent','工作流','产品设计','个人效率'].map(value => <button key={value} className={filter === value ? 'filter-tab active' : 'filter-tab'} onClick={() => setFilter(value)}>{value}{value !== '全部' && payload?.filters?.category === value ? ` · ${payload.meta?.total || 0}` : ''}</button>)}</div>
      <form className="explore-search" onSubmit={search}><Search size={17} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索案例、工具或工作场景" /><button className="search-button" disabled={loading}>{loading ? '查询中…' : '搜索'}</button></form>
    </section>
    <div className="feed-heading"><div><strong>{filter === '全部' ? '精选案例' : filter}</strong><span>{payload?.meta?.total || 0} 条已核验内容</span></div><span className="live-note"><span className={`dot ${collection && ['queued','running'].includes(collection.status) ? 'pulse' : ''}`}></span>{collection && ['queued','running'].includes(collection.status) ? `Agent 正在${stageText} · 本轮新增 ${collection.new_candidates ?? collection.discovered ?? 0} 条` : '实时连接本地知识库'}</span></div>
    <section className="case-feed">{items.length ? items.map((item, index) => <ExploreCard item={item} index={index} key={item.id} onOpen={onOpenCase} />) : <div className="empty-state"><CircleAlert size={22} /><p>{payload ? '暂时没有符合筛选条件的案例' : '正在加载案例…'}</p></div>}</section>
  </>
}

function ExploreCard({item, index, onOpen}) {
  return <article className="explore-card">
    <div className="card-main"><div className="card-meta"><span className="rank">{String(index + 1).padStart(2, '0')}</span><span className="tag">{item.selected ? '精选' : '已核验 · 未入精选'}</span><SourceBadge source={item} /></div>
      <h3>{item.title}</h3>
      <CaseDates item={item} />
      <div className="case-summary"><div><b>遇到的问题</b><p>{item.summary.problem}</p></div><div><b>怎么做的</b><p>{item.summary.approach}</p></div><div><b>带来的结果</b><p>{item.summary.outcome}</p></div></div>
      <div className="card-footer"><span><ShieldCheck size={14} /> 已完成来源核验</span><button className="source-link" onClick={() => onOpen(item)}>查看案例讲解 <ArrowUpRight size={14} /></button></div>
    </div><aside className="score-block"><strong>{item.quality_score ?? '待评分'}</strong><span>质量分</span><small>{item.selected ? '入选案例' : '未入精选'}</small></aside>
  </article>
}

function CaseDetail({item, onBack}) {
  const evidence = item.evidence || []
  const evidenceLabels = ['问题证据', '方法证据', '结果证据']
  return <section className="case-detail">
    <button className="back-link" onClick={onBack}><ArrowLeft size={15} /> 返回精选案例</button>
    <div className="detail-hero">
      <div className="eyebrow">案例讲解 · 从零读懂</div>
      <div className="detail-meta"><span className="tag">{item.selected ? '精选' : '已核验 · 未入精选'}</span><SourceBadge source={item} /><span>质量分 {item.quality_score ?? '待评分'}</span></div>
      <h2>{item.title}</h2>
      <CaseDates item={item} detail />
      <p className="detail-lead">先把这篇案例翻译成一个可理解的工作流：谁遇到了什么问题，如何把 AI 放进任务里，最后实际得到了什么结果。</p>
      <div className="detail-actions"><a className="primary detail-source" href={item.source_url} target="_blank" rel="noreferrer">打开外部原文 <ExternalLink size={14} /></a><span className="source-caption">以下讲解只依据已核验的原文证据</span></div>
    </div>
    <ScoreBreakdown caseId={item.case_id || item.id || item.source_item_id} scorecard={item.scorecard} />
    <div className="detail-grid">
      <article className="detail-section detail-wide"><div className="section-kicker">先看结论</div><h3>这个案例是怎么运作的？</h3><p>{item.summary.approach}</p><div className="flow-strip"><div><span>01</span><b>真实任务</b><small>{item.summary.problem}</small></div><div className="flow-arrow">→</div><div><span>02</span><b>AI 介入</b><small>{item.summary.approach}</small></div><div className="flow-arrow">→</div><div><span>03</span><b>观察结果</b><small>{item.summary.outcome}</small></div></div></article>
      <article className="detail-section"><div className="section-kicker">第 0 步 · 背景</div><h3>原来遇到了什么问题？</h3><p>{item.summary.problem}</p></article>
      <article className="detail-section"><div className="section-kicker">第 1 步 · 输入</div><h3>用户把什么交给了 AI？</h3><p>{item.summary.approach}</p></article>
      <article className="detail-section detail-wide"><div className="section-kicker">第 2 步 · 执行链路</div><h3>AI 在工作流中做了什么？</h3><ol className="explain-steps"><li><span>任务被明确</span><p>先从一个已经发生的工作或生活任务开始，而不是从“这个工具能做什么”开始。</p></li><li><span>AI 完成对应处理</span><p>{item.summary.approach}</p></li><li><span>人或原文给出结果</span><p>{item.summary.outcome}</p></li></ol></article>
      <article className="detail-section detail-wide outcome-section"><div className="section-kicker">第 3 步 · 结果</div><h3>最后实际发生了什么？</h3><p>{item.summary.outcome}</p></article>
      <article className="detail-section detail-wide evidence-section"><div className="section-kicker">证据 · 中文解读与原文对照</div><h3>为什么工作台认为它值得参考？</h3><div className="evidence-list">{evidence.map((quote, index) => <div className="evidence-row" key={`${quote}-${index}`}><span>{evidenceLabels[index] || '原文证据'}</span><EvidenceQuote caseId={item.id} quote={quote} explanation={item.summary?.[['problem', 'approach', 'outcome'][index]]} /></div>)}</div></article>
      <article className="detail-section detail-wide limitation-section"><div className="section-kicker">边界</div><h3>哪些地方不能直接照搬？</h3>{item.limitations?.length ? <ul>{item.limitations.map(limit => <li key={limit}>{limit}</li>)}</ul> : <p>原文没有提供额外限制说明，仍应把它看作单个来源的案例记录，不能自动推导成普遍效果。</p>}</article>
    </div>
  </section>
}
function EvalMetric({label, value, good}) { return <div className="metric"><span>{label}</span><strong className={good ? 'green' : ''}>{value}</strong><small>准确率</small></div> }

const rootElement = document.getElementById('root')
// Vite preserves the DOM during HMR. Reusing the root prevents the dev server
// from mounting a second React tree while a collection is being polled.
const appRoot = window.__aiCaseWorkbenchRoot || (window.__aiCaseWorkbenchRoot = createRoot(rootElement))
appRoot.render(<App />)

import React, { useEffect, useRef, useState } from 'react'
import { ExternalLink, FileVideo, RefreshCw, Upload, Video } from 'lucide-react'
import './douyin.css'

const API = import.meta.env.VITE_API_BASE_URL || '/api'
const ROOT = '/v1/douyin'
const STATUS = {
  discovered: '已发现线索', queued: '等待读取', pending: '等待读取', running: '处理中', reading: '读取页面中',
  awaiting_browser: '等待浏览器读取', browser_required: '等待浏览器读取', needs_browser: '等待浏览器读取',
  waiting_login: '等待登录', login_required: '等待登录', needs_login: '等待登录',
  manual_verification: '等待人工验证', verification_required: '等待人工验证', captcha_required: '等待人工验证',
  rate_limited: '限流暂停', unavailable: '内容不可用', deleted: '内容已删除', network_error: '网络故障',
  media_unavailable: '素材不可获取', awaiting_media: '等待导入素材', media_ready: '素材已保存',
  transcription_failed: '转写失败', transcription_unavailable: '转写环境未就绪', transcribing: '语音转写中',
  needs_date: '日期待核实', date_unknown: '日期待核实', outdated: '超出一年',
  needs_evidence: '证据不足', evidence_insufficient: '证据不足', awaiting_review: '待核验评分', ready_for_review: '待核验评分',
  needs_extraction: '待抽取', needs_scoring: '待评分或复审', candidate: '候选', selected: '已入选', featured: '精选',
  rejected: '未入选', completed: '已完成', failed: '处理失败', paused: '来源已暂停', ready: '证据待核验', processing: '核验处理中', processed: '已移交审核', future: '日期晚于当前时间',
}
const KINDS = { author_statement: '作者口述', author_speech: '作者口述', machine_transcript: '机器转写', speech: '作者口述', transcript: '机器转写',
  visible_frame: '画面可见内容', visual: '画面可见内容', visual_observation: '画面观察', visual_ocr: '画面文字识别', ocr: '画面文字识别',
  external: '外部印证', external_corroboration: '外部印证', manual_transcript: '人工转写' }
const STAGES = { import: '链接导入', discovery: '发现作品', discover: '发现作品', browser: '浏览器读取', metadata: '读取信息',
  date_filter: '日期筛选', media: '素材保存', download: '获取素材', transcript: '语音转写', transcribe: '语音转写',
  transcription: '语音转写', frames: '画面核对', evidence: '保存证据', extraction: '案例抽取', extract: '案例抽取',
  verify: '独立核验', score: '质量评分', review: '复审', curator: '保存案例', process: '处理素材', browser_observation: '页面与证据观察', collection: '移交案例审核', media_cleanup: '清理本地素材' }
const pausedStates = new Set(['waiting_login', 'login_required', 'needs_login', 'manual_verification', 'verification_required', 'captcha_required', 'rate_limited', 'paused'])
const activeStates = new Set(['queued', 'running', 'reading', 'transcribing', 'processing'])
const ROLES = { problem: '问题', workflow: '使用步骤', outcome: '结果', context: '背景' }
const identity = item => item?.item_id || item?.aweme_id || item?.id
const label = value => STATUS[value] || value || '尚未处理'
const formatDate = (value, dateOnly = false) => {
  if (!value) return '未知，等待核实'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : dateOnly ? date.toLocaleDateString('zh-CN') : date.toLocaleString('zh-CN', { hour12: false })
}
const timestamp = seconds => {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value < 0) return '时间未标注'
  return `${Math.floor(value / 60).toString().padStart(2, '0')}:${Math.floor(value % 60).toString().padStart(2, '0')}`
}
const safeLink = value => {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : null } catch { return null }
}
async function request(path, options) {
  const response = await fetch(`${API}${ROOT}${path}`, options)
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const message = typeof data?.detail === 'string' ? data.detail : data?.message
    throw new Error(message || `请求未完成（${response.status}），请稍后重试`)
  }
  return data || {}
}
const post = (path, body = {}) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
async function readReview(runId) {
  const response = await fetch(`${API}/v1/collection/jobs/${encodeURIComponent(runId)}`)
  if (!response.ok) throw new Error('暂时无法读取核验进度，请稍后刷新；已提交的记录仍保留。')
  return response.json()
}

function StateBadge({ value }) {
  return <span className={`dy-state ${pausedStates.has(value) ? 'paused' : ['selected', 'featured', 'completed'].includes(value) ? 'good' : ''}`}>{label(value)}</span>
}

export default function DouyinPanel() {
  const [items, setItems] = useState([])
  const [currentId, setCurrentId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [inputMode, setInputMode] = useState('link')
  const [text, setText] = useState('')
  const [query, setQuery] = useState('AI 解决 工作 实际效果')
  const [authorUrl, setAuthorUrl] = useState('')
  const [maxResults, setMaxResults] = useState(5)
  const [status, setStatus] = useState('all')
  const [source, setSource] = useState('all')
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [task, setTask] = useState(null)
  const [tasks, setTasks] = useState([])
  const [sourceState, setSourceState] = useState(null)
  const [storage, setStorage] = useState(null)
  const [reviewRun, setReviewRun] = useState(null)
  const selectedRef = useRef(null)
  const requestRevision = useRef(0)

  async function refresh() {
    const [data, discovery, review] = await Promise.all([request('/items?limit=200'), request('/discovery/tasks'), reviewRun?.run_id ? readReview(reviewRun.run_id) : Promise.resolve(null)])
    setItems(data.items || [])
    setTasks(discovery.items || [])
    setSourceState(data.source_state || discovery.source_state || null)
    setStorage(data.storage || null)
    if (review) setReviewRun(review)
    setTask(current => (discovery.items || []).find(value => value.task_id === current?.task_id) || current)
    setLoading(false)
    return data.items || []
  }
  async function openItem(id) {
    selectedRef.current = id
    setCurrentId(id)
    setDetail(null)
    const revision = ++requestRevision.current
    try {
      const data = await request(`/items/${encodeURIComponent(id)}`)
      if (revision === requestRevision.current) setDetail(data.item || data)
    } catch (e) { if (revision === requestRevision.current) setError(e.message) }
  }
  async function refreshDetail() {
    if (!selectedRef.current) return
    const id = selectedRef.current
    const data = await request(`/items/${encodeURIComponent(id)}`)
    if (selectedRef.current === id) setDetail(data.item || data)
  }
  useEffect(() => { refresh().catch(e => { setError(e.message); setLoading(false) }) }, [])
  useEffect(() => {
    if (!items.some(item => activeStates.has(item.status)) && !activeStates.has(task?.status) && !activeStates.has(reviewRun?.status)) return
    const timer = setInterval(() => Promise.all([refresh(), refreshDetail()]).catch(e => setError(e.message)), 6000)
    return () => clearInterval(timer)
  }, [items.some(item => activeStates.has(item.status)), task?.status, reviewRun?.run_id, reviewRun?.status])

  async function submit(event) {
    event.preventDefault()
    setBusy('import'); setError(''); setNotice('')
    try {
      const data = inputMode === 'link'
        ? await post('/import', { text: text.trim() })
        : await post('/discover', { query: inputMode === 'keyword' ? query.trim() : '', author_url: inputMode === 'author' ? authorUrl.trim() : '', max_results: Number(maxResults) })
      if (inputMode !== 'link') setTask(data.task || data.job || data)
      const saved = await refresh()
      const first = data.item || data.items?.[0]
      if (first && identity(first)) await openItem(identity(first))
      else if (saved.length && !currentId) await openItem(identity(saved[0]))
      setNotice(data.message || (inputMode === 'link' ? '链接已登记。请查看作品状态；登记线索不代表完成采集或通过核验。' : '发现任务已登记。请打开抖音页面，由本人或当前浏览器协作逐批记录实际可读的作品。'))
      if (inputMode === 'link') setText('')
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  async function mutate(action, taskFn) {
    setBusy(action); setError(''); setNotice('')
    try {
      const data = await taskFn()
      if (data.item) { setDetail(data.item); selectedRef.current = identity(data.item); setCurrentId(identity(data.item)) }
      await refresh()
      await refreshDetail()
      if (data.job?.run_id) setReviewRun(data.job)
      setNotice(data.message || '处理记录已更新，请核对当前状态与证据。')
      return true
    } catch (e) { setError(e.message); return false } finally { setBusy('') }
  }

  const filtered = items.filter(item => {
    if (status === 'paused' ? !pausedStates.has(item.status) : status !== 'all' && item.status !== status) return false
    if (source === 'keyword' && !item.query) return false
    if (source === 'author' && !item.author_url) return false
    if (source === 'link' && (item.query || item.author_url)) return false
    return !search.trim() || `${item.title || ''} ${item.author_name || ''} ${identity(item) || ''}`.toLowerCase().includes(search.trim().toLowerCase())
  })
  const statusOptions = [...new Set(items.map(item => item.status).filter(Boolean))]
  const pausedCount = items.filter(item => pausedStates.has(item.status)).length
  const canSubmit = inputMode === 'link' ? text.trim() : inputMode === 'keyword' ? query.trim() : authorUrl.trim()

  return <section className="dy-page">
    <div className="dy-intro"><div><div className="section-kicker">DOUYIN · 视频案例</div><h2>从视频线索，追到真实使用证据</h2><p>最近一年内的公开作品。先核实发布时间，再结合带时间位置的语音与画面核验；口播效果、点赞数不会直接成为成功结论。</p></div><Video size={34} aria-hidden="true" /></div>
    <section className="dy-import" aria-label="导入或发现抖音作品">
      <div className="dy-mode-tabs" role="tablist" aria-label="发现方式">{[['link', '粘贴分享链接'], ['keyword', '关键词发现'], ['author', '作者主页']].map(([key, title]) => <button id={`dy-tab-${key}`} key={key} type="button" role="tab" aria-selected={inputMode === key} aria-controls="dy-discovery-form" className={inputMode === key ? 'active' : ''} onClick={() => setInputMode(key)}>{title}</button>)}</div>
      <form id="dy-discovery-form" className="dy-input-form" onSubmit={submit} aria-labelledby={`dy-tab-${inputMode}`}>
        {inputMode === 'link' ? <label>抖音分享文本、短链接或作品链接<textarea rows={3} maxLength={10000} value={text} onChange={e => setText(e.target.value)} placeholder="粘贴包含抖音链接的分享文本，支持直接作品链接与短链接" /></label>
          : <label>{inputMode === 'keyword' ? '查找实际使用 AI 的案例' : '公开作者主页'}<input type={inputMode === 'author' ? 'url' : 'text'} value={inputMode === 'keyword' ? query : authorUrl} maxLength={inputMode === 'keyword' ? 300 : 2000} onChange={e => inputMode === 'keyword' ? setQuery(e.target.value) : setAuthorUrl(e.target.value)} placeholder={inputMode === 'keyword' ? '例如：AI 自动整理客户反馈 实测' : 'https://www.douyin.com/user/…'} /></label>}
        <div className="dy-input-bottom"><p>{inputMode === 'link' ? '保存原始链接与处理状态，重复导入同一作品会复用记录。' : '登记后打开本机浏览器读取；本页不会自动控制抖音浏览器。登录、验证或限流时暂停该来源。'}</p><div>{inputMode !== 'link' && <label className="dy-limit">本批上限<select value={maxResults} onChange={e => setMaxResults(e.target.value)}>{[3, 5, 10].map(value => <option key={value} value={value}>{value} 条</option>)}</select></label>}<button className="primary" disabled={Boolean(busy) || !canSubmit}>{busy === 'import' ? '正在提交…' : inputMode === 'link' ? '登记链接' : '登记发现任务'}</button></div></div>
      </form>
    </section>
    {error && <div className="error" role="alert">{error}</div>}
    {notice && <div className="dy-notice" role="status">{notice}</div>}
    {reviewRun && <div className="dy-task"><StateBadge value={reviewRun.status} /><span>案例核验 · {reviewRun.run_id}</span><span>{reviewRun.error || reviewRun.events?.at(-1)?.action || '抽取、核验、评分与复审进度将自动刷新。'}</span></div>}
    {task?.status && <div className="dy-task"><StateBadge value={task.status} /><span>{task.reason || task.status_reason || task.message || '任务状态以浏览器读取和后续处理记录为准。'}</span>{task.discovered != null && <span>实际发现 {task.discovered} 条</span>}{safeLink(task.search_url) && <a href={safeLink(task.search_url)} target="_blank" rel="noopener noreferrer">打开抖音发现页面 ↗</a>}</div>}
    {tasks.length > 0 && <details className="dy-discovery-tasks"><summary>发现任务与续跑进度（{tasks.length}）</summary>{tasks.map(value => <div className="dy-discovery-task" key={value.task_id}><div><strong>{value.query || '作者主页发现'}</strong><StateBadge value={value.status} /></div><p>已记录 {value.progress?.item_ids?.length || 0} 个作品 · 已记录 {value.progress?.batches || 0} 批次 · 每批最多 {value.limit_count || 5} 条</p>{value.reason && <p>{value.reason}</p>}{value.progress?.last_checked_at && <small>上次检查：{formatDate(value.progress.last_checked_at)}</small>}{safeLink(value.search_url) && <a href={safeLink(value.search_url)} target="_blank" rel="noopener noreferrer">{value.kind === 'author' ? '打开作者主页' : '打开抖音搜索'} ↗</a>}<DiscoveryObservation task={value} busy={busy} mutate={mutate} /></div>)}</details>}
    {(sourceState?.paused || pausedCount > 0) && <div className="dy-pause-note"><p>{pausedCount > 0 ? `${pausedCount} 条作品` : '抖音来源'}因登录、验证或限流暂停。{sourceState?.reason || '请处理本机浏览器中的提示；其他来源可继续采集。'}</p><button type="button" className="secondary" disabled={Boolean(busy)} onClick={() => mutate('resume', () => post('/source/resume'))}>已处理浏览器提示，恢复待核对</button><small>此操作只恢复队列；需要在原站重新确认可读状态。</small></div>}
    {storage && <div className="dy-storage-note">本地素材 {storage.files || 0} 个 · 已用 {(Number(storage.used_bytes || 0) / 1024 / 1024).toFixed(1)} MiB / {(Number(storage.max_bytes || 0) / 1024 / 1024 / 1024).toFixed(1)} GiB · 单文件最多 {Math.round(Number(storage.max_upload_bytes || 0) / 1024 / 1024)} MiB。{storage.capabilities?.transcription_available ? '本地转写已就绪。' : '本地转写模型尚未就绪，可先导入带时间段的字幕。'}</div>}
    <div className="dy-library-head"><div><h3>抖音作品记录 <small>{items.length}</small></h3><p>原始线索与核验结果分别保留。选中作品可查看证据、素材和处理进度。</p></div><button type="button" className="secondary" disabled={Boolean(busy)} onClick={() => mutate('refresh', async () => ({}))}><RefreshCw size={14} aria-hidden="true" /> 刷新记录</button></div>
    <div className="dy-filters"><input aria-label="筛选抖音标题、作者或作品ID" placeholder="搜索标题、作者、作品 ID" value={search} onChange={e => setSearch(e.target.value)} /><select aria-label="处理状态" value={status} onChange={e => setStatus(e.target.value)}><option value="all">全部处理状态</option><option value="paused">登录 / 验证 / 限流暂停</option>{statusOptions.filter(key => key !== 'paused').map(key => <option key={key} value={key}>{label(key)}</option>)}</select><select aria-label="线索来源" value={source} onChange={e => setSource(e.target.value)}><option value="all">全部线索来源</option><option value="link">分享链接导入</option><option value="keyword">关键词发现</option><option value="author">作者主页发现</option></select></div>
    <div className="dy-workspace"><div className="dy-item-list" aria-label="抖音作品列表">
      {loading ? <div className="dy-empty">正在读取作品记录…</div> : filtered.length === 0 ? <div className="dy-empty">{items.length ? '此筛选下没有作品。' : '还没有抖音线索。从分享链接开始，逐条保留可追溯的证据。'}</div> : filtered.map(item => <button key={identity(item)} className={`dy-item ${currentId === identity(item) ? 'active' : ''}`} type="button" aria-pressed={currentId === identity(item)} onClick={() => openItem(identity(item))}><StateBadge value={item.status} /><strong>{item.title || '尚未读取作品标题'}</strong><span>{item.author_name || '作者待核实'}</span><small>发布：{formatDate(item.published_at, true)}</small><small className="dy-mono">ID {item.aweme_id || identity(item)}</small></button>)}
    </div><div className="dy-detail-area">{detail ? <ItemDetail key={identity(detail)} item={detail} busy={busy} mutate={mutate} /> : <div className="dy-empty dy-detail-empty"><FileVideo size={30} aria-hidden="true" /><p>{currentId ? '正在读取作品详情…' : '选择一条作品，查看原始记录与时间段证据。'}</p></div>}</div></div>
  </section>
}

function ItemDetail({ item, busy, mutate }) {
  const videoRef = useRef(null)
  const uploadRef = useRef(null)
  const [seekNote, setSeekNote] = useState('')
  const id = identity(item)
  const path = `/items/${encodeURIComponent(id)}`
  const originalUrl = safeLink(item.canonical_url || item.source_url)
  const segments = item.segments || item.evidence || []
  const events = item.events || item.stage_events || []
  const hasMedia = Boolean(item.media_available || item.media?.available || item.media?.status === 'ready')
  const mediaKind = item.media?.kind || item.media_type || ''
  const isImage = mediaKind === 'image' || String(item.media?.mime_type || '').startsWith('image/')
  const stats = item.stats || item.metrics || {}
  function locate(segment) {
    const start = segment.start ?? segment.start_seconds
    if (videoRef.current && Number.isFinite(Number(start))) {
      videoRef.current.currentTime = Number(start)
      videoRef.current.play().catch(() => {})
      videoRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setSeekNote(`本地视频已定位至 ${timestamp(start)}。`)
    } else setSeekNote(`请打开原视频，手动定位至 ${timestamp(start)}${(segment.end ?? segment.end_seconds) != null ? `–${timestamp(segment.end ?? segment.end_seconds)}` : ''} 核对。`)
  }
  async function upload(event) {
    const file = event.target.files?.[0]
    if (!file) return
    event.target.value = ''
    await mutate('upload', () => request(`${path}/media?filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file }))
  }
  return <article className="dy-detail">
    <div className="dy-detail-top"><StateBadge value={item.status} />{item.case_status && <StateBadge value={item.case_status} />}</div>
    <h3>{item.title || '作品标题待浏览器读取'}</h3>
    {originalUrl && <a className="dy-original" href={originalUrl} target="_blank" rel="noopener noreferrer">打开抖音原作品 <ExternalLink size={13} aria-hidden="true" /></a>}
    <dl className="dy-facts"><div><dt>作品 ID</dt><dd className="dy-mono">{item.aweme_id || id}</dd></div><div><dt>作者</dt><dd>{item.author_name || '未知，等待读取'}</dd></div><div><dt>发布时间</dt><dd>{formatDate(item.published_at, true)}</dd></div><div><dt>日期依据</dt><dd>{item.date_source || item.published_at_source || '尚无可核实的原始日期'}</dd></div><div><dt>采集时间</dt><dd>{formatDate(item.collected_at || item.discovered_at || item.created_at)}</dd></div><div><dt>发现方式</dt><dd>{item.query ? `关键词：${item.query}` : item.author_url ? '作者主页' : '分享链接导入'}</dd></div>{item.author_url && <div className="dy-full"><dt>来源账号</dt><dd>{safeLink(item.author_url) ? <a href={safeLink(item.author_url)} target="_blank" rel="noopener noreferrer">查看作者主页 ↗</a> : item.author_url}</dd></div>}</dl>
    <ObservationInput item={item} path={path} mutate={mutate} busy={busy} />
    {(item.status_reason || item.reason || item.last_error) && <div className={`dy-reason ${pausedStates.has(item.status) ? 'paused' : ''}`}><strong>{pausedStates.has(item.status) ? '当前来源已暂停' : '当前处理说明'}</strong><p>{item.status_reason || item.reason || item.last_error}</p></div>}
    {Object.keys(stats).length > 0 && <div className="dy-stats">{[['like_count', '点赞'], ['digg_count', '点赞'], ['comment_count', '评论'], ['share_count', '分享']].filter(([key]) => stats[key] != null && !(key === 'digg_count' && stats.like_count != null)).map(([key, title]) => <span key={key}>{title}：{stats[key]}</span>)}<small>观测于 {formatDate(item.stats_observed_at || stats.observed_at)} · 互动数据不计入质量分</small></div>}
    <section className="dy-section"><div className="dy-section-heading"><h4>素材与转写</h4><span>{hasMedia ? '本地素材已保存' : '尚无本地素材'}</span></div>
      {hasMedia ? (isImage ? <img className="dy-local-image" src={`${API}${ROOT}${path}/media`} alt="用户导入的作品画面，请结合来源与时间位置核对" /> : <video key={id} ref={videoRef} controls preload="metadata" src={`${API}${ROOT}${path}/media`} aria-label="本地抖音视频" />) : <p className="dy-help">无法取得素材时，可导入你已合法取得的视频或音频。没有素材与转写时，不将标题当作全文证据。</p>}
      <input ref={uploadRef} className="dy-hidden-input" aria-hidden="true" tabIndex={-1} type="file" accept="video/*,audio/*,image/*" onChange={upload} aria-label="导入本地素材" disabled={Boolean(busy)} />
      <div className="dy-actions"><button type="button" className="secondary" disabled={Boolean(busy)} onClick={() => uploadRef.current?.click()}><Upload size={14} aria-hidden="true" /> {busy === 'upload' ? '导入中…' : '导入本地素材'}</button><button type="button" className="secondary" disabled={Boolean(busy) || !hasMedia || isImage} onClick={() => mutate('process', () => post(`${path}/process`))}>{busy === 'process' ? '转写中…' : '本地语音转写'}</button><button type="button" className="secondary" disabled={Boolean(busy)} onClick={() => mutate('review', () => post(`${path}/review`))}>{busy === 'review' ? '核验中…' : '提交核验与评分'}</button></div>
      <p className="dy-help">机器转写和画面文字识别均可能有误。作者口述、可见演示与外部印证分别记录；没有结果依据时保留“证据不足”。</p>
    </section>
    <section className="dy-section"><div className="dy-section-heading"><h4>时间段证据</h4><span>{segments.length} 条</span></div>
      {!segments.length && <p className="dy-help">还没有带时间位置的证据。本地转写或人工录入后，会在这里显示；画面内容需人工核对与记录。</p>}
      {seekNote && <p className="dy-seek-note" role="status">{seekNote} {!hasMedia && originalUrl && <a href={originalUrl} target="_blank" rel="noopener noreferrer">打开原视频 ↗</a>}</p>}
      <div className="dy-segments">{segments.map((segment, index) => <div className="dy-segment" key={segment.id || index}><div><button type="button" className="dy-time" onClick={() => locate(segment)} title={hasMedia ? '定位本地素材' : '显示原视频核对时间'}>{timestamp(segment.start ?? segment.start_seconds)}{(segment.end ?? segment.end_seconds) != null && `–${timestamp(segment.end ?? segment.end_seconds)}`}</button><span className="dy-kind">{KINDS[segment.kind] || segment.kind || '类型未标注'} · {ROLES[segment.role] || '背景'}{segment.verified ? ' · 已人工核对' : ''}</span></div><p>{segment.text || segment.content}</p>{segment.confidence != null && <small>识别置信度：{Number.isFinite(Number(segment.confidence)) ? `${Math.round(Number(segment.confidence) * 100)}%` : segment.confidence}（不代表事实已核实）</small>}{(segment.source || segment.provenance || segment.observation_method) && <small>依据：{segment.source || segment.provenance || segment.observation_method}</small>}{safeLink(segment.source_url) && <a href={safeLink(segment.source_url)} target="_blank" rel="noopener noreferrer">{segment.kind === 'external_corroboration' ? '查看外部印证' : '查看原视频出处'} ↗</a>}</div>)}</div>
      <EvidenceInput path={path} mutate={mutate} busy={busy} />
    </section>
    <section className="dy-section"><div className="dy-section-heading"><h4>阶段记录</h4><span>{events.length} 次</span></div>{!events.length ? <p className="dy-help">尚无处理事件。</p> : <ol className="dy-events">{events.map((event, index) => <li key={event.event_id || index}><div><strong>{STAGES[event.stage] || event.stage || event.action || '处理事件'}</strong><StateBadge value={event.status} />{event.duration_ms != null && <span>{(Number(event.duration_ms) / 1000).toFixed(2)} 秒</span>}</div><p>{event.reason || event.message || event.error || event.action}</p><small>{formatDate(event.at || event.created_at)}</small></li>)}</ol>}</section>
    {item.case_id && <div className="dy-case-note">案例记录：<span className="dy-mono">{item.case_id}</span>{item.score != null && ` · 质量分 ${item.score} / 100`}。核验和复审完成后，符合门槛的案例会进入案例库。</div>}
  </article>
}

function ObservationInput({ item, path, mutate, busy }) {
  const [canonical, setCanonical] = useState(item.aweme_id ? item.canonical_url || '' : '')
  const [title, setTitle] = useState(item.title || '')
  const [author, setAuthor] = useState(item.author_name || item.author || '')
  const [published, setPublished] = useState(item.published_at?.slice(0, 10) || '')
  const [dateSource, setDateSource] = useState(item.published_at_source || '')
  const [dateEvidence, setDateEvidence] = useState(item.published_at_evidence || '')
  const [status, setStatus] = useState('')
  const [reason, setReason] = useState('')
  async function save(event) {
    event.preventDefault()
    await mutate('observation', () => post(`${path}/observation`, {
      title: title.trim(), author: author.trim(), observation_method: 'manual_browser_observation',
      ...(canonical.trim() ? { canonical_url: canonical.trim() } : {}),
      ...(published ? { published_at: published, published_at_source: dateSource.trim(), published_at_evidence: dateEvidence.trim() } : {}),
      ...(status ? { status, reason: reason.trim() } : {}),
    }))
  }
  return <details className="dy-evidence-input"><summary>记录页面信息与访问状态</summary><p className="dy-help">打开原作品后，填写页面真实可见的信息。发布日期缺失时留空；不要使用今天或采集时间代替。</p><form onSubmit={save}>
    <label>浏览器打开后的作品链接<input type="url" value={canonical} maxLength={2000} onChange={e => setCanonical(e.target.value)} placeholder="短链接打开后，复制含作品 ID 的实际页面链接" /></label>
    <label>作品标题<input value={title} maxLength={2000} onChange={e => setTitle(e.target.value)} placeholder="抖音页面显示的原始标题" /></label>
    <label>作者<input value={author} maxLength={400} onChange={e => setAuthor(e.target.value)} placeholder="页面显示的作者名称" /></label>
    <label>原始发布日期<input type="date" value={published} onChange={e => setPublished(e.target.value)} /></label>
    {published && <><label>日期来源<input value={dateSource} required maxLength={200} onChange={e => setDateSource(e.target.value)} placeholder="例如：抖音作品页的发布时间" /></label><label>页面中的日期原文<input value={dateEvidence} required maxLength={400} onChange={e => setDateEvidence(e.target.value)} placeholder="例如：发布时间：2026-08-15 14:30" /></label></>}
    <label>访问状态<select value={status} onChange={e => setStatus(e.target.value)}><option value="">页面可读，按日期与证据判断状态</option><option value="waiting_login">等待登录</option><option value="manual_verification">等待人工验证</option><option value="rate_limited">限流暂停</option><option value="unavailable">作品已删除或不可用</option><option value="media_unavailable">作品可读但素材无法获取</option><option value="network_error">网络错误</option></select></label>
    {status && <label>页面提示或失败原因<textarea rows={2} value={reason} required maxLength={1000} onChange={e => setReason(e.target.value)} placeholder="如实填写页面提示，不包含账号或登录凭证" /></label>}
    <div className="dy-actions"><button type="submit" className="secondary" disabled={Boolean(busy)}>{busy === 'observation' ? '保存中…' : '保存页面观察'}</button></div>
  </form></details>
}

function EvidenceInput({ path, mutate, busy }) {
  const [kind, setKind] = useState('author_statement')
  const [role, setRole] = useState('context')
  const [verified, setVerified] = useState(false)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [text, setText] = useState('')
  const [source, setSource] = useState('')
  const [sourceUrl, setSourceUrl] = useState('')
  const [error, setError] = useState('')
  const srtRef = useRef(null)
  async function save(event) {
    event.preventDefault(); setError('')
    if (!Number.isFinite(Number(start)) || !Number.isFinite(Number(end)) || Number(end) < Number(start)) {
      setError('结束时间不能早于开始时间。'); return
    }
    if (kind === 'external_corroboration' && !safeLink(sourceUrl)) {
      setError('外部印证请填写可访问的 HTTP 或 HTTPS 来源链接。'); return
    }
    const ok = await mutate('evidence', () => post(`${path}/evidence`, { segments: [{ start: Number(start), end: Number(end), text: text.trim(), kind, role, verified: ['visible_frame', 'external_corroboration'].includes(kind) && verified, observation_method: source.trim(), ...(kind === 'external_corroboration' && sourceUrl.trim() ? { source_url: sourceUrl.trim() } : {}) }] }))
    if (ok) { setText(''); setStart(''); setEnd(''); setVerified(false) }
  }
  async function importSrt(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setError('')
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error('字幕文件请控制在 2 MB 以内。')
      if (!source.trim()) throw new Error('请先填写字幕来源和核对方式，再导入字幕。')
      if (!['author_statement', 'machine_transcript'].includes(kind)) throw new Error('字幕类型请选择“作者口述”或“机器转写”。')
      const body = (await file.text()).replace(/\r/g, '')
      const parseTime = value => {
        const match = value.trim().match(/^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$/)
        if (!match || Number(match[2]) > 59 || Number(match[3]) > 59) throw new Error('字幕时间格式需为 00:00:00,000。')
        return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]) + Number(match[4]) / 1000
      }
      const segments = body.trim().split(/\n\s*\n/).filter(Boolean).map(block => {
        const lines = block.split('\n')
        const timeIndex = lines.findIndex(line => line.includes('-->'))
        if (timeIndex < 0) throw new Error('字幕缺少时间段，请导入 UTF-8 编码的 SRT 文件。')
        const [from, to] = lines[timeIndex].split('-->')
        const begin = parseTime(from), finish = parseTime(to)
        const content = lines.slice(timeIndex + 1).join('\n').trim()
        if (!content || finish < begin) throw new Error('字幕中存在空文本或倒序时间段。')
        return { start: begin, end: finish, text: content, kind, role, verified: false, observation_method: `${source.trim()}；字幕文件：${file.name}` }
      })
      if (!segments.length || segments.length > 1000) throw new Error('每次字幕需包含 1–1000 个有效时间段。')
      await mutate('evidence', () => post(`${path}/evidence`, { segments }))
    } catch (e) { setError(e.message) }
  }
  return <details className="dy-evidence-input"><summary>补充字幕或人工核对证据</summary><p className="dy-help">只记录你在原视频中实际听到或看到的内容，并注明来源和时间。补充证据仍需核验，不会直接改变入选状态。</p><form onSubmit={save}>
    <label>证据类型<select value={kind} onChange={e => { setKind(e.target.value); setVerified(false) }}><option value="author_statement">作者口述（人工听写）</option><option value="machine_transcript">机器转写（可能识别错误）</option><option value="visible_frame">画面可见内容（人工观察）</option><option value="external_corroboration">外部印证</option></select></label>
    <label>这条证据支持什么<select value={role} onChange={e => setRole(e.target.value)}>{Object.entries(ROLES).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
    {['visible_frame', 'external_corroboration'].includes(kind) && <label className="dy-checkbox"><input type="checkbox" checked={verified} onChange={e => setVerified(e.target.checked)} />我已实际核对该画面或外部来源，确认它支持这里记录的内容</label>}
    <label>来源与核对方式<input value={source} maxLength={500} required onChange={e => setSource(e.target.value)} placeholder="例如：本人播放原视频逐句听写；或本地 Whisper 转写" /></label>
    {kind === 'external_corroboration' && <label>外部来源链接<input type="url" value={sourceUrl} required maxLength={2000} onChange={e => setSourceUrl(e.target.value)} placeholder="https://…" /></label>}
    <div className="dy-time-inputs"><label>开始时间（秒）<input type="number" required min="0" max="1800" step="0.001" value={start} onChange={e => setStart(e.target.value)} placeholder="0" /></label><label>结束时间（秒）<input type="number" required min="0" max="1800" step="0.001" value={end} onChange={e => setEnd(e.target.value)} placeholder="10" /></label></div>
    <label>证据原文<textarea rows={3} required maxLength={8000} value={text} onChange={e => setText(e.target.value)} placeholder="填写原话、画面中的文字或可见操作；不补写未展示的结果。" /></label>
    <div className="dy-actions"><button type="submit" className="secondary" disabled={Boolean(busy)}>{busy === 'evidence' ? '保存中…' : '保存这条证据'}</button><button type="button" className="secondary" disabled={Boolean(busy)} onClick={() => srtRef.current?.click()}>导入带时间段的 SRT 字幕</button></div>
    <input ref={srtRef} className="dy-hidden-input" aria-hidden="true" tabIndex={-1} type="file" accept=".srt" onChange={importSrt} aria-label="导入 SRT 字幕" disabled={Boolean(busy)} />
    <p className="dy-help">导入 SRT 时使用上方的证据类型与来源说明，文件需为 UTF-8 编码。</p>{error && <p className="dy-input-error" role="alert">{error}</p>}
  </form></details>
}

function DiscoveryObservation({ task, busy, mutate }) {
  const [links, setLinks] = useState('')
  const [status, setStatus] = useState('awaiting_browser')
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  async function save(event) {
    event.preventDefault(); setError('')
    const values = [...new Set(links.split(/\n/).map(value => value.trim()).filter(Boolean))]
    if (values.length > (task.limit_count || 5)) { setError(`本批最多记录 ${task.limit_count || 5} 条作品，请分批提交。`); return }
    if (values.some(value => !safeLink(value))) { setError('每行请填写一个完整作品链接，不要粘贴额外分享文案。'); return }
    const ok = await mutate('discovery-observation', () => post(`/discovery/tasks/${encodeURIComponent(task.task_id)}/observations`, {
      observations: values.map(canonical_url => ({ canonical_url, observation_method: 'manual_discovery_page' })), status, reason: reason.trim(),
    }))
    if (ok) setLinks('')
  }
  return <details className="dy-evidence-input"><summary>记录本批浏览器读取结果</summary><p className="dy-help">仅登记原站真实可见的作品。每行一个链接，最多 {task.limit_count || 5} 条；登录或验证页面没有作品时，留空并记录阻碍。</p><form onSubmit={save}>
    <label>本批作品链接<textarea rows={3} value={links} onChange={e => setLinks(e.target.value)} maxLength={20000} placeholder="https://www.douyin.com/video/…" /></label>
    <label>本批读取结果<select value={status} onChange={e => setStatus(e.target.value)}><option value="awaiting_browser">仍待浏览器读取</option><option value="completed">本批读取结束</option><option value="waiting_login">要求登录</option><option value="manual_verification">要求人工验证</option><option value="rate_limited">限流暂停</option><option value="network_error">网络错误</option><option value="unavailable">内容不可用</option></select></label>
    <label>页面提示或观察说明<textarea rows={2} value={reason} required maxLength={2000} onChange={e => setReason(e.target.value)} placeholder="说明实际读到了什么，或填写页面的登录、验证、限流提示" /></label>
    <div className="dy-actions"><button className="secondary" disabled={Boolean(busy)}>{busy === 'discovery-observation' ? '保存中…' : '保存本批观察'}</button></div>
    {error && <p className="dy-input-error" role="alert">{error}</p>}
  </form></details>
}

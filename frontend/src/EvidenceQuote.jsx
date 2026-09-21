import React, {useEffect, useRef, useState} from 'react'

const API = import.meta.env.VITE_API_BASE_URL || '/api'
const pending = new Map()
const translated = new Map()
const batches = new Map()
const keyFor = (caseId, quote) => JSON.stringify([caseId, quote])
const isChinese = text => {
  const han = (text.match(/[\u4e00-\u9fff]/g) || []).length
  return han > 0 && (text.match(/[A-Za-z]/g) || []).length <= han * 2
}

function requestTranslation(caseId, quote) {
  const key = keyFor(caseId, quote)
  if (translated.has(key)) return Promise.resolve(translated.get(key))
  if (pending.has(key)) return pending.get(key)
  const promise = new Promise((resolve, reject) => {
    if (!batches.has(caseId)) {
      const batch = []
      batches.set(caseId, batch)
      setTimeout(async () => {
        batches.delete(caseId)
        while (batch.length) {
          const group = []
          let length = 0
          while (batch.length && group.length < 8 && (length + batch[0].quote.length <= 24000 || !group.length)) {
            const entry = batch.shift(); group.push(entry); length += entry.quote.length
          }
          try {
            const response = await fetch(`${API}/v1/collection/items/${encodeURIComponent(caseId)}/evidence-translation`, {
              method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({quotes:group.map(entry => entry.quote)})
            })
            if (!response.ok) throw new Error('中文翻译暂时不可用')
            const data = await response.json()
            const values = new Map(data.items.map(item => [item.original, item.text_zh]))
            for (const entry of group) {
              const value = values.get(entry.quote)
              if (!value) throw new Error('中文译文不完整')
            }
            for (const entry of group) {
              const entryKey = keyFor(caseId, entry.quote)
              translated.set(entryKey, values.get(entry.quote)); pending.delete(entryKey)
              entry.resolve(values.get(entry.quote))
            }
          } catch (error) {
            for (const entry of group) { pending.delete(keyFor(caseId, entry.quote)); entry.reject(error) }
          }
        }
      }, 40)
    }
    batches.get(caseId).push({quote, resolve, reject})
  })
  pending.set(key, promise)
  return promise
}

export default function EvidenceQuote({caseId, quote = '', explanation = ''}) {
  const container = useRef(null)
  const [zh, setZh] = useState('')
  const [state, setState] = useState('waiting')
  const [retry, setRetry] = useState(0)
  const chinese = isChinese(quote)
  useEffect(() => {
    let canceled = false
    setZh(''); setState('waiting')
    if (chinese || !quote || !caseId) return
    const load = () => {
      setState('loading')
      requestTranslation(caseId, quote).then(value => {
        if (!canceled) { setZh(value); setState('ready') }
      }).catch(() => { if (!canceled) setState('error') })
    }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { observer.disconnect(); load() }
    }, {rootMargin:'160px'})
    observer.observe(container.current)
    return () => { canceled = true; observer.disconnect() }
  }, [caseId, quote, chinese, retry])
  if (!quote) return null
  if (chinese) return <div className="evidence-translation"><small>原文证据</small><blockquote lang="zh-CN">{quote}</blockquote></div>
  return <div className="evidence-translation" ref={container}>
    {zh ? <><small>中文译文</small><blockquote lang="zh-CN">{zh}</blockquote></> : <>
      {explanation && <><small>中文说明 · 已有案例摘要</small><p>{explanation}</p></>}
      <p className="translation-status" role="status">{state === 'error' ? '译文暂时未能加载，可稍后重试。' : caseId ? '正在准备中文译文…' : '此条证据暂未提供中文译文。'}</p>
      {state === 'error' && <button className="text-button" onClick={() => setRetry(value => value + 1)}>重试中文翻译</button>}
    </>}
    <details className="evidence-original"><summary>查看原文（核对用）</summary><blockquote>{quote}</blockquote></details>
    <small className="translation-note">中文译文辅助阅读，核验和评分以原文为准。</small>
  </div>
}

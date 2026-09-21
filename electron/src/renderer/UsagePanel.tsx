import { useEffect, useState } from 'react'
import { setOverlayDimmed } from './theme'

type GroupBy = 'model' | 'day' | 'session'

interface UsageGroup {
  key: string
  prompt_tokens: number
  completion_tokens: number
  n_calls: number
  cost: number | null
}

interface UsageReport {
  group_by: string
  groups: UsageGroup[]
  total: { prompt_tokens: number; completion_tokens: number; n_calls: number }
}

/** 用量成本面板（Phase 16）。group_by 可切换 model/day/session。 */
export default function UsagePanel({ onClose }: { onClose: () => void }) {
  const [groupBy, setGroupBy] = useState<GroupBy>('model')
  const [report, setReport] = useState<UsageReport | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    setLoading(true)
    window.kitty
      ?.getUsageReport({ groupBy })
      .then(r => { if (alive) setReport(r) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [groupBy])

  // 遮罩盖不住右上角的原生窗口控制按钮（titleBarOverlay 是系统层），
  // 打开时把 overlay 调成遮罩混合色让它"沉下去"，关闭时还原主题配色。
  useEffect(() => {
    setOverlayDimmed(true)
    return () => setOverlayDimmed(false)
  }, [])

  function fmt(n: number): string {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M'
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k'
    return String(n)
  }

  function fmtUsd(v: number | null): string {
    if (v === null) return '—'
    if (v === 0) return '$0.00'
    if (v < 0.01) return '<$0.01'
    return '$' + v.toFixed(2)
  }

  const tabs: Array<{ key: GroupBy; label: string }> = [
    { key: 'model', label: '按模型' },
    { key: 'day', label: '按日' },
    { key: 'session', label: '按会话' },
  ]

  return (
    <div className="usage-overlay" onClick={onClose}>
      <div className="usage-panel" onClick={e => e.stopPropagation()}>
        <div className="usage-head">
          <span className="usage-title">用量与成本</span>
          <button className="usage-close" onClick={onClose}>×</button>
        </div>

        <div className="usage-tabs">
          {tabs.map(t => (
            <button
              key={t.key}
              className={`usage-tab${groupBy === t.key ? ' active' : ''}`}
              onClick={() => setGroupBy(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {loading || !report ? (
          <div className="usage-empty">{loading ? '加载中…' : '暂无用量数据'}</div>
        ) : report.groups.length === 0 ? (
          <div className="usage-empty">暂无用量数据（自 v7 起统计）</div>
        ) : (
          <>
            <div className="usage-list">
              {report.groups.map(g => (
                <div key={g.key} className="usage-row">
                  <div className="usage-row-key" title={g.key}>{g.key}</div>
                  <div className="usage-row-meta">
                    <span className="usage-tok">{fmt(g.prompt_tokens)} in</span>
                    <span className="usage-tok">{fmt(g.completion_tokens)} out</span>
                    <span className="usage-calls">{g.n_calls} 次</span>
                  </div>
                  <div className="usage-cost">{fmtUsd(g.cost)}</div>
                </div>
              ))}
            </div>
            <div className="usage-total">
              <span>合计</span>
              <span className="usage-total-tok">
                {fmt(report.total.prompt_tokens)} in · {fmt(report.total.completion_tokens)} out
              </span>
              <span>{report.total.n_calls} 次调用</span>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

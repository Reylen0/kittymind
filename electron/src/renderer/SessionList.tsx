import { useState, useRef, useEffect } from 'react'
import type { Session } from './types'

interface Props {
  sessions: Session[]
  currentId: string | null
  sidebarOpen: boolean
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
  onToggle: () => void
}

function IcoPanel() {
  return (
    <svg width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <rect x="1" y="1.5" width="13" height="12" rx="2"/>
      <line x1="5" y1="1.5" x2="5" y2="13.5"/>
    </svg>
  )
}

function IcoSearch() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <circle cx="6" cy="6" r="4"/>
      <line x1="9.2" y1="9.2" x2="13" y2="13"/>
    </svg>
  )
}

function IcoPlus() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <circle cx="7" cy="7" r="5.5"/>
      <line x1="7" y1="4.5" x2="7" y2="9.5"/>
      <line x1="4.5" y1="7" x2="9.5" y2="7"/>
    </svg>
  )
}

export default function SessionList({
  sessions, currentId, sidebarOpen,
  onSelect, onCreate, onDelete, onToggle,
}: Props) {
  const [searching, setSearching] = useState(false)
  const [query, setQuery]         = useState('')
  const searchRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (searching) searchRef.current?.focus()
    else           setQuery('')
  }, [searching])

  const filtered = query.trim()
    ? sessions.filter(s => (s.title || '新会话').toLowerCase().includes(query.toLowerCase()))
    : sessions

  // ── Collapsed state ────────────────────────────────────────────────────────
  if (!sidebarOpen) {
    return (
      <div className="sidebar-collapsed">
        <button className="sidebar-icon-btn" onClick={onToggle} title="展开侧边栏">
          <IcoPanel />
        </button>
        <button className="sidebar-icon-btn" onClick={onCreate} title="新建会话">
          <IcoPlus />
        </button>
      </div>
    )
  }

  // ── Open state ─────────────────────────────────────────────────────────────
  return (
    <div className="session-list">
      <div className="sidebar-header">
        <span className="sidebar-version">版本 v0.1.0</span>
        <div className="sidebar-header-btns">
          <button
            className={`sidebar-icon-btn${searching ? ' active' : ''}`}
            onClick={() => setSearching(v => !v)}
            title="搜索会话"
          >
            <IcoSearch />
          </button>
          <button className="sidebar-icon-btn" onClick={onToggle} title="收起侧边栏">
            <IcoPanel />
          </button>
        </div>
      </div>

      {searching && (
        <div className="sidebar-search">
          <input
            ref={searchRef}
            className="sidebar-search-input"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => e.key === 'Escape' && setSearching(false)}
            placeholder="搜索会话名称…"
          />
        </div>
      )}

      <div className="session-new">
        <button className="btn-new-text" onClick={onCreate}>
          <span>新对话</span>
        </button>
      </div>

      <div className="session-section-label">对话</div>

      <div className="session-items">
        {filtered.map(s => (
          <div
            key={s.id}
            className={`session-item${s.id === currentId ? ' active' : ''}`}
            onClick={() => onSelect(s.id)}
          >
            <span className="session-title">{s.title || '新会话'}</span>
            <button
              className="session-delete"
              title="删除"
              onClick={e => { e.stopPropagation(); onDelete(s.id) }}
            >×</button>
          </div>
        ))}
        {filtered.length === 0 && (
          <div className="session-empty">
            {query ? '无匹配会话' : '无历史会话'}
          </div>
        )}
      </div>
    </div>
  )
}

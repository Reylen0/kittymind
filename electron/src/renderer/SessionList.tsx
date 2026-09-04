import { useState, useRef, useEffect } from 'react'
import type { Session, Workspace } from './types'

interface Props {
  sessions:    Session[]
  workspaces:  Workspace[]
  currentId:   string | null
  sidebarOpen: boolean
  onSelect:    (id: string) => void
  onCreate:    () => void
  onDelete:    (id: string) => void
  onToggle:    () => void
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
function IcoChevronDown() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="2,4 6,8 10,4"/>
    </svg>
  )
}
function IcoChevronRight() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4,2 8,6 4,10"/>
    </svg>
  )
}
function IcoFolder() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 3.5C1 2.67 1.67 2 2.5 2H5l1.5 1.5H11.5C12.33 3.5 13 4.17 13 5v5.5C13 11.33 12.33 12 11.5 12h-9C1.67 12 1 11.33 1 10.5V3.5z"/>
    </svg>
  )
}

function SessionItem({ s, currentId, onSelect, onDelete }: {
  s: Session; currentId: string | null
  onSelect: (id: string) => void; onDelete: (id: string) => void
}) {
  return (
    <div
      className={`session-item${s.id === currentId ? ' active' : ''}`}
      onClick={() => onSelect(s.id)}
    >
      <span className="session-title">{s.title || '新对话'}</span>
      <button className="session-delete" title="删除"
        onClick={e => { e.stopPropagation(); onDelete(s.id) }}>×</button>
    </div>
  )
}

export default function SessionList({
  sessions, workspaces, currentId, sidebarOpen,
  onSelect, onCreate, onDelete, onToggle,
}: Props) {
  const [searching,         setSearching]         = useState(false)
  const [query,             setQuery]             = useState('')
  const [collapsedSpaces,   setCollapsedSpaces]   = useState<Set<string>>(new Set())
  const [dialogCollapsed,   setDialogCollapsed]   = useState(false)
  const [spacesCollapsed,   setSpacesCollapsed]   = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (searching) searchRef.current?.focus()
    else           setQuery('')
  }, [searching])

  const freeSessions = sessions.filter(s => !s.workspace_id)
  const filteredFree = query.trim()
    ? freeSessions.filter(s => (s.title || '新对话').toLowerCase().includes(query.toLowerCase()))
    : freeSessions

  function toggleSpace(id: string) {
    setCollapsedSpaces(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  // ── Collapsed sidebar ──────────────────────────────────────────
  if (!sidebarOpen) {
    return (
      <div className="sidebar-collapsed">
        <button className="sidebar-icon-btn" onClick={onToggle} title="展开侧边栏"><IcoPanel /></button>
        <button className="sidebar-icon-btn" onClick={onCreate} title="新建对话"><IcoPlus /></button>
      </div>
    )
  }

  // ── Open sidebar ───────────────────────────────────────────────
  return (
    <div className="session-list">
      <div className="sidebar-header">
        <span className="sidebar-version">版本 v0.1.0</span>
        <div className="sidebar-header-btns">
          <button
            className={`sidebar-icon-btn${searching ? ' active' : ''}`}
            onClick={() => setSearching(v => !v)} title="搜索对话"
          ><IcoSearch /></button>
          <button className="sidebar-icon-btn" onClick={onToggle} title="收起侧边栏"><IcoPanel /></button>
        </div>
      </div>

      {searching && (
        <div className="sidebar-search">
          <input
            ref={searchRef} className="sidebar-search-input"
            value={query} onChange={e => setQuery(e.target.value)}
            onKeyDown={e => e.key === 'Escape' && setSearching(false)}
            placeholder="搜索对话名称…"
          />
        </div>
      )}

      <div className="session-new">
        <button className="btn-new-text" onClick={onCreate}>新对话</button>
      </div>

      <div className="session-scroll">
      {/* ── 对话 section（无工作区） ── */}
      {(!query || filteredFree.length > 0) && (
        <>
          <button
            className="session-section-label collapsible"
            onClick={() => setDialogCollapsed(v => !v)}
          >
            <span>对话{freeSessions.length > 0 ? ` (${freeSessions.length})` : ''}</span>
            <span className="section-arrow">{dialogCollapsed ? <IcoChevronRight /> : <IcoChevronDown />}</span>
          </button>
          {!dialogCollapsed && (
            <div className="session-items">
              {filteredFree.map(s => (
                <SessionItem key={s.id} s={s} currentId={currentId} onSelect={onSelect} onDelete={onDelete} />
              ))}
              {filteredFree.length === 0 && !query && (
                <div className="session-empty">无历史对话</div>
              )}
            </div>
          )}
        </>
      )}

      {/* ── 空间 section（有工作区） ── */}
      {workspaces.length > 0 && (
        <>
          <button
            className="session-section-label collapsible"
            onClick={() => setSpacesCollapsed(v => !v)}
          >
            <span>工作区 ({workspaces.length})</span>
            <span className="section-arrow">{spacesCollapsed ? <IcoChevronRight /> : <IcoChevronDown />}</span>
          </button>
          {!spacesCollapsed && workspaces.map(ws => {
            const wsSessions = sessions.filter(s => s.workspace_id === ws.id)
            const filteredWs = query.trim()
              ? wsSessions.filter(s => (s.title || '新对话').toLowerCase().includes(query.toLowerCase()))
              : wsSessions
            if (query && filteredWs.length === 0) return null
            const collapsed = collapsedSpaces.has(ws.id)
            return (
              <div key={ws.id} className="workspace-group">
                <button className="workspace-group-header" onClick={() => toggleSpace(ws.id)}>
                  <IcoFolder />
                  <span className="workspace-group-name">{ws.name}</span>
                  <span className="workspace-group-arrow">{collapsed ? <IcoChevronRight /> : <IcoChevronDown />}</span>
                </button>
                {!collapsed && (
                  <div className="workspace-group-items">
                    {filteredWs.map(s => (
                      <SessionItem key={s.id} s={s} currentId={currentId} onSelect={onSelect} onDelete={onDelete} />
                    ))}
                    {filteredWs.length === 0 && (
                      <div className="session-empty">暂无对话</div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}
      </div>  {/* /session-scroll */}
    </div>
  )
}

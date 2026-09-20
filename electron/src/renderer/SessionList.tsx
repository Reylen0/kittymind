import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import type { Session, Workspace } from './types'
import { getTheme, toggleTheme, type Theme } from './theme'

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

function IcoGear() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}

function IcoMoon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 8.5A5.5 5.5 0 0 1 5.5 2a5.5 5.5 0 1 0 6.5 6.5z"/>
    </svg>
  )
}

function IcoSun() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
      <circle cx="7" cy="7" r="2.6"/>
      <line x1="7" y1="0.8" x2="7" y2="2.2"/><line x1="7" y1="11.8" x2="7" y2="13.2"/>
      <line x1="0.8" y1="7" x2="2.2" y2="7"/><line x1="11.8" y1="7" x2="13.2" y2="7"/>
      <line x1="2.6" y1="2.6" x2="3.6" y2="3.6"/><line x1="10.4" y1="10.4" x2="11.4" y2="11.4"/>
      <line x1="2.6" y1="11.4" x2="3.6" y2="10.4"/><line x1="10.4" y1="3.6" x2="11.4" y2="2.6"/>
    </svg>
  )
}

/** 在侧栏里按 id 找到会话项（用数据集比对，避开 id 需转义的问题）。 */
function findSessionItem(box: HTMLElement, id: string): HTMLElement | undefined {
  return Array.from(box.querySelectorAll<HTMLElement>('[data-sid]'))
    .find(n => n.dataset.sid === id)
}

/** 把会话项滚进 box 的可视区（只改 box.scrollTop）。已完全可见时不做任何事。 */
function revealSessionItem(box: HTMLElement, el: HTMLElement) {
  const MARGIN = 6
  const r = el.getBoundingClientRect()
  const c = box.getBoundingClientRect()
  if (r.top < c.top)            box.scrollTop += r.top - c.top - MARGIN
  else if (r.bottom > c.bottom) box.scrollTop += r.bottom - c.bottom + MARGIN
}

function SessionItem({ s, currentId, onSelect, onDelete }: {
  s: Session; currentId: string | null
  onSelect: (id: string) => void; onDelete: (id: string) => void
}) {
  return (
    <div
      className={`session-item${s.id === currentId ? ' active' : ''}`}
      data-sid={s.id}
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
  // 记录「已手动展开」的工作区：默认空集合 = 每个工作区下的对话都收起。
  // 用展开集而非收起集，workspaces 异步到达时新工作区天然是收起态，无需同步。
  const [expandedSpaces,    setExpandedSpaces]    = useState<Set<string>>(new Set())
  const [dialogCollapsed,   setDialogCollapsed]   = useState(true)   // 「对话」「工作区」默认折叠
  const [spacesCollapsed,   setSpacesCollapsed]   = useState(true)
  const [theme,             setTheme]             = useState<Theme>(getTheme())
  const searchRef = useRef<HTMLInputElement>(null)

  // ── 定位当前会话 ────────────────────────────────────────────────
  // 侧栏两级默认全收起，当前会话很可能藏在折叠的工作区里。会话切换（含启动时
  // 自动选中最近一条）时展开它所在的那一条路径，然后把它滚进可视区——保证任何
  // 时候左侧都能看出「中间正在看哪个会话」。
  // locatedRef 保证每个 currentId 只自动展开一次：用户随后手动收起分组、
  // 或会话列表因标题更新而刷新时，都不会再被强行展开（只有切会话才会重新定位）。
  const scrollRef  = useRef<HTMLDivElement>(null)
  const locatedRef = useRef<string | null>(null)
  const pendingRef = useRef<string | null>(null)   // 等待滚入视野的会话 id

  useLayoutEffect(() => {
    pendingRef.current = null                    // 上一次遗留的待定位作废
    if (!currentId || locatedRef.current === currentId) return
    const s = sessions.find(x => x.id === currentId)
    if (!s) return                               // 新建但尚未落库的会话：列表里还没有它
    locatedRef.current = currentId
    pendingRef.current = currentId
    if (s.workspace_id) {
      setSpacesCollapsed(false)
      setExpandedSpaces(prev => prev.has(s.workspace_id!)
        ? prev
        : new Set(prev).add(s.workspace_id!))
    } else {
      setDialogCollapsed(false)
    }
  }, [currentId, sessions])

  // 目标进入 DOM 后再滚动（工作区列表异步到达、或刚由上面展开，会晚一两次渲染）。
  // 只动 .session-scroll 自己的 scrollTop：用 rect 差值而不是 scrollIntoView，
  // 免得带动外层容器，也免得命中那个 overflow:auto 的 .session-items。
  useLayoutEffect(() => {
    const id  = pendingRef.current
    const box = scrollRef.current
    if (!id || !box) return
    const el = findSessionItem(box, id)
    if (!el) return                              // 还没进 DOM：下一次渲染再试
    revealSessionItem(box, el)
    if (document.fonts?.status === 'loaded') {
      pendingRef.current = null
      return
    }
    // 网络字体（Noto Sans SC）就绪后行高会变，上面那次按「加载中」行高算的落点
    // 会差十几像素（实测 scrollHeight 884→913）。等字体就绪再校正一次。
    document.fonts.ready.then(() => {
      if (pendingRef.current !== id) return      // 已被新的定位或用户操作取代
      const b = scrollRef.current
      const e = b ? findSessionItem(b, id) : undefined
      if (b && e) revealSessionItem(b, e)
      pendingRef.current = null
    })
  })

  useEffect(() => {
    if (searching) searchRef.current?.focus()
    else           setQuery('')
  }, [searching])

  const freeSessions = sessions.filter(s => !s.workspace_id)
  const filteredFree = query.trim()
    ? freeSessions.filter(s => (s.title || '新对话').toLowerCase().includes(query.toLowerCase()))
    : freeSessions

  function toggleSpace(id: string) {
    setExpandedSpaces(prev => {
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
        <button
          className="sidebar-icon-btn"
          onClick={() => setTheme(toggleTheme())}
          title={theme === 'dark' ? '切换浅色模式' : '切换深色模式'}
        >{theme === 'dark' ? <IcoSun /> : <IcoMoon />}</button>
      </div>
    )
  }

  // ── Open sidebar ───────────────────────────────────────────────
  return (
    <div className="session-list">
      <div className="sidebar-header">
        <span className="sidebar-title">版本 v0.1.0</span>
        <div className="sidebar-header-btns">
          <button
            className={`sidebar-icon-btn${searching ? ' active' : ''}`}
            onClick={() => setSearching(v => !v)} title="搜索对话"
          ><IcoSearch /></button>
          <button
            className="sidebar-icon-btn"
            onClick={() => setTheme(toggleTheme())}
            title={theme === 'dark' ? '切换浅色模式' : '切换深色模式'}
          >{theme === 'dark' ? <IcoSun /> : <IcoMoon />}</button>
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
        <button className="btn-new-text" onClick={onCreate}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
            <line x1="7" y1="2.5" x2="7" y2="11.5" /><line x1="2.5" y1="7" x2="11.5" y2="7" />
          </svg>
          <span>新对话</span>
        </button>
      </div>

      <div
        className="session-scroll"
        ref={scrollRef}
        // 用户自己动这个列表（滚轮 / 拖滚动条 / 触摸）→ 放弃待定位，
        // 否则目标稍后渲染出来会把列表拽走。用这两类事件而不是 onScroll：
        // 定位本身会程序化改 scrollTop，onScroll 会把自己的定位取消掉。
        onWheel={() => { pendingRef.current = null }}
        onPointerDown={() => { pendingRef.current = null }}
      >
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
            const collapsed = !expandedSpaces.has(ws.id)
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

      <div className="sidebar-footer">
        <button className="footer-settings-btn" title="设置">
          <IcoGear />
          <span>设置</span>
        </button>
      </div>
    </div>
  )
}

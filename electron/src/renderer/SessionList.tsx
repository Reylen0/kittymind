import { useState, useRef, useEffect, useLayoutEffect, type ReactNode } from 'react'
import type { SearchGroup, Session, Workspace } from './types'
import { getTheme, toggleTheme, type Theme } from './theme'

interface Props {
  sessions:    Session[]
  workspaces:  Workspace[]
  currentId:   string | null
  sidebarOpen: boolean
  onSelect:    (id: string) => void
  onCreate:    () => void
  onDelete:    (id: string) => void
  onDeleteWorkspace: (id: string) => void
  onToggle:    () => void
}

/** 删除确认弹窗的目标：会话直接删；工作区删除后其会话移回「对话」分组 */
type ConfirmTarget = { kind: 'session' | 'workspace'; id: string; name: string }

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
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="2,4 6,8 10,4"/>
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

function IcoTrash() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <line x1="1.5" y1="3.5" x2="12.5" y2="3.5"/>
      <path d="M5.5 1.5h3"/>
      <path d="M2.8 3.5l.6 8a1.3 1.3 0 0 0 1.3 1.2h4.6a1.3 1.3 0 0 0 1.3-1.2l.6-8"/>
      <line x1="5.6" y1="6.2" x2="5.6" y2="10.2"/>
      <line x1="8.4" y1="6.2" x2="8.4" y2="10.2"/>
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

function SessionItem({ s, currentId, onSelect, onAskDelete }: {
  s: Session; currentId: string | null
  onSelect: (id: string) => void; onAskDelete: (s: Session) => void
}) {
  return (
    <div
      className={`session-item${s.id === currentId ? ' active' : ''}`}
      data-sid={s.id}
      onClick={() => onSelect(s.id)}
    >
      <span className="session-title">{s.title || '新对话'}</span>
      {/* 悬浮才出现的删除按钮：点击先弹确认，不直接删 */}
      <button className="session-delete" title="删除" aria-label={`删除对话 ${s.title || '新对话'}`}
        onClick={e => { e.stopPropagation(); onAskDelete(s) }}>
        <IcoTrash />
      </button>
    </div>
  )
}

/** 按 marks 把片段切成普通文字 + <mark>。
 *
 * 刻意用节点数组而不是 dangerouslySetInnerHTML：片段内容来自历史消息，
 * 里面什么字符都可能有，全项目零 innerHTML 的姿态在这里尤其不该破例。
 * marks 由后端保证已排序且互不重叠。
 */
function Highlight({ text, marks }: { text: string; marks: Array<[number, number]> }) {
  if (!marks.length) return <>{text}</>
  const parts: ReactNode[] = []
  let at = 0
  marks.forEach(([start, end], i) => {
    if (start > at) parts.push(text.slice(at, start))
    parts.push(<mark key={i}>{text.slice(start, end)}</mark>)
    at = end
  })
  if (at < text.length) parts.push(text.slice(at))
  return <>{parts}</>
}

export default function SessionList({
  sessions, workspaces, currentId, sidebarOpen,
  onSelect, onCreate, onDelete, onDeleteWorkspace, onToggle,
}: Props) {
  const [searching,         setSearching]         = useState(false)
  const [query,             setQuery]             = useState('')
  // 记录「已手动展开」的工作区：默认空集合 = 每个工作区下的对话都收起。
  // 用展开集而非收起集，workspaces 异步到达时新工作区天然是收起态，无需同步。
  const [expandedSpaces,    setExpandedSpaces]    = useState<Set<string>>(new Set())
  const [dialogCollapsed,   setDialogCollapsed]   = useState(true)   // 「对话」「工作区」默认折叠
  const [spacesCollapsed,   setSpacesCollapsed]   = useState(true)
  const [theme,             setTheme]             = useState<Theme>(getTheme())
  const [hits,              setHits]              = useState<SearchGroup[]>([])
  const [searchBusy,        setSearchBusy]        = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const reqRef    = useRef(0)      // 请求序号：丢弃过期响应，见下面的防抖 effect

  // 删除确认弹窗：任何删除（会话/工作区）都先到这里，确认后才执行
  const [confirmTarget, setConfirmTarget] = useState<ConfirmTarget | null>(null)

  // 弹窗期间 Escape 关闭（挂 window 监听，避免 div 聚焦问题）
  useEffect(() => {
    if (!confirmTarget) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setConfirmTarget(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [confirmTarget])

  function handleConfirmDelete() {
    if (!confirmTarget) return
    if (confirmTarget.kind === 'session') onDelete(confirmTarget.id)
    else                                  onDeleteWorkspace(confirmTarget.id)
    setConfirmTarget(null)
  }

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

  // ── 全文搜索（防抖打后端）────────────────────────────────────
  // 标题过滤是本地即时的（下面的 filteredFree），这里只管内容检索。
  // 退出搜索态时上面那个 effect 会把 query 清空，本 effect 随即清掉结果。
  useEffect(() => {
    const q = query.trim()
    if (!q) {
      setHits([])
      setSearchBusy(false)
      return
    }
    setSearchBusy(true)
    const timer = setTimeout(async () => {
      // 序号在真正发请求时才递增，被防抖掐掉的那些不占号
      const seq = ++reqRef.current
      let groups: SearchGroup[] = []
      try {
        const r = await window.kitty?.searchSessions(q)
        if (Array.isArray(r)) groups = r
      } catch {
        /* 未连接 / RPC 超时：按无结果处理，不打断输入 */
      }
      // 快速连续输入时响应可能乱序返回，只认最后发出的那次
      if (seq !== reqRef.current) return
      setHits(groups)
      setSearchBusy(false)
    }, 250)
    return () => clearTimeout(timer)
  }, [query])

  const freeSessions = sessions.filter(s => !s.workspace_id)
  const filteredFree = query.trim()
    ? freeSessions.filter(s => (s.title || '新对话').toLowerCase().includes(query.toLowerCase()))
    : freeSessions
  const hitCount = hits.reduce((n, g) => n + g.hits.length, 0)
  // 工作区区块是否至少有一个标题命中——与「对话」区块用同一套判断，搜索时
  // 没有任何标题匹配就整体隐藏区块，而不是留一个展开了却空空如也的标题行
  // （原来只在 workspaces.map 内部逐组过滤，标题行自己不受影响，视觉上像是
  // 「工作区」区块被搜索莫名清空了）。
  const hasAnyWorkspaceTitleMatch = !query.trim() || workspaces.some(ws =>
    sessions
      .filter(s => s.workspace_id === ws.id)
      .some(s => (s.title || '新对话').toLowerCase().includes(query.toLowerCase()))
  )

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
            placeholder="搜索对话名称或内容…"
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
            <span>对话{freeSessions.length > 0 ? ' ' : ''}{freeSessions.length > 0 && <span className="section-count">({freeSessions.length})</span>}</span>
            <span className={`section-arrow${dialogCollapsed ? ' collapsed' : ''}`}><IcoChevronDown /></span>
          </button>
          {!dialogCollapsed && (
            <div className="session-items">
              {filteredFree.map(s => (
                <SessionItem key={s.id} s={s} currentId={currentId} onSelect={onSelect}
                  onAskDelete={x => setConfirmTarget({ kind: 'session', id: x.id, name: x.title || '新对话' })} />
              ))}
              {filteredFree.length === 0 && !query && (
                <div className="session-empty">无历史对话</div>
              )}
            </div>
          )}
        </>
      )}

      {/* ── 空间 section（有工作区） ── */}
      {workspaces.length > 0 && hasAnyWorkspaceTitleMatch && (
        <>
          <button
            className="session-section-label collapsible"
            onClick={() => setSpacesCollapsed(v => !v)}
          >
            <span>工作区 <span className="section-count">({workspaces.length})</span></span>
            <span className={`section-arrow${spacesCollapsed ? ' collapsed' : ''}`}><IcoChevronDown /></span>
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
                <div className="workspace-group-row">
                  <button className="workspace-group-header" onClick={() => toggleSpace(ws.id)}>
                    <IcoFolder />
                    <span className="workspace-group-name">{ws.name}</span>
                    <span className={`workspace-group-arrow${collapsed ? ' collapsed' : ''}`}><IcoChevronDown /></span>
                  </button>
                  {/* 悬浮才出现：删工作区（其下会话保留，移回「对话」） */}
                  <button className="ws-delete" title="删除工作区" aria-label={`删除工作区 ${ws.name}`}
                    onClick={() => setConfirmTarget({ kind: 'workspace', id: ws.id, name: ws.name })}>
                    <IcoTrash />
                  </button>
                </div>
                {!collapsed && (
                  <div className="workspace-group-items">
                    {filteredWs.map(s => (
                      <SessionItem key={s.id} s={s} currentId={currentId} onSelect={onSelect}
                        onAskDelete={x => setConfirmTarget({ kind: 'session', id: x.id, name: x.title || '新对话' })} />
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
      {/* ── 内容匹配 section（全文搜索，仅搜索态出现） ── */}
      {query.trim() && (
        <>
          <div className="session-section-label">
            <span>内容匹配{hitCount > 0 ? ` (${hitCount})` : ''}</span>
          </div>
          {hitCount === 0 ? (
            <div className="session-empty">{searchBusy ? '搜索中…' : '无匹配内容'}</div>
          ) : (
            <div className="session-items">
              {hits.map(g => (
                <div key={g.session_id} className="search-group">
                  <div className="search-group-title" title={g.title}>
                    {g.title || '新对话'}
                  </div>
                  {g.hits.map(h => (
                    <div
                      key={h.seq}
                      className={`search-hit${g.session_id === currentId ? ' active' : ''}`}
                      onClick={() => onSelect(g.session_id)}
                      onDoubleClick={() => { onSelect(g.session_id); setSearching(false) }}
                      title="双击跳转到会话列表中的该对话"
                    >
                      <Highlight text={h.text} marks={h.marks} />
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </>
      )}
      </div>  {/* /session-scroll */}

      <div className="sidebar-footer">
        <button className="footer-settings-btn" title="设置">
          <IcoGear />
          <span>设置</span>
        </button>
      </div>

      {/* ── 删除确认弹窗 ───────────────────────────────────────── */}
      {confirmTarget && (
        <div className="confirm-overlay" onClick={() => setConfirmTarget(null)}>
          <div className="confirm-dialog" role="dialog" aria-modal="true"
            aria-label={confirmTarget.kind === 'workspace' ? '删除工作区' : '删除对话'}
            onClick={e => e.stopPropagation()}>
            <div className="confirm-title">
              {confirmTarget.kind === 'workspace' ? '删除工作区' : '删除对话'}
            </div>
            <div className="confirm-text">
              {confirmTarget.kind === 'workspace'
                ? <>确定删除工作区「{confirmTarget.name}」吗？<br />其中的对话会保留，并移到「对话」分组。</>
                : <>确定删除对话「{confirmTarget.name}」吗？<br />删除后无法恢复。</>}
            </div>
            <div className="confirm-actions">
              <button className="btn-confirm-cancel" onClick={() => setConfirmTarget(null)}>取消</button>
              <button className="btn-confirm-danger" onClick={handleConfirmDelete}>删除</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

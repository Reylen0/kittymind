import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo } from 'react'
import MessageItem, { ToolGroup } from './MessageItem'
import WorkspaceSelector from './WorkspaceSelector'
import type { Message, SessionMessage, Workspace } from './types'

interface Props {
  sessionId:          string
  workspaces:         Workspace[]
  onSessionUpdate:    () => void
  onWorkspaceCreated: (ws: Workspace) => void
}

// 预览辅助：?loading 初始即处于"思考中"状态，便于截图验证取消按钮（生产环境无参数，恒为 false）
const INIT_LOADING = new URLSearchParams(window.location.search).has('loading')
// 预览辅助：?tools 强制展开工具调用组，便于截图验证展开态（生产环境无参数，恒为 false）
const INIT_TOOLS_OPEN = new URLSearchParams(window.location.search).has('tools')

/** 历史分页：首屏只取最新这么多条，更早的按需「加载更早的消息」逐页前插。 */
const PAGE_SIZE = 40
/** 视口离顶部不超过这个像素，就当作「贴着顶部」——此时前插后停在顶部而不是保位置。 */
const STICK_TOP_EPS = 8

interface PermRequest {
  request_id: string
  tool: string
  args: Record<string, unknown>
  reason: string
}

const fmtK = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M`
  : n >= 1_000   ? `${Math.round(n / 1_000)}k`
  : String(n)

/** 工具入参格式化：兼容「JSON 字符串 / 已解析对象 / 非 JSON 文本」；空入参返回空串。 */
function formatToolArgs(args: unknown): string {
  if (args === null || args === undefined) return ''
  let value: unknown = args
  if (typeof args === 'string') {
    const raw = args.trim()
    if (!raw) return ''
    try { value = JSON.parse(raw) } catch { return raw }
  }
  if (typeof value === 'object') {
    const text = JSON.stringify(value, null, 2)
    return text === '{}' ? '' : text
  }
  return String(value)
}

type RenderUnit =
  | { kind: 'single'; message: Message }
  | { kind: 'tools'; id: string; items: Message[] }

/**
 * 历史消息（后端展示视图）→ 渲染用 Message[]。
 *
 * 工具行的名字与入参由后端随行下发（tool_name / tool_args），不再靠「上一条带
 * tool_calls 的 assistant 消息」现场配对：分页会把历史切成若干段，跨段的配对必然
 * 失败（曾导致同批第二个工具显示成 'tool'）。
 *
 * id 用 seq 而非下标：前插更早的一页后，下标会整体后移，而 seq 在整个会话内稳定且
 * 唯一——React key 稳定才不会让已展开的工具卡片在翻页后重置。
 */
function mapHistory(rows: SessionMessage[], tag: string): Message[] {
  const out: Message[] = []
  rows.forEach((m, i) => {
    const id = m.seq !== undefined && m.seq !== null ? `h-${m.seq}` : `h-${tag}-${i}`
    if ((m.role === 'user' || m.role === 'assistant') && m.content) {
      out.push({ id, role: m.role, content: m.content, noAnim: true })
    } else if (m.role === 'tool' && m.content) {
      out.push({
        id, role: 'tool', content: '', noAnim: true,
        toolName: m.tool_name ?? 'tool',
        toolArgs: formatToolArgs(m.tool_args),
        toolResult: m.content,
      })
    }
  })
  return out
}

/**
 * 相邻的 tool 消息合并为一个渲染单元（= 同一批工具调用），其余消息各自成单元。
 * 只依赖「相邻」这一事实：跨批次的工具消息之间必然隔着 assistant 文本，故不会被误并。
 */
function toRenderUnits(messages: Message[]): RenderUnit[] {
  const units: RenderUnit[] = []
  for (const m of messages) {
    const last = units[units.length - 1]
    if (m.role === 'tool') {
      if (last?.kind === 'tools') last.items.push(m)
      else units.push({ kind: 'tools', id: `g-${m.id}`, items: [m] })
    } else {
      units.push({ kind: 'single', message: m })
    }
  }
  return units
}

export default function ChatView({ sessionId, workspaces, onSessionUpdate, onWorkspaceCreated }: Props) {
  const [messages,            setMessages]            = useState<Message[]>([])
  const [input,               setInput]               = useState('')
  const [isLoading,           setIsLoading]           = useState(INIT_LOADING)
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null)
  const [ctxRatio,            setCtxRatio]            = useState(0)
  const [ctxUsed,             setCtxUsed]             = useState(0)
  const [ctxTotal,            setCtxTotal]            = useState(0)
  const [isHistoryLoading,    setIsHistoryLoading]    = useState(true)
  // 历史分页：hasMore / 游标（本页最早一条的 seq）/ 正在加载更早的一页
  const [hasMore,             setHasMore]             = useState(false)
  const [olderCursor,         setOlderCursor]         = useState<number | null>(null)
  const [isLoadingOlder,      setIsLoadingOlder]      = useState(false)
  // 审批队列：并发多个审批时排队展示，不互相覆盖；队首可交互
  const [permQueue, setPermQueue] = useState<PermRequest[]>([])
  // 相邻 tool 消息分组合并（必须在任何提前 return 之前调用 hook）
  const units = useMemo(() => toRenderUnits(messages), [messages])
  const permRequest = permQueue[0] ?? null
  const bottomRef = useRef<HTMLDivElement>(null)
  const listRef   = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLTextAreaElement>(null)
  // 前插更早一页时要保住的视口位置（滚动容器的 scrollHeight / scrollTop 前值）
  const prependRef = useRef<{ height: number; top: number } | null>(null)
  // 下一批内容要「瞬间」定位到底部（首屏加载历史时置位）：不能用平滑滚动
  const jumpRef = useRef(false)

  const onSessionUpdateRef = useRef(onSessionUpdate)
  useEffect(() => { onSessionUpdateRef.current = onSessionUpdate })

  // 消息变化后的滚动：前插历史时保持视口不动，否则（新消息 / 流式增量）贴到底部
  useLayoutEffect(() => {
    const el = listRef.current
    if (!el) return
    const snap = prependRef.current
    if (snap) {
      prependRef.current = null
      // 已贴着顶部点「加载更早」→ 直接停在顶部，让刚补进来的历史立刻可见；
      // 否则按前插的高度差补偿 scrollTop，视口显示的内容原地不动。
      el.scrollTop = snap.top <= STICK_TOP_EPS
        ? 0
        : el.scrollHeight - snap.height + snap.top
      return
    }
    // 首屏历史（打开会话 / 切回会话）：必须直接「跳」到底部。
    // 用 scrollIntoView({behavior:'smooth'}) 会看到视口从顶部一路滑到底的动画；
    // 在 useLayoutEffect 里改 scrollTop 发生在浏览器绘制之前，不会先闪一下顶部。
    if (jumpRef.current) {
      jumpRef.current = false
      el.scrollTop = el.scrollHeight
      return
    }
    // 会话进行中（新消息 / 流式增量）：保持贴底的平滑跟随
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    setMessages([])
    setIsLoading(INIT_LOADING)
    setSelectedWorkspaceId(null)
    setCtxRatio(0)
    setCtxUsed(0)
    setCtxTotal(0)
    setIsHistoryLoading(true)
    setHasMore(false)
    setOlderCursor(null)
    setIsLoadingOlder(false)
    prependRef.current = null

    async function loadHistory() {
      try {
        // 只取最新一页：超长会话首屏不再全量加载
        const data = await window.kitty?.getSession(sessionId, { limit: PAGE_SIZE })
        const savedUsed  = data?.header?.used_tokens  as number | undefined
        const savedTotal = data?.header?.total_tokens as number | undefined
        if (savedTotal && savedTotal > 0) {
          setCtxUsed(savedUsed ?? 0)
          setCtxTotal(savedTotal)
          setCtxRatio(Math.min((savedUsed ?? 0) / savedTotal, 1.0))
        }
        jumpRef.current = true        // 首屏这一页直接到底，不走平滑滚动
        setMessages(mapHistory(data?.messages ?? [], 'first'))
        setHasMore(Boolean(data?.has_more))
        setOlderCursor(typeof data?.cursor === 'number' ? data.cursor : null)
      } finally {
        setIsHistoryLoading(false)
      }
    }
    loadHistory()
  }, [sessionId])

  /** 前插更早的一页：先记下滚动位置，渲染后由 useLayoutEffect 补偿，视口不跳。 */
  const loadOlder = useCallback(async () => {
    if (!hasMore || olderCursor === null || isLoadingOlder) return
    const el = listRef.current
    prependRef.current = el ? { height: el.scrollHeight, top: el.scrollTop } : null
    setIsLoadingOlder(true)
    try {
      const data = await window.kitty?.getSession(sessionId, {
        limit: PAGE_SIZE, beforeSeq: olderCursor,
      })
      const older = mapHistory(data?.messages ?? [], 'older')
      if (older.length === 0) {
        prependRef.current = null       // 没有可前插的内容：不做滚动补偿
        setHasMore(false)
        return
      }
      setMessages(prev => [...older, ...prev])
      setHasMore(Boolean(data?.has_more))
      setOlderCursor(typeof data?.cursor === 'number' ? data.cursor : null)
    } finally {
      setIsLoadingOlder(false)
    }
  }, [hasMore, olderCursor, isLoadingOlder, sessionId])

  useEffect(() => {
    const onChunk = (d: { delta: string; session_id: string }) => {
      if (d.session_id !== sessionId) return
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (last?.isStreaming) {
          return [...prev.slice(0, -1), { ...last, content: last.content + d.delta }]
        }
        return [...prev, { id: `s-${Date.now()}`, role: 'assistant', content: d.delta, isStreaming: true }]
      })
    }

    const onToolCall = (d: { name: string; args: string; session_id: string }) => {
      if (d.session_id !== sessionId) return
      setMessages(prev => [
        ...prev,
        { id: `tc-${Date.now()}`, role: 'tool', content: '', toolName: d.name, toolArgs: formatToolArgs(d.args) },
      ])
    }

    const onToolResult = (d: { name: string; result: string; session_id: string }) => {
      if (d.session_id !== sessionId) return
      setMessages(prev => {
        const idx = [...prev].reverse().findIndex(
          m => m.role === 'tool' && m.toolName === d.name && m.toolResult === undefined,
        )
        if (idx < 0) return prev
        const ri = prev.length - 1 - idx
        return [...prev.slice(0, ri), { ...prev[ri], toolResult: d.result }, ...prev.slice(ri + 1)]
      })
    }

    const onDone = (d: { session_id: string }) => {
      if (d.session_id !== sessionId) return
      setMessages(prev => prev.map(m => ({ ...m, isStreaming: false })))
      setIsLoading(false)
      onSessionUpdateRef.current()
      inputRef.current?.focus()
    }

    const onError = (d: { session_id: string; error: string }) => {
      if (d.session_id !== sessionId) return
      setMessages(prev => [
        ...prev.map(m => ({ ...m, isStreaming: false })),
        { id: `err-${Date.now()}`, role: 'assistant', content: d.error, isError: true },
      ])
      setIsLoading(false)
    }

    const onContextUsage = (d: { session_id: string; ratio: number; used_tokens: number; total_tokens: number }) => {
      if (d.session_id !== sessionId) return
      setCtxRatio(d.ratio)
      setCtxUsed(d.used_tokens)
      setCtxTotal(d.total_tokens)
    }

    const unsubs = [
      window.kitty?.on('agent.chunk',         onChunk),
      window.kitty?.on('agent.tool_call',     onToolCall),
      window.kitty?.on('agent.tool_result',   onToolResult),
      window.kitty?.on('agent.done',          onDone),
      window.kitty?.on('agent.error',         onError),
      window.kitty?.on('agent.context_usage', onContextUsage),
    ]
    return () => unsubs.forEach(u => u?.())
  }, [sessionId])

  useEffect(() => {
    const unsubReq = window.kitty?.on('tool.permission_request', (data: PermRequest) => {
      // 同一 request_id 只入队一次（后端推送与前端订阅的重连场景可能重复）
      setPermQueue(prev =>
        prev.some(r => r.request_id === data.request_id) ? prev : [...prev, data],
      )
    })
    // 超时/作废：后端等待超时后会推此事件，把对应弹窗从队列移除
    const unsubExp = window.kitty?.on('tool.permission_expired', (data: { request_id: string }) => {
      setPermQueue(prev => prev.filter(r => r.request_id !== data.request_id))
    })
    return () => { unsubReq?.(); unsubExp?.() }
  }, [])

  async function handlePermission(approved: boolean) {
    if (!permRequest) return
    const id = permRequest.request_id
    setPermQueue(prev => prev.filter(r => r.request_id !== id))
    await window.kitty?.respondPermission(id, approved)
  }

  const sendMessage = useCallback(async () => {
    const text = input.trim()
    if (!text || isLoading) return
    setInput('')
    if (inputRef.current) inputRef.current.style.height = 'auto'
    setIsLoading(true)
    setMessages(prev => [...prev, { id: `u-${Date.now()}`, role: 'user', content: text }])
    // 首条消息携带 workspace_id（若已选择）
    const wsId = messages.length === 0 ? selectedWorkspaceId ?? undefined : undefined
    await window.kitty?.sendMessage(text, sessionId, wsId)
  }, [input, isLoading, sessionId, messages.length, selectedWorkspaceId])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  // 输入框高度自适应（上限 200px）
  const autoResize = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }

  const lastIsStreaming = messages[messages.length - 1]?.isStreaming === true
  const thinkingBubble = isLoading && !lastIsStreaming && (
    <div className="msg msg-thinking">
      <div className="msg-avatar">🐱</div>
      <div className="msg-content thinking-dots"><i /><i /><i /></div>
    </div>
  )

  // ── 输入框工具行：纯 UI 状态（暂不接服务端） ──
  const MODELS = ['DeepSeek-V4-Flash', 'Qwen3-Max']
  const [visibility, setVisibility] = useState<'view' | 'edit'>('view')
  const [model, setModel]           = useState(MODELS[0])
  const [openPop, setOpenPop]       = useState<'none' | 'visibility' | 'model'>('none')
  const toolsRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (toolsRef.current && !toolsRef.current.contains(e.target as Node)) setOpenPop('none')
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const inputBox = (
    <div className="input-wrapper">
      <textarea
        ref={inputRef}
        className="input-box"
        value={input}
        onChange={e => { setInput(e.target.value); autoResize(e.target) }}
        onKeyDown={handleKeyDown}
        placeholder="发消息或做任务... / 调用指令 @ 文件或对话"
        disabled={isLoading}
        autoFocus
      />
      <div className="input-footer" ref={toolsRef}>
        <div className="input-tools">
          <button className="tool-round-btn" type="button" title="添加附件（即将支持）">
            <svg width="17" height="17" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <line x1="8" y1="3" x2="8" y2="13" /><line x1="3" y1="8" x2="13" y2="8" />
            </svg>
          </button>

          <div className="chip-wrap">
            <button
              className="chip-btn"
              onClick={() => setOpenPop(v => v === 'visibility' ? 'none' : 'visibility')}
              title="会话权限（演示）"
            >
              <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
                <path d="M7 1.2 12 3v3.2c0 3-2.1 5.6-5 6.6-2.9-1-5-3.6-5-6.6V3z" />
                <path d="M4.8 6.8l1.6 1.6 2.8-2.9" />
              </svg>
              <span>{visibility === 'view' ? '仅可查看' : '可读写'}</span>
              <IcoChevron />
            </button>
            {openPop === 'visibility' && (
              <div className="chip-pop">
                <div className={`chip-pop-item${visibility === 'view' ? ' sel' : ''}`} onClick={() => { setVisibility('view'); setOpenPop('none') }}>
                  仅可查看<span className="chip-pop-desc">助手只读工作区</span>
                </div>
                <div className={`chip-pop-item${visibility === 'edit' ? ' sel' : ''}`} onClick={() => { setVisibility('edit'); setOpenPop('none') }}>
                  可读写<span className="chip-pop-desc">助手可修改文件（演示）</span>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="input-right">
          <div className="chip-wrap">
            <button
              className="chip-btn"
              onClick={() => setOpenPop(v => v === 'model' ? 'none' : 'model')}
              title="选择模型（演示）"
            >
              <span className="chip-model">{model}</span>
              <IcoChevron />
            </button>
            {openPop === 'model' && (
              <div className="chip-pop">
                {MODELS.map(m => (
                  <div key={m} className={`chip-pop-item${model === m ? ' sel' : ''}`} onClick={() => { setModel(m); setOpenPop('none') }}>
                    {m}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 上下文占用量圆环 */}
          {(() => {
            const CIRC = 2 * Math.PI * 7          // ≈ 43.98
            const arc  = Math.max(0.5, CIRC * ctxRatio)  // 最小 0.5 保证弧线可见
            const gap  = Math.max(0, CIRC - arc)
            const pct  = Math.round(ctxRatio * 100)
            const stroke = ctxRatio >= 0.9 ? 'var(--danger, #f03)' :
                           ctxRatio >= 0.7 ? 'var(--warning, #f90)' :
                           'var(--accent)'
            return (
              <div className="ctx-ring" role="status" aria-label={`上下文已用 ${pct}%`}>
                <svg width="18" height="18" viewBox="0 0 18 18">
                  <circle className="ctx-ring-track" cx="9" cy="9" r="7" />
                  <circle className="ctx-ring-arc" cx="9" cy="9" r="7"
                    strokeDasharray={`${arc} ${gap}`}
                    style={{ stroke }} />
                </svg>
                <span className="ctx-tip">
                  上下文已用 <b>{pct}%</b>
                  {ctxTotal > 0 && <> · {fmtK(ctxUsed)} / {fmtK(ctxTotal)}</>}
                </span>
              </div>
            )
          })()}

          {isLoading
            ? (
              <button className="btn-cancel-round" onClick={() => window.kitty?.cancelTurn(sessionId)} title="停止生成">
                <svg width="12" height="12" viewBox="0 0 12 12">
                  <rect x="1.5" y="1.5" width="9" height="9" rx="2" fill="currentColor" />
                </svg>
              </button>
            )
            : (
              <button className="btn-send-round" onClick={sendMessage} disabled={!input.trim()} title="发送 (Enter)">
                <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="8" y1="13" x2="8" y2="3" />
                  <polyline points="3.5,7.5 8,3 12.5,7.5" />
                </svg>
              </button>
            )}
        </div>
      </div>
    </div>
  )

  if (isHistoryLoading) {
    return <div className="chat-view" />
  }

  if (messages.length === 0) {
    return (
      <div className="chat-view">
        <div className="landing-view">
          <div className="landing-content">
            <div className="landing-brand">
              <div className="landing-badge">🐱</div>
              <div>
                <div className="landing-title">KittyMind</div>
                <div className="landing-tagline">你的桌面智能伙伴</div>
              </div>
            </div>
            <div className="landing-input-area">
              <div className="landing-ws-row">
                <WorkspaceSelector
                  workspaces={workspaces}
                  selectedId={selectedWorkspaceId}
                  onSelect={setSelectedWorkspaceId}
                  onCreated={onWorkspaceCreated}
                />
              </div>
              {permRequest && (
                <PermissionBanner req={permRequest} pendingCount={permQueue.length - 1} onRespond={handlePermission} />
              )}
              {inputBox}
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="chat-view">
      <div className="messages" ref={listRef}>
        {hasMore && (
          <div className="load-older">
            <button
              className="load-older-btn"
              type="button"
              onClick={loadOlder}
              disabled={isLoadingOlder}
            >
              {isLoadingOlder ? '加载中…' : '↑ 加载更早的消息'}
            </button>
          </div>
        )}
        {units.map(u => u.kind === 'tools'
          ? <ToolGroup key={u.id} items={u.items} forceOpen={INIT_TOOLS_OPEN} />
          : <MessageItem key={u.message.id} message={u.message} />)}
        {thinkingBubble}
        <div ref={bottomRef} />
      </div>
      {permRequest && (
        <PermissionBanner req={permRequest} pendingCount={permQueue.length - 1} onRespond={handlePermission} />
      )}
      <div className="input-area">
        {inputBox}
      </div>
    </div>
  )
}

function IcoChevron() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="2.5,4.5 6,8 9.5,4.5" />
    </svg>
  )
}

function PermissionBanner({
  req,
  pendingCount = 0,
  onRespond,
}: {
  req: PermRequest
  pendingCount?: number
  onRespond: (approved: boolean) => void
}) {
  const argsText = Object.entries(req.args)
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join('\n')

  return (
    <div className="perm-banner">
      <div className="perm-banner-header">
        <span className="perm-banner-icon">🔐</span>
        <span className="perm-banner-title">工具请求确认</span>
        <span className="perm-banner-tool">{req.tool}</span>
        {pendingCount > 0 && (
          <span className="perm-banner-tool" title="队列中还有待审批的请求">
            +{pendingCount}
          </span>
        )}
      </div>
      <div className="perm-banner-reason">{req.reason}</div>
      {argsText && <div className="perm-banner-args">{argsText}</div>}
      <div className="perm-banner-actions">
        <button className="perm-btn perm-btn-deny"  onClick={() => onRespond(false)}>拒绝</button>
        <button className="perm-btn perm-btn-allow" onClick={() => onRespond(true)}>允许</button>
      </div>
    </div>
  )
}

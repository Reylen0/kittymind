import { useState, useEffect, useRef, useCallback } from 'react'
import MessageItem from './MessageItem'
import WorkspaceSelector from './WorkspaceSelector'
import type { Message, Workspace } from './types'

interface Props {
  sessionId:          string
  workspaces:         Workspace[]
  onSessionUpdate:    () => void
  onWorkspaceCreated: (ws: Workspace) => void
}

export default function ChatView({ sessionId, workspaces, onSessionUpdate, onWorkspaceCreated }: Props) {
  const [messages,            setMessages]            = useState<Message[]>([])
  const [input,               setInput]               = useState('')
  const [isLoading,           setIsLoading]           = useState(false)
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string | null>(null)
  const [permRequest, setPermRequest] = useState<{
    request_id: string
    tool: string
    args: Record<string, unknown>
    reason: string
  } | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLTextAreaElement>(null)

  const onSessionUpdateRef = useRef(onSessionUpdate)
  useEffect(() => { onSessionUpdateRef.current = onSessionUpdate })

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    setMessages([])
    setIsLoading(false)
    setSelectedWorkspaceId(null)

    async function loadHistory() {
      const data = await window.kitty?.getSession(sessionId)
      if (!data?.messages?.length) return

      const msgs: Message[] = []
      for (let i = 0; i < data.messages.length; i++) {
        const m = data.messages[i]
        if (m.role === 'user' && m.content) {
          msgs.push({ id: `h-${i}`, role: 'user', content: m.content })
        } else if (m.role === 'assistant' && m.content) {
          msgs.push({ id: `h-${i}`, role: 'assistant', content: m.content })
        } else if (m.role === 'tool' && m.content) {
          const prior = data.messages[i - 1]
          const tc = Array.isArray(prior?.tool_calls) ? prior.tool_calls[0] : null
          const name = (tc as { function?: { name?: string } } | null)?.function?.name ?? 'tool'
          msgs.push({ id: `h-${i}`, role: 'tool', content: '', toolName: name, toolArgs: '', toolResult: m.content })
        }
      }
      setMessages(msgs)
    }
    loadHistory()
  }, [sessionId])

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
        { id: `tc-${Date.now()}`, role: 'tool', content: '', toolName: d.name, toolArgs: d.args },
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

    const unsubs = [
      window.kitty?.on('agent.chunk',       onChunk),
      window.kitty?.on('agent.tool_call',   onToolCall),
      window.kitty?.on('agent.tool_result', onToolResult),
      window.kitty?.on('agent.done',        onDone),
      window.kitty?.on('agent.error',       onError),
    ]
    return () => unsubs.forEach(u => u?.())
  }, [sessionId])

  useEffect(() => {
    const unsub = window.kitty?.on('tool.permission_request', (data) => {
      setPermRequest(data)
    })
    return () => unsub?.()
  }, [])

  async function handlePermission(approved: boolean) {
    if (!permRequest) return
    const id = permRequest.request_id
    setPermRequest(null)
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

          {/* 上下文占用量圆环（占位数据），悬停显示文字 */}
          <div className="ctx-ring" role="status" aria-label="上下文已用 1%">
            <svg width="18" height="18" viewBox="0 0 18 18">
              <circle className="ctx-ring-track" cx="9" cy="9" r="7" />
              <circle className="ctx-ring-arc" cx="9" cy="9" r="7" strokeDasharray="3 41" />
            </svg>
            <span className="ctx-tip">上下文已用 <b>1%</b></span>
          </div>

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
                <PermissionBanner req={permRequest} onRespond={handlePermission} />
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
      <div className="messages">
        {messages.map(m => <MessageItem key={m.id} message={m} />)}
        {thinkingBubble}
        <div ref={bottomRef} />
      </div>
      {permRequest && (
        <PermissionBanner req={permRequest} onRespond={handlePermission} />
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
  onRespond,
}: {
  req: { request_id: string; tool: string; args: Record<string, unknown>; reason: string }
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

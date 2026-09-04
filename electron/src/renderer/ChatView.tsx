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
        { id: `err-${Date.now()}`, role: 'assistant', content: `❌ ${d.error}` },
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

  const sendMessage = useCallback(async () => {
    const text = input.trim()
    if (!text || isLoading) return
    setInput('')
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

  const inputBox = (
    <div className="input-wrapper">
      <textarea
        ref={inputRef}
        className="input-box"
        value={input}
        onChange={e => setInput(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="发送消息… (Enter 发送，Shift+Enter 换行)"
        disabled={isLoading}
        autoFocus
      />
      <div className="input-footer">
        <button className="btn-add" type="button">＋</button>
        {isLoading
          ? <button className="btn-cancel-round" onClick={() => window.kitty?.cancelTurn(sessionId)}>■</button>
          : <button className="btn-send-round" onClick={sendMessage} disabled={!input.trim()}>↑</button>
        }
      </div>
    </div>
  )

  if (messages.length === 0) {
    return (
      <div className="chat-view">
        <div className="landing-view">
          <div className="landing-content">
            <div className="landing-title">🐱 KittyMind</div>
            <div className="landing-input-area">
              <div className="landing-ws-row">
                <WorkspaceSelector
                  workspaces={workspaces}
                  selectedId={selectedWorkspaceId}
                  onSelect={setSelectedWorkspaceId}
                  onCreated={onWorkspaceCreated}
                />
              </div>
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
        <div ref={bottomRef} />
      </div>
      <div className="input-area">
        {inputBox}
      </div>
    </div>
  )
}

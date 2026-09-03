import { useState, useEffect } from 'react'
import SessionList from './SessionList'
import ChatView from './ChatView'
import type { Session } from './types'
import './style.css'

export default function App() {
  const [sessions, setSessions]     = useState<Session[]>([])
  const [currentId, setCurrentId]   = useState<string | null>(null)
  const [sidebarOpen, setSidebar]   = useState(true)

  useEffect(() => { loadSessions() }, [])

  async function loadSessions() {
    const list = await window.kitty?.listSessions()
    if (!Array.isArray(list)) return
    setSessions(list)
    // 只在没有当前会话时才自动选中第一个（不覆盖正在输入中的新会话）
    setCurrentId(prev => {
      if (prev !== null) return prev
      return list[0]?.id ?? null
    })
  }

  function createSession() {
    // 只在前端生成 UUID，不预先调后端
    // Python 会在第一条消息的 append_turn 里用消息内容自动生成标题并建 session
    setCurrentId(crypto.randomUUID())
  }

  async function deleteSession(id: string) {
    await window.kitty?.deleteSession(id)
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      if (id === currentId) setCurrentId(next[0]?.id ?? null)
      return next
    })
  }

  const currentTitle = sessions.find(s => s.id === currentId)?.title ?? '新会话'

  return (
    <div className="app">
      {sidebarOpen && (
        <SessionList
          sessions={sessions}
          currentId={currentId}
          onSelect={setCurrentId}
          onCreate={createSession}
          onDelete={deleteSession}
        />
      )}

      <div className="main">
        <div className="topbar">
          <button className="icon-btn" onClick={() => setSidebar(v => !v)}>☰</button>
          <span className="topbar-title">{currentTitle}</span>
        </div>

        {currentId
          ? <ChatView key={currentId} sessionId={currentId} onSessionUpdate={loadSessions} />
          : (
            <div className="empty-state">
              <span>🐱 选择或新建一个会话开始对话</span>
              <button className="btn-primary" onClick={createSession}>＋ 新建会话</button>
            </div>
          )
        }
      </div>
    </div>
  )
}

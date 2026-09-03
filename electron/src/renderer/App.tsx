import { useState, useEffect } from 'react'
import TopBar from './TopBar'
import SessionList from './SessionList'
import ChatView from './ChatView'
import type { Session } from './types'
import './style.css'

export default function App() {
  const [sessions, setSessions]       = useState<Session[]>([])
  const [currentId, setCurrentId]     = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)

  useEffect(() => { loadSessions() }, [])

  async function loadSessions() {
    const list = await window.kitty?.listSessions()
    if (!Array.isArray(list)) return
    setSessions(list)
    setCurrentId(prev => {
      if (prev !== null) return prev
      return list[0]?.id ?? null
    })
  }

  function createSession() {
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

  return (
    <div className="app">
      <TopBar />
      <div className="app-body">
        <SessionList
          sessions={sessions}
          currentId={currentId}
          sidebarOpen={sidebarOpen}
          onSelect={setCurrentId}
          onCreate={createSession}
          onDelete={deleteSession}
          onToggle={() => setSidebarOpen(v => !v)}
        />

        <div className="main">
          {currentId
            ? <ChatView key={currentId} sessionId={currentId} onSessionUpdate={loadSessions} />
            : (
              <div className="empty-state">
                <span>🐱 选择或新建一个会话</span>
                <button className="btn-new-text" onClick={createSession}>
                  <span>＋ 新建会话</span>
                </button>
              </div>
            )
          }
        </div>
      </div>
    </div>
  )
}

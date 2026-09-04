import { useState, useEffect } from 'react'
import TopBar from './TopBar'
import SessionList from './SessionList'
import ChatView from './ChatView'
import type { Session, Workspace } from './types'
import './style.css'

export default function App() {
  const [sessions,    setSessions]    = useState<Session[]>([])
  const [workspaces,  setWorkspaces]  = useState<Workspace[]>([])
  const [currentId,   setCurrentId]   = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)

  useEffect(() => {
    loadSessions()
    loadWorkspaces()
  }, [])

  async function loadSessions() {
    const list = await window.kitty?.listSessions()
    if (!Array.isArray(list)) return
    setSessions(list)
    setCurrentId(prev => {
      if (prev !== null) return prev
      // 有历史对话 → 选最近一条；无历史 → 直接开新对话
      return list[0]?.id ?? crypto.randomUUID()
    })
  }

  async function loadWorkspaces() {
    const list = await window.kitty?.listWorkspaces()
    if (Array.isArray(list)) setWorkspaces(list as Workspace[])
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

  function handleWorkspaceCreated(ws: Workspace) {
    setWorkspaces(prev => [...prev, ws])
  }

  return (
    <div className="app">
      <TopBar />
      <div className="app-body">
        <SessionList
          sessions={sessions}
          workspaces={workspaces}
          currentId={currentId}
          sidebarOpen={sidebarOpen}
          onSelect={setCurrentId}
          onCreate={createSession}
          onDelete={deleteSession}
          onToggle={() => setSidebarOpen(v => !v)}
        />

        <div className="main">
          {currentId
            ? <ChatView
                key={currentId}
                sessionId={currentId}
                workspaces={workspaces}
                onSessionUpdate={loadSessions}
                onWorkspaceCreated={handleWorkspaceCreated}
              />
            : (
              <div className="empty-state">
                <span>🐱 选择或新建一个对话</span>
                <button className="btn-new-text" onClick={createSession}>
                  <span>＋ 新建对话</span>
                </button>
              </div>
            )
          }
        </div>
      </div>
    </div>
  )
}

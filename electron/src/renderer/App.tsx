import { useState, useEffect, useLayoutEffect } from 'react'
import TopBar from './TopBar'
import SessionList from './SessionList'
import ChatView from './ChatView'
import type { Session, Workspace } from './types'
import './style.css'

/**
 * 常驻保留的会话视图数量上限（最近访问优先）。
 *
 * 为什么不随切换卸载：整体卸载会把会话级状态一并丢掉——消息、输入草稿、
 * 生成中标志、待审批队列，于是「切走再切回」就变成审批弹窗消失、草稿清空、
 * 界面像是已经结束。常驻挂载让这些状态天然存活。
 *
 * 被淘汰的老会话可以接受：能被淘汰说明很久没访问，其 run 早已结束并落库，
 * 重新打开时从历史重放即可。
 */
const MAX_LIVE_VIEWS = 6

export default function App() {
  const [sessions,    setSessions]    = useState<Session[]>([])
  const [workspaces,  setWorkspaces]  = useState<Workspace[]>([])
  const [currentId,   setCurrentId]   = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  // 已挂载的会话视图，按访问顺序排列（见 MAX_LIVE_VIEWS）
  const [openIds,     setOpenIds]     = useState<string[]>([])

  useEffect(() => {
    loadSessions()
    loadWorkspaces()
  }, [])

  // 把当前会话登记进保留列表，超过上限时淘汰最久未访问的那个。
  // 用 layout effect 在浏览器绘制前完成，避免出现「一帧什么都没有」。
  useLayoutEffect(() => {
    if (!currentId) return
    setOpenIds(prev =>
      prev.includes(currentId) ? prev : [...prev, currentId].slice(-MAX_LIVE_VIEWS),
    )
  }, [currentId])

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
    setOpenIds(prev => prev.filter(x => x !== id))
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      // 删光后不进空状态页：直接落到新对话的输入框（landing）界面
      if (id === currentId) setCurrentId(next[0]?.id ?? crypto.randomUUID())
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
          {/* 会话视图常驻挂载，切换只切显隐——消息 / 输入草稿 / 生成中标志 /
              待审批队列因此得以保留。包装层用 display:contents 不产生盒子，
              可见项的布局与原来完全一致 */}
          {openIds.map(id => (
            <div key={id} style={{ display: id === currentId ? 'contents' : 'none' }}>
              <ChatView
                sessionId={id}
                active={id === currentId}
                workspaces={workspaces}
                onSessionUpdate={loadSessions}
                onWorkspaceCreated={handleWorkspaceCreated}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

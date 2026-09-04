import { useState, useRef, useEffect } from 'react'
import type { Workspace } from './types'

interface Props {
  workspaces: Workspace[]
  selectedId: string | null
  onSelect:   (id: string | null) => void
  onCreated:  (ws: Workspace) => void
}

function IcoFolder() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 3.5C1 2.67 1.67 2 2.5 2H5l1.5 1.5H11.5C12.33 3.5 13 4.17 13 5v5.5C13 11.33 12.33 12 11.5 12h-9C1.67 12 1 11.33 1 10.5V3.5z"/>
    </svg>
  )
}

function IcoChevronDown() {
  return (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="2,4 6,8 10,4"/>
    </svg>
  )
}

export default function WorkspaceSelector({ workspaces, selectedId, onSelect, onCreated }: Props) {
  const [open, setOpen]   = useState(false)
  const [query, setQuery] = useState('')
  const rootRef           = useRef<HTMLDivElement>(null)
  const searchRef         = useRef<HTMLInputElement>(null)

  const selected = workspaces.find(w => w.id === selectedId) ?? null

  useEffect(() => {
    if (open) { setQuery(''); setTimeout(() => searchRef.current?.focus(), 50) }
  }, [open])

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const filtered = query.trim()
    ? workspaces.filter(w => w.name.toLowerCase().includes(query.toLowerCase()))
    : workspaces

  async function handleAdd() {
    setOpen(false)
    const folderPath = await window.kitty?.selectWorkspace()
    if (!folderPath) return
    const name = folderPath.split(/[\\/]/).filter(Boolean).pop() || folderPath
    const ws = await window.kitty?.createWorkspace(name, folderPath)
    if (ws) {
      onCreated(ws as Workspace)
      onSelect((ws as Workspace).id)
    }
  }

  return (
    <div className="ws-sel" ref={rootRef}>
      {/* 小胶囊触发按钮 */}
      <button className="ws-pill" onClick={() => setOpen(v => !v)}>
        <IcoFolder />
        <span className="ws-pill-label">{selected ? selected.name : '选择工作区'}</span>
        <IcoChevronDown />
      </button>

      {/* 下拉面板 */}
      {open && (
        <div className="ws-drop">
          {workspaces.length > 3 && (
            <div className="ws-drop-search">
              <input
                ref={searchRef}
                value={query}
                onChange={e => setQuery(e.target.value)}
                onKeyDown={e => e.key === 'Escape' && setOpen(false)}
                placeholder="搜索…"
                className="ws-drop-search-input"
              />
            </div>
          )}

          {filtered.map(ws => (
            <button
              key={ws.id}
              className={`ws-drop-item${ws.id === selectedId ? ' active' : ''}`}
              onClick={() => { onSelect(ws.id); setOpen(false) }}
            >
              <IcoFolder />
              <span>{ws.name}</span>
              {ws.id === selectedId && <span className="ws-check">✓</span>}
            </button>
          ))}

          {filtered.length === 0 && (
            <div className="ws-drop-empty">无匹配</div>
          )}

          <div className="ws-drop-sep" />

          <button className="ws-drop-item ws-drop-add" onClick={handleAdd}>
            <span className="ws-drop-plus">＋</span>
            <span>添加工作区…</span>
          </button>
        </div>
      )}
    </div>
  )
}

import { useState, useEffect, useRef } from 'react'

interface MenuItemDef {
  label:        string
  accelerator?: string
  action?:      string
  separator?:   boolean
}

const MENUS: { label: string; items: MenuItemDef[] }[] = [
  {
    label: '编辑(E)',
    items: [
      { label: '撤销(U)', accelerator: 'Ctrl+Z', action: 'undo' },
      { label: '重做(R)', accelerator: 'Ctrl+Y', action: 'redo' },
      { separator: true,  label: '' },
      { label: '剪切(T)', accelerator: 'Ctrl+X', action: 'cut' },
      { label: '复制(C)', accelerator: 'Ctrl+C', action: 'copy' },
      { label: '粘贴(P)', accelerator: 'Ctrl+V', action: 'paste' },
      { separator: true,  label: '' },
      { label: '全选(A)', accelerator: 'Ctrl+A', action: 'selectAll' },
    ],
  },
  {
    label: '窗口(W)',
    items: [
      { label: '最小化',      action: 'minimize' },
      { label: '最大化/还原', action: 'maximize' },
      { separator: true, label: '' },
      { label: '关闭窗口',   action: 'close' },
    ],
  },
  {
    label: '帮助(H)',
    items: [
      { label: '关于 KittyMind', action: 'about' },
    ],
  },
]

export default function TopBar({ onUsage }: { onUsage?: () => void }) {
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  const barRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        setOpenIdx(null)
      }
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  function runAction(action?: string) {
    setOpenIdx(null)
    if (!action) return
    if (action === 'about') {
      alert('KittyMind v0.1.0\n通用桌面 AI Agent')
      return
    }
    window.kitty?.windowControl(action)
  }

  return (
    <div className="topbar" ref={barRef}>
      <div className="topbar-brand">
        <span className="topbar-brand-icon">🐱</span>
        <span className="topbar-brand-name">KittyMind</span>
      </div>

      <div className="topbar-menus">
        {MENUS.map((menu, i) => (
          <div key={i} className="topbar-menu-wrap">
            <button
              className={`topbar-menu-btn${openIdx === i ? ' open' : ''}`}
              style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}
              onMouseDown={e => { e.stopPropagation(); setOpenIdx(openIdx === i ? null : i) }}
            >
              {menu.label}
            </button>

            {openIdx === i && (
              <div className="topbar-dropdown">
                {menu.items.map((item, j) =>
                  item.separator ? (
                    <div key={j} className="topbar-dropdown-sep" />
                  ) : (
                    <div
                      key={j}
                      className="topbar-dropdown-item"
                      onClick={() => runAction(item.action)}
                    >
                      <span>{item.label}</span>
                      {item.accelerator && (
                        <span className="topbar-dropdown-accel">{item.accelerator}</span>
                      )}
                    </div>
                  )
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {onUsage && (
        <button
          className="topbar-usage-btn"
          style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}
          onClick={onUsage}
          aria-label="用量统计"
          data-tip="用量统计"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
            <path d="M3.5 13V9.5" />
            <path d="M8 13V4.5" />
            <path d="M12.5 13V7.5" />
          </svg>
        </button>
      )}
    </div>
  )
}

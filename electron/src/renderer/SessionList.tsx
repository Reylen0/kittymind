import type { Session } from './types'

interface Props {
  sessions: Session[]
  currentId: string | null
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
}

export default function SessionList({ sessions, currentId, onSelect, onCreate, onDelete }: Props) {
  return (
    <div className="session-list">
      <div className="session-header">
        <span>会话</span>
        <button className="icon-btn" onClick={onCreate} title="新建会话">＋</button>
      </div>

      <div className="session-items">
        {sessions.map(s => (
          <div
            key={s.id}
            className={`session-item${s.id === currentId ? ' active' : ''}`}
            onClick={() => onSelect(s.id)}
          >
            <span className="session-title">{s.title || '新会话'}</span>
            <button
              className="session-delete"
              title="删除"
              onClick={e => { e.stopPropagation(); onDelete(s.id) }}
            >×</button>
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="session-empty">无历史会话</div>
        )}
      </div>

      <div className="session-footer">
        <button className="btn-primary" style={{ width: '100%' }} onClick={onCreate}>
          ＋ 新建会话
        </button>
      </div>
    </div>
  )
}

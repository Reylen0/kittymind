import type { Message } from './types'

interface Props { message: Message }

export default function MessageItem({ message }: Props) {
  if (message.role === 'user') {
    return (
      <div className="msg msg-user">
        <div className="msg-bubble">{message.content}</div>
      </div>
    )
  }

  if (message.role === 'tool') {
    const done = message.toolResult !== undefined
    return (
      <div className="msg msg-tool">
        <div className="tool-card">
          <div className="tool-header">
            <span className="tool-icon">🔧</span>
            <span className="tool-name">{message.toolName}</span>
            <span className={done ? 'tool-ok' : 'tool-running'}>
              {done ? '✓ 完成' : '运行中…'}
            </span>
          </div>
          {message.toolArgs && (
            <div className="tool-args">{message.toolArgs}</div>
          )}
          {done && message.toolResult && (
            <div className="tool-result">{message.toolResult}</div>
          )}
        </div>
      </div>
    )
  }

  // assistant
  return (
    <div className="msg msg-assistant">
      <div className="msg-avatar">🐱</div>
      <div className="msg-content">
        {message.content}
        {message.isStreaming && <span className="cursor">▋</span>}
      </div>
    </div>
  )
}

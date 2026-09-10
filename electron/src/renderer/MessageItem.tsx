import { useState } from 'react'
import type { Message } from './types'
import Markdown, { copyText } from './Markdown'

interface Props { message: Message }

function IcoSpinner() {
  return (
    <svg className="spin" width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
      <path d="M7 1.5A5.5 5.5 0 1 1 1.5 7" />
    </svg>
  )
}
function IcoCheck() {
  return (
    <svg width="11" height="11" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="2,7.5 5.5,11 12,3.5" />
    </svg>
  )
}
function IcoCopy() {
  return (
    <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4.5" y="4.5" width="8" height="8" rx="1.5" />
      <path d="M9.5 4.5v-2A1.5 1.5 0 0 0 8 1H3A1.5 1.5 0 0 0 1.5 2.5v5A1.5 1.5 0 0 0 3 9h1.5" />
    </svg>
  )
}

/** 启发式判断工具结果是否为失败 */
function looksLikeError(result: string): boolean {
  return /\b(error|Error|ERROR|Traceback|Exception|失败|错误)\b/.test(result)
}

export default function MessageItem({ message }: Props) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    if (await copyText(message.content)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    }
  }

  if (message.role === 'user') {
    return (
      <div className="msg msg-user">
        <div className="msg-bubble">{message.content}</div>
      </div>
    )
  }

  if (message.role === 'tool') {
    const done = message.toolResult !== undefined
    const isErr = done && !!message.toolResult && looksLikeError(message.toolResult)
    return (
      <div className="msg msg-tool">
        <div className="tool-card">
          <div className="tool-header">
            <span className="tool-icon">
              {done
                ? <IcoCheck />
                : <IcoSpinner />}
            </span>
            <span className="tool-name">{message.toolName}</span>
            <span className={`tool-status ${done ? (isErr ? 'tool-err' : 'tool-ok') : 'tool-running'}`}>
              {done ? (isErr ? '✕ 失败' : '✓ 完成') : '运行中'}
            </span>
          </div>
          {message.toolArgs && <div className="tool-args">{message.toolArgs}</div>}
          {done && message.toolResult && (
            <div className={`tool-result ${isErr ? 'tool-result-err' : 'tool-result-ok'}`}>
              {message.toolResult}
            </div>
          )}
        </div>
      </div>
    )
  }

  // assistant
  return (
    <div className={`msg msg-assistant${message.isError ? ' msg-error' : ''}`}>
      <div className="msg-avatar">🐱</div>
      <div className="msg-body">
        {message.isError
          ? <div className="error-card">{message.content}</div>
          : (
            <div className="msg-content">
              <Markdown>{message.content}</Markdown>
              {message.isStreaming && <span className="cursor" />}
            </div>
          )}
        {!message.isStreaming && !message.isError && (
          <div className="msg-actions">
            <button type="button" className="msg-action-btn" onClick={handleCopy}>
              {copied ? <IcoCheck /> : <IcoCopy />}
              {copied ? '已复制' : '复制'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

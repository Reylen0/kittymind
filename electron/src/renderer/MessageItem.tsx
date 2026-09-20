import { useState, useEffect, useRef } from 'react'
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
function IcoChevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`tool-chevron${open ? ' tool-chevron-open' : ''}`}
      width="10" height="10" viewBox="0 0 12 12" fill="none"
      stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
    >
      <polyline points="4,2 8,6 4,10" />
    </svg>
  )
}

/** 启发式判断工具结果是否为失败 */
function looksLikeError(result: string): boolean {
  return /\b(error|Error|ERROR|Traceback|Exception|失败|错误)\b/.test(result)
}

/** 展开态入参压成单行，供折叠时的摘要显示 */
function oneLineArgs(args?: string): string {
  return (args ?? '').replace(/\s+/g, ' ').trim()
}

/**
 * 单张工具卡：头部（图标 + 工具名 + 状态）始终可见，点击头部折叠/展开
 * 入参与结果。默认展开——外层 ToolGroup 折叠时用户点开就是想看细节。
 */
export function ToolCard({ message }: { message: Message }) {
  const [open, setOpen] = useState(true)
  const done = message.toolResult !== undefined
  const isErr = done && !!message.toolResult && looksLikeError(message.toolResult)
  const preview = oneLineArgs(message.toolArgs)

  return (
    <div className="tool-card">
      <button
        type="button"
        className="tool-header tool-header-btn"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        title={open ? '收起本次调用' : '展开本次调用'}
      >
        <span className="tool-icon">{done ? <IcoCheck /> : <IcoSpinner />}</span>
        <span className="tool-name">{message.toolName ?? 'tool'}</span>
        {!open && preview && <span className="tool-args-inline">{preview}</span>}
        <span className={`tool-status ${done ? (isErr ? 'tool-err' : 'tool-ok') : 'tool-running'}`}>
          {done ? (isErr ? '✕ 失败' : '✓ 完成') : '运行中'}
        </span>
        <IcoChevron open={open} />
      </button>
      {open && (
        <>
          {message.toolArgs && <div className="tool-args">{message.toolArgs}</div>}
          {done && message.toolResult && (
            <div className={`tool-result ${isErr ? 'tool-result-err' : 'tool-result-ok'}`}>
              {message.toolResult}
            </div>
          )}
        </>
      )}
    </div>
  )
}

/**
 * 一轮内的工具调用组：把同一批（相邻的 tool 消息）合并成一行摘要，
 * 默认只占一行（落在最终文本上方），点击展开为逐张工具卡。
 *
 * 展开状态：工具还在跑时自动展开（能实时看到调了什么），整组完成时自动收起；
 * 用户的点击优先于此默认值，直到下一次运行状态变化。
 */
export function ToolGroup({ items, forceOpen = false }: { items: Message[]; forceOpen?: boolean }) {
  const running = items.some(m => m.toolResult === undefined)
  const [userOpen, setUserOpen] = useState<boolean | null>(null)
  const prevRunning = useRef(running)

  useEffect(() => {
    if (prevRunning.current !== running) {
      prevRunning.current = running
      setUserOpen(null)   // 状态切换 → 回到「运行中展开 / 完成后收起」的默认语义
    }
  }, [running])

  const open = forceOpen || (userOpen ?? running)
  const names = [...new Set(items.map(m => m.toolName ?? 'tool'))]
  const errCount = items.filter(m => m.toolResult !== undefined && looksLikeError(m.toolResult ?? '')).length
  const doneCount = items.filter(m => m.toolResult !== undefined).length

  return (
    <div className="msg msg-tool">
      <div className={`tool-group${open ? ' tool-group-open' : ''}`}>
        <button
          type="button"
          className="tool-group-header"
          onClick={() => setUserOpen(!open)}
          aria-expanded={open}
          title={open ? '收起工具调用' : '展开工具调用'}
        >
          <span className="tool-icon">{running ? <IcoSpinner /> : <IcoCheck />}</span>
          <span className="tool-group-title">
            已调用 {items.length} 个工具
            <span className="tool-group-names">{names.join('、')}</span>
          </span>
          <span className={`tool-status ${running ? 'tool-running' : (errCount ? 'tool-err' : 'tool-ok')}`}>
            {running
              ? `${doneCount}/${items.length} 运行中`
              : (errCount ? `✕ ${errCount} 个失败` : '✓ 全部完成')}
          </span>
          <IcoChevron open={open} />
        </button>
        {open && (
          <div className="tool-group-body">
            {items.map(m => <ToolCard key={m.id} message={m} />)}
          </div>
        )}
      </div>
    </div>
  )
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
    // 单张工具卡（ToolGroup 之外的兜底路径）：默认展开，点击头部折叠
    return (
      <div className="msg msg-tool">
        <ToolCard message={message} />
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

import { useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'

/** 剪贴板写入（Electron file:// 下 clipboard API 不可用时回退 execCommand） */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    let ok = false
    try { ok = document.execCommand('copy') } catch { ok = false }
    document.body.removeChild(ta)
    return ok
  }
}

/** 从 hast node 递归取纯文本 */
function textOf(node: any): string {
  if (!node) return ''
  if (node.type === 'text') return node.value
  return (node.children ?? []).map(textOf).join('')
}

function IcoCopy() {
  return (
    <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4.5" y="4.5" width="8" height="8" rx="1.5" />
      <path d="M9.5 4.5v-2A1.5 1.5 0 0 0 8 1H3A1.5 1.5 0 0 0 1.5 2.5v5A1.5 1.5 0 0 0 3 9h1.5" />
    </svg>
  )
}
function IcoCheck() {
  return (
    <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="2,7.5 5.5,11 12,3.5" />
    </svg>
  )
}

function CodeBlock({ lang, raw, children }: { lang: string; raw: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    if (await copyText(raw)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    }
  }

  return (
    <div className="code-block">
      <div className="code-block-bar">
        <span className="code-block-lang">{lang || 'text'}</span>
        <button
          type="button"
          className={`code-block-copy${copied ? ' copied' : ''}`}
          onClick={handleCopy}
        >
          {copied ? <IcoCheck /> : <IcoCopy />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre>{children}</pre>
    </div>
  )
}

const mdComponents: Components = {
  pre({ node, children }: any) {
    const codeNode = node?.children?.find((c: any) => c.tagName === 'code') ?? node?.children?.[0]
    const cls: string[] = (codeNode?.properties?.className as string[]) ?? []
    const lang = cls.find(c => c.startsWith('language-'))?.replace('language-', '') ?? ''
    const raw = textOf(codeNode).replace(/\n$/, '')
    return (
      <CodeBlock lang={lang} raw={raw}>
        {children}
      </CodeBlock>
    )
  },
}

/** 统一的 Markdown 渲染器：GFM 表格/任务列表 + 代码高亮 + 复制按钮 */
export default function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={mdComponents}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}

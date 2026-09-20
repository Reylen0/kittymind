/**
 * UI 预览入口（仅用于浏览器内视觉验证，不参与应用运行）
 * 在 App 加载前注入 mock 的 window.kitty 桥与演示数据。
 */

const now = Date.now()

const DEMO_MARKDOWN = `## 文本文档创建成功

- **文件名：** \`poem.txt\`
- **路径：** \`E:\\workspace\\kittymind\\ws1\\poem.txt\`
- **内容：** 李白的《静夜思》

如果你想说换一首诗，或者写入其他内容，随时告诉我哦！🐱

---

下面是一段代码示例：

\`\`\`python
def greet(name: str) -> str:
    """向用户打招呼"""
    return f"你好，{name}！"

for who in ["Kitty", "Mind"]:
    print(greet(who))
\`\`\`

> 提示：工具调用结果支持折叠查看，代码块右上角可以一键复制。

| 模型 | 上下文 | 备注 |
|------|--------|------|
| DeepSeek | 64K | 默认 |
| Qwen | 32K | 备选 |`

const mockMessages = [
  {
    seq: 0,
    role: 'user',
    content: '创建一个文本文档，写入一首诗，再用一段 python 代码演示输出',
    ts: now - 60000,
  },
  {
    seq: 1,
    role: 'assistant',
    content: '',
    tool_calls: [
      { id: 'c1', type: 'function', function: { name: 'glob', arguments: '{"pattern": "*.txt", "path": "E:\\\\workspace\\\\kittymind\\\\ws1"}' } },
      { id: 'c2', type: 'function', function: { name: 'file_read', arguments: '{"path": "E:\\\\workspace\\\\kittymind\\\\ws1\\\\poem.txt"}' } },
    ],
    ts: now - 55000,
  },
  {
    seq: 2,
    role: 'tool',
    tool_call_id: 'c1',
    // 真实后端随行下发名字/入参（分页切段也不丢）；preview 照此契约造数据
    tool_name: 'glob',
    tool_args: '{"pattern": "*.txt", "path": "E:\\\\workspace\\\\kittymind\\\\ws1"}',
    content: 'E:\\workspace\\kittymind\\ws1\\poem.txt\n\n共 1 个文件',
    ts: now - 54000,
  },
  {
    seq: 3,
    role: 'tool',
    tool_call_id: 'c2',
    tool_name: 'file_read',
    tool_args: '{"path": "E:\\\\workspace\\\\kittymind\\\\ws1\\\\poem.txt"}',
    content: '静夜思\n床前明月光，疑是地上霜。\n举头望明月，低头思故乡。\n\n(4 行, 46 字节)',
    ts: now - 53500,
  },
  {
    seq: 4,
    role: 'assistant',
    content: DEMO_MARKDOWN,
    ts: now - 50000,
  },
]

// 预览用：模拟「更早的一页」，点「加载更早的消息」时返回
const mockOlderMessages = [
  { seq: -2, role: 'user', content: '（更早）帮我把工作区里的 txt 都找出来', ts: now - 120000 },
  { seq: -1, role: 'assistant', content: '（更早）好的，我先看一下目录结构。', ts: now - 110000 },
]
let olderServed = false

const emptyView = new URLSearchParams(window.location.search).has('empty')
// 预览用：强制主题，如 preview.html?theme=dark
const themeParam = new URLSearchParams(window.location.search).get('theme')
if (themeParam === 'dark') document.documentElement.dataset.theme = 'dark'
// 预览用：强制显示上下文圆环 tooltip，如 preview.html?tip
if (new URLSearchParams(window.location.search).has('tip')) {
  document.documentElement.dataset.forceTip = '1'
}
// 预览用：自动打开第一个演示会话（含 mock 消息），如 preview.html?chat
if (new URLSearchParams(window.location.search).has('chat')) {
  setTimeout(() => {
    document.querySelector<HTMLElement>('.session-item')?.click()
  }, 60)
}

const w = window as any
w.kitty = {
  listSessions: async () => emptyView
    ? []
    : [
    { id: 'demo-1', title: '演示：写诗与代码高亮', created_at: new Date(now - 60000).toISOString(), workspace_id: null },
    { id: 'demo-2', title: '修复 WebSocket 重连逻辑', created_at: new Date(now - 86400000).toISOString(), workspace_id: 'ws-1' },
    { id: 'demo-3', title: '调研桌宠动画方案', created_at: new Date(now - 172800000).toISOString(), workspace_id: 'ws-1' },
  ],
  listWorkspaces: async () => [
    { id: 'ws-1', name: 'kittymind', path: 'E:\\class\\roadmap\\Agent\\code\\kittymind', created_at: '' },
  ],
  getSession: async (id: string, opts: { limit?: number; beforeSeq?: number } = {}) => {
    const header = {
      id, title: '演示', created_at: new Date(now - 60000).toISOString(),
      workspace_id: null, used_tokens: 24000, total_tokens: 64000,
    }
    if (id !== 'demo-1') return { header, messages: [] }
    // 模拟分页：首屏给一页 + has_more，带游标回传时给更早的一页
    if (typeof opts.beforeSeq === 'number') {
      if (olderServed) return { header, messages: [], has_more: false, cursor: null }
      olderServed = true
      return { header, messages: mockOlderMessages, has_more: false, cursor: -2 }
    }
    return { header, messages: mockMessages, has_more: true, cursor: 0 }
  },
  sendMessage: async () => {},
  cancelTurn: async () => {},
  deleteSession: async () => {},
  createWorkspace: async () => null,
  selectWorkspace: async () => null,
  respondPermission: async () => {},
  windowControl: () => {},
  on: (_: string, __: (d: any) => void) => () => {},
}

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/noto-sans-sc'
import '@fontsource-variable/fraunces'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './style.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

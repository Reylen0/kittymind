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
    role: 'user',
    content: '创建一个文本文档，写入一首诗，再用一段 python 代码演示输出',
    ts: now - 60000,
  },
  {
    role: 'assistant',
    content: '',
    tool_calls: [{ function: { name: 'file_write', arguments: '{}' } }],
    ts: now - 55000,
  },
  {
    role: 'tool',
    tool_call_id: 'c1',
    content: '已写入: E:\\workspace\\kittymind\\ws1\\poem.txt (8 行, 113 字节)',
    ts: now - 54000,
  },
  {
    role: 'assistant',
    content: DEMO_MARKDOWN,
    ts: now - 50000,
  },
]

const emptyView = new URLSearchParams(window.location.search).has('empty')
// 预览用：强制主题，如 preview.html?theme=dark
const themeParam = new URLSearchParams(window.location.search).get('theme')
if (themeParam === 'dark') document.documentElement.dataset.theme = 'dark'
// 预览用：强制显示上下文圆环 tooltip，如 preview.html?tip
if (new URLSearchParams(window.location.search).has('tip')) {
  document.documentElement.dataset.forceTip = '1'
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
  getSession: async (id: string) => ({
    id,
    title: '演示',
    messages: id === 'demo-1' ? mockMessages : [],
  }),
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

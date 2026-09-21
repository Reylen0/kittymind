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
// 预览用：?switch —— 两个自由会话（同在「对话」组，当前会话所在组会自动展开），
// 用于验证「切走再切回」时审批弹窗 / 输入草稿 / 消息是否保留
const switchView = new URLSearchParams(window.location.search).has('switch')
// 预览用：?deep —— 当前会话藏在「最后一个工作区」里，上方还有一整列折叠的工作区组，
// 用于验证「启动/切换会话时自动展开路径 + 滚到当前会话」
const deepView = new URLSearchParams(window.location.search).has('deep')
const deepWorkspaces = Array.from({ length: 24 }, (_, i) => ({
  id: `ws-${i}`, name: `工作区 ${String(i + 1).padStart(2, '0')}`,
  path: `E:\\ws\\${i}`, created_at: '',
}))
const deepSessions = [
  // 列表首条 = App 启动自动选中的那条；它落在最后一个工作区里
  { id: 'deep-target', title: '当前会话：藏在最后一个工作区', created_at: new Date(now).toISOString(), workspace_id: 'ws-23' },
  ...deepWorkspaces.map((ws, i) => ({
    id: `deep-${i}`, title: `折叠分组 ${i + 1} 的对话`,
    created_at: new Date(now - (i + 1) * 1000).toISOString(), workspace_id: ws.id,
  })),
  // 与当前会话同组的兄弟项，用于验证「点选另一个会话时重新定位」
  { id: 'deep-sib-1', title: '同组：兄弟会话 1', created_at: new Date(now - 500).toISOString(), workspace_id: 'ws-23' },
  { id: 'deep-sib-2', title: '同组：兄弟会话 2（切换目标）', created_at: new Date(now - 400).toISOString(), workspace_id: 'ws-23' },
]
// 预览用：?search —— 给 searchSessions 一份固定结果，用于脱离 Python 后端调
// 「内容匹配」段的渲染（分组、片段截断、高亮区间、命中数）。marks 与 text 的
// 对应关系按真实后端的口径手写：偏移相对 text，含省略号在内。
const searchView = new URLSearchParams(window.location.search).has('search')
const mockSearchGroups = [
  {
    session_id: 'demo-1', title: '演示：写诗与代码高亮',
    hits: [
      { seq: 3, role: 'user',      text: '我们来聊聊上下文压缩的实现思路', marks: [[8, 10]] as Array<[number, number]> },
      { seq: 7, role: 'assistant', text: '…多层阈值触发的压缩管线，先做微压缩再做轨迹压缩…', marks: [[8, 10], [16, 18], [22, 24]] as Array<[number, number]> },
    ],
  },
  {
    session_id: 'demo-2', title: '修复 WebSocket 重连逻辑',
    hits: [
      { seq: 12, role: 'user', text: '这里也提到了压缩，不过是另一个意思', marks: [[6, 8]] as Array<[number, number]> },
    ],
  },
]

// 预览用：强制主题，如 preview.html?theme=dark
const themeParam = new URLSearchParams(window.location.search).get('theme')
if (themeParam === 'dark') document.documentElement.dataset.theme = 'dark'
// 预览用：强制显示上下文圆环 tooltip，如 preview.html?tip
if (new URLSearchParams(window.location.search).has('tip')) {
  document.documentElement.dataset.forceTip = '1'
}
// 预览用：自动打开第一个演示会话（含 mock 消息），如 preview.html?chat
// ?ws 则打开工作区内的演示会话（定位逻辑会自动展开该工作区），如 preview.html?ws
{
  const sp = new URLSearchParams(window.location.search)
  if (sp.has('chat') || sp.has('ws')) {
    const idx = sp.has('ws') ? 1 : 0
    const tryClick = () => {
      const el = document.querySelectorAll<HTMLElement>('.session-item')[idx]
      el ? el.click() : setTimeout(tryClick, 80)
    }
    tryClick()
  }
}

// 预览用：mock 的事件订阅真实可分发，并暴露 window.__permFire 供自动化触发。
// 同时模拟后端「待审批可查询」——会话 B 从未挂载过时事件无处可去，只有补拉
// 才能救回来（真实后端由 permission/pending 接口承担这个职责）。
const listeners: Record<string, Array<(d: any) => void>> = {}
const mockPending: Record<string, any[]> = {}
;(window as any).__permFire = (event: string, data: any) => {
  if (event === 'tool.permission_request' && data?.session_id) {
    (mockPending[data.session_id] ??= []).push({
      request_id: data.request_id, tool: data.tool,
      args: data.args, reason: data.reason, session_id: data.session_id,
    })
  } else if (event === 'tool.permission_expired') {
    for (const k of Object.keys(mockPending)) {
      mockPending[k] = mockPending[k].filter(r => r.request_id !== data.request_id)
    }
  }
  for (const cb of [...(listeners[event] ?? [])]) cb(data)
}

const w = window as any
w.__overlayCalls = [] as Array<{ color: string; symbolColor: string }>

// 可变的会话/工作区清单：让删除类操作在预览里真实生效（删工作区 → 其会话
// 移回「对话」分组），供自动化脚本断言移动行为。
const mockSessions: Array<{ id: string; title: string; created_at: string; workspace_id: string | null }> = switchView
  ? [
    { id: 'demo-1', title: '会话 A：等审批中', created_at: new Date(now - 60000).toISOString(), workspace_id: null },
    { id: 'demo-2', title: '会话 B：另一个', created_at: new Date(now - 120000).toISOString(), workspace_id: null },
  ]
  : [
    { id: 'demo-1', title: '演示：写诗与代码高亮', created_at: new Date(now - 60000).toISOString(), workspace_id: null },
    { id: 'demo-2', title: '修复 WebSocket 重连逻辑', created_at: new Date(now - 86400000).toISOString(), workspace_id: 'ws-1' },
    { id: 'demo-3', title: '调研桌宠动画方案', created_at: new Date(now - 172800000).toISOString(), workspace_id: 'ws-1' },
  ]
const mockWsList = [
  { id: 'ws-1', name: 'kittymind', path: 'E:\\class\\roadmap\\Agent\\code\\kittymind', created_at: '' },
  ...(deepView ? deepWorkspaces : []),
]

/** 已归档会话 id 集合（预览页可变状态，供归档交互的自动化验证）。 */
const archivedIds = new Set<string>()

w.kitty = {
  // 归档状态在预览里也要"真能被改"：验证脚本会点归档按钮、再断言列表变化。
  // 默认不给任何会话打归档标记，所以既有脚本看到的默认列表与改动前一致。
  listSessions: async (opts: { includeArchived?: boolean } = {}) => {
    const base = emptyView ? [] : deepView ? deepSessions : mockSessions
    const flagged = base.map(s => ({ ...s, archived: archivedIds.has(s.id) }))
    return opts.includeArchived ? flagged : flagged.filter(s => !s.archived)
  },
  listWorkspaces: async () => mockWsList,
  getSession: async (id: string, opts: { limit?: number; beforeSeq?: number } = {}) => {
    const header = {
      id, title: '演示', created_at: new Date(now - 60000).toISOString(),
      workspace_id: null, used_tokens: 24000, total_tokens: 64000,
    }
    if (id !== 'demo-1') {
      return {
        header,
        messages: switchView
          ? [{ seq: 0, role: 'user', content: '这是会话 B 自己的一条消息', ts: now - 120000 }]
          : [],
      }
    }
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
  deleteSession: async (id: string) => {
    const at = mockSessions.findIndex(s => s.id === id)
    if (at >= 0) mockSessions.splice(at, 1)
    archivedIds.delete(id)
    return { deleted: at >= 0 }
  },
  // 归档 / 取消归档：只改侧栏可见性，会话本身不动（与后端契约一致）
  setSessionArchived: async (id: string, archived = true) => {
    if (archived) archivedIds.add(id)
    else archivedIds.delete(id)
    return { ok: true, archived }
  },
  // ?search 场景给固定结果；其余场景返回空数组（等价于「没搜到」）。
  // 带 200ms 延迟，好观察防抖与「搜索中…」占位。
  searchSessions: async (query: string) => {
    await new Promise(r => setTimeout(r, 200))
    if (!searchView || !query.trim()) return []
    return mockSearchGroups
  },
  createWorkspace: async () => null,
  // 与真实后端同语义：工作区删除，其下会话保留但解除归属（workspace_id 置空）
  deleteWorkspace: async (id: string) => {
    const at = mockWsList.findIndex(x => x.id === id)
    if (at < 0) return { deleted: false, moved_sessions: 0 }
    mockWsList.splice(at, 1)
    let moved = 0
    for (const s of mockSessions) {
      if (s.workspace_id === id) { s.workspace_id = null; moved += 1 }
    }
    return { deleted: true, moved_sessions: moved }
  },
  selectWorkspace: async () => null,
  respondPermission: async (id: string) => {
    for (const k of Object.keys(mockPending)) {
      mockPending[k] = mockPending[k].filter(r => r.request_id !== id)
    }
  },
  windowControl: () => {},
  // 记录 titleBarOverlay 调色请求，供自动化断言「弹窗打开时 overlay 变暗、关闭还原」
  setNativeTheme: async (opts: { color: string; symbolColor: string }) => {
    w.__overlayCalls.push(opts)
  },
  agentStatus: async () => ({ name: 'kitty', model: 'demo-model', running_sessions: [] }),
  getPendingPermissions: async (sessionId: string) => ({
    pending: mockPending[sessionId] ?? [],
  }),
  getUsageReport: async (opts: { groupBy?: string } = {}) => ({
    group_by: opts.groupBy || 'model',
    groups:
      opts.groupBy === 'day'
        ? [
            { key: '2026-09-21', prompt_tokens: 128000, completion_tokens: 32000, n_calls: 42, cost: 1.18 },
            { key: '2026-09-20', prompt_tokens: 560000, completion_tokens: 98000, n_calls: 128, cost: 4.53 },
          ]
        : opts.groupBy === 'session'
          ? [
              { key: 'df1c9df3-fb0d-4f1f-abaf-15a671ad4251', prompt_tokens: 11400, completion_tokens: 207, n_calls: 4, cost: 0.08, title: '修复审批弹窗切会话丢失的 bug', workspace: 'kittymind' },
              { key: 'fd9bc3ff-2d74-473d-ab58-d4f9941a9c13', prompt_tokens: 399, completion_tokens: 879, n_calls: 5, cost: 0.02, title: 'phase16 用量与成本追踪方案', workspace: null },
              { key: '86eaad94-ace6-4c46-bf39-e1fd6de86570', prompt_tokens: 11, completion_tokens: 265, n_calls: 4, cost: 0.01, title: null, workspace: null },
            ]
          : [
              { key: 'claude-sonnet-4-6', prompt_tokens: 680000, completion_tokens: 125000, n_calls: 164, cost: 3.92 },
              { key: 'claude-haiku-4-5', prompt_tokens: 8000, completion_tokens: 5000, n_calls: 6, cost: 0.03 },
            ],
    total: { prompt_tokens: 688000, completion_tokens: 130000, n_calls: 170, cost: 5.71 },
  }),
  on: (event: string, cb: (d: any) => void) => {
    (listeners[event] ??= []).push(cb)
    return () => {
      listeners[event] = (listeners[event] ?? []).filter(x => x !== cb)
    }
  },
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

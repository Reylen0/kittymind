# KittyMind 通用桌面 Agent 架构文档

## 目录

1. [项目定位](#1-项目定位)
2. [整体架构](#2-整体架构)
3. [技术选型](#3-技术选型)
4. [模块详解](#4-模块详解)
5. [核心数据流](#5-核心数据流)
6. [IPC 通信协议](#6-ipc-通信协议)
7. [会话持久化设计](#7-会话持久化设计)
8. [流式输出设计](#8-流式输出设计)
9. [桌宠情绪系统](#9-桌宠情绪系统)
10. [开发阶段与任务清单](#10-开发阶段与任务清单)
11. [目录结构规划](#11-目录结构规划)
12. [可复用资产](#12-可复用资产)

---

## 1. 项目定位

KittyMind 是一个**通用桌面 Agent**，具备：

- 多 LLM 支持（DeepSeek / OpenAI / Claude / 本地 Ollama）
- 全程流式输出（含工具调用步骤）
- 桌宠悬浮窗（情绪驱动动画，始终置顶）
- 桌面原生能力（Shell 执行、截图、剪贴板）
- MCP 工具扩展协议
- 会话持久化（JSONL 追加写入）
- 全局热键唤起

**平台**：Windows 优先，架构上预留跨平台空间。

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Electron 主进程                          │
│                                                             │
│   WindowManager │ PythonBridge │ HotkeyManager │ TrayManager│
└────────┬────────┴──────┬───────┴───────────────┴────────────┘
         │               │
         │    ipcMain / ipcRenderer (contextBridge)
         │               │
┌────────▼───────┐ ┌─────▼──────────┐ ┌──────────────────────┐
│  Chat Window   │ │   Pet Window   │ │   Quick Overlay      │
│  (Renderer)    │ │  (Renderer)    │ │   (Renderer)         │
│                │ │                │ │                      │
│ • 流式聊天 UI  │ │ • Canvas 动画  │ │ • 全局热键唤起       │
│ • 工具调用卡片 │ │ • 情绪状态机   │ │ • 极简输入框         │
│ • 会话侧边栏   │ │ • 拖拽 / 气泡  │ │                      │
│ • 设置面板     │ │                │ │                      │
└────────┬───────┘ └────────────────┘ └──────────────────────┘
         │
         │   WebSocket JSON-RPC  ws://127.0.0.1:8765
         │
┌────────▼────────────────────────────────────────────────────┐
│                    Python Agent Core                        │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │                    KittyAgent                        │  │
│  │  • 全程流式 ReAct 循环                                │  │
│  │  • 每步 stream_with_tools()                          │  │
│  │  • 记忆召回 / 上下文压缩 / Goal 评估                  │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │ LLM Pool │  │ Tool Reg │  │ Session  │  │  Memory   │  │
│  │ (多厂商) │  │ (插件化) │  │ Manager  │  │  Store    │  │
│  └──────────┘  └──────────┘  └──────────┘  └───────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              asyncio Event Bus                       │  │
│  │   agent.thinking / tool_call / chunk / done / error  │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │   MCP    │  │ Compactor│  │  Tasks   │  │  Cron     │  │
│  │ Manager  │  │ (4步压缩)│  │  Store   │  │  Manager  │  │
│  └──────────┘  └──────────┘  └──────────┘  └───────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 技术选型

| 层次 | 技术 | 理由 |
|------|------|------|
| 桌面容器 | **Electron 36+** | 透明窗口、全局热键、系统托盘成熟；跨平台 |
| 前端框架 | **React 18 + TypeScript** | 生态完善，组件库丰富 |
| 桌宠动画 | **Canvas API / Pixi.js** | 精灵图帧动画性能好，支持透明背景 |
| Agent 核心 | **Python 3.10+ + asyncio** | LLM 生态最完整，AI 库无缝衔接 |
| IPC 通道 | **websockets（Python）** | 支持全双工流式推送，JSON-RPC 协议 |
| LLM 调用 | **OpenAI SDK（兼容多厂商）** | 统一接口，DeepSeek/Qwen/Ollama 全支持 |
| 工具扩展 | **MCP（Model Context Protocol）** | 标准生态，stdio + SSE 两种传输 |
| 长期记忆 | **文件系统（Markdown + YAML frontmatter）** | 零依赖，人类可读，易于调试 |
| 会话持久化 | **JSONL 追加写入** | 参考 deepseek-harness，崩溃安全 |
| 上下文压缩 | **四步管线** | 参考 codeagent，无损到有损递进 |
| 打包 | **electron-builder** | Windows NSIS 安装包，支持自动更新 |

---

## 4. 模块详解

### 4.1 Python 侧

#### `baseagent/` — 基础框架（已有，需改造）

```
baseagent/
├── core/
│   ├── llm.py              # BaseAgentLLM：统一 LLM 客户端
│   ├── llm_adapters.py     # OpenAIAdapter（支持所有兼容接口）
│   ├── llm_response.py     # LLMResponse 封装
│   ├── agent.py            # Agent 抽象基类
│   └── message.py          # Message（需补 tool_calls / tool_call_id 字段）
├── agent/
│   ├── tool_agent.py       # ReAct 循环（stream_run 有 bug，待修复）
│   ├── simple_agent.py     # 无工具的简单 Agent
│   └── supervisor.py       # 多 Agent 路由
├── tools/
│   ├── base.py             # BaseTool（Pydantic 参数 + 自动 schema）
│   ├── registry.py         # ToolRegistry
│   ├── executor.py         # ToolExecutor
│   └── builtin/            # calculator / file_reader / http / get_time
├── memory/
│   ├── buffer_memory.py    # 滑动窗口内存历史
│   └── summary_memory.py   # LLM 摘要压缩历史
├── events/                 # 【新增 Step 2】
│   └── bus.py              # asyncio Pub/Sub 事件总线
├── session/                # 【新增 Step 3】
│   ├── manager.py          # SessionManager：CRUD + 绑定 memory
│   └── store.py            # JSONL 文件持久化
├── rag/                    # 向量检索（embedder/splitter/loader/store/retriever）
├── prompts/                # PromptTemplate + hub
└── callbacks/              # BaseCallBack 钩子接口
```

#### `kittymind/` — 桌面 Agent 实现（新增）

```
kittymind/
├── agent.py                # KittyAgent：继承并重写 stream_run（全程流式）
├── config.py               # KittyConfig：从 .env / settings.json 读取
└── tools/
    ├── shell_tool.py       # PowerShell/cmd 执行（含危险命令拦截）
    ├── screenshot_tool.py  # 截图 → base64
    ├── clipboard_tool.py   # 读写剪贴板
    ├── open_tool.py        # 打开文件 / URL
    └── (移植自 codeagent)
        ├── glob_tool.py
        ├── grep_tool.py
        ├── file_read_tool.py
        ├── file_write_tool.py
        ├── file_edit_tool.py
        ├── ls_tool.py
        └── git_tool.py
```

#### `server/` — WebSocket JSON-RPC 服务端（新增）

```
server/
├── app.py          # 启动入口：python server/app.py --port 8765
├── ws_server.py    # WebSocket 服务器：接收请求、推送事件
└── rpc_handler.py  # 方法路由：turn/run、session/*、agent/*
```

### 4.2 Electron 侧（新增）

```
electron/
├── package.json
├── main.js             # 主进程：启动 Python 子进程，创建所有窗口
├── preload.js          # contextBridge：安全暴露 ipcRenderer API
└── src/
    ├── renderer/       # React 聊天窗口
    │   ├── App.tsx
    │   ├── ChatView.tsx      # 消息列表 + 输入框
    │   ├── MessageItem.tsx   # 流式文字 + 工具调用卡片
    │   ├── SessionList.tsx   # 会话侧边栏
    │   └── Settings.tsx      # 设置面板
    ├── pet/            # React 桌宠窗口
    │   ├── PetApp.tsx
    │   ├── SpriteRenderer.tsx   # Canvas 帧动画
    │   ├── EmotionMachine.ts    # 情绪状态机
    │   └── SpeechBubble.tsx     # 对话气泡
    └── overlay/        # 快速输入悬浮框
        └── OverlayApp.tsx
```

---

## 5. 核心数据流

### 5.1 完整一轮对话流程

```
用户在 Chat Window 输入 "帮我查一下当前时间"
  │
  ▼
Electron Renderer → ipcRenderer.invoke('turn/run', { text, session_id })
  │
  ▼
Electron Main → WebSocket → Python ws_server.py
  │
  ▼
KittyAgent.stream_run(session_id, input_text)
  │
  ├─ 1. 召回相关记忆，临时追加到 system_prompt
  ├─ 2. 构建 messages（system + 历史上下文 + 用户消息）
  │
  └─ ReAct 循环（每步都流式）:
       │
       ├─ Step 1: llm.stream_with_tools(messages, tools_schema)
       │   ├─ chunk "我" → emit('agent.chunk', delta) → WS → Electron → UI 显示
       │   ├─ chunk "来" → emit('agent.chunk', delta) → WS → Electron → UI 显示
       │   ├─ tool_call delta 累积...
       │   └─ 流结束，检测到 tool_call: get_current_time
       │
       ├─ emit('agent.tool_call', { tool: 'get_current_time' })
       │   └─ WS → Electron → UI 显示工具调用卡片 / Pet 切换 WORKING 动画
       │
       ├─ 执行 GetCurrentTimeTool → "2026-09-02 14:30:00"
       ├─ emit('agent.tool_result', { result })
       │
       └─ Step 2: llm.stream_with_tools(messages_with_tool_result, tools_schema)
           ├─ chunk "当前时间是..." → emit → WS → Electron → UI 实时显示
           ├─ 流结束，无 tool_call
           └─ 完成
  │
  ▼
emit('agent.done', { session_id })
  └─ 保存本轮到 JSONL 文件
  └─ 异步提取记忆
  └─ Pet 切换 HAPPY 动画
```

### 5.2 流式输出架构（解决 tool loop + streaming 的矛盾）

参考 deepseek-harness 的核心设计：**每步都流式，工具调用也在流里检测**。

```python
# kittymind/agent.py - KittyAgent.stream_run()

async def stream_run(self, session_id, input_text):
    messages = self._build_messages(session_id, input_text)
    tools_schema = self.tool_registry.get_schemas()

    for step in range(self.max_iterations):
        # ① 每步都用流式，无论是否有工具调用
        text_chunks = []
        tool_calls_acc = {}

        async for event in self.llm.stream_with_tools(messages, tools_schema):
            if event.type == 'text_delta':
                await self.event_bus.emit('agent.chunk', event.delta)
                yield event.delta                      # ← 直接推给用户
                text_chunks.append(event.delta)
            elif event.type == 'tool_call_delta':
                accumulate(tool_calls_acc, event)      # 累积 tool_call JSON

        final_text = ''.join(text_chunks)

        # ② 流结束后检测
        if not tool_calls_acc:
            break   # 无工具调用，这次流就是最终回答，已推完

        # ③ 有工具调用 → 执行 → 追加结果 → 继续下一步
        await self.event_bus.emit('agent.tool_call', ...)
        messages.append(assistant_msg_with_tool_calls(tool_calls_acc))
        for tc in tool_calls_acc.values():
            result = await self.tool_executor.execute_async(tc)
            await self.event_bus.emit('agent.tool_result', ...)
            messages.append(tool_result_msg(tc, result))

    await self._save_turn(session_id, messages, final_text)
    await self.event_bus.emit('agent.done', { 'session_id': session_id })
```

关键：`OpenAIAdapter` 新增 `stream_with_tools()` 方法，返回同时含 `text_delta` 和 `tool_call_delta` 的异步生成器。OpenAI 流式 API 原生支持。

---

## 6. IPC 通信协议

### 6.1 Electron ↔ Python（WebSocket JSON-RPC）

**客户端请求：**
```jsonc
// 发送消息（开启一轮对话）
{ "id": 1, "method": "turn/run", "params": { "text": "帮我...", "session_id": "s1" } }

// 会话操作
{ "id": 2, "method": "session/create", "params": { "title": "新会话" } }
{ "id": 3, "method": "session/list",   "params": {} }
{ "id": 4, "method": "session/delete", "params": { "session_id": "s1" } }

// 中断当前对话
{ "id": 5, "method": "turn/cancel", "params": { "session_id": "s1" } }
```

**服务端推送（无 id，服务端主动）：**
```jsonc
// 流式文字块
{ "method": "agent.chunk",       "params": { "delta": "你好", "session_id": "s1" } }

// 工具调用（驱动 Pet 动画 + UI 显示卡片）
{ "method": "agent.tool_call",   "params": { "tool": "shell", "args": "ls -la", "session_id": "s1" } }
{ "method": "agent.tool_result", "params": { "tool": "shell", "result": "...", "session_id": "s1" } }

// Agent 状态（驱动 Pet 情绪）
{ "method": "agent.thinking",    "params": { "session_id": "s1" } }
{ "method": "agent.done",        "params": { "session_id": "s1" } }
{ "method": "agent.error",       "params": { "error": "...",  "session_id": "s1" } }
```

### 6.2 Electron Main ↔ Renderer（ipcMain / contextBridge）

```typescript
// preload.js 暴露给 Renderer 的 API
contextBridge.exposeInMainWorld('kitty', {
  sendMessage: (text, sessionId) => ipcRenderer.invoke('turn/run', { text, sessionId }),
  onChunk:     (cb) => ipcRenderer.on('agent.chunk', cb),
  onToolCall:  (cb) => ipcRenderer.on('agent.tool_call', cb),
  onDone:      (cb) => ipcRenderer.on('agent.done', cb),
  onPetEmotion:(cb) => ipcRenderer.on('pet.emotion', cb),
  // ...
})
```

---

## 7. 会话持久化设计

参考 deepseek-harness 的 JSONL 方案：**追加写入，每会话一个文件，崩溃安全**。

### 目录结构
```
~/.kittymind/
└── sessions/
    ├── s_abc123/
    │   └── session.jsonl
    └── s_def456/
        └── session.jsonl
```

### 文件格式
```jsonl
{"version":1,"id":"s_abc123","title":"时间查询","created_at":1725264000}
{"seq":0,"role":"user","content":"帮我查一下当前时间","ts":1725264001}
{"seq":1,"role":"assistant","content":null,"tool_calls":[{"id":"tc1","name":"get_current_time","args":"{}"}],"ts":1725264002}
{"seq":2,"role":"tool","tool_call_id":"tc1","content":"2026-09-02 14:30:00","ts":1725264002}
{"seq":3,"role":"assistant","content":"当前时间是 2026-09-02 14:30:00。","ts":1725264003}
```

### 读取时的崩溃恢复
```python
def load_session(path):
    events = []
    with open(path, encoding='utf-8') as f:
        header = json.loads(f.readline())   # 第1行：header
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                break   # torn tail：停在最后一条完整记录
    return header, events
```

---

## 8. 流式输出设计

### `OpenAIAdapter.stream_with_tools()` 实现

```python
async def stream_with_tools(self, messages, tools=None):
    """
    流式调用，同时返回 text_delta 和 tool_call_delta 事件。
    OpenAI 流式 API 原生支持在同一 stream 里混合返回文字和 tool_calls。
    """
    tool_calls_buf = {}   # { index: { id, name, arguments_parts[] } }

    response = self._client.chat.completions.create(
        model=self.model,
        messages=messages,
        tools=tools or [],
        stream=True,
        **self.default_kwargs,
    )
    for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue

        # 文字内容
        if delta.content:
            yield StreamEvent(type='text_delta', delta=delta.content)

        # 工具调用（分批 delta 累积）
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tool_calls_buf:
                    tool_calls_buf[idx] = {'id': '', 'name': '', 'args': ''}
                if tc_delta.id:
                    tool_calls_buf[idx]['id'] += tc_delta.id
                if tc_delta.function.name:
                    tool_calls_buf[idx]['name'] += tc_delta.function.name
                if tc_delta.function.arguments:
                    tool_calls_buf[idx]['args'] += tc_delta.function.arguments
                yield StreamEvent(type='tool_call_delta', data=tool_calls_buf[idx])

    # 流结束后输出完整的 tool_calls（如有）
    if tool_calls_buf:
        yield StreamEvent(type='tool_calls_done', tool_calls=list(tool_calls_buf.values()))
```

---

## 9. 桌宠情绪系统

### 情绪状态机

```
Agent 事件              Pet 情绪状态        动画序列
────────────────────────────────────────────────────
agent.thinking        → THINKING         → 摸下巴 / 眼睛转动
agent.tool_call(shell)→ WORKING          → 敲键盘
agent.tool_call(web)  → SEARCHING        → 望远镜
agent.tool_call(file) → WORKING          → 翻文件
agent.done(success)   → HAPPY            → 跳起来 / 竖大拇指
agent.error           → SAD             → 低头 / 冒汗
user.inactive > 30min → SLEEPY           → 打哈欠 / 趴下
time: 00:00-06:00     → NIGHT            → 戴睡帽
idle                  → IDLE             → 随机小动作（循环）
user.interact         → ALERT            → 抬头看
```

### Pet 窗口技术实现

```typescript
// electron/main.js
const petWin = new BrowserWindow({
  transparent: true,     // 透明背景
  frame: false,          // 无边框
  alwaysOnTop: true,     // 始终置顶
  skipTaskbar: true,     // 不在任务栏显示
  resizable: false,
  webPreferences: { preload: petPreload }
})
petWin.setIgnoreMouseEvents(true, { forward: true }) // 点击穿透（可切换）
```

```typescript
// electron/src/pet/SpriteRenderer.tsx
// Canvas 帧动画：从精灵图中按帧号截取并绘制
const draw = (frameIndex: number) => {
  ctx.clearRect(0, 0, W, H)
  const sx = (frameIndex % COLS) * FRAME_W
  const sy = Math.floor(frameIndex / COLS) * FRAME_H
  ctx.drawImage(spriteSheet, sx, sy, FRAME_W, FRAME_H, 0, 0, W, H)
}
```

---

## 10. 开发阶段与任务清单

### Phase 1 — Agent Core 修复（Python 侧）

**目标**：baseagent 跑通全程流式工具调用，无 bug，支持 async。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 1.1 | 修复 `stream_run` 重复调用 bug | `baseagent/agent/tool_agent.py` | 工具循环结束后不再调 `think()`，改为全程流式 |
| 1.2 | 新增 `stream_with_tools()` | `baseagent/core/llm_adapters.py` | 同时 yield `text_delta` 和 `tool_call_delta` |
| 1.3 | 补全 `Message` 模型 | `baseagent/core/message.py` | 加 `tool_calls`、`tool_call_id` 字段 |
| 1.4 | 给 `BaseAgentLLM` 加 `provider` 属性 | `baseagent/core/llm.py` | 从 `base_url` 或 model 名称推断 |
| 1.5 | 用 `asyncio.to_thread` 包同步调用 | `baseagent/agent/tool_agent.py` | 为 WebSocket server 准备 |

### Phase 2 — 事件总线

**目标**：Agent 状态变化能通知 WebSocket server 推给前端。

| # | 任务 | 文件 |
|---|------|------|
| 2.1 | 实现 asyncio Pub/Sub 事件总线 | `baseagent/events/bus.py` |
| 2.2 | 在 `KittyAgent` 关键节点 emit 事件 | `kittymind/agent.py` |
| 2.3 | 事件类型定义 | `baseagent/events/types.py` |

**事件清单：**
```python
# 必须支持的事件
"agent.start"        # 开始处理输入
"agent.thinking"     # LLM 推理中（每次 LLM 调用前）
"agent.chunk"        # 流式文字 delta
"agent.tool_call"    # 调用了某个工具
"agent.tool_result"  # 工具返回结果
"agent.done"         # 本轮完成
"agent.error"        # 出错
```

### Phase 3 — 会话持久化

**目标**：重启后会话历史不丢失。

| # | 任务 | 文件 |
|---|------|------|
| 3.1 | JSONL 存储层 | `baseagent/session/store.py` |
| 3.2 | SessionManager（CRUD + 关联 memory） | `baseagent/session/manager.py` |
| 3.3 | 读时崩溃恢复（torn tail 处理） | `baseagent/session/store.py` |
| 3.4 | 会话标题自动生成（首条消息截取或 LLM 生成） | `baseagent/session/manager.py` |

### Phase 4 — WebSocket JSON-RPC Server

**目标**：Python Agent 对外暴露标准接口，Electron 可以连接。

| # | 任务 | 文件 |
|---|------|------|
| 4.1 | WebSocket 服务器（websockets 库） | `server/ws_server.py` |
| 4.2 | JSON-RPC 方法路由 | `server/rpc_handler.py` |
| 4.3 | 流式事件推送（server push） | `server/ws_server.py` |
| 4.4 | Python 进程启动入口 | `server/app.py` |

**需实现的 RPC 方法：**
```
turn/run      → 开始一轮对话（流式事件 server push）
turn/cancel   → 中断当前对话
session/create → 创建会话
session/list   → 列出所有会话
session/get    → 获取单个会话（含历史）
session/delete → 删除会话
agent/status   → 当前 Agent 状态
```

### Phase 5 — 桌面工具集

**目标**：从 codeagent 移植 + 新增桌面专属工具。

| # | 任务 | 来源 | 改动 |
|---|------|------|------|
| 5.1 | `BashTool`（ShellTool） | codeagent 移植 | 改路径、改 import |
| 5.2 | `FileReadTool/WriteEditTool` | codeagent 移植 | 直接用 |
| 5.3 | `GlobTool/GrepTool/LsTool/GitTool` | codeagent 移植 | 直接用 |
| 5.4 | `MCPManager` | codeagent 移植 | 改配置文件路径 |
| 5.5 | `ContextCompactor`（四步压缩） | codeagent 移植 | 改存储路径 |
| 5.6 | `PermissionToolExecutor` | codeagent 移植 | `_ask_user()` 改为 emit WebSocket 事件等待用户在 GUI 确认 |
| 5.7 | `ScreenshotTool`（新增） | 新写 | `mss` 库截图 → base64 |
| 5.8 | `ClipboardTool`（新增） | 新写 | `pyperclip` 读写剪贴板 |

### Phase 6 — Electron 主进程骨架

**目标**：Electron 能启动 Python 子进程并创建窗口。

| # | 任务 | 文件 |
|---|------|------|
| 6.1 | Electron 项目初始化 | `electron/package.json` |
| 6.2 | 主进程：启动 Python server 子进程，等待就绪信号 | `electron/main.js` |
| 6.3 | 主进程：创建 Chat / Pet / Overlay 三个窗口 | `electron/main.js` |
| 6.4 | contextBridge：安全暴露 IPC API | `electron/preload.js` |
| 6.5 | 全局热键注册（Ctrl+Shift+Space） | `electron/main.js` |
| 6.6 | 系统托盘：状态图标 + 右键菜单 | `electron/main.js` |

### Phase 7 — React 聊天界面

**目标**：最小可用聊天 UI，支持流式显示和工具调用卡片。

| # | 任务 | 文件 |
|---|------|------|
| 7.1 | 消息列表（流式追加） | `ChatView.tsx` |
| 7.2 | 工具调用可视化卡片 | `MessageItem.tsx` |
| 7.3 | 输入框（支持 Shift+Enter 换行） | `ChatView.tsx` |
| 7.4 | 会话切换侧边栏 | `SessionList.tsx` |
| 7.5 | 基础设置面板（模型配置） | `Settings.tsx` |

### Phase 8 — 桌宠窗口

**目标**：透明悬浮窗，情绪动画跟随 Agent 状态变化。

| # | 任务 | 文件 |
|---|------|------|
| 8.1 | 透明无边框窗口 | `electron/main.js` |
| 8.2 | 精灵图帧动画（Canvas） | `SpriteRenderer.tsx` |
| 8.3 | 情绪状态机（订阅 Agent 事件） | `EmotionMachine.ts` |
| 8.4 | 对话气泡（显示回复摘要） | `SpeechBubble.tsx` |
| 8.5 | 拖拽 + 屏幕边缘检测 | `PetApp.tsx` |
| 8.6 | 右键上下文菜单（打开主窗口、静音等） | `PetApp.tsx` |

### Phase 9 — 端到端联调与打包

| # | 任务 |
|---|------|
| 9.1 | 端到端联调：输入 → 流式显示 → 工具卡片 → Pet 动画 → 历史持久化 |
| 9.2 | 权限弹窗联调（`PermissionToolExecutor` → WebSocket → Electron 弹框） |
| 9.3 | Python 打包为 .exe（PyInstaller）或随 Electron 分发 |
| 9.4 | electron-builder 打包 Windows NSIS 安装包 |
| 9.5 | 自动更新配置 |

---

## 11. 目录结构规划

```
kittymind/
├── baseagent/              # Agent 基础框架（现有 + 改造）
│   ├── core/
│   ├── agent/
│   ├── tools/
│   ├── memory/
│   ├── events/             # 【Phase 2 新增】
│   ├── session/            # 【Phase 3 新增】
│   ├── rag/
│   ├── prompts/
│   └── callbacks/
│
├── kittymind/              # 桌面 Agent 实现【Phase 1/5 新增】
│   ├── agent.py            # KittyAgent（全程流式 ReAct）
│   ├── config.py
│   └── tools/              # 桌面工具集
│
├── server/                 # WebSocket JSON-RPC 服务端【Phase 4 新增】
│   ├── app.py
│   ├── ws_server.py
│   └── rpc_handler.py
│
├── electron/               # Electron 桌面应用【Phase 6-8 新增】
│   ├── package.json
│   ├── main.js
│   ├── preload.js
│   └── src/
│       ├── renderer/       # 聊天窗口
│       ├── pet/            # 桌宠窗口
│       └── overlay/        # 快速输入悬浮框
│
├── assets/                 # 静态资源
│   └── pet/
│       └── sprites/        # 精灵图（各情绪动画帧）
│
├── .kittymind/             # 运行时数据目录（用户级）
│   ├── sessions/           # JSONL 会话文件
│   ├── memory/             # 长期记忆（Markdown）
│   ├── tasks/              # 任务图
│   ├── mcp.json            # MCP 服务器配置
│   └── settings.json       # 用户设置 + Shell Hooks
│
├── pyproject.toml
├── .env
├── CLAUDE.md
└── ARCHITECTURE.md         # 本文件
```

---

## 12. 可复用资产

以下模块来自 `E:\class\roadmap\Agent\code\codeagent\codeagent\`，可直接移植：

| 模块 | 原路径 | 移植方式 | 改动点 |
|------|--------|----------|--------|
| `BashTool` | `tools/bash_tool.py` | 复制 | import 路径 |
| `FileReadTool/WriteEditTool` | `tools/file_*.py` | 复制 | import 路径 |
| `GlobTool/GrepTool/LsTool` | `tools/glob/grep/ls_tool.py` | 复制 | import 路径 |
| `GitTool` | `tools/git_tool.py` | 复制 | import 路径 |
| `MCPManager` | `mcp_manager.py` | 复制 | 配置文件路径改为 `.kittymind/mcp.json` |
| `ContextCompactor` | `compactor.py` | 复制 | 存储路径改为 `.kittymind/` |
| `MemoryStore` | `memory_store.py` | 复制 | 路径改为 `.kittymind/memory/` |
| `MemoryExtract/Recall` | `memory_extract/recall.py` | 复制 | 直接用 |
| `TaskStore` | `task_system.py` | 复制 | 路径改为 `.kittymind/tasks/` |
| `BackgroundManager` | `background.py` | 复制 | 直接用 |
| `CronManager` | `cron.py` | 复制 | 路径改为 `.kittymind/` |
| `GoalController` | `goal.py` | 复制 | 直接用 |
| `ShellHook` | `hooks.py` | 复制 | 配置路径改为 `.kittymind/settings.json` |
| `PermissionToolExecutor` | `permission.py` | 复制 + 改造 | `_ask_user()` 改为 emit WebSocket 事件 |

**不直接复用、作为参考的**：

| 参考项目 | 参考内容 |
|----------|----------|
| deepseek-harness `agent-loop/src/agent.ts` | 全程流式 ReAct 循环的设计模式 |
| deepseek-harness `session-persistence-jsonl/` | JSONL 追加写入 + 崩溃恢复 |
| deepseek-harness `write-behind.ts` | 写后缓冲（桌面版可简化为同步写） |

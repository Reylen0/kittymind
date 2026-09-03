# KittyMind

通用桌面 Agent，具备流式对话、桌宠悬浮窗、多工具调用、会话持久化能力。

- **对话界面**：流式显示、工具调用卡片、多会话切换
- **桌宠**：情绪驱动动画，跟随 Agent 状态变化（思考 / 工作 / 开心 / 睡眠）
- **快速唤起**：`Ctrl+Shift+Space` 弹出悬浮输入框
- **会话持久化**：JSONL 追加写入，重启不丢历史（`~/.kittymind/sessions/`）
- **工具集**：Shell、文件读写编辑、Glob/Grep/Git、截图、剪贴板、MCP 扩展

---

## 环境要求

| 工具 | 版本 |
|------|------|
| Python | 3.10+ |
| [uv](https://docs.astral.sh/uv/) | 任意新版 |
| Node.js | 18+ |
| pnpm | 8+ |

---

## 快速开始（开发模式）

### 1. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，填入 LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL
```

支持所有 OpenAI 兼容接口：DeepSeek、Qwen、Ollama 等。

### 2. 安装依赖

```bash
# Python 依赖
uv sync

# Electron 依赖（在 electron/ 目录下）
cd electron
pnpm install
```

### 3. 启动

```bash
# 在 electron/ 目录下
pnpm run dev
```

开发模式同时启动 Vite（热更新）和 Electron，DevTools 自动打开。

---

## CLI 演示（无 Electron）

```bash
# 同步流式对话
uv run python chat.py

# 异步对话 + 会话持久化 + 事件总线演示
uv run python chat_async.py
```

---

## 打包发布

### 第一步：打包 Python 服务端

```bash
# 在项目根目录执行
uv run pyinstaller build-python.spec --distpath dist-python --noconfirm
```

输出：`dist-python/server/`（`server.exe` + `_internal/` 依赖库，约 50MB）

### 第二步：打包 Electron

```bash
# 在 electron/ 目录下
pnpm run build:win
```

输出：`electron/dist-build/win-unpacked/KittyMind.exe`

> 若需要 NSIS 安装包（`.exe` 安装程序），先下载并缓存：
> - `nsis-3.0.4.1.7z` → `%LOCALAPPDATA%\electron-builder\cache\nsis\nsis-3.0.4.1\`
> - 下载地址：`https://npmmirror.com/mirrors/electron-builder-binaries/nsis-3.0.4.1/nsis-3.0.4.1.7z`

### 第三步：分发

将 `dist-build/win-unpacked/` 整个目录压缩发给对方。

对方需在以下路径创建 `.env` 文件并填入自己的 API Key：

```
C:\Users\<用户名>\.kittymind\.env
```

```env
LLM_MODEL_ID=deepseek-chat
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
```

---

## 目录结构

```
kittymind/
├── kittymind/          # Python 核心包
│   ├── core/           # LLM 客户端（OpenAI 兼容）
│   ├── agent/          # Agent 基类 + KittyAgent
│   ├── tools/          # 工具集（11 个内置工具 + PermissionExecutor）
│   ├── events/         # asyncio 事件总线
│   ├── session/        # JSONL 会话持久化
│   ├── memory/         # 内存历史（滑动窗口）
│   └── callbacks/      # 回调钩子
├── server/             # WebSocket JSON-RPC 服务端
│   ├── app.py          # 启动入口
│   ├── ws_server.py    # WebSocket 服务器
│   └── rpc_handler.py  # 方法路由
├── electron/           # Electron 桌面应用
│   ├── main.js         # 主进程
│   ├── preload*.js     # contextBridge
│   └── src/
│       ├── renderer/   # React 聊天界面
│       ├── pet/        # 桌宠窗口（Canvas + 图层动画）
│       └── overlay/    # 快速输入悬浮框
├── chat.py             # CLI 同步对话演示
├── chat_async.py       # CLI 异步对话演示
├── build-python.spec   # PyInstaller 打包配置
└── .env.example        # 环境变量模板
```

---

## 常用命令

| 命令 | 说明 |
|------|------|
| `pnpm run dev` | 开发模式（Vite HMR + Electron） |
| `pnpm start` | 生产预览（加载 renderer-dist） |
| `pnpm run build:win` | 打包 Windows 应用 |
| `uv run pytest test/` | 运行单元测试 |
| `uv run python chat_async.py` | CLI 对话演示 |

---

## IPC 协议

Electron ↔ Python 通过 WebSocket JSON-RPC（`ws://127.0.0.1:8765`）通信。

**Renderer → Python（调用）：**
```jsonc
{ "id": 1, "method": "turn/run", "params": { "text": "...", "session_id": "s1" } }
{ "id": 2, "method": "session/list", "params": {} }
```

**Python → Renderer（推送）：**
```jsonc
{ "method": "agent.chunk",     "params": { "delta": "你好", "session_id": "s1" } }
{ "method": "agent.tool_call", "params": { "name": "bash",  "session_id": "s1" } }
{ "method": "agent.done",      "params": { "session_id": "s1", "text": "..." } }
```

# KittyMind 通用桌面 Agent 架构文档

## 目录

1. [项目定位](#1-项目定位)
2. [整体架构](#2-整体架构)
3. [技术选型](#3-技术选型)
4. [模块与目录结构](#4-模块与目录结构)
5. [核心机制](#5-核心机制)
6. [MVP 完成状态（Phase 1-9）](#6-mvp-完成状态phase-1-9)
7. [对标 hermes 的能力差距分析](#8-对标-hermes-的能力差距分析)
8. [进阶开发路线（Phase 10+）](#9-进阶开发路线phase-10)

---

## 1. 项目定位

KittyMind 是一个**通用桌面 Agent**（Windows 优先，预留跨平台）：多 LLM 支持、全程流式输出、桌宠悬浮窗（情绪驱动）、桌面原生能力（Shell/截图/剪贴板）、MCP 扩展、会话持久化、全局热键唤起。

**产品定位（已确认）**：**桌面为主，架构预留网关**——不投入 hermes 那套多平台/多租户重型服务端基建，把复杂度集中投入 Agent 智能本身。

---

## 2. 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                     Electron 主进程                          │
│   WindowManager │ PythonBridge │ HotkeyManager │ TrayManager│
└────────┬────────┴──────┬───────┴───────────────┴────────────┘
         │    ipcMain / ipcRenderer (contextBridge)
┌────────▼───────┐ ┌─────▼──────────┐ ┌──────────────────────┐
│  Chat Window   │ │   Pet Window   │ │   Quick Overlay      │
│ 流式聊天/工具卡片│ │ Canvas动画/情绪 │ │ 全局热键/极简输入     │
└────────┬───────┘ └────────────────┘ └──────────────────────┘
         │   WebSocket JSON-RPC  ws://127.0.0.1:8765
┌────────▼────────────────────────────────────────────────────┐
│                    Python Agent Core                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  KittyAgent — 全程流式 ReAct + 记忆召回 + 上下文压缩  │  │
│  └──────────────────────────────────────────────────────┘  │
│  LLM(多厂商) │ Tool Registry │ Session Manager │ Memory Store│
│  ────────────── asyncio Event Bus ──────────────           │
│  MCP Manager │ Compactor │ Task/子Agent │ Cron（规划中）    │
└─────────────────────────────────────────────────────────────┘
```

> 图中 MCP / Compactor / Cron 为目标态，属进阶路线（§9）范畴。

---

## 3. 技术选型

| 层次 | 技术 | 理由 |
|------|------|------|
| 桌面容器 | **Electron 36+** | 透明窗口、全局热键、系统托盘；跨平台 |
| 前端 | **React 18 + TypeScript** | 生态完善 |
| 桌宠动画 | **Canvas API** | 精灵帧动画，支持透明背景 |
| Agent 核心 | **Python 3.10+ + asyncio** | LLM 生态最完整 |
| IPC | **websockets（JSON-RPC）** | 全双工流式推送 |
| LLM 调用 | **OpenAI SDK（兼容多厂商）** | DeepSeek/Qwen/Ollama 全支持 |
| 工具扩展 | **MCP** | 标准生态，stdio + SSE |
| 长期记忆 | **Markdown + YAML frontmatter** | 零依赖，人类可读 |
| 会话持久化（MVP） | **JSONL 追加写入** | 崩溃安全，实现简单 |
| 会话持久化（进阶） | **SQLite + FTS5 + WAL** | 全文搜索（含 CJK）、用量统计、并发健壮（见 §5.3） |
| 上下文压缩 | **多层阈值管线 + 辅助模型摘要** | 解决长任务撞墙（见 Phase 10） |
| 打包 | **electron-builder** | Windows NSIS + 自动更新 |

---

## 4. 模块与目录结构

```
kittymind/                      # 项目根目录
├── kittymind/                  # Python 统一包
│   ├── core/                   # LLM 层：llm / llm_adapters / llm_response / message / exceptions
│   ├── agent/                  # base / tool_agent（流式 ReAct）/ kitty_agent（EventBus+Session 集成）
│   ├── context/                # token_counter / micro_compaction / compressor（上下文压缩管线）
│   ├── tools/                  # base / registry / executor / permission + builtin/（13 个工具）
│   ├── memory/                 # store / extract / recall（LLM 驱动的长期记忆）
│   ├── events/                 # bus（asyncio Pub/Sub）/ types
│   ├── session/                # store（JSONL）/ manager
│   ├── workspace/              # manager（cwd 上下文隔离）
│   ├── callbacks/              # base（回调接口）
│   ├── config.py               # 统一配置（~/.kittymind/settings.json 覆盖）
│   └── prompts.py              # 集中管理 system_prompt
├── server/                     # app（启动入口）/ ws_server / rpc_handler / permission_bridge
├── electron/                   # Electron 主进程 & 前端
│   ├── main.js                 # 主进程：子进程管理 / 多窗口 / 热键 / 托盘
│   ├── preload.js              # 聊天窗口 preload（IPC 桥）
│   ├── preload-pet.js          # 桌宠窗口 preload
│   ├── preload-overlay.js      # 悬浮层 preload
│   ├── src/
│   │   ├── renderer/           # React 聊天界面（App / ChatView / SessionList / TopBar…）
│   │   ├── pet/                # 桌宠窗口（精灵图分层动画）
│   │   └── overlay/            # 全局悬浮覆盖层
│   ├── scripts/                # 开发辅助脚本（dev / analyze-bounds / screenshot…）
│   ├── vite.config.ts          # Vite 构建配置
│   └── package.json
├── test/                       # Python 单元测试
│   ├── test_event_bus.py       # EventBus pub/sub 测试
│   ├── test_rpc_handler.py     # WebSocket RPC 协议测试
│   └── test_session.py         # JSONL 会话读写测试
├── assets/                     # 应用图标 / 托盘图
├── chat.py / chat_async.py     # CLI 演示
└── ARCHITECTURE.md
```

内置工具（13）：`bash` `file_read/write/edit` `glob` `grep` `ls` `git` `screenshot` `clipboard` `get_current_time` `write_memory` `task`（子任务）。

运行时数据目录 `~/.kittymind/`：`sessions/`（会话）、`memory/`（长期记忆）、`workspaces.json`、`settings.json`、`mcp.json`（规划）。

---

## 5. 核心机制

### 5.1 全程流式 ReAct 循环

参考 deepseek-harness：**每步都流式，工具调用也在流里检测**，不存在"非流式工具循环 + 流式最终回答"的分离。

- `OpenAIAdapter.stream_with_tools()` 在同一 stream 里同时 yield `text_delta` 和累积 `tool_call_delta`，流结束时输出 `tool_calls_done`。
- 循环每步：文字 delta 实时 `yield` 给用户 + emit `agent.chunk`；流结束若有 tool_calls 则执行并追加结果、继续下一步；无则本步即最终回答（已推完）。
- `async_stream_run()`：Thread + `asyncio.Queue` 桥接，把同步生成器接入事件循环，供 WebSocket 推送。
- 实现见 `agent/kitty_agent.py`、`core/llm_adapters.py`。

### 5.2 IPC 通信协议（WebSocket JSON-RPC）

**客户端请求**：`turn/run`、`turn/cancel`、`session/create`、`session/list`、`session/get`、`session/delete`。

**服务端推送（无 id，驱动 UI 与桌宠）**：

| 事件 | 载荷 | 用途 |
|------|------|------|
| `agent.chunk` | `{delta, session_id}` | 流式文字 |
| `agent.thinking` | `{session_id}` | LLM 推理中 → 桌宠 THINKING |
| `agent.tool_call` | `{name, args, session_id}` | 工具调用 → UI 卡片 / 桌宠 WORKING |
| `agent.tool_result` | `{name, result, session_id}` | 工具结果 |
| `agent.done` | `{session_id, text}` | 本轮完成 → 桌宠 HAPPY |
| `agent.error` | `{error, session_id}` | 出错 → 桌宠 SAD |

Electron Main ↔ Renderer 经 `contextBridge` 暴露 `kitty.*`（sendMessage / onChunk / onToolCall / onDone / onPetEmotion …）。

### 5.3 会话持久化

**MVP（JSONL）**：每会话一个 `~/.kittymind/sessions/{id}/session.jsonl`，第 1 行 header，后续每消息一行，append-only。读时遇不完整行直接 break（torn tail 崩溃恢复）。

**进阶（SQLite + FTS5，Phase 14-17）**：JSONL 在会话变多后无法全文搜索、无用量统计、并发靠文件锁。参考 hermes `hermes_state_*` 迁移 SQLite，作为主存储 + 索引，JSONL 保留为可选导出格式。桌面单机无需 hermes 的读连接池/多进程 generation/网关路由，大幅精简。

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, title TEXT, title_source TEXT,
  workspace_id TEXT, parent_session_id TEXT,          -- 压缩链 / 子 Agent 血缘
  created_at INTEGER, updated_at INTEGER,
  message_count INTEGER, tool_call_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  archived INTEGER DEFAULT 0
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, seq INTEGER,
  role TEXT, content TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT,
  token_count INTEGER, ts INTEGER,
  active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0   -- 压缩标记
);
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages');
CREATE VIRTUAL TABLE messages_fts_cjk USING fts5(content, tokenize='trigram');
```

要点：**WAL**（网络盘失败降级 DELETE）｜**FTS5 三层搜索** MATCH→trigram→CJK 二元组回退（中文必需）｜`active`/`compacted` 支持压缩后只取活跃消息、历史仍可查｜`session_model_usage` 表做成本追踪｜迁移脚本导入现有 JSONL，`SessionStore` 抽象双实现配置切换｜健壮性精简为启动 `integrity_check` + 损坏备份重建。

### 5.4 桌宠情绪映射（事件 → 状态）

| Agent 事件 | 情绪状态 | 动画 |
|-----------|---------|------|
| `agent.thinking` | THINKING | 摸下巴 |
| `agent.tool_call(shell/file)` | WORKING | 敲键盘/翻文件 |
| `agent.tool_call(web)` | SEARCHING | 望远镜 |
| `agent.done` | HAPPY | 跳起来 |
| `agent.error` | SAD | 低头 |
| 用户 30min 无操作 | SLEEPY | 打哈欠 |
| 00:00-06:00 | NIGHT | 戴睡帽 |
| idle | IDLE | 随机小动作 |

Pet 窗口：`BrowserWindow` + `transparent/frame:false/alwaysOnTop/skipTaskbar`，`setIgnoreMouseEvents` 点击穿透（可切换）；Canvas 按帧号从精灵图截取绘制。

---

## 6. MVP 完成状态（Phase 1-9）

MVP 目标：跑通"输入 → 流式显示 → 工具卡片 → 桌宠动画 → 历史持久化"闭环。

| Phase | 主题 | 状态 | 产出 |
|-------|------|:----:|------|
| 1 | Agent Core（全程流式工具调用 + async） | ✅ | `tool_agent.py`、`stream_with_tools()` |
| 2 | 事件总线（asyncio Pub/Sub） | ✅ | `events/bus.py`、7 类事件 |
| 3 | 会话持久化（JSONL + torn-tail 恢复） | ✅ | `session/store.py`、`manager.py` |
| 4 | WebSocket JSON-RPC Server | ✅ | `server/`（turn/session/agent 方法） |
| 5 | 桌面工具集（bash/文件/git/截图/剪贴板 + 权限） | ✅ 大部分 | `tools/builtin/`、`permission.py` |
| 6 | Electron 主进程骨架（子进程/窗口/热键/托盘） | 🟡 骨架 | `electron/main.js`、`preload` |
| 7 | React 聊天界面（流式/工具卡片/会话侧栏） | 🟡 骨架 | `electron/src/renderer/` |
| 8 | 桌宠窗口（透明浮窗/精灵动画/情绪机） | 🟡 骨架 | `electron/src/pet/` |
| 9 | 端到端联调与打包 | ⬜ 待办 | 见 Phase 26 |

MVP 之外已额外落地：长期记忆系统（`memory/`：提取/整合/召回）、工作区 cwd 隔离（`workspace/`）、子任务工具（`task`）、统一配置（`settings.json`）。

---

## 7. 对标 hermes 的能力差距分析

以 `E:\class\roadmap\Agent\resource\hermes-agent`（Nous Research 的成熟自进化 Agent 产品）为标杆。hermes 规模：**Agent 核心 ~94K 行**、**工具实现 255 个文件**、**状态层 22 个模块 ~700KB**、**23+ 消息平台适配器**、**~39K 测试**。kittymind **不追求 1:1 复制**（尤其多平台网关、多租户等重型服务端能力，与"桌面为主"定位不符），而是识别出**最能提升 Agent 智能与产品成熟度**的能力，务实补齐。

### 7.1 能力差距矩阵

| 能力域 | kittymind 现状 | hermes 成熟做法 | 差距等级 | 计划阶段 |
|--------|---------------|----------------|:------:|:------:|
| **上下文压缩** | ❌ 无，messages 无限增长到 `max_iterations` 撞墙 | 多层阈值（50%/85%）+ 微压缩 + 辅助模型摘要 + 缓存不变量保护 | 🔴 关键 | Phase 10 |
| **子 Agent 编排** | ⚠️ `task_tool` 雏形：同步 `run`、无隔离、无深度/并发限制、无用量追踪 | `delegate_task`：隔离沙箱、角色（leaf/orchestrator）、深度限制、后台委派、usage 回传 | 🔴 关键 | Phase 11 |
| **工具守护栏** | ❌ 无失败循环检测 | 精确失败/相同失败/幂等无进展/限额四重守护，交互态警告、非交互态硬停 | 🟠 重要 | Phase 12 |
| **自我验证** | ❌ 无 | `verify/runner` 后台验证（bootstrap→build→test→probe），收集证据决定重试 | 🟡 增强 | Phase 13 |
| **会话持久化** | JSONL 纯追加，无搜索/无用量 | SQLite + FTS5（含 CJK trigram）+ WAL + 自动修复 + 成本追踪 | 🟠 重要 | Phase 14-17 |
| **核心工具广度** | 13 个（bash/文件/git/截图/剪贴板/时间/记忆/task） | 120+：web_search/web_extract/browser_*/process_manage/vision/image_gen/todo/computer_use… | 🟠 重要 | Phase 18-19 |
| **工具分组分发** | 扁平列表，全量下发 | `TOOLSETS`（60+）按平台/场景动态启停，MCP 运行时注册 | 🟡 增强 | Phase 18 |
| **MCP 扩展** | ❌ 无（架构曾规划未实现） | MCP 客户端（stdio+SSE 接入外部工具）+ MCP 服务端（暴露自身能力） | 🟠 重要 | Phase 20-21 |
| **Skills 技能系统** | ❌ 无 | YAML frontmatter 技能、agent 自创建、curator 自动策展/归档、open standard | 🟡 增强 | Phase 22 |
| **长期记忆系统** | ✅ **已实现**（Markdown+frontmatter、LLM 提取/自动整合/召回）；但召回全量喂 LLM、中文关键词兜底失效、注入破坏缓存 | provider 插件化（honcho/mem0）+ 辩证式用户建模 + 向量语义召回 | 🟡 增强 | Phase 17M + 25 |
| **可观测性** | ⚠️ `print` 日志 | Observer hooks（never-block）+ OTLP + 健康检查 + 关联 ID 全链路 | 🟠 重要 | Phase 23-24 |
| **提示词缓存** | ⚠️ 每轮改 `messages[0]` 破坏缓存前缀 | 系统提示字节稳定，压缩是唯一合法破坏点，记忆变更推迟到下一会话 | 🟠 重要 | Phase 25 |
| **打包分发** | ❌ 未完成 | PyInstaller + electron-builder + 自动更新 + bootstrap installer | 🟠 重要 | Phase 26 |
| **定时/后台** | ❌ 无 | Cron（自然语言定义 + 多目标投递）+ 后台委派 + Serverless 常驻 | 🟡 增强 | Phase 27-28 |
| **多渠道网关** | ❌ 仅 Electron 本地 | 单进程 23+ 平台 + profile 多路复用 + 密钥隔离 | ⚪ 预留 | Phase 29 |
| **桌宠情绪系统** | ⚠️ 事件已具备，动画/状态机待完善 | hermes 有终端 pet（多协议精灵渲染）+ 桌面浮窗 | 🟡 增强 | Phase 30 |

图例：🔴 关键（阻碍长任务/核心体验）｜🟠 重要（成熟产品必备）｜🟡 增强（锦上添花）｜⚪ 预留（架构留位，暂不实现）

### 7.2 关键洞察

1. **上下文压缩是当前最大瓶颈**：`kitty_agent.py` 的 ReAct 循环无任何压缩，一旦长任务累积消息就会撞 `max_iterations` 而中断——这是 kittymind 目前**唯一会导致任务失败**的结构性缺陷，故列为 Phase 10 最高优先。
2. **子 Agent 已有雏形但太弱**：`task_tool` 能隔离 context 是正确方向，但缺隔离沙箱、深度限制、用量回传，容易递归爆炸或失控。
3. **hermes 的"重"大多在服务端**：多平台、多租户、读连接池、generation 管理、网关路由——这些是它作为**云端多用户产品**的需求。kittymind 桌面单机场景可**大幅精简**，把省下的复杂度投入 Agent 智能本身。
4. **架构分解是可借鉴的工程财富**：hermes 把 94K 行拆成 40+ 个 `turn_*.py` 阶段模块、22 个 `hermes_state_*.py`，避免 god file。kittymind 在补能力时应同步保持模块化。

---

## 8. 进阶开发路线（Phase 10+）

**总原则**：桌面为主、架构预留网关；按「Agent 核心智能 → 健壮持久化 → 工具生态 → 可观测与交付 → 定时后台 → 网关预留」推进。每个里程碑可独立交付、独立验证。

### 里程碑总览

| 里程碑 | 主题 | 优先级 | 包含 Phase | 一句话目标 |
|--------|------|:------:|-----------|-----------|
| **M-A** | Agent 核心智能 | P0 | 10-13 | 让 Agent 能扛住长任务不崩、子任务可控、失败不空转 |
| **M-B** | 健壮持久化 + 记忆检索 | P0 | 14-17M | 会话可全文搜索、用量可统计、并发安全、记忆向量召回 |
| **M-C** | 工具与扩展生态 | P1 | 18-22 | 能力边界大幅扩张 + 接入 MCP/技能生态 |
| **M-D** | 可观测与产品化交付 | P1 | 23-26 | 可观测、缓存省钱、能打包发给真实用户 |
| **M-E** | 定时与后台自动化 | P2 | 27-28 | 无人值守定时任务 + 后台委派 |
| **M-F** | 网关预留与桌宠深化 | P2 | 29-30 | 为未来多渠道铺抽象层 + 桌宠体验完善 |

---

### 里程碑 M-A：Agent 核心智能（P0）

#### Phase 10 — 上下文压缩管线 🔴

**目标**：Agent 处理长任务时自动压缩历史，永不因上下文溢出而中断；压缩不破坏工具调用/响应配对，不破坏提示缓存前缀。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 10.1 | Token 计量 | `kittymind/context/token_counter.py` | 用 tiktoken 或模型 usage 字段估算上下文占用比 |
| 10.2 | 微压缩（工具输出修剪） | `kittymind/context/micro_compaction.py` | 超长工具结果就地截断/摘要，保留头尾与关键行 |
| 10.3 | 轨迹压缩器 | `kittymind/context/compressor.py` | 保护头部（system+首轮）与尾部（最近 N 轮），中间段调辅助模型摘要为合成消息 |
| 10.4 | 阈值触发 | `kittymind/agent/kitty_agent.py` | 循环内检查占用比 > 阈值（默认 75%）则触发压缩；从不分裂 tool_call/tool 对 |
| 10.5 | 压缩标记落库 | 配合 Phase 14 | 旧消息标 `compacted`，重建对话只取 `active`（Phase 14 前先内存标记） |

**参考 hermes**：`agent/context_compressor.py`、`agent/micro_compaction.py`、`agent/turn_context_compaction.py`

#### Phase 11 — 子 Agent 编排强化 🔴

**目标**：把 `task_tool` 从"雏形"升级为可控的委派系统。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 11.1 | 委派上下文 | `kittymind/agent/delegation.py` | `subagent_id` / `parent_session_id` / `depth` 血缘追踪 |
| 11.2 | 深度与并发限制 | `kittymind/agent/delegation.py` | 最大深度（默认 3）、单轮最大子 Agent 数，防爆炸 |
| 11.3 | 角色区分 | `task_tool.py` | leaf（干活）/ orchestrator（再委派）；leaf 不含 task 工具（已实现） |
| 11.4 | 用量回传 | `task_tool.py` | 子 Agent 结果附带 token/耗时/工具数，父 Agent 可见 |
| 11.5 | 异步流式委派 | `task_tool.py` | 用 `async_stream_run`，子任务进度可 emit 事件驱动桌宠 |
| 11.6 | 可选工具集约束 | `task_tool.py` | `allowed_tools` / `blocked_tools` 限制子 Agent 能力面 |

**参考 hermes**：`agent/subagent_lifecycle.py`、`agent/delegation_context.py`

#### Phase 12 — 工具守护栏 🟠

**目标**：检测并阻断工具失败循环、无进展空转。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 12.1 | 调用历史追踪 | `kittymind/tools/guardrails.py` | 记录 (工具名, 参数指纹, 结果指纹) 序列 |
| 12.2 | 精确失败循环 | `guardrails.py` | 同工具+同参+同错误：第 2 次警告、第 5 次阻断（可配） |
| 12.3 | 幂等无进展 | `guardrails.py` | 工具执行但无状态变化：累计告警/阻断 |
| 12.4 | 限额 | `guardrails.py` | 单轮 web/子 Agent 调用上限 |
| 12.5 | 交互态区分 | `kitty_agent.py` | 桌面交互态默认警告并提示用户；后台/cron 态硬停 |

**参考 hermes**：`agent/tool_guardrails.py`

#### Phase 13 — 自我验证（增强）🟡

**目标**：修改类操作后可选自动验证（跑测试/启动探针），失败则重试或上报。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 13.1 | 验证器接口 | `kittymind/agent/verify/runner.py` | 可插拔：命令验证器（跑 lint/test）、就绪探针 |
| 13.2 | 证据收集 | `verify/evidence.py` | 记录验证输出，供 Agent 决策 |
| 13.3 | 触发策略 | `kitty_agent.py` | 写文件/改代码后可选触发，异步不阻塞主对话 |

**参考 hermes**：`agent/verify/runner.py`、`agent/verification_evidence.py`

---

### 里程碑 M-B：健壮持久化 + 记忆检索（P0）

> 详细 schema 与设计见 **§5.3**。

#### Phase 14 — SQLite 会话存储

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 14.1 | Store 接口抽象 | `kittymind/session/store.py` | 抽象 `SessionStore`，保留 `JsonlSessionStore` |
| 14.2 | SQLite 实现 | `kittymind/session/sqlite_store.py` | sessions/messages 表 + WAL + schema 版本；messages 表含 `active`/`compacted`/`_compressed_summary` 三个软标记字段（见 §5.3 schema） |
| 14.3 | 压缩落库原子操作 | `kittymind/session/sqlite_store.py` | `archive_and_compact(session_id, summary_msg, tail_msgs)`：单事务内将旧活跃消息标记 `active=0, compacted=1`（不删除），摘要+尾部作为新 `active=1` 行写入；**压缩视图持久化后重启无需重压** |
| 14.4 | 双视图读取 | `sqlite_store.py` | 模型视图 `WHERE active=1`（摘要+尾部）；完整视图 `WHERE active=1 OR compacted=1`（原始历史+摘要，供审计/回溯）；重启加载 session 走模型视图，直接得到已压缩状态 |
| 14.5 | 迁移脚本 | `kittymind/session/migrate.py` | 扫描现有 JSONL 导入 SQLite，所有行写入时 `active=1, compacted=0` |
| 14.6 | 配置切换 | `config.py` | `SESSION_BACKEND = jsonl \| sqlite` |
| 14.7 | 压缩状态跨轮持久化 | `kitty_agent.py` | 现 `TokenTracker`/`ContextCompressor` 是单轮局部对象，`_compressed_once`/反抖动冷却/token 校准基线每轮重置。改为 per-session 实例属性并落库，使衰减、冷却、校准跨轮（乃至跨重启）延续 |
| 14.8 | 移除 `_history` 内存缓存 | `agent/base.py`、`kitty_agent.py` | `_history` 是 JSONL 阶段的过渡设计：因存储层不可改写+读慢，才在 Agent 层维护内存镜像。SQLite 落地后 `WHERE active=1` 读取足够快，`archive_and_compact` 直接落库，`_history`/`replace_history`/`_loaded_sessions`/`_load_session_history` 可整体删除，`_build_messages` 改为每轮从 SQLite 读活跃消息 |

#### Phase 15 — FTS5 全文搜索

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 15.1 | FTS 索引 | `sqlite_store.py` | `messages_fts` + CJK trigram 表，触发器同步 |
| 15.2 | 三层搜索 | `kittymind/session/search.py` | MATCH → trigram → CJK 二元组回退 |
| 15.3 | 搜索 RPC | `server/rpc_handler.py` | `session/search` 方法 |
| 15.4 | 前端搜索 UI | `electron/src/renderer/` | 会话搜索框 + 结果高亮 |

#### Phase 16 — 用量与成本追踪

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 16.1 | usage 表 | `sqlite_store.py` | `session_model_usage`：按模型/任务汇总 token |
| 16.2 | 成本估算 | `kittymind/usage/cost.py` | 模型价目表 + 估算成本 |
| 16.3 | 用量面板 | `electron/src/renderer/` | 会话/全局 token 与成本展示 |

#### Phase 17 — 维护与健壮性（精简）

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 17.1 | 完整性检查 | `sqlite_store.py` | 启动 `PRAGMA integrity_check`，损坏则备份重建 |
| 17.2 | schema 迁移 | `kittymind/session/schema.py` | 版本号驱动的迁移链 |
| 17.3 | 归档/清理 | `kittymind/session/maintenance.py` | 旧会话 archived 标记 + 可选 VACUUM |

#### Phase 17M — 长期记忆检索升级 🟡

**目标**：长期记忆系统 MVP 已可用（提取/整合/召回齐全），本阶段解决三个规模化短板——召回不随记忆量膨胀、支持中文、不破坏提示缓存。复用 M-B 的 SQLite 底座（embedding 用 sqlite-vec 存储）。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 17M.1 | 向量语义召回 | `kittymind/memory/recall.py` | embedding 检索取代"全量 catalog 喂 LLM 选序号"，记忆多时不撑爆 prompt、不增延迟 |
| 17M.2 | 向量存储 | `kittymind/memory/vector_store.py` | sqlite-vec 存记忆 embedding，写入记忆时同步向量化 |
| 17M.3 | 中文召回修复 | `recall.py` | 现 `_keyword_select` 用 `split()` 对中文无效；向量召回天然跨语言，兜底改用字符 n-gram |
| 17M.4 | 缓存友好注入 | `kittymind/agent/kitty_agent.py` | 召回结果**不再改 `messages[0]`**，改为独立消息（与 Phase 25.1 协同，保护缓存前缀） |
| 17M.5 | 记忆 provider 抽象（可选） | `kittymind/memory/provider.py` | 抽象接口，本地文件为默认实现，预留 honcho/mem0 式后端 |
| 17M.6 | 辩证式用户建模（可选） | `memory/user_model.py` | 持续更新 user 类型记忆，追踪偏好演化，对标 hermes Honcho |

**参考 hermes**：`plugins/memory/`（honcho/mem0/hindsight）、`agent/memory_provider.py`、Honcho 辩证式建模

---

### 里程碑 M-C：工具与扩展生态（P1）

#### Phase 18 — 核心工具补齐 + 工具集分组

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 18.1 | `web_search` | `tools/builtin/web_search_tool.py` | 可插拔搜索后端（Bing/SearXNG/Tavily） |
| 18.2 | `web_extract` | `tools/builtin/web_extract_tool.py` | URL 正文提取 |
| 18.3 | `process_manage` | `tools/builtin/process_tool.py` | 后台进程启动/查看/终止 |
| 18.4 | `todo_list` | `tools/builtin/todo_tool.py` | Agent 任务清单 |
| 18.5 | 工具集分组 | `kittymind/tools/toolsets.py` | `TOOLSETS` 字典，按场景启停，动态过滤 schema |

#### Phase 19 — 浏览器自动化（重型，可选）

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 19.1 | 浏览器工具组 | `tools/builtin/browser/` | Playwright 驱动：导航/点击/填写/截图/提取 |
| 19.2 | 会话复用 | `browser/session.py` | 持久 browser context，跨工具调用复用 |

#### Phase 20 — MCP 客户端集成

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 20.1 | MCP Manager | `kittymind/mcp/manager.py` | 读 `~/.kittymind/mcp.json`，管理 stdio + SSE server 生命周期 |
| 20.2 | 工具动态注册 | `mcp/manager.py` | 把 MCP 工具注册进 registry，schema 合并下发 |
| 20.3 | 前端管理 | `electron/src/renderer/` | MCP server 增删改查 UI |

#### Phase 21 — MCP 服务端模式

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 21.1 | MCP server | `mcp_serve.py` | 把 kittymind 会话/工具暴露为 MCP，供 Claude Code/Cursor 连接 |

#### Phase 22 — Skills 技能系统

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 22.1 | 技能格式 | `kittymind/skills/loader.py` | YAML frontmatter + Markdown，`~/.kittymind/skills/` |
| 22.2 | 发现与注入 | `skills/loader.py` | 按需把技能说明注入 system prompt（保持缓存稳定，见 Phase 25） |
| 22.3 | 自动策展 | `skills/curator.py` | 使用计数 + 过期归档（可恢复），对标 hermes curator |
| 22.4 | Agent 自创建 | `skills/loader.py` | Agent 可写入新技能，标 `created_by: agent` |

---

### 里程碑 M-D：可观测与产品化交付（P1）

#### Phase 23 — Observer Hooks + 结构化日志

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 23.1 | Hook 总线 | `kittymind/observability/hooks.py` | never-block 只读钩子 + **超时保护**（借鉴 hermes 对 Pi/OpenCode 的教训） |
| 23.2 | 事件族 | `hooks.py` | pre/post_llm_call、pre/post_tool_call、session 生命周期 |
| 23.3 | 关联 ID | `kitty_agent.py` | session_id/turn_id/tool_call_id 贯穿全链路 |
| 23.4 | 结构化日志 | `kittymind/observability/logging.py` | 替换散落的 print，分级 + 文件轮转 |

#### Phase 24 — 健康检查与遥测（轻量）

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 24.1 | 健康检查 | `server/health.py` | 模型延迟/连通性探针，RPC `agent/health` |
| 24.2 | 可选 OTLP | `observability/otlp.py` | 接 Langfuse/Jaeger（默认关闭，进阶用户开启） |

#### Phase 25 — 提示词缓存优化 🟠

**目标**：修复当前"每轮改 `messages[0]` 破坏缓存前缀"的问题，显著降本降延迟。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 25.1 | 系统提示字节稳定 | `kitty_agent.py` | 记忆召回**不再直接改 system 内容**，改为独立消息或推迟到下一会话 |
| 25.2 | 缓存不变量 | `kitty_agent.py` | 压缩是唯一合法的前缀破坏点；断言前缀稳定 |
| 25.3 | provider 缓存标记 | `core/llm_adapters.py` | 支持 Anthropic/OpenAI 的 prompt caching 参数 |

#### Phase 26 — 打包分发

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 26.1 | Python 打包 | `build-python.spec` | PyInstaller 打 server 为单 exe（已有 spec，需完善） |
| 26.2 | Electron 打包 | `electron/package.json` | electron-builder → Windows NSIS |
| 26.3 | 首启引导 | `electron/` | 首次运行配置模型/API key 的 onboarding |
| 26.4 | 自动更新 | `electron/main.js` | electron-updater |

---

### 里程碑 M-E：定时与后台自动化（P2）

#### Phase 27 — Cron 定时任务

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 27.1 | Job 存储与调度 | `kittymind/cron/scheduler.py` | `~/.kittymind/cron/jobs.json`，60s ticker |
| 27.2 | 自然语言定义 | `tools/builtin/cron_tool.py` | Agent 可创建/暂停/删除定时任务 |
| 27.3 | 结果投递 | `kittymind/cron/delivery.py` | 投递到聊天窗 / 系统通知 / 本地文件 |
| 27.4 | 失败连击提醒 | `scheduler.py` | 连续失败 N 次 → 提示暂停修复 |

#### Phase 28 — 后台任务委派

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 28.1 | 后台委派 | `kittymind/agent/background.py` | 长任务不等待返回，完成后事件通知 + 桌宠提示 |
| 28.2 | 任务列表 UI | `electron/src/renderer/` | 后台任务进度/结果查看 |

---

### 里程碑 M-F：网关预留与桌宠深化（P2）

#### Phase 29 — 网关抽象层（预留，不实现具体平台）

**目标**：为未来接入 Telegram/飞书/企业微信留出干净接口，当前只落地抽象 + 复用现有 Electron 通道，**不实现任何第三方平台适配器**。

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 29.1 | SessionSource 模型 | `kittymind/gateway/session_source.py` | platform/chat_id/user_id/thread_id 描述符 + session_key 生成 |
| 29.2 | 平台适配器接口 | `kittymind/gateway/adapter.py` | `BasePlatformAdapter` 抽象，Electron 作为首个实现 |
| 29.3 | 消息路由 | `kittymind/gateway/router.py` | 入站消息 → session_key → Agent；预留 profile 字段 |

**说明**：多租户 profile 多路复用、密钥 ContextVar 隔离、23+ 平台实现均**明确不在本路线内**（与桌面单机定位不符），仅保留概念占位，未来若转向按需再启。

#### Phase 30 — 桌宠情绪系统深化

| # | 任务 | 文件 | 关键点 |
|---|------|------|--------|
| 30.1 | 情绪状态机 | `electron/src/pet/EmotionMachine.ts` | 订阅 §5.4 事件表，完整状态转移 |
| 30.2 | 精灵帧动画 | `electron/src/pet/SpriteRenderer.tsx` | Canvas 帧动画，各情绪动画序列 |
| 30.3 | 对话气泡 | `electron/src/pet/SpeechBubble.tsx` | 回复摘要气泡 |
| 30.4 | 交互 | `electron/src/pet/PetApp.tsx` | 拖拽、边缘吸附、右键菜单、点击唤起主窗 |

---

### 推进建议

- **立即启动 Phase 10（上下文压缩）**：它是当前唯一会导致长任务失败的结构性缺陷，投入产出比最高。
- **M-A 与 M-B 可交错**：Phase 10 的压缩标记落库依赖 Phase 14 的 SQLite，可先内存标记、后落库。
- **每个 Phase 配套测试**：hermes 用 ~39K 测试保障质量；kittymind 应对每个核心能力（压缩、守护栏、SQLite 迁移）建立回归测试。
- **保持模块化**：补能力时避免让 `kitty_agent.py` 膨胀成 god file，参考 hermes 的阶段化拆分。

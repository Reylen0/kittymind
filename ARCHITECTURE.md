# KittyMind 通用桌面 Agent 架构文档

## 目录

1. [项目定位](#1-项目定位)
2. [整体架构](#2-整体架构)
3. [技术选型](#3-技术选型)
4. [模块与目录结构](#4-模块与目录结构)
5. [开发路线与实现状态（Phase 1-30）](#5-开发路线与实现状态phase-1-30)

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
│  LLM(多厂商) │ Tool Registry │ Session Manager(SQLite)      │
│  Memory Store │ Guardrails │ Permission Bridge │ Audit      │
│  ────────────── asyncio Event Bus（唯一事件出口）────────     │
│  MCP Manager │ Task/子Agent │ Cron（规划中）                 │
└─────────────────────────────────────────────────────────────┘
```

> 图中 MCP / Cron 为目标态，属第 5 章（Phase 20 / 27）范畴。

---

## 3. 技术选型

| 层次 | 技术 | 理由 |
|------|------|------|
| 桌面容器 | **Electron 36+** | 透明窗口、全局热键、系统托盘；跨平台 |
| 前端 | **React 18 + TypeScript** | 生态完善 |
| 桌宠动画 | **Canvas API + 精灵分层** | 透明背景，按情绪切换图层 |
| Agent 核心 | **Python 3.10+ + asyncio** | LLM 生态最完整 |
| IPC | **websockets（JSON-RPC）** | 全双工流式推送 |
| LLM 调用 | **OpenAI SDK（兼容多厂商）** | DeepSeek/Qwen/Ollama 全支持；Anthropic 适配器含 cache_control |
| 工具扩展 | **MCP** | 标准生态，stdio + SSE（未实现，Phase 20-21） |
| 长期记忆 | **Markdown + YAML frontmatter** | 零依赖，人类可读 |
| 会话持久化 | **SQLite + WAL** | 单文件 `~/.kittymind/sessions.db`；压缩原子落库、双视图读取、状态跨轮持久化 |
| 上下文压缩 | **多层阈值管线 + 辅助模型摘要** | 微压缩 + 轨迹压缩 + 反抖动 |
| 打包 | **PyInstaller + electron-builder** | Python 单 exe（spec 已有）；Electron NSIS + 自动更新（未做） |

---

## 4. 模块与目录结构

```
kittymind/                      # 项目根目录
├── kittymind/                  # Python 统一包
│   ├── core/                   # LLM 层：llm / llm_adapters / llm_response / message / exceptions
│   ├── agent/                  # base / kitty_agent（唯一 ReAct 入口）/ delegation / verify/
│   ├── context/                # token_counter / micro_compaction / compressor（压缩管线）
│   ├── tools/                  # base / registry / executor / permission / guardrails / audit
│   │                           #   / redaction + builtin/（14 个工具 + _paths/_fmt/_dangerous 辅助）
│   ├── memory/                 # store / extract / recall（LLM 驱动的长期记忆）
│   ├── events/                 # bus（asyncio Pub/Sub，唯一事件出口）/ types
│   ├── session/                # store（SQLite + FTS5 全文检索）/ manager / _search_text（CJK 二元组切词）
│   ├── workspace/              # manager（cwd 上下文隔离）
│   ├── config.py               # 统一配置（~/.kittymind/settings.json 覆盖）
│   └── prompts.py              # 集中管理 system_prompt
├── server/                     # app（装配）/ ws_server / rpc_handler / permission_bridge
├── electron/                   # Electron 主进程 & 前端
│   ├── main.js                 # 主进程：子进程管理 / 多窗口 / 热键 / 托盘 / 事件白名单
│   ├── preload*.js             # 聊天 / 桌宠 / 悬浮层三份 contextBridge
│   ├── src/
│   │   ├── renderer/           # React 聊天界面（ChatView / SessionList / TopBar / WorkspaceSelector…）
│   │   ├── pet/                # 桌宠窗口（分层精灵动画）
│   │   └── overlay/            # 全局悬浮覆盖层
│   ├── vite.config.ts
│   └── package.json
├── test/                       # 31 个测试文件 / 512 个用例（特征测试 + 回归）
├── assets/                     # 应用图标 / 托盘图
├── chat_async.py               # CLI 演示入口
├── build-python.spec           # PyInstaller 打包配置
└── ARCHITECTURE.md
```

内置工具（14）：`bash` `file_read/write/edit` `glob` `grep` `ls` `git` `screenshot` `clipboard` `get_current_time` `write_memory` `verify`（自我验证）`task`（子任务）。

运行时数据目录 `~/.kittymind/`：`sessions.db`（SQLite 会话库）、`memory/`（长期记忆）、`workspaces.json`、`settings.json`。

---

## 5. 开发路线与实现状态（Phase 1-30）

> 依据**当前代码**（2026-09，512 测试全绿）梳理。状态图例：✅ 已实现 ｜ 🟡 部分实现 ｜ ⬜ 未实现。
> 对标参照：Nous Research 的 hermes-agent（仅借鉴其 Agent 侧设计，服务端重型能力不在路线内）。

### 里程碑总览

| 里程碑 | 主题 | 包含 Phase | 状态 |
|--------|------|-----------|:----:|
| MVP | 跑通「输入→流式→工具→桌宠→持久化」闭环 | 1-9 | 🟡 核心 ✅ / 打包 🟡 |
| **M-A** | Agent 核心智能（压缩/委派/守护/验证） | 10-13 | ✅ |
| **M-B** | 健壮持久化 + 记忆检索 | 14-17 | 🟡 存储层 ✅ / 全文搜索 ✅ / 检索升级 ⬜ |
| **M-C** | 工具与扩展生态 | 18-22 | ⬜ |
| **M-D** | 可观测与产品化交付 | 23-26 | 🟡 缓存 ✅ / 打包 🟡 |
| **M-E** | 定时与后台自动化 | 27-28 | ⬜ |
| **M-F** | 网关预留与桌宠深化 | 29-30 | ⬜ |

---

### MVP（Phase 1-9）

**Phase 1 — Agent Core（全程流式 ReAct）✅**
单一流式路径：LLM 适配器在同一 stream 里同时 yield `text_delta` 与 `tool_calls_done`，循环每步文字实时外推、流结束有工具则执行后继续、无则本步即最终回答。`async_stream_run()` 是**唯一入口**：根 Agent 正常调用，子 Agent 传 `root=False` 复用父级作用域（cwd / 委派预算 / 根 session 标签），不是另一套实现。同批 tool_calls 由执行器**分区并行**：决策段整批按声明序串行、只读工具区段内 `gather`、变更工具自成屏障，结果按声明序回灌。

**Phase 2 — 事件总线 ✅**
`EventBus`：asyncio Pub/Sub，支持通配符订阅，异常不中断其他处理器。现为**全链路唯一事件出口**——WS 推送、task_tool 生命周期事件（subagent.start/done）、子 Agent 工具计数（订阅子 Agent 私有总线实现 per-run 隔离）都走它（原 callbacks 双通道机制已删除）。

**Phase 3 — 会话持久化 ✅**
已由原 JSONL 方案升级为 **SQLite**（见 Phase 14）：WAL、压缩原子落库、模型/完整双视图、session state 跨轮持久化。

**Phase 4 — WebSocket JSON-RPC Server ✅**
`turn/run`（流式消费 async_stream_run）、`turn/cancel`、`session/create|list|get|delete`；服务端推送经 RpcHandler 按 session_id 过滤转发 EventBus 事件（子 Agent 的 `session_id=None` 事件天然不透传，前端只见 subagent.start/done）。`PermissionBridge`（asyncio.Future 单事件循环实现）负责审批往返。

**Phase 5 — 桌面工具集 + 权限 ✅**
14 个工具；`ToolExecutor` 六段式管线（前置守护栏 → 三道权限闸门 → `to_thread` 执行 → 输出截断 → 敏感信息脱敏 → 审计落库）。权限三道闸门：硬拒绝黑名单（shell 类工具组合）→ `_RULES` 软规则 → 审批桥（超时 fail-closed，超时推送 `tool.permission_expired` 撤回前端弹窗）。拒绝以 tool result 文本返回（模型可换策略），审计记 decision。

**Phase 6 — Electron 主进程 ✅（基础）**
子进程拉起 Python（就绪探测 + 端口获取）、三窗口管理（聊天 / 桌宠透明置顶 / 悬浮层）、全局热键、系统托盘、崩溃处理、事件推送白名单。

**Phase 7 — React 聊天界面 ✅（基础）**
流式 Markdown 渲染、工具卡片（按模型声明序出现）、会话侧栏、工作区选择器、上下文占用环（ctx-ring）、审批队列（并发审批排队展示 +N，`permission_expired` 自动移除）。

**Phase 8 — 桌宠窗口 🟡**
透明浮窗 + 分层精灵动画（`src/pet/layers/`）+ 事件→情绪基础映射（thinking/working/done/error）。完整情绪状态机与交互深化见 Phase 30。

**Phase 9 — 端到端联调与打包 🟡**
日常开发联调已通（应用可用）；分发打包见 Phase 26（Python 侧 spec 已有，Electron 侧未做）。

---

### 里程碑 M-A：Agent 核心智能（Phase 10-13）✅

**Phase 10 — 上下文压缩管线 ✅**
架构：双轨 token 计量（字符估算 + usage 真值校准）→ 微压缩（超长工具输出就地截断保头尾）→ 轨迹压缩（保头部 system+首轮、护尾部最近 N 轮，中间段调辅助模型摘要为合成消息，从不分裂 tool_call/tool 配对）→ 阈值触发（默认 75%）→ 压缩标记落库 + 反抖动（无效压缩冷却）。压缩状态 per-session 落库，跨轮延续。关键文件：`context/`、`agent/kitty_agent.py`。
已知边界：`LLM_CONTEXT_WINDOW` 需按实际模型配置，默认值过大时阈值永不触发。

**Phase 11 — 子 Agent 编排 ✅**
架构：`DelegationBudget`（深度/总数上限，ContextVar 携带）+ `child_scope` 血缘递进；leaf/orchestrator 角色（leaf 不含 task 工具）；`allowed_tools` 约束能力面；子 Agent 走 `async_stream_run(root=False)` 异步流式委派，同批多个 task 真并发；用量回传（工具数/token/耗时）以脚注进父 context，`subagent.start/done` 事件驱动前端。关键文件：`agent/delegation.py`、`tools/builtin/task_tool.py`。

**Phase 12 — 工具守护栏 ✅**
架构：per-turn 顺序状态机，记录 (工具， 参数指纹， 结果指纹) 序列；相同失败 streak 逐个升级 warn→block、幂等无进展检测、web/子 Agent 限额；交互态警告、非交互态（`interactive=False`）硬停。MUTATING/IDEMPOTENT 分类与并发分区协同：决策段串行保证记账看得到同批前序结果（跨屏障）；同并行区内的失败升级是分区模型的已知代价。

**Phase 13 — 自我验证 ✅（基础）**
`verify` 工具 + `CommandVerifier`（跑 lint/test 等命令收集证据）；后台验证 runner（bootstrap→build→test→probe）已有骨架。触发策略目前依赖模型主动调用 verify，未做「写后自动触发」。

---

### 里程碑 M-B：健壮持久化 + 记忆检索（Phase 14-17M）🟡

**Phase 14 — SQLite 会话存储 ✅（核心）**
架构：单文件 `~/.kittymind/sessions.db`，sessions/messages 表 + WAL；`archive_and_compact` 单事务原子压缩落库（旧消息 `active=0,compacted=1` 不删除，摘要+尾部作为新活跃行写入）——压缩视图持久化，**重启无需重压**；双视图读取：模型视图 `WHERE active=1`（加载即已压缩状态）、完整视图含 compacted 行（审计/回溯）；session_state 跨轮保存压缩冷却/校准基线。JSONL 后端已移除。
剩余：会话切换/重启后前端 ctx-ring 的恢复（`agent.context_usage` 事件链路已具备，加载时回推未做）。

**Phase 15 — FTS5 全文搜索 ✅**
架构：原计划的 CJK 方案是「trigram 回退」，实测推翻——trigram 要求查询词 ≥3 字符，「压缩」「阈值」这类 2 字词全部 0 命中，而 2 字词恰恰是中文检索主力。改为自行预处理：CJK 段展开成重叠二元组（`session/_search_text.py`），非 CJK 段交给 `unicode61` 按空白切；入库与查询走同一份切词函数。索引是普通 FTS5 表（`messages_fts`，非 contentless——3.39.4 不支持 `contentless_delete`），`rowid` 对齐 `messages.id`；INSERT 侧在 Python 层显式维护（二元组要现算），DELETE 侧用 `AFTER DELETE` 触发器，借 `sessions` 的 `ON DELETE CASCADE` 自动清干净（已验证级联删除会触发该触发器，无孤儿行）。只索引 `role IN ('user','assistant')`——工具原始输出（文件全文、bash stdout）不进索引，否则搜索结果被回显淹没。搜索走完整视图（`active=1 OR compacted=1`），压缩摘要行不重复索引（原文仍在 compacted 行里）。`session/search` RPC 结果按会话分组，manager 层基于原文计算高亮片段与区间（不用 FTS5 的 `snippet()`——索引里存的是二元组串，不是原文）。前端升级侧栏现有搜索框，标题过滤本地即时、内容搜索防抖 250ms，`<mark>` 高亮（非 `dangerouslySetInnerHTML`）。v5 库升级时一次性回填存量消息。

**Phase 16 — 用量与成本追踪 ⬜**
思路：`session_model_usage` 表按模型/任务聚合 token（usage 数据源已具备——TokenTracker 每轮拿真值）；价目表估成本；前端用量面板。

**Phase 17 — 维护与健壮性 ⬜**
思路：启动 `integrity_check` + 损坏备份重建；schema 版本号迁移链；旧会话 archived 归档 + 可选 VACUUM。

**Phase 17M — 长期记忆检索升级 ⬜**
现状基础：Markdown+frontmatter 记忆、LLM 提取/整合/召回已可用，召回以独立消息注入（缓存友好）。
思路：sqlite-vec 向量语义召回取代「全量 catalog 喂 LLM 选序号」；修中文关键词兜底（现 `split()` 对中文无效，改字符 n-gram）；可选 provider 抽象（honcho/mem0 式后端）。

---

### 里程碑 M-C：工具与扩展生态（Phase 18-22）⬜

**Phase 18 — 核心工具补齐 + 工具集分组 ⬜**
思路：`web_search`（可插拔后端 Bing/SearXNG/Tavily）、`web_extract`、`process_manage`（后台进程）、`todo_list`；`TOOLSETS` 分组字典按场景动态过滤 schema 下发。

**Phase 19 — 浏览器自动化 ⬜（重型，可选）**
思路：Playwright 驱动的浏览器工具组（导航/点击/填写/截图/提取）+ 持久 browser context 跨调用复用。

**Phase 20 — MCP 客户端集成 ⬜**
思路：MCP Manager 读 `~/.kittymind/mcp.json`，管理 stdio/SSE server 生命周期；MCP 工具动态注册进 registry 合并下发；前端管理 UI。注意与工具分组（Phase 18）协同做能力面控制。

**Phase 21 — MCP 服务端模式 ⬜**
思路：把 kittymind 的会话/工具经 MCP 暴露，供 Claude Code/Cursor 等外部宿主连接。

**Phase 22 — Skills 技能系统 ⬜**
思路：YAML frontmatter + Markdown 技能格式；按需注入 system prompt（追加尾部，保持缓存前缀稳定）；使用计数 + 过期归档策展；Agent 自创建技能。

---

### 里程碑 M-D：可观测与产品化交付（Phase 23-26）🟡

**Phase 23 — Observer Hooks + 结构化日志 ⬜（有基础）**
现有基础：EventBus + 审计 SQLite 已覆盖 tool 生命周期记录。
思路：never-block 只读钩子总线（超时保护）、pre/post_llm_call 事件族、session/turn/tool_call 关联 ID 贯穿、结构化日志替换散落 print（分级 + 轮转）。

**Phase 24 — 健康检查与遥测 ⬜**
思路：模型连通性/延迟探针 + `agent/health` RPC；可选 OTLP 导出（默认关闭）。

**Phase 25 — 提示词缓存优化 ✅**
架构：记忆召回以独立 system 消息注入，**不改主 system 字节**；压缩是唯一合法的前缀破坏点（且压缩后主动标记缓存失效）；Anthropic 适配器在 content block 上打 `cache_control` 断点（OpenAI 自动前缀缓存）。`PERMISSION_ASK_TIMEOUT` 等长等待均已 async 化，不影响缓存。

**Phase 26 — 打包分发 🟡**
已有：PyInstaller spec（`build-python.spec`）。
思路：electron-builder → Windows NSIS；首启 onboarding（配置模型/API key）；electron-updater 自动更新。

---

### 里程碑 M-E：定时与后台自动化（Phase 27-28）⬜

**Phase 27 — Cron 定时任务 ⬜**
思路：`jobs.json` + 60s ticker 调度器；Agent 经 `cron_tool` 自然语言创建/暂停/删除；结果投递到聊天窗/系统通知/文件；连续失败 N 次提示暂停。审批语义需明确：无人值守态 = `interactive=False`（守护栏硬停生效）。

**Phase 28 — 后台任务委派 ⬜**
思路：task 委派不等待返回，完成事件通知 + 桌宠提示；前端后台任务列表。

---

### 里程碑 M-F：网关预留与桌宠深化（Phase 29-30）⬜

**Phase 29 — 网关抽象层 ⬜（预留，不实现具体平台）**
思路：SessionSource 描述符（platform/chat_id/user_id/thread_id → session_key）+ `BasePlatformAdapter` 抽象（Electron 为首个实现）+ 消息路由。多租户/多平台实现明确不在路线内。

**Phase 30 — 桌宠情绪系统深化 🟡**
现状基础：透明浮窗、分层精灵动画、基础事件→情绪映射。
思路：完整情绪状态机（含 SLEEPY/NIGHT/idle 小动作与状态转移）、各情绪动画序列补全、对话气泡（回复摘要）、交互（拖拽/边缘吸附/右键菜单/点击唤起主窗）。

---

### 推进建议

- **近期优先 Phase 14 收尾**：ctx-ring 恢复（15 的全文搜索已完成）直接提升日常可用性，SQLite 底座已就绪。
- **M-C 之前先做 18 的工具分组**：工具数即将扩张，先有 TOOLSETS 分组与能力面控制，再接 MCP（Phase 20），避免扁平列表失控。
- **Phase 27 依赖 12/25 的语义**：无人值守 = 非交互态硬停 + 缓存纪律，这两块已就绪，Cron 可直接叠加。
- **每个 Phase 配套测试**：当前 512 用例的「特征测试钉行为」模式（`test_react_loops.py` 等）应延续到新模块。

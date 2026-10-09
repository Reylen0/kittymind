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
│   ├── memory/                 # store / extract / recall / index（向量索引库）—— LLM 驱动的长期记忆
│   ├── embeddings/             # base（Protocol）/ openai_compat / get_embedding（Phase 17M 语义检索）
│   ├── storage/                # migrations（版本链）/ maintenance（自检·备份·回收）/ atomic（原子写）
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
├── test/                       # 38 个测试文件 / 684 个用例（特征测试 + 回归）
├── assets/                     # 应用图标 / 托盘图
├── chat_async.py               # CLI 演示入口
├── build-python.spec           # PyInstaller 打包配置
└── ARCHITECTURE.md
```

内置工具（14）：`bash` `file_read/write/edit` `glob` `grep` `ls` `git` `screenshot` `clipboard` `get_current_time` `write_memory` `verify`（自我验证）`task`（子任务）。

运行时数据目录 `~/.kittymind/`：`sessions.db`（SQLite 会话库）、`tool_audit.db` + `tool_audit.jsonl`（工具审计）、`memory/`（长期记忆 Markdown）、`memory_index.db`（记忆向量索引，纯派生数据可删）、`workspaces.json`、`settings.json`、`backups/`（会话库备份）。

---

## 5. 开发路线与实现状态（Phase 1-30）

> 依据**当前代码**（2026-09，684 测试全绿）梳理。状态图例：✅ 已实现 ｜ 🟡 部分实现 ｜ ⬜ 未实现。
> 对标参照：Nous Research 的 hermes-agent（仅借鉴其 Agent 侧设计，服务端重型能力不在路线内）。

### 里程碑总览

| 里程碑 | 主题 | 包含 Phase | 状态 |
|--------|------|-----------|:----:|
| MVP | 跑通「输入→流式→工具→桌宠→持久化」闭环 | 1-9 | 🟡 核心 ✅ / 打包 🟡 |
| **M-A** | Agent 核心智能（压缩/委派/守护/验证） | 10-13 | ✅ |
| **M-B** | 健壮持久化 + 记忆检索 | 14-17M | ✅ |
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

### 里程碑 M-B：健壮持久化 + 记忆检索（Phase 14-17M）✅

**Phase 14 — SQLite 会话存储 ✅（核心）**
架构：单文件 `~/.kittymind/sessions.db`，sessions/messages 表 + WAL；`archive_and_compact` 单事务原子压缩落库（旧消息 `active=0,compacted=1` 不删除，摘要+尾部作为新活跃行写入）——压缩视图持久化，**重启无需重压**；双视图读取：模型视图 `WHERE active=1`（加载即已压缩状态）、完整视图含 compacted 行（审计/回溯）；session_state 跨轮保存压缩冷却/校准基线。JSONL 后端已移除。
剩余：会话切换/重启后前端 ctx-ring 的恢复（`agent.context_usage` 事件链路已具备，加载时回推未做）。

**Phase 15 — FTS5 全文搜索 ✅**
架构：原计划的 CJK 方案是「trigram 回退」，实测推翻——trigram 要求查询词 ≥3 字符，「压缩」「阈值」这类 2 字词全部 0 命中，而 2 字词恰恰是中文检索主力。改为自行预处理：CJK 段展开成重叠二元组（`session/_search_text.py`），非 CJK 段交给 `unicode61` 按空白切；入库与查询走同一份切词函数。索引是普通 FTS5 表（`messages_fts`，非 contentless——3.39.4 不支持 `contentless_delete`），`rowid` 对齐 `messages.id`；INSERT 侧在 Python 层显式维护（二元组要现算），DELETE 侧用 `AFTER DELETE` 触发器，借 `sessions` 的 `ON DELETE CASCADE` 自动清干净（已验证级联删除会触发该触发器，无孤儿行）。只索引 `role IN ('user','assistant')`——工具原始输出（文件全文、bash stdout）不进索引，否则搜索结果被回显淹没。搜索走完整视图（`active=1 OR compacted=1`），压缩摘要行不重复索引（原文仍在 compacted 行里）。`session/search` RPC 结果按会话分组，manager 层基于原文计算高亮片段与区间（不用 FTS5 的 `snippet()`——索引里存的是二元组串，不是原文）。前端升级侧栏现有搜索框，标题过滤本地即时、内容搜索防抖 250ms，`<mark>` 高亮（非 `dangerouslySetInnerHTML`）。v5 库升级时一次性回填存量消息。

**Phase 16 — 用量与成本追踪 ✅**
架构：新增 `kittymind/usage/`（`UsageRecorder` per-turn 聚合器 + `pricing.py` 价目表）。usage 真值在 kitty_agent 主循环的 `usage` 事件分支经 recorder 按模型累加，随 `_commit_turn` 原子落库到 `model_usage` 表（schema v7，`session_id` 不带外键——**删会话保留成本记录**）。子 Agent 的 token 真值经 `_root_usage_recorder` ContextVar 回传父级合并，**计入父会话成本**（替代原 `estimate_tokens` 估算）。价目表内置默认（Claude/DeepSeek/Qwen/Ollama）+ `USAGE_PRICING` settings.json 覆盖，查不到返回 null 不编造。新增 `usage/report` RPC（`group_by ∈ {model, day, session}` + since/until），前端顶栏「用量」入口 + 弹层面板（按模型/按日/按会话切换，含成本列）。历史数据不回填，自 v7 起统计。

**Phase 17 — 维护与健壮性 ✅**
架构：新增 `kittymind/storage/`（两个 SQLite 库共用的底座）。
① **版本链迁移**（`migrations.py`）：每个步骤显式声明目标版本，`schema_migrations` 表记录"哪些步骤真的成功过"，**只有步骤成功才提升 `user_version`**——旧实现用 `suppress` 吞掉异常后无条件标最新版本，半截 schema 会永久固化；库版本高于代码版本时直接拒绝写入（防止降级运行写坏新库）。会话库迁移链 v3/v5/v6/v7/v8，允许跳号（对齐历史语义，不为连续插空步骤）。
② **启动自检与损坏恢复**：`PRAGMA quick_check` → 失败则把库连同 `-wal/-shm` **改名隔离**（不删）→ 尝试把可读页面抢救到新库 → 失败才建空库并告警。
③ **备份**：`Connection.backup()`（在线备份 API，能带上未 checkpoint 的 WAL 数据）写入库同级 `backups/`，按 `<库名>-<时间戳>.db` 命名、24h 间隔、滚动保留 5 份；空库不备份。
④ **空间维护**：启动 `wal_checkpoint(TRUNCATE)` + 退出时同样截断，运行期把 `wal_autocheckpoint` 收到 512 页；空闲页占比 ≥40% 且库 ≥32 MB 才在后台线程 VACUUM（重活不上启动路径，避免拖慢 Electron 就绪探测），另有 `store.compact()` 手动入口。
⑤ 工具审计库（此前 `user_version` 恒为 0、无迁移入口）纳入同一套版本链与自检。
⑥ 记忆文件改为**原子写**（同目录 tmp + `os.replace`），启动自检把解析不出来的 md 移入 `memory/_quarantine/` 并告警（此前是静默消失）。
实测效果（真实库）：WAL 3.80 MB → 0，`sessions.db` 0.53 MB → 0.07 MB（空闲页 86% → 0%），会话数据完好。
⑦ **行数保留期**：`model_usage` 保留 90 天、`tool_audit` 保留 30 天（`USAGE_RETENTION_DAYS` / `AUDIT_RETENTION_DAYS`，settings.json 可覆盖，`<=0` = 永久保留）。两者性质不同故分开定：前者是聚合数据（一个 turn 一行，增长极慢）且属成本审计凭证；后者是每次工具调用的明细，增长快、排查价值随时间快速衰减。用量裁剪在会话库启动维护里同步做（一条 DELETE，毫秒级，不看备份是否到期——保留期是卫生规则，不该被备份周期绑架）；审计裁剪挂在记录器**首次建立**时（懒加载单例，天然避开启动路径），SQLite 与 JSONL **必须一起裁**，否则会出现"文件里翻得到、库里查不到"。`kittymind/storage/atomic.py` 抽出共用的 `atomic_write_text`（记忆文件与审计 JSONL 共用，此前两处各写一份）。

⑧ **会话归档**（Phase 17 收尾）：`sessions.archived` 列（schema v8）。语义边界三条（都是刻意的）：**能继续聊**（归档不是只读锁）、**搜索不过滤** `archived`（搜不到等于真找不回）、**追加消息即自动取消归档**（`append` 里与写入同一次加锁做 `UPDATE ... WHERE archived=1`，避免"消息已落库、却还挂在归档分组"的中间态）。`list_headers(include_archived=False)` 默认过滤，对应前端侧栏顶部开关（`session/list` 的 `include_archived`，用 `is True` 判断以免字符串 "false" 被当真值）；`session/archive` RPC **不取消正在跑的任务**（归档不中断对话，这点与 `session/delete` 不同）。前端：顶部归档图标开关 + 会话行悬浮的归档按钮（排在删除左侧，破坏性操作离视线中心更远）+ 已归档行 `opacity:.6` 灰显。归档不纳入保留期清理——归档的语义就是长期留存。

**Phase 17M — 长期记忆检索升级 ✅**

问题（实测，非估算）：召回 / 提取 / 整合三条链路**都是 O(全量)**，规模墙在 **~30 条记忆**。① 召回把全部记忆目录喂给 LLM 选序号（12 条 525 tok，300 条约 11.7k tok，且选序号准确率随目录变长下降），而记忆**正文**（合计是目录的 4.8 倍）完全不参与匹配；② 提取去重靠 `existing_catalog[:4000]` 硬截断 ≈ **32 条**，之后后半记忆对提取完全失明 → 同一事实被反复提取成新记忆；③ 整合把全部正文灌 prompt，300 条实测 **68.2k tokens**（aux 小模型上下文必爆）。

架构：新增 `kittymind/embeddings/`（`BaseEmbedding` Protocol + `OpenAICompatEmbedding` + `get_embedding()` 单例，与 `get_aux_llm` 同构：构造失败返回 `None`、失败不缓存）与 `kittymind/memory/index.py`（独立库，schema 走 `storage/` 版本链 v1）。关键决策：

- **检索用 numpy 全量余弦，刻意不引入 sqlite-vec / ANN**：本机实测 1000 条 × 768 维一次全量余弦 **0.082 ms**、常驻 2.93 MB（2048 维 1000 条约 7.8 MB / 0.25 ms）。ANN 在 10³ 量级毫无意义，而 loadable extension 的打包成本是实打实的；`numpy` 本就在依赖里。
- **索引文本 = `[type] name: description` + 正文前 300 字**（实测依据：只用平均 42 字的 description 时平均 `top1 - median` 间距 0.132，加上正文后升到 0.145，且「你应该怎么跟我说话」的 top1 由错误项纠正为 persona 记忆）。
- **`model_id` / `dim` 是硬校验而非元数据**：不同模型（乃至同模型不同 `dimensions`）的向量不可比，混用会静默给出垃圾相似度——这是这类设计最阴的失败模式。对账时任一不匹配即整表重嵌（这条有测试钉住：清表后必须同步清掉内存视图，否则漂移根本不触发重嵌）。
- **对账走 stat 快路径**：mtime+size 未变就跳过（1000 条记忆不该每次召回都读 1000 个文件），疑似变化才构造索引文本算 hash；hash 一致（被 touch / 原子写重写同内容）则只刷新元数据不重嵌。拿不到文件时间戳的纯 dict 条目退化为纯 hash 对账。
- **索引库路径由记忆目录派生**（与 Phase 17 备份目录同一条纪律）：写成全局固定路径会让任何触发提取/召回的测试污染真实库，而且同名库还会让两个记忆目录**按名字互相覆盖**。
- **索引是纯派生数据，不进备份、不进隔离**：损坏时直接删库重建（与 `sessions.db` 刻意不同——没有抢救价值）。**懒索引**，不上启动路径（首次召回 / 提取后补齐），与 Phase 17「重活不上启动路径」同一条纪律。

召回：`MEMORY_RECALL_MODE ∈ {auto, vector, llm, keyword}`。`auto`（默认）走「向量分 + 0.3 × 归一化关键词分」融合；向量路不可用（无 key / 端点报错 / 超时 / 索引为空）时退回改造前的 LLM 选序号 → 2-gram 关键词。**旧路径不是删掉而是降级链的末端**——未配 `EMBEDDING_*` 的用户行为**逐字节不变**（硬要求），回滚对照（取 git 里旧 `recall.py` 跑 56 组输入）零差异。融合而非替换的理由：2-gram 对专名 / 代码标识符 / 版本号天生比向量强（`sqlite-vec`、`MEMORY_RECALL_TIMEOUT` 这类串在向量空间几乎无区分度）。门控（`MIN_SCORE` / `MIN_MARGIN`）**默认全关**——实测 top1 只有 0.348~0.564，而"不相关"项的 median 高达 0.219~0.437，两区间重叠，**任何绝对阈值换模型即失效**。召回超时（`MEMORY_RECALL_TIMEOUT=2.0s`）在 `kitty_agent` 层用 `asyncio.wait_for` 兜底：降级为关键词结果先注入，被超时的 `to_thread` 任务**不会被取消**、继续把索引补完，下一轮即命中缓存——**记忆召回绝不阻塞一个 turn**。

另两条链路：**提取去重视图**改为向量检索 top-8（token 预算 1500）替代 4000 字符截断；**整合预筛**用两两余弦（`M @ M.T`，1000 条约 4 MB / 1 ms）只把 `cos >= 0.86` 的近重复候选对交给模型。两条保护：记忆数 < `MEMORY_CONSOLIDATE_FULL_MAX=40` 或向量不可用则保留全量行为（小库上全量更准，也便于回滚对照）；预筛只缩候选集，`_plan_ops` 的「未被提及 = 保留」语义不变——**漏掉的对子不会丢数据**，只是这轮不合并。

实测（真实库 12 条 + 真实端点）：冷建索引 12 条 / **1187 ms**（一次网络往返），二次 `sync` **0.9 ms**（stat 快路径），索引库 16 KB（512 维，实测 `dimensions=512` 被端点支持，比 2048 维省 4 倍常驻）；4 个中文 query **全部 top1 命中**；每次召回 ~180 ms（一次 query embedding，且按会话冻结，不是每轮）。单轮注入量不变（5 条 × 800 字 ≈ 2340 字符）——变的是"这 5 条怎么选出来"。

规模化实测（同一套真实代码路径 + 假索引，只量 prompt 长度，不打网络；旧值取自 git 里的旧 `recall.py`）：

| 记忆条数 | 召回 prompt（旧） | 召回 LLM 调用（新） | 提取去重视图 tok 旧→新 | 旧视图能看到深处记忆 | 新视图 | 整合 prompt tok 旧→新 |
|---:|---:|---:|---|---|:---:|---|
| 12 | 320 | **0** | 268 → 1425 | 是（12/12） | 是 | 2,545 → 966 |
| 50 | 1,170 | **0** | 1,118 → 1,386 | 是（50/50） | 是 | 10,267 → 966 |
| 100 | 2,289 | **0** | 1,696 → 1,386 | **否**（只见前 76 条） | 是 | 20,427 → 966 |
| 300 | 6,866 | **0** | 1,696 → 1,342 | **否**（同上） | 是 | 62,618 → 966 |
| 1000 | 22,884 | **0** | 1,696 → 1,342 | **否**（同上） | 是 | 210,287 → 966 |

三点读法：① 召回不再产生任何 LLM prompt（只有一次 ~30 tok 的 query embedding），而旧路径 1000 条时要 22.9k tok 且选序号准确率随目录变长下降；② 提取去重视图 **从 100 条起就看不到"深处"记忆**（4000 字符截断只覆盖按文件名排序的前 76 条，与相关性无关），新路径始终能看到语义最相关的那条——注意小库（12 条）上新视图 token 反而更高（含正文前 300 字），这是**有意的代价**：多花约 1.1k tok 换"看得见正文的去重判断"；③ 整合 prompt 从线性膨胀（1000 条 210k tok）变成只含近重复候选对。

隐私：启用后**记忆正文（截断到 300 字）会发给 embedding 端点**，这是记忆内容第一次离开本机。`MEMORY_RECALL_MODE=keyword` 提供零外发模式；`.env.example` 与 `README.md` 均已标注。

**不做第三方记忆后端抽象**（mem0 / honcho 式）：它与自研检索是**替代关系**而非叠加——接进来等于把上述三条链路整个作废；且可插拔层最贵的部分正是接口本身（mem0 的 `add/search/delete` + `user_id/agent_id/run_id` 作用域、Honcho 的 workspace/peer/representation 模型与我们的 `write/list/snapshot` 形状差很远），在只有一个实现时是投机性抽象；还会丢掉 Markdown 的「人类可读 / 可 git / 改一条即生效」。若将来真要接：首选 **mem0 的 OSS 或自托管模式**（数据不出本机），避开 Platform；**Honcho 是 AGPL-3.0**，闭源产品需先过合规。

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

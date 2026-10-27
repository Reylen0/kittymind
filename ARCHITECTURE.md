# KittyMind 通用桌面 Agent 架构文档

## 目录

1. [项目定位](#1-项目定位)
2. [整体架构](#2-整体架构)
3. [技术选型](#3-技术选型)
4. [模块与目录结构](#4-模块与目录结构)
5. [开发路线与实现状态（Phase 1-33）](#5-开发路线与实现状态phase-1-33)

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

> 图中 MCP / Cron 为目标态，属第 5 章（Phase 23 / 30）范畴。

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
| 工具扩展 | **MCP** | 标准生态，stdio + SSE（未实现，Phase 23-24） |
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
│   ├── memory/                 # store / extract / recall / index（向量索引）
│   │                           #   / schema（v2 frontmatter）/ db（派生库）/ ledger（事件+召回留痕）
│   ├── embeddings/             # base（Protocol）/ openai_compat / get_embedding（Phase 17 语义检索）
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
├── test/                       # 41 个测试文件 / 712 个用例（特征测试 + 回归）
├── assets/                     # 应用图标 / 托盘图
├── chat_async.py               # CLI 演示入口
├── build-python.spec           # PyInstaller 打包配置
└── ARCHITECTURE.md
```

内置工具（14）：`bash` `file_read/write/edit` `glob` `grep` `ls` `git` `screenshot` `clipboard` `get_current_time` `write_memory` `verify`（自我验证）`task`（子任务）。

运行时数据目录 `~/.kittymind/`：`sessions.db`（SQLite 会话库）、`tool_audit.db` + `tool_audit.jsonl`（工具审计）、`memory/`（长期记忆 Markdown + `events.jsonl` 变更真相源 + `_trash/` 软删墓碑 + `_quarantine/` 损坏隔离）、`memory.db`（记忆派生库：向量 + 召回留痕，纯派生数据可删）、`workspaces.json`、`settings.json`、`backups/`（会话库与记忆目录备份）。

---

## 5. 开发路线与实现状态（Phase 1-33）

> 依据**当前代码**（2026-10，712 测试全绿）梳理。状态图例：✅ 已实现 ｜ 🟡 部分实现 ｜ ⬜ 未实现。
> 对标参照：Nous Research 的 hermes-agent（仅借鉴其 Agent 侧设计，服务端重型能力不在路线内）。

### 里程碑总览

| 里程碑 | 主题 | 包含 Phase | 状态 |
|--------|------|-----------|:----:|
| MVP | 跑通「输入→流式→工具→桌宠→持久化」闭环 | 1-9 | 🟡 核心 ✅ / 打包 🟡 |
| **M-A** | Agent 核心智能（压缩/委派/守护/验证） | 10-13 | ✅ |
| **M-B** | 健壮持久化 + 长期记忆 | 14-20 | ✅ 存储 ✅ / 记忆 ✅ |
| **M-C** | 工具与扩展生态 | 21-25 | ⬜ |
| **M-D** | 可观测与产品化交付 | 26-29 | 🟡 缓存 ✅ / 打包 🟡 |
| **M-E** | 定时与后台自动化 | 30-31 | ⬜ |
| **M-F** | 网关预留与桌宠深化 | 32-33 | ⬜ |

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
透明浮窗 + 分层精灵动画（`src/pet/layers/`）+ 事件→情绪基础映射（thinking/working/done/error）。完整情绪状态机与交互深化见 Phase 33。

**Phase 9 — 端到端联调与打包 🟡**
日常开发联调已通（应用可用）；分发打包见 Phase 29（Python 侧 spec 已有，Electron 侧未做）。

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

### 里程碑 M-B：健壮持久化 + 长期记忆（Phase 14-20）✅

**Phase 14 — SQLite 会话存储 ✅**
架构：单文件 `~/.kittymind/sessions.db`（WAL），`sessions` / `messages` / `session_state` 三表。压缩以 `archive_and_compact` 单事务落库：旧消息标 `active=0, compacted=1`（不删除），摘要与尾部作为新的活跃行写入——**压缩视图持久化，重启无需重压**。读取分双视图：模型视图 `WHERE active=1`（加载即已压缩状态），完整视图含 compacted 行（审计 / 全文检索用）。`session_state` 跨轮保存压缩冷却与 usage 校准基线。JSONL 后端已移除。关键文件：`session/`。
剩余：会话切换 / 重启后前端 ctx-ring 的恢复（`agent.context_usage` 事件链路已具备，加载时回推未做）。

**Phase 15 — FTS5 全文搜索 ✅**
架构：库内 `messages_fts`（普通 FTS5 表，`rowid` 对齐 `messages.id`）承载跨会话内容检索。切词自研：CJK 段展开成重叠二元组、非 CJK 段交 `unicode61`，入库与查询共用同一份切词函数（`session/_search_text.py`）。用二元组而非 trigram 是因为 trigram 要求查询 ≥3 字符，「压缩」「阈值」这类 2 字词全部 0 命中，而 2 字词恰是中文检索主力。索引只覆盖 `role IN ('user','assistant')`（工具原始输出不进索引，否则结果被回显淹没），并覆盖 compacted 行（压缩掉的历史照样能搜到），摘要行不重复索引。写入侧 Python 显式维护（二元组要现算），删除侧用 `AFTER DELETE` 触发器借 `sessions` 级联自动清理。`session/search` 按会话分组返回，高亮片段由 manager 层基于原文计算。前端复用侧栏搜索框：标题过滤本地即时，内容搜索防抖。

**Phase 16 — 用量与成本追踪 ✅**
架构：新增 `kittymind/usage/`。`UsageRecorder` 按模型累加主循环的 `usage` 真值，随 `_commit_turn` 原子落库到 `model_usage` 表。该表 `session_id` **不带外键**——删会话保留成本记录。子 Agent 的 token 真值经 ContextVar 回传父级合并，计入父会话成本。价目表内置默认（Claude/DeepSeek/Qwen/Ollama）并支持 `USAGE_PRICING` 覆盖，查不到返回 null 不编造。`usage/report` RPC 支持 `group_by ∈ {model, day, session}`，前端顶栏弹层展示。

**Phase 17 — 长期记忆 ✅**
架构：四层，存储层是唯一真相，其余都是可重建的派生结构。关键文件：`memory/`、`embeddings/`。

- **存储层**（`store.py` / `schema.py` / `ledger.py`）：Markdown + YAML frontmatter 落 `~/.kittymind/memory/`，人类可读、可 git、改一条即生效；文件原子写，启动自检把解析不出来的 md 移入 `_quarantine/` 并告警。frontmatter 字段只增不删，写出前做 round-trip 断言，不一致就拒绝写入；读侧刻意不用 `safe_load`（它会做类型推断、吃注释、在特定值上直接报错），改以 `schema: 2` 首键分发——v2 走 YAML，v1 走正则且不做类型推断。`events.jsonl` 是变更**真相源**（append-only + fsync、坏行跳过不删、随记忆目录一起备份）。
- **向量索引**（`index.py` / `db.py` / `embeddings/`）：独立派生库 `memory.db`，与 `sessions.db` 刻意分开——索引是纯派生数据，不进备份、不进隔离，损坏直接删库重建；路径由记忆目录名派生（固定路径会让测试污染真实库，同名库还会让两个记忆目录按名字互相覆盖）。`model_id` / `dim` 是硬校验而非元数据：不同模型的向量不可比，混用会**静默**返回垃圾相似度，任一不匹配即整表重嵌。对账走 stat 快路径（mtime + size 未变即跳过），疑似变化才算 hash。检索用 numpy 全量余弦，不引入 sqlite-vec / ANN——10³ 量级下 ANN 毫无意义，而 loadable extension 的打包成本是实打实的。**懒索引**，不上启动路径。
- **召回**（`recall.py`）：`MEMORY_RECALL_MODE ∈ {auto, vector, llm, keyword}`。`auto`（默认）走「向量分 + 0.3 × 归一化关键词分」融合，向量路不可用时逐级降级到 LLM 选序号 → 2-gram 关键词——**旧路径是降级链末端而非被删除**，未配 `EMBEDDING_*` 的用户行为逐字节不变。融合而非替换：2-gram 对专名 / 代码标识符 / 版本号天生强于向量。门控默认全关（实测相关与不相关的分数区间重叠，任何绝对阈值换模型即失效）。召回段按会话冻结、以独立 system 消息注入（不改主 system 字节，见 Phase 28），并有超时兜底：降级为关键词结果先注入，被超时的任务继续补索引、下一轮命中缓存——**记忆召回绝不阻塞一个 turn**。
- **提取与整合**（`extract.py`）：会话结束后由辅助模型扫描对话提取候选记忆，到阈值触发整合。整合只让模型产出「保留 / 合并 / 删除」**短指令**，正文由本地按行拼接（模型不碰 `\"` / `\n` 转义），并定义「**未被提及 = 保留**」——漏写不再等于丢失。去重视图与整合候选集均由向量检索给出，无 embedding 时退回字符截断 / 全量。

数据安全（针对旧实现会静默丢数据的三条路径）：身份从文件名改为稳定 `id`（旧实现下名字不同但 slug 撞车会互相吃掉）；同名写入不再无痕覆盖，旧正文进 `events.jsonl` 可回溯；整合的 `drop` 改为软删（`status=forgotten` + 移入 `_trash/`，可恢复），物理删除只有显式 `hard_delete()`。迁移「正则读、YAML 写」，文件名与位置不变，缺失时间戳用文件 mtime 兜底。
隐私：启用后记忆正文（截断）会发给 embedding 端点，这是记忆内容第一次离开本机；`MEMORY_RECALL_MODE=keyword` 提供零外发模式。**不做第三方记忆后端抽象**（mem0 / honcho 式）：它与自研检索是替代关系而非叠加，在只有一个实现时是投机性抽象，还会丢掉 Markdown 的可读 / 可 git / 改一条即生效。
尚未做完：`memory/*` RPC 命名空间与前端面板（list / get / update / delete / restore / pin / stats / events / recall-log / review / reindex / export）。

**Phase 18 — SQLite 存储底座 ✅**
架构：抽出 `kittymind/storage/`（会话库与工具审计库共用的底座；放独立包是因为 `session/` 不该被 `tools/` 反向依赖）。
① **版本链迁移**（`migrations.py`）：每步显式声明目标版本，`schema_migrations` 表记录真的成功过哪些步骤，**只有成功才提升 `user_version`**（失败停在上一版，下次启动自然重试）；库版本高于代码版本时拒绝写入。会话库迁移链 v3/v5/v6/v7/v8，允许跳号。
② **启动自检与损坏恢复**：`quick_check` 失败则把库连同 `-wal/-shm` **改名隔离**（不删）→ 尝试抢救可读页面到新库 → 失败才建空库并告警。
③ **在线备份**：`Connection.backup()`（能带上未 checkpoint 的 WAL 数据）写入同级 `backups/`，24h 间隔、滚动保留 5 份，空库不备份。
④ **空间维护**：启动 / 退出 `wal_checkpoint(TRUNCATE)`，运行期收窄 `wal_autocheckpoint`；空闲页 ≥40% 且库 ≥32 MB 才在后台线程 VACUUM（**重活不上启动路径**，避免拖慢 Electron 就绪探测），另有 `store.compact()` 手动入口。
`atomic.py` 抽出共用的 `atomic_write_text`（记忆文件与审计 JSONL 共用，此前两处各写一份）。

**Phase 19 — 数据保留期 ✅**
架构：按行裁剪两张表——`model_usage` 90 天、`tool_audit` 30 天（`USAGE_RETENTION_DAYS` / `AUDIT_RETENTION_DAYS`，`<=0` = 永久保留）。分开定值是因为两者性质不同：前者是聚合数据（一 turn 一行，增长极慢）且属成本审计凭证；后者是每次工具调用的明细，增长快、排查价值随时间快速衰减。用量裁剪随会话库启动维护同步做；审计裁剪挂在记录器**首次建立**时（懒加载单例，天然避开启动路径），SQLite 与 JSONL 两份**必须一起裁**，否则会出现「文件里翻得到、库里查不到」。

**Phase 20 — 会话归档 ✅**
架构：`sessions.archived` 列。语义三条（都是刻意的）：**能继续聊**（归档不是只读锁）、**搜索不过滤** archived（搜不到等于真找不回）、**追加消息即自动取消归档**（与写入同一次加锁完成，避免「消息已落库、却还挂在归档分组」的中间态）。`list_headers` 默认过滤，对应前端侧栏顶部开关；`session/archive` **不取消正在跑的任务**（归档不中断对话，这点与 `session/delete` 不同）。归档不纳入保留期清理——归档的语义就是长期留存。

---

### 里程碑 M-C：工具与扩展生态（Phase 21-25）🟡

**Phase 21 — 核心工具补齐 + 工具集分组 🟡**
`web_search` ✅：`kittymind/web_search/` 下 `TavilyBackend`/`BingBackend`/`SearxngBackend` 三选一，与 `embeddings/` 同构的工厂模式（`get_web_search_backend()`，构造失败不缓存，密钥只从 `WEB_SEARCH_API_KEY` 环境变量读，未配置时工具返回提示而不是报错中断主流程）；只读无副作用，`is_concurrency_safe=True` 且归入 `IDEMPOTENT_TOOLS`，未设审批规则。
剩余思路：`web_extract`（抓取完整网页正文，与 `web_search` 的摘要列表是两个工具）、`process_manage`（后台进程）、`todo_list`；`TOOLSETS` 分组字典按场景动态过滤 schema 下发。

**Phase 22 — 浏览器自动化 ⬜（重型，可选）**
思路：Playwright 驱动的浏览器工具组（导航/点击/填写/截图/提取）+ 持久 browser context 跨调用复用。

**Phase 23 — MCP 客户端集成 ⬜**
思路：MCP Manager 读 `~/.kittymind/mcp.json`，管理 stdio/SSE server 生命周期；MCP 工具动态注册进 registry 合并下发；前端管理 UI。注意与工具分组（Phase 21）协同做能力面控制。

**Phase 24 — MCP 服务端模式 ⬜**
思路：把 kittymind 的会话/工具经 MCP 暴露，供 Claude Code/Cursor 等外部宿主连接。

**Phase 25 — Skills 技能系统 ⬜**
思路：YAML frontmatter + Markdown 技能格式；按需注入 system prompt（追加尾部，保持缓存前缀稳定）；使用计数 + 过期归档策展；Agent 自创建技能。

---

### 里程碑 M-D：可观测与产品化交付（Phase 26-29）🟡

**Phase 26 — Observer Hooks + 结构化日志 ⬜（有基础）**
现有基础：EventBus + 审计 SQLite 已覆盖 tool 生命周期记录。
思路：never-block 只读钩子总线（超时保护）、pre/post_llm_call 事件族、session/turn/tool_call 关联 ID 贯穿、结构化日志替换散落 print（分级 + 轮转）。

**Phase 27 — 健康检查与遥测 ⬜**
思路：模型连通性/延迟探针 + `agent/health` RPC；可选 OTLP 导出（默认关闭）。

**Phase 28 — 提示词缓存优化 ✅**
架构：记忆召回以独立 system 消息注入，**不改主 system 字节**；压缩是唯一合法的前缀破坏点（且压缩后主动标记缓存失效）；Anthropic 适配器在 content block 上打 `cache_control` 断点（OpenAI 自动前缀缓存）。`PERMISSION_ASK_TIMEOUT` 等长等待均已 async 化，不影响缓存。

**Phase 29 — 打包分发 🟡**
已有：PyInstaller spec（`build-python.spec`）。
思路：electron-builder → Windows NSIS；首启 onboarding（配置模型/API key）；electron-updater 自动更新。

---

### 里程碑 M-E：定时与后台自动化（Phase 30-31）⬜

**Phase 30 — Cron 定时任务 ⬜**
思路：`jobs.json` + 60s ticker 调度器；Agent 经 `cron_tool` 自然语言创建/暂停/删除；结果投递到聊天窗/系统通知/文件；连续失败 N 次提示暂停。审批语义需明确：无人值守态 = `interactive=False`（守护栏硬停生效）。

**Phase 31 — 后台任务委派 ⬜**
思路：task 委派不等待返回，完成事件通知 + 桌宠提示；前端后台任务列表。

---

### 里程碑 M-F：网关预留与桌宠深化（Phase 32-33）⬜

**Phase 32 — 网关抽象层 ⬜（预留，不实现具体平台）**
思路：SessionSource 描述符（platform/chat_id/user_id/thread_id → session_key）+ `BasePlatformAdapter` 抽象（Electron 为首个实现）+ 消息路由。多租户/多平台实现明确不在路线内。

**Phase 33 — 桌宠情绪系统深化 🟡**
现状基础：透明浮窗、分层精灵动画、基础事件→情绪映射。
思路：完整情绪状态机（含 SLEEPY/NIGHT/idle 小动作与状态转移）、各情绪动画序列补全、对话气泡（回复摘要）、交互（拖拽/边缘吸附/右键菜单/点击唤起主窗）。

---

### 推进建议

- **近期优先 Phase 14 收尾**：ctx-ring 恢复直接提升日常可用性，SQLite 底座已就绪。
- **M-C 之前先做 21 的工具分组**：工具数即将扩张，先有 TOOLSETS 分组与能力面控制，再接 MCP（Phase 23），避免扁平列表失控。
- **Phase 30 依赖 12/28 的语义**：无人值守 = 非交互态硬停 + 缓存纪律，这两块已就绪，Cron 可直接叠加。
- **每个 Phase 配套测试**：当前 712 用例的「特征测试钉行为」模式（`test_react_loops.py` 等）应延续到新模块。


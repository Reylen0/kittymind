# KittyMind 通用桌面 Agent 架构文档

## 目录

1. [项目定位](#1-项目定位)
2. [整体架构](#2-整体架构)
3. [技术选型](#3-技术选型)
4. [模块与目录结构](#4-模块与目录结构)
5. [开发路线与实现状态（Phase 1-34）](#5-开发路线与实现状态phase-1-34)

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

## 5. 开发路线与实现状态（Phase 1-34）

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
| **M-G** | 长任务稳定性与断点恢复 | 34 | 🟡 |

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
剩余：会话切换 / 重启后前端 ctx-ring 的恢复（`agent.context_usage` 事件链路已具备，加载时回推未做）。**已知边界**：`_commit_turn` 是整轮才落库一次——进程在 ReAct 循环中途崩溃会丢失本轮全部消息（含已经真实发生的工具副作用的记录），设计见 Phase 34。

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
`web_extract` ✅：`kittymind/web_extract.py`，与 `web_search` 的多后端抽象不同——单一实现（httpx 直连 GET）不需要 Protocol/工厂这层。标准库 `html.parser.HTMLParser` 剥标签取正文，不引入第三方 HTML 解析库；不是 readability，只剔 script/style/svg 等不可读内容，不做导航/广告等版式噪音的语义剔除。流式下载 + `WEB_EXTRACT_MAX_BYTES` 硬上限，超限中途断开而非拉完整页再截断。只读无副作用，同归 `IDEMPOTENT_TOOLS`，未设审批规则。
剩余思路：`process_manage`（后台进程）、`todo_list`；`TOOLSETS` 分组字典按场景动态过滤 schema 下发。

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
思路：`jobs.json` + 60s ticker 调度器；Agent 经 `cron_tool` 自然语言创建/暂停/删除；结果投递到聊天窗/系统通知/文件；连续失败 N 次提示暂停。审批语义需明确：无人值守态 = `interactive=False`（守护栏硬停生效）。定时任务本质是「不保证有人在前台盯着的 turn」，崩溃恢复应直接复用 Phase 34 的 `turn_in_progress` 标记与幂等分类恢复机制，不单独发明一套。

**Phase 31 — 后台任务委派 ⬜**
思路：task 委派不等待返回，完成事件通知 + 桌宠提示；前端后台任务列表。同 Phase 30，断点恢复复用 Phase 34。

---

### 里程碑 M-F：网关预留与桌宠深化（Phase 32-33）⬜

**Phase 32 — 网关抽象层 ⬜（预留，不实现具体平台）**
思路：SessionSource 描述符（platform/chat_id/user_id/thread_id → session_key）+ `BasePlatformAdapter` 抽象（Electron 为首个实现）+ 消息路由。多租户/多平台实现明确不在路线内。

**Phase 33 — 桌宠情绪系统深化 🟡**
现状基础：透明浮窗、分层精灵动画、基础事件→情绪映射。
思路：完整情绪状态机（含 SLEEPY/NIGHT/idle 小动作与状态转移）、各情绪动画序列补全、对话气泡（回复摘要）、交互（拖拽/边缘吸附/右键菜单/点击唤起主窗）。

---

### 里程碑 M-G：长任务稳定性与断点恢复（Phase 34）🟡

**Phase 34 — 长任务稳定性与断点恢复 🟡（①②已实现）**

**现状缺口**：当前一次流式 ReAct 循环（`async_stream_run`）从组装 `TurnContext` 到产出 `final_text` 全程只在内存里推进——`turn.messages`/`turn.turn_messages` 直到循环结束才由 `_commit_turn()` 一次性落库（Phase 14）。这意味着：

- 进程在循环中途被杀（断电、Electron 强退、OOM、人工 kill）时，**不止本轮输出丢失，连用户刚发的那条消息、以及期间已经真实执行的工具副作用（已经跑过的 `bash` 命令、已经写入的文件、已经做的 `git commit`）都不会出现在 `sessions.db` 里**——重启后看到的是"消息从未发出"，但磁盘/仓库上发生的改动是真实且不可逆的。
- 工具调用审计（`tool_audit.db`，Phase 5/18）其实是**每次调用落一条**，比会话消息（整轮才落一条）粒度细得多——同一套系统里两个数据面的持久化粒度不一致，审计库能查到"做过什么"，`sessions.db` 却没有对应消息，复盘时两者对不上。
- `core/llm.py` 对 LLM 调用的网络抖动 / 429 / 5xx **零重试**，瞬时失败直接整轮作废；长任务工具调用步数越多，网络调用次数越多，被"一次性失败"拖垮的概率随步数线性上升。
- `bash` 等同步工具体经 `asyncio.to_thread` 丢进线程池执行：`task.cancel()` 只是不再等待线程，**线程内部的 `subprocess` 不会被杀**，用户点"停止"后它可能继续在后台裸跑到自然结束。
- `task` 子 Agent 委派只有步数上限（`SUBAGENT_MAX_ITERATIONS`），没有挂钟超时——子任务某次工具调用卡死，父 Agent 跟着整体挂起。

设计围绕一条原则展开：**长任务崩溃后，已经发生的事绝不能从历史里凭空消失；框架无法安全自动判断的，交还给模型自己核实**。五块各自独立、可渐进落地：

**① 消息面：轮内增量提交 ✅**
把 `_commit_turn()` 的调用时机从"`final_text` 产出后一次"改成"每个 ReAct 步骤（一条 assistant 消息 + 对应一批 tool_result 回填完）结束后一次"，复用现有 `append_turn()` 原子写入（不新发明持久化机制）；用户消息在轮次一开始、第一次调用 LLM 之前就先行提交。写入频率从 O(1)/轮升到 O(步数)/轮，但单次写入量更小，量级与现有 `tool_audit.db`（本来就是每次工具调用一条）相当，不是新的负载数量级。压缩（`archive_and_compact`）本身已经是原子操作，触发时机不变，只是压缩后的消息随下一步的增量提交一起落盘，不再需要撑到轮结束才生效。
架构：`KittyAgent._commit_new_messages()`（包 `append_turn`，把分配到的真实 seq 原地标回 `turn.messages`/`turn.turn_messages` 共享的同一批 dict 对象，`turn.loaded_seqs` 随之增长）在三处被调用——轮次开始提交用户消息、每步工具调用批次执行完立即提交、循环结束后提交尾部的最终 assistant 消息；`_commit_compaction()` 在 `did_compress` 为真的当下立即调用 `archive_and_compact`（`new_active_msgs` 固定传空列表，因为本轮新消息已由前者单独提交），同步把 `compressed_once` 写入 `session_state`。原 `_commit_turn()` 瘦身为"只补最后一条消息 + 落用量成本"，`compressed_this_turn` 字段连同分支判断整体删除（已无存在必要）。关键文件：`agent/kitty_agent.py`、`session/manager.py`（`append_turn` 现在返回分配到的 seq 列表）。测试：`test/test_turn_incremental_commit.py`（LLM 在循环中途抛异常模拟进程崩溃，断言已完成的步骤仍在 `sessions.db` 里；直接单测 `_commit_compaction` 的落库与 `loaded_seqs` 更新）。
剩余：③④⑤仍未实现，见下。

**② 状态面：`turn_in_progress` 标记（崩溃检测）✅**
`sessions` 表新增两列（schema v9→v10，走既有迁移链）：`turn_in_progress INTEGER NOT NULL DEFAULT 0`、`turn_started_at TEXT`。轮次开始（①的用户消息增量提交落库之后）置位，`run_conversation` 的 `finally` 分支无条件清除——正常结束、抛异常（如 LLM 调用失败，这类错误已经有 `agent.error` 事件明确报告给用户，不是"进程神秘消失"）、显式 `turn/cancel`（`task.cancel()` 触发的 `CancelledError` 同样会沿 `async for` 传播到这个 `finally`）都会走到这里清除；**真正的进程被杀**（断电、强杀、OOM）时 Python 代码完全来不及跑，`finally` 没有机会执行，标记原样留在库里。
`session/list`（`SessionManager.list_sessions()` → `store.list_headers()`）随每条会话 header 带上 `turn_in_progress` 布尔值——不走"启动时推一个新事件"的设计（那需要在还没有任何 WebSocket 连接存在的进程启动阶段就能推送，当前架构没有这个机制，也没必要为这一件事新建），而是复用前端本来就会在加载时调用的 `session/list`，侧栏在对应会话标题前标一个警示角标（`SessionList.tsx` 的 `IcoInterrupted`，悬浮提示"上次异常中断"）；用户点进去就能看到实际已经发生了什么（①已保证这些内容已落库）。另外在 `SqliteSessionStore._prepare_schema()` 末尾（`_purge_orphans()` 之后，同一处既有的"启动时顺带做一次"的位置，不单独起一遍扫描）加了一次廉价计数查询，发现有会话卡在 `turn_in_progress=1` 时写一条 warning 日志，方便运维层面定位。关键文件：`session/store.py`（schema、迁移、`get_state`/`save_state`/`list_headers` 扩展、`_log_interrupted_sessions`）、`session/manager.py`（`mark_turn_started`/`mark_turn_finished`）、`agent/kitty_agent.py`（`async_stream_run` 里提交用户消息后调 `mark_turn_started`；`run_conversation` 的 `finally` 里调 `mark_turn_finished`）、`electron/src/renderer/{types.ts,SessionList.tsx,style.css}`。测试：`test/test_turn_in_progress_marker.py`——**踩坑记录**：不能用"让 LLM 抛异常"模拟真正的进程崩溃来断言标记留下来，异常会被 Python 正常捕获、正常解包栈帧、正常跑到 `finally`，标记一样会被清掉（这是正确行为，不是 bug）；测的是"工具执行期间回读状态，此刻库里已经是 True"这个真正可断言的不变式。
剩余：③④⑤仍未实现，见下。

**③ 执行面：悬空 tool_call 的幂等恢复（恢复逻辑的核心规则）**
检测条件：最后一条 assistant 消息带 `tool_calls`，但对应 `tool_call_id` 在 `messages` 表里找不到匹配的 tool 结果行——说明进程恰好死在"执行这批工具调用"期间。恢复策略直接复用现成的 `guardrails.py::IDEMPOTENT_TOOLS` / `MUTATING_TOOLS` 分类，不新造一套判断：

- 悬空调用落在 `IDEMPOTENT_TOOLS`（`file_read`/`glob`/`grep`/`ls`/`web_search`/`load_skill`…）：安全，直接重新执行一次拿确定结果。
- 悬空调用落在 `MUTATING_TOOLS`（`bash`/`file_write`/`file_edit`/`git`/`write_memory`/`write_skill`…）：**绝不自动重放**——重放一个已经执行过的 `bash` 或 `git commit` 可能是实打实的重复副作用。改为合成一条模板化 `tool_result`："该工具调用的执行状态未知（进程在执行期间被中断），请用只读工具自行核实实际状态后再决定下一步"，把判断权交还模型本身（与本项目"不替模型做它该做的判断"的一贯风格一致，参见记忆整合 Phase 17 的同类取舍）。

新增 RPC `turn/resume`（区别于 `turn/run`：不追加新 user 消息，只把已持久化的消息前缀连同上面生成的合成结果一起喂给 `async_stream_run` 续跑）。前端检测到会话带"异常中断"标记时提供「继续」按钮调用它；用户也可以不管它、直接发新消息（新一轮 `turn/run` 自然用新的 `turn_started_at` 覆盖旧标记）。

**④ 工具取消的物理传导**
`bash` 工具由当前 `subprocess.run`（阻塞，`to_thread` 包裹）改为 `asyncio.create_subprocess_exec`（原生协作式取消：`task.cancel()` 能真正触发 `proc.kill()`），解决"点停止但子进程还在后台裸跑"的问题。`task` 委派增加挂钟超时 `SUBAGENT_WALL_TIMEOUT`（`asyncio.wait_for` 包裹 `sub_agent.run()`），子任务超时即视为失败返回，而不是让父 Agent 无限期等待。

**⑤ LLM 调用的瞬时错误重试**
`core/llm.py` 的 `invoke`/`async_stream_with_tools` 外包一层重试：只重试网络层 / 429 / 5xx / 超时这类分类明确的瞬时错误（400/401 这类业务错误不重试），指数退避，次数与基础延迟可配置（`LLM_RETRY_MAX_ATTEMPTS`/`LLM_RETRY_BASE_DELAY`）。流式调用的重试边界卡在**第一个 `text_delta`/`tool_calls_done` 产出之前**——一旦已经有内容流给前端，失败就不再重试（避免输出重复/错乱），走现有"抛异常中断本轮"路径，由①的增量提交兜底，不会丢失已完成的步骤。

**对 Phase 30 / 31 的复用**：Cron 定时任务与后台任务委派本质上都是"不保证有人在前台盯着的 turn"，设计时直接复用本 phase 的 `turn_in_progress` 标记与③的幂等分类恢复机制，不需要各自发明一套。

**推进顺序**：五块耦合度不同，按"独立 / 低风险"先、"依赖 / 高风险"后分两条线推进，不要混在一个 PR 里：

1. **⑤ LLM 调用重试**——改动面最小（`core/llm.py` 外包一层，零 schema 变更），不依赖其余四块，先做建立"瞬时错误分类 + 退避"这类测试模式的信心。
2. **④ 工具取消的物理传导**——同样不碰持久化层，`bash` 换 `asyncio.create_subprocess_exec`、`task` 加挂钟超时，修的是"停止按钮是假的"这个当下就能感知到的体验问题，和①②③完全解耦，可以和⑤并行甚至调换顺序。
3. **① 轮内增量提交 ✅（已实现）**——本阶段真正的地基，改动面最大（动的是 `async_stream_run`/`_commit_turn` 热路径），已单独落地并配套 `test/test_turn_incremental_commit.py`，全量 788 测试通过。
4. **② `turn_in_progress` 标记 ✅（已实现）**——只有①落地之后才有意义（没有增量提交，标记了也没有可恢复的数据可看），是①之上的轻量追加（一次 schema 迁移 + 置位/清除两行 + 启动日志 + 侧栏角标），已落地并配套 `test/test_turn_in_progress_marker.py`，全量 816 测试通过。
5. **③ 悬空 tool_call 恢复 + `turn/resume`**——依赖①②都跑稳，是最复杂的一块（幂等分类判断 + 新 RPC + 前端"继续"按钮三件套），放最后；这也是 Phase 30/31 真正要复用的部分，③完成之前 30/31 复用不了什么实质内容。

也就是**⑤④可以立刻动手且顺序随意，①→②→③是一条严格的依赖链，必须按序**——①②现已完成，下一步是③。

**已知边界**（这次设计不解决什么）：
- 不追求"分布式 exactly-once"语义——单机单进程场景下目标是"崩溃后可观测、可人工核实、不隐藏数据丢失"，不是"自动保证任意副作用工具恰好执行一次"（`bash` 这类工具的副作用本来就不可能被框架兜底，参见"安全模型"一节的同类立场）。
- 增量提交的粒度是"一批工具调用"而非单个工具调用——批内工具仍按现有分区并行语义一起提交，没必要细到单工具级事务。
- 流式重试仍有极小概率的"首字节已发出、客户端未确认"窗口，这是 HTTP 流式协议本身的限制，不特别处理，依赖①的可观测性兜底。

**验证思路**（延续"特征测试"模式）：mock LLM 响应序列中途人为抛异常模拟进程死亡，断言用户消息与崩溃前已完成步骤已落库、`turn_in_progress` 已置位；构造"有 `tool_calls` 无对应 tool_result"的消息序列直接灌入 store，断言悬空 mutating 调用生成的是"状态未知"合成结果而非自动重放；`bash` 工具起长命令后 cancel，断言底层子进程确实被杀死。

---

### 推进建议

- **近期优先 Phase 14 收尾**：ctx-ring 恢复直接提升日常可用性，SQLite 底座已就绪。
- **M-C 之前先做 21 的工具分组**：工具数即将扩张，先有 TOOLSETS 分组与能力面控制，再接 MCP（Phase 23），避免扁平列表失控。
- **Phase 30 依赖 12/28 的语义**：无人值守 = 非交互态硬停 + 缓存纪律，这两块已就绪，Cron 可直接叠加。
- **Phase 34 建议先于 Phase 30/31 做**：Cron 与后台任务委派都要依赖它的 `turn_in_progress` 标记与幂等恢复原语，先做 34 能让 30/31 直接复用而不是各自补一套断点恢复逻辑；34 本身的①（增量提交）也不依赖 30/31，可以独立先行。
- **每个 Phase 配套测试**：当前 712 用例的「特征测试钉行为」模式（`test_react_loops.py` 等）应延续到新模块。


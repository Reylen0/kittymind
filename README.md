# KittyMind

通用桌面 Agent（Windows 优先）。全程流式 ReAct、长期记忆、上下文自动压缩、子 Agent 委派、工具权限审批、桌宠悬浮窗。

Python Agent 核心 + Electron 桌面外壳，两者通过本地 WebSocket JSON-RPC 通信。

> 架构设计与实现状态的完整说明见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
> 本文档只讲**怎么跑起来、怎么配、有哪些能力**。

---

## 功能

**对话与交互**
- 全程流式输出，文字边生成边显示；工具卡片按模型声明顺序出现
- 多会话切换、重命名、归档、删除；跨会话全文检索（支持中文 2 字词）
- 上下文占用环实时显示 token 用量
- 工具审批弹窗，支持并发排队；审批超时自动撤回
- 工作区选择器：每个会话绑定一个工作目录，工具操作在该目录内进行
- 桌宠悬浮窗：分层精灵动画，按 Agent 状态切换情绪（思考 / 工作 / 完成 / 错误）
- 全局热键：`Ctrl+Shift+Space` 唤起极简输入框，`Ctrl+Shift+K` 显示/隐藏主聊天窗

**Agent 能力**
- **流式 ReAct**：同一个 stream 里同时产出文字与工具调用；同批工具分区并行执行（只读工具并发、变更工具串行）
- **上下文压缩**：超过阈值自动压缩历史（保头部 + 护尾部 + 中段摘要），压缩结果落库，重启无需重压
- **长期记忆**：会话结束自动提取，Markdown + YAML frontmatter 落盘，人类可读可 git；召回走「向量 + 关键词」融合
- **子 Agent 委派**：`task` 工具派发子任务，带深度/总数预算上限，同批多任务真并发
- **自我验证**：`verify` 工具跑 lint / test / 探活收集证据
- **工具守护栏**：重复失败、无进展循环自动升级为警告或硬停

**工程设施**
- SQLite 会话库（WAL）+ FTS5 全文索引；启动自检、损坏隔离恢复、自动备份、空间回收
- 工具调用审计（SQLite + JSONL 双写）+ 敏感信息脱敏
- 用量与成本追踪，按模型 / 按天 / 按会话聚合
- 提示词缓存优化：主 system prompt 跨轮字节级稳定
- 764 个测试用例（41 个测试文件）

**多厂商 LLM**
OpenAI 兼容接口通用（DeepSeek / Qwen / Kimi / 智谱 / Ollama / 各类网关），Anthropic 原生适配器带 `cache_control` 断点。

---

## 内置工具（16 个）

| 分类 | 工具 | 说明 |
|---|---|---|
| Shell | `bash` | 执行 shell 命令，有超时上限与危险命令黑名单 |
| 文件 | `file_read` `file_write` `file_edit` | 读 / 写 / 精确替换 |
| 检索 | `glob` `grep` `ls` | 按模式找文件 / 内容搜索 / 列目录 |
| 版本控制 | `git` | status / log / diff / add / commit 等 |
| 桌面 | `screenshot` `clipboard` | 截屏 / 剪贴板读写 |
| 联网 | `web_search` `web_extract` | 搜索（Tavily / Bing / SearXNG）/ 抓网页正文 |
| Agent | `task` `verify` | 派发子 Agent / 自我验证 |
| 其他 | `write_memory` `get_current_time` | 主动写入长期记忆 / 取当前时间 |

> MCP 扩展、浏览器自动化、工具分组尚未实现，路线见 `ARCHITECTURE.md` 第 5 章 Phase 21-25。

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

### 1. 配置 LLM

```bash
cp .env.example .env
```

编辑 `.env`，必填三项：

```env
LLM_MODEL_ID=deepseek-chat
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
```

### 2. 安装依赖

```bash
uv sync                      # Python 依赖
cd electron && pnpm install  # Electron 依赖
```

### 3. 启动

```bash
# 在 electron/ 目录下
pnpm run dev
```

同时启动 Vite（热更新）与 Electron，DevTools 自动打开。Python 服务端由 Electron 主进程拉起，无需手动启动。

---

## 可选配置

以下三项都是**不配也能正常用**，配了增强。

### 辅助小模型

给「压缩摘要 / 记忆提取 / 记忆召回筛选」这类辅助任务单独配一个便宜快的小模型。
留空则这些任务回退主模型——**主任务的推理与工具调用始终用主模型，不受影响**。

```env
LLM_AUX_MODEL_ID=qwen-turbo
# LLM_AUX_API_KEY=sk-xxx      # 不填复用 LLM_API_KEY
# LLM_AUX_BASE_URL=...        # 不填复用 LLM_BASE_URL
```

### 记忆语义检索

配了 embedding 端点后，记忆召回从「把记忆目录喂给模型选序号」变成「向量 + 关键词融合」，记忆涨到上百条也不会变慢。**不配则行为与之前逐字节一致**（自动走降级链）。

```env
EMBEDDING_MODEL_ID=embedding-3
EMBEDDING_BASE_URL=https://open.bigmodel.cn/api/paas/v4
EMBEDDING_API_KEY=your-embedding-key
# EMBEDDING_DIM=512           # 0 = 用服务端默认维度
```

> ⚠️ **隐私**：启用后会把**记忆正文**（每条截断到 300 字）发给该端点做向量化，这是记忆内容第一次离开本机。
> 想零外发：别配这段，或在 `settings.json` 里写 `{"MEMORY_RECALL_MODE": "keyword"}` 强制只走本地 2-gram 关键词。

### 联网搜索

```env
WEB_SEARCH_API_KEY=tvly-xxx   # 密钥只从环境变量读，不进 settings.json
```

后端默认 Tavily，可在 `settings.json` 里改 `WEB_SEARCH_BACKEND` 为 `bing` / `searxng`（SearXNG 需同时配 `WEB_SEARCH_BASE_URL` 指向自建实例）。未配密钥时 `web_search` 工具返回「未配置」提示，不会中断主流程。

---

## 进阶配置：settings.json

约 100 个运行参数可在 `~/.kittymind/settings.json` 覆盖，无需改代码。支持 `//` 行注释。

```jsonc
{
  // 模型上下文窗口（**重要**：默认 1024000 偏大，按实际模型调小否则压缩永不触发）
  "LLM_CONTEXT_WINDOW": 128000,
  "COMPRESS_THRESHOLD_RATIO": 0.70,   // 占用比超过此值触发压缩

  "BASH_TIMEOUT": 60,
  "AGENT_MAX_ITERATIONS": 50,
  "PERMISSION_ASK_TIMEOUT": 300,      // 审批等待上限，超时按拒绝处理

  "MEMORY_RECALL_MODE": "auto",       // auto | vector | llm | keyword
  "SUBAGENT_MAX_DEPTH": 3,
  "SUBAGENT_MAX_TOTAL": 8,

  "TOOL_GUARDRAIL_ENABLED": true,
  "LLM_TRACE_LEVEL": "on_error"       // off | on_error | all，排查问题时调成 all
}
```

**完整可覆盖清单与每项默认值见 [`kittymind/config.py`](kittymind/config.py)**（`_OVERRIDABLE` 集合定义了允许覆盖的 key；路径类配置刻意不可覆盖，避免破坏目录结构）。

配置层的错误一律**显式告警到 stderr**，不静默忽略——键名写错、JSON 语法错误、类型不匹配都会打印 `[warn] config: ...`。

> 优先级：环境变量 > `settings.json` > 代码默认值。
> API 密钥只从环境变量读，不进 `settings.json`。

`.env` 的查找顺序（`server/app.py`）：

1. `~/.kittymind/.env` —— 存在则**只读这一个**（打包分发版走这条）
2. 可执行文件 / 项目根目录下的 `.env`（开发模式走这条）
3. 当前工作目录的 `.env`（兜底）

---

## 运行时数据目录

全部数据在 `~/.kittymind/`（Windows 为 `C:\Users\<用户名>\.kittymind\`）：

| 路径 | 内容 | 可否删除 |
|---|---|---|
| `sessions.db` | 会话与消息（SQLite + WAL + FTS5 索引） | 删即丢历史 |
| `memory/` | 长期记忆 Markdown + `events.jsonl` 变更真相源 | 删即丢记忆 |
| `memory/_trash/` | 软删除的记忆（可恢复） | 可删 |
| `memory/_quarantine/` | 解析失败被隔离的记忆文件 | 可删（但建议先看一眼） |
| `memory.db` | 记忆向量索引 + 召回留痕（**纯派生数据**） | **可随时删，会自动重建** |
| `tool_audit.db` / `.jsonl` | 工具调用审计（双写同内容） | 可删 |
| `llm_trace.jsonl` | LLM 调用留档（默认只记失败） | 可删 |
| `settings.json` | 用户配置覆盖 | 可删（回到默认值） |
| `workspaces.json` | 工作区列表 | 可删 |
| `screenshots/` | 截图（按修改时间保留最新 200 张） | 可删 |
| `backups/` | 会话库与记忆目录的自动备份 | 可删 |
| `.env` | **打包分发版**在这里读 API Key | — |

保留期：用量明细 90 天、工具审计 30 天、记忆召回日志 30 天、记忆回收站 90 天（均可在 `settings.json` 调整，`<=0` 表示永久保留）。归档会话不纳入清理。

---

## 安全模型

这是一个**本地单用户**桌面 Agent，安全模型与云端多租户服务不同，需要明确知道边界在哪。

**有意识的设计选择**

- **`bash` 在宿主机直接执行**，以当前用户权限运行，不做容器 / microVM 隔离。
  桌面 Agent 的用途就是操作用户的真实文件、截图、剪贴板——隔离层会让这些能力失去意义，且在 Windows 上强依赖 Docker 对消费级应用不可接受。同类本地编码 Agent 采用相同策略，靠**逐次权限确认**而非 OS 隔离。
- **WebSocket 服务端无鉴权**，绑定环回地址 `127.0.0.1` 是唯一防线。
  `WS_ALLOW_NON_LOOPBACK` 默认 `False`，启动时拒绝非环回绑定——改成 `0.0.0.0` 意味着同网段任何进程都能远程驱动 `bash` 工具。确有局域网需求才显式打开，自担风险。
- **危险命令黑名单防手滑，不防对手**。`_dangerous.py` 的正则拦 `rm -rf`、`mkfs`、`shutdown` 这类误操作，但等价绕过路径在 shell 里是无穷的（如 `python -c "shutil.rmtree(...)"`）。真正的防线是下面的权限审批。

**实际生效的防线**

1. **三道权限闸门**（`tools/permission.py`）：硬拒绝黑名单 → 软规则匹配 → 用户审批。
   软规则覆盖工作区外读写、删除操作、系统路径写入、`git` 改写历史、剪贴板读写、`verify` 执行任意命令等。
2. **审批超时 fail-closed**：超时按拒绝处理，并推送 `tool.permission_expired` 撤回前端弹窗。
3. **工作区 containment**：文件类工具的路径参数被约束在工作目录内，越界需审批。
   注意这约束的是**工具参数**，不约束 `bash` 命令字符串内部的行为。
4. **输出脱敏 + 审计留档**：工具输出与审计参数中的敏感信息会被脱敏；每次调用（含拒绝决策）落库。
5. **工具守护栏**：重复失败 / 无进展循环升级为警告或硬停，防止模型陷入死循环烧 token。

**已知边界（尚未处理）**

- **间接提示注入无防护**。`web_extract` / `web_search` 把外部网页正文直接作为工具结果回灌进上下文，没有来源标记或内容分区。恶意网页中的指令可能被模型当作任务执行。
- **外部内容会进入长期记忆提取**。工具结果会参与会话结束后的记忆提取，意味着来自网页的内容有可能被写入 `memory/`，从而跨会话持续影响。**启用联网工具 + 长期记忆时，建议定期人工检查 `~/.kittymind/memory/` 下的内容。**
- **凭证路径未硬禁**。`~/.ssh`、`~/.aws`、`.env` 等路径目前只受工作区 containment 约束，不在工作区内时需审批，但没有独立的拒绝名单。

---

## CLI 演示（不启动 Electron）

```bash
uv run python chat_async.py
```

流式对话 + 工具调用 + 会话持久化 + 事件总线的最小演示，审批走命令行 `[y/N]` 交互。适合调试 Agent 核心而不想起前端时使用。

---

## 开发

### 常用命令

| 命令 | 位置 | 说明 |
|------|------|------|
| `pnpm run dev` | `electron/` | 开发模式（Vite HMR + Electron + 自动拉起 Python） |
| `pnpm start` | `electron/` | 生产预览（加载 `renderer-dist/`） |
| `pnpm run build:renderer` | `electron/` | 只构建前端 |
| `pnpm run build:win` | `electron/` | 打包 Windows 应用 |
| `uv run pytest test/` | 根目录 | 跑全部测试（764 用例） |
| `uv run pytest test/test_react_loops.py -v` | 根目录 | 跑单个测试文件 |
| `uv run ruff check .` | 根目录 | lint |
| `uv run ruff check . --fix` | 根目录 | lint 并自动修复 |
| `uv run pre-commit install` | 根目录 | 安装 git hook（首次） |
| `uv run python chat_async.py` | 根目录 | CLI 对话演示 |

### 测试约定

测试采用**特征测试**模式：用 mock LLM 给定固定响应序列，断言 Agent 的行为（如「压缩后 tool_call / tool_result 配对不断裂」、「权限闸门按序触发」、「审批超时推送撤回事件」）。

这类测试钉的是**代码行为**，不评估 Agent 的任务质量——换模型或改 prompt 后测试照样全绿。新增模块请延续这个模式，见 `test/test_react_loops.py`、`test/test_permission.py`。

### Lint 配置说明

`pyproject.toml` 里刻意**不开** bandit（`S`）规则集——本项目大量使用 `subprocess` 是设计意图，开了会淹没在 `noqa` 里。其余 ignore 项都附了具体理由，修改前先读注释。

---

## 目录结构

```
kittymind/
├── kittymind/                  # Python 核心包
│   ├── core/                   # LLM 层：llm / llm_adapters / llm_response
│   │                           #   / llm_json / llm_trace / message / exceptions
│   ├── agent/                  # base / kitty_agent（唯一 ReAct 入口）
│   │                           #   / delegation（子 Agent 预算）/ verify/
│   ├── context/                # token_counter / micro_compaction / compressor
│   ├── tools/                  # base / registry / executor / permission
│   │                           #   / guardrails / audit / redaction
│   │                           #   + builtin/（16 个工具 + _paths/_fmt/_dangerous）
│   ├── memory/                 # store / schema / ledger（存储层，真相源）
│   │                           #   + index / db（向量索引，派生）
│   │                           #   + recall / extract（召回与提取整合）
│   ├── embeddings/             # base（Protocol）/ openai_compat
│   ├── web_search/             # base / tavily / bing / searxng（工厂模式）
│   ├── web_extract.py          # 网页正文抓取（单一实现，标准库解析）
│   ├── session/                # store（SQLite + FTS5）/ manager
│   │                           #   / _search_text（CJK 二元组切词）
│   ├── storage/                # migrations（版本链）/ maintenance（自检·备份·回收）
│   │                           #   / atomic（原子写）
│   ├── usage/                  # recorder / pricing（用量与成本）
│   ├── events/                 # bus（asyncio Pub/Sub，全链路唯一事件出口）/ types
│   ├── workspace/              # manager（cwd 上下文隔离）
│   ├── config.py               # 统一配置（settings.json 覆盖）
│   └── prompts.py              # 集中管理 system prompt
├── server/                     # WebSocket JSON-RPC 服务端
│   ├── app.py                  # 装配与启动入口
│   ├── ws_server.py            # WebSocket 服务器
│   ├── rpc_handler.py          # 方法路由 + 事件按 session 过滤转发
│   └── permission_bridge.py    # 审批往返（asyncio.Future 实现）
├── electron/                   # Electron 桌面应用
│   ├── main.js                 # 主进程：子进程管理 / 三窗口 / 热键 / 托盘 / 事件白名单
│   ├── preload*.js             # 聊天 / 桌宠 / 悬浮层三份 contextBridge
│   └── src/
│       ├── renderer/           # React 聊天界面
│       ├── pet/                # 桌宠窗口（Canvas + 分层精灵）
│       └── overlay/            # 全局悬浮输入层
├── test/                       # 41 个测试文件 / 764 用例
├── docs/                       # 补充文档
├── chat_async.py               # CLI 演示入口
├── build-python.spec           # PyInstaller 打包配置
├── ARCHITECTURE.md             # 架构设计与实现状态
└── .env.example                # 环境变量模板
```

---

## IPC 协议

Electron ↔ Python 通过 WebSocket JSON-RPC 通信，默认 `ws://127.0.0.1:8765`（端口被占用时自动递增重试，实际端口由主进程探测后获取）。

### Renderer → Python（请求）

| 方法 | 说明 |
|---|---|
| `turn/run` | 跑一轮对话（流式消费，结果经事件推送） |
| `turn/cancel` | 取消正在跑的轮次 |
| `session/create` `session/list` `session/get` `session/delete` | 会话增删查 |
| `session/rename` `session/archive` | 重命名 / 归档 |
| `session/search` | 跨会话全文检索（按会话分组返回，带高亮片段） |
| `workspace/create` `workspace/list` `workspace/delete` | 工作区管理 |
| `usage/report` | 用量报表（`group_by` ∈ `model` / `day` / `session`） |
| `tool/permission_response` | 回传审批结果 |
| `permission/pending` | 查询待审批队列（重连后恢复弹窗用） |
| `agent/status` | Agent 运行状态 |

```jsonc
{ "id": 1, "method": "turn/run", "params": { "text": "...", "session_id": "s1" } }
{ "id": 2, "method": "usage/report", "params": { "group_by": "model" } }
```

### Python → Renderer（推送）

| 事件 | 说明 |
|---|---|
| `agent.start` `agent.thinking` | 轮次开始 / 等待模型 |
| `agent.chunk` | 流式文本增量 |
| `agent.tool_call` `agent.tool_result` | 工具调用 / 结果（按模型声明序） |
| `agent.context_usage` | 上下文占用（驱动前端占用环） |
| `agent.done` `agent.error` | 轮次结束 / 出错 |
| `subagent.start` `subagent.done` | 子 Agent 生命周期 |
| `tool.permission_request` | 请求审批（弹窗） |
| `tool.permission_expired` | 审批超时（撤回弹窗） |

```jsonc
{ "method": "agent.chunk",     "params": { "delta": "你好", "session_id": "s1" } }
{ "method": "agent.tool_call", "params": { "name": "bash", "args": "...", "session_id": "s1" } }
{ "method": "agent.done",      "params": { "session_id": "s1", "text": "..." } }
```

> 事件按 `session_id` 过滤转发。子 Agent 的事件 `session_id=None`，天然不透传给前端——前端只看到 `subagent.start/done`，不会被子 Agent 的内部工具调用刷屏。
> Electron 主进程另有一份事件白名单（`main.js`），只有列入的事件才会转发给渲染进程。

---

## 打包发布

### 1. 打包 Python 服务端

```bash
# 项目根目录
uv run pyinstaller build-python.spec --distpath dist-python --noconfirm
```

输出 `dist-python/server/`（`server.exe` + `_internal/`，约 50MB）。

### 2. 打包 Electron

```bash
# electron/ 目录
pnpm run build:win
```

输出 `electron/dist-build/`：`win-unpacked/KittyMind.exe`（免安装目录）+ NSIS 安装包。

> NSIS 需要先缓存构建工具：下载 [`nsis-3.0.4.1.7z`](https://npmmirror.com/mirrors/electron-builder-binaries/nsis-3.0.4.1/nsis-3.0.4.1.7z) 解压到
> `%LOCALAPPDATA%\electron-builder\cache\nsis\nsis-3.0.4.1\`。
> 只要免安装版的话，`build.win.target` 里已配了 `dir`，忽略 NSIS 报错即可。

### 3. 分发

把 `dist-build/win-unpacked/` 整个目录压缩发出即可。使用者需自行创建配置文件：

```
C:\Users\<用户名>\.kittymind\.env
```

```env
LLM_MODEL_ID=deepseek-chat
LLM_API_KEY=sk-xxx
LLM_BASE_URL=https://api.deepseek.com/v1
# LLM_AUX_MODEL_ID=qwen-turbo    # 可选
```

> 首启 onboarding（引导式填 API Key）与自动更新尚未实现，见 `ARCHITECTURE.md` Phase 29。

---

## 常见问题

**压缩从来不触发？**
`LLM_CONTEXT_WINDOW` 默认 `1024000`，比多数模型的实际窗口大得多，导致占用比永远到不了阈值。按实际模型在 `settings.json` 里调小。

**改了 `settings.json` 没生效？**
看 stderr 有没有 `[warn] config: ...`。常见原因：键名不在 `_OVERRIDABLE` 清单里、JSON 语法错误、类型不匹配（如布尔值写成 `"false"` 字符串——这个会被正确识别，但 `"maybe"` 不会）。

**记忆召回变慢 / 结果不对？**
`memory.db` 是纯派生数据，直接删掉会自动重建。换了 embedding 模型也必须删（`model_id` / `dim` 不匹配时会整表重嵌，但手动删更快）。

**开发时改了项目根目录的 `.env` 却不生效？**
如果 `~/.kittymind/.env` 存在，它会被**优先且独占**读取，项目根目录的 `.env` 根本不会加载。装过打包版再回来开发时容易踩。删掉或改名 `~/.kittymind/.env` 即可。

**端口 8765 被占用？**
会自动向上递增重试（最多 `PORT_RETRY_COUNT` 次），实际端口由 Electron 主进程探测获取，无需手动处理。

**工具审批弹窗消失了但任务卡住？**
审批超时默认 300 秒，超时按拒绝处理并推送撤回事件。调大 `PERMISSION_ASK_TIMEOUT` 即可。

---

## 许可

MIT License — 见 [LICENSE](LICENSE)。

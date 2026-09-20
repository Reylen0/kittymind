export type MessageRole = 'user' | 'assistant' | 'tool'

export interface Message {
  id: string
  role: MessageRole
  content: string
  isStreaming?: boolean
  isError?: boolean
  // tool call fields
  toolName?: string
  toolArgs?: string
  toolResult?: string   // undefined = still running
  /** 历史消息（从库里读出来的整页）不播入场动画：打开会话时内容应「立刻就在那里」 */
  noAnim?: boolean
}

export interface Session {
  id: string
  title: string
  created_at: string
  workspace_id?: string | null
}

/** session/get 返回的一条历史消息（展示视图）。tool 行的名字/入参由后端随行下发。 */
export interface SessionMessage {
  seq?: number
  role: string
  content: string | null
  tool_calls?: unknown[]
  tool_call_id?: string
  tool_name?: string
  tool_args?: string
}

export interface Workspace {
  id: string
  name: string
  path: string
  created_at: string
}

/** session/search 的一条命中。`marks` 是 `text` 内的高亮区间 [起, 止)。 */
export interface SearchHit {
  seq: number
  role: string
  text: string
  marks: Array<[number, number]>
}

/** session/search 的结果按会话分组，组序即相关度序（最相关的会话在前）。 */
export interface SearchGroup {
  session_id: string
  title: string
  hits: SearchHit[]
}

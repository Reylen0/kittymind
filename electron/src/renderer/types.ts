export type MessageRole = 'user' | 'assistant' | 'tool'

export interface Message {
  id: string
  role: MessageRole
  content: string
  isStreaming?: boolean
  // tool call fields
  toolName?: string
  toolArgs?: string
  toolResult?: string   // undefined = still running
}

export interface Session {
  id: string
  title: string
  created_at: string
  workspace_id?: string | null
}

export interface Workspace {
  id: string
  name: string
  path: string
  created_at: string
}

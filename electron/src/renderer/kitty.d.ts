export {}

import type { SearchGroup } from './types'

// Injected by electron/preload.js via contextBridge
declare global {
  interface Window {
    kitty?: {
      call(method: string, params?: Record<string, unknown>): Promise<unknown>

      sendMessage(text: string, sessionId: string, workspaceId?: string): Promise<unknown>
      cancelTurn(sessionId: string): Promise<unknown>

      createSession(title?: string, workspaceId?: string): Promise<{ session_id: string; title: string }>
      listSessions(): Promise<Array<{ id: string; title: string; created_at: string; workspace_id?: string | null }>>
      // 不带 opts → 全量消息；带 opts.limit → 只取 seq 比 opts.beforeSeq 更早的一页
      getSession(sessionId: string, opts?: { limit?: number; beforeSeq?: number }): Promise<{
        header: {
          id: string
          title: string
          created_at: string
          workspace_id?: string | null
          used_tokens?: number
          total_tokens?: number
        }
        messages: Array<{
          seq?: number
          role: string
          content: string | null
          tool_calls?: unknown[]
          tool_call_id?: string
          /** tool 行随行下发的发起信息（分页切到哪都不会丢） */
          tool_name?: string
          tool_args?: string
        }>
        /** 分页模式下：更早是否还有消息 */
        has_more?: boolean
        /** 分页模式下：本页最早一条的 seq，回传即可继续向前翻页 */
        cursor?: number | null
      } | null>
      deleteSession(sessionId: string): Promise<{ deleted: boolean }>
      /** 全文搜索历史消息；结果按会话分组，每组带若干命中片段 */
      searchSessions(query: string, opts?: { sessionId?: string; limit?: number }): Promise<SearchGroup[]>

      listWorkspaces(): Promise<Array<{ id: string; name: string; path: string; created_at: string }>>
      createWorkspace(name: string, path: string): Promise<{ id: string; name: string; path: string; created_at: string }>
      selectWorkspace(): Promise<string | null>

      agentStatus(): Promise<{ name: string; model: string; running_sessions: string[] }>

      windowControl(action: string): Promise<unknown>
      setNativeTheme?(opts: { color: string; symbolColor: string }): Promise<unknown>

      respondPermission(requestId: string, approved: boolean): Promise<unknown>
      /** 切回会话 / 重载窗口时补拉仍在等待确认的审批（审批事件是一次性推送） */
      getPendingPermissions(sessionId: string): Promise<{
        pending: Array<{
          request_id: string
          tool: string
          args: Record<string, unknown>
          reason: string
          session_id: string | null
        }>
      }>

      // on() returns an unsubscribe function — call it in useEffect cleanup
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      on(event: string, cb: (data: any) => void): () => void
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      once(event: string, cb: (data: any) => void): void
    }
  }
}

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
      /** includeArchived → 连已归档会话一起返回（默认 false：归档的语义就是"收起"） */
      listSessions(opts?: { includeArchived?: boolean }): Promise<Array<{
        id: string; title: string; created_at: string
        workspace_id?: string | null
        /** 已归档：侧栏默认不显示，开关打开时灰显列出 */
        archived?: boolean
      }>>
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
      /** 归档 / 取消归档：只影响侧栏可见性，数据一行不删（ok=false = 会话不存在） */
      setSessionArchived(sessionId: string, archived?: boolean): Promise<{ ok: boolean; archived: boolean }>
      /** 全文搜索历史消息；结果按会话分组，每组带若干命中片段 */
      searchSessions(query: string, opts?: { sessionId?: string; limit?: number }): Promise<SearchGroup[]>

      listWorkspaces(): Promise<Array<{ id: string; name: string; path: string; created_at: string }>>
      createWorkspace(name: string, path: string): Promise<{ id: string; name: string; path: string; created_at: string }>
      /** 删除工作区；其下会话保留并移回「对话」分组 */
      deleteWorkspace(workspaceId: string): Promise<{ deleted: boolean; moved_sessions: number }>
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
      /** 用量成本报告（Phase 16）；cost 在价目表查不到时为 null。
       *  session 分组额外带 title / workspace（会话已删除时 title 为 null）。 */
      getUsageReport(opts?: { groupBy?: 'model' | 'day' | 'session'; since?: number; until?: number }): Promise<{
        group_by: string
        groups: Array<{
          key: string
          prompt_tokens: number
          completion_tokens: number
          n_calls: number
          cost: number | null
          title?: string | null
          workspace?: string | null
        }>
        total: { prompt_tokens: number; completion_tokens: number; n_calls: number; cost?: number | null }
      }>

      // on() returns an unsubscribe function — call it in useEffect cleanup
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      on(event: string, cb: (data: any) => void): () => void
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      once(event: string, cb: (data: any) => void): void
    }
  }
}

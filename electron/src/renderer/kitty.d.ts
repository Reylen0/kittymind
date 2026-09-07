export {}

// Injected by electron/preload.js via contextBridge
declare global {
  interface Window {
    kitty?: {
      call(method: string, params?: Record<string, unknown>): Promise<unknown>

      sendMessage(text: string, sessionId: string, workspaceId?: string): Promise<unknown>
      cancelTurn(sessionId: string): Promise<unknown>

      createSession(title?: string, workspaceId?: string): Promise<{ session_id: string; title: string }>
      listSessions(): Promise<Array<{ id: string; title: string; created_at: string; workspace_id?: string | null }>>
      getSession(sessionId: string): Promise<{
        header: { id: string; title: string; created_at: string; workspace_id?: string | null }
        messages: Array<{
          role: string
          content: string | null
          tool_calls?: unknown[]
          tool_call_id?: string
        }>
      } | null>
      deleteSession(sessionId: string): Promise<{ deleted: boolean }>

      listWorkspaces(): Promise<Array<{ id: string; name: string; path: string; created_at: string }>>
      createWorkspace(name: string, path: string): Promise<{ id: string; name: string; path: string; created_at: string }>
      selectWorkspace(): Promise<string | null>

      agentStatus(): Promise<{ name: string; model: string; running_sessions: string[] }>

      respondPermission(requestId: string, approved: boolean): Promise<unknown>

      // on() returns an unsubscribe function — call it in useEffect cleanup
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      on(event: string, cb: (data: any) => void): () => void
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      once(event: string, cb: (data: any) => void): void
    }
  }
}

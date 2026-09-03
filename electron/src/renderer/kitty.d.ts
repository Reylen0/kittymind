export {}

// Injected by electron/preload.js via contextBridge
declare global {
  interface Window {
    kitty?: {
      call(method: string, params?: Record<string, unknown>): Promise<unknown>

      sendMessage(text: string, sessionId: string): Promise<unknown>
      cancelTurn(sessionId: string): Promise<unknown>

      createSession(title?: string): Promise<{ session_id: string; title: string }>
      listSessions(): Promise<Array<{ id: string; title: string; created_at: string }>>
      getSession(sessionId: string): Promise<{
        header: { id: string; title: string; created_at: string }
        messages: Array<{
          role: string
          content: string | null
          tool_calls?: unknown[]
          tool_call_id?: string
        }>
      } | null>
      deleteSession(sessionId: string): Promise<{ deleted: boolean }>
      agentStatus(): Promise<{ name: string; model: string; running_sessions: string[] }>

      // on() returns an unsubscribe function — call it in useEffect cleanup
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      on(event: string, cb: (data: any) => void): () => void
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      once(event: string, cb: (data: any) => void): void
    }
  }
}

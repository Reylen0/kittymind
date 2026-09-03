'use strict'
// Chat window contextBridge — exposes kitty API to renderer
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('kitty', {
  // ── Generic RPC ──────────────────────────────────────────────────────────
  call: (method, params = {}) =>
    ipcRenderer.invoke('ws:call', { method, params }),

  // ── Conversation ─────────────────────────────────────────────────────────
  sendMessage: (text, sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'turn/run',    params: { text, session_id: sessionId } }),
  cancelTurn: (sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'turn/cancel', params: { session_id: sessionId } }),

  // ── Session management ───────────────────────────────────────────────────
  createSession: (title) =>
    ipcRenderer.invoke('ws:call', { method: 'session/create', params: { title } }),
  listSessions: () =>
    ipcRenderer.invoke('ws:call', { method: 'session/list',   params: {} }),
  getSession: (sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'session/get',    params: { session_id: sessionId } }),
  deleteSession: (sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'session/delete', params: { session_id: sessionId } }),

  // ── Agent state ──────────────────────────────────────────────────────────
  agentStatus: () =>
    ipcRenderer.invoke('ws:call', { method: 'agent/status', params: {} }),

  // ── Event subscriptions ──────────────────────────────────────────────────
  // on() registers a wrapper and returns an unsubscribe function.
  // Do NOT use off() — pass the returned cleanup to useEffect's return instead.
  on: (event, cb) => {
    const wrapper = (_e, data) => cb(data)
    ipcRenderer.on(`ws:event:${event}`, wrapper)
    return () => ipcRenderer.removeListener(`ws:event:${event}`, wrapper)
  },
  once: (event, cb) => ipcRenderer.once(`ws:event:${event}`, (_e, data) => cb(data)),
})

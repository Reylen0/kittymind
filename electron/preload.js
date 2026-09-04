'use strict'
// Chat window contextBridge — exposes kitty API to renderer
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('kitty', {
  // Generic RPC
  call: (method, params = {}) =>
    ipcRenderer.invoke('ws:call', { method, params }),

  // Conversation
  sendMessage: (text, sessionId, workspaceId) =>
    ipcRenderer.invoke('ws:call', {
      method: 'turn/run',
      params: { text, session_id: sessionId, ...(workspaceId ? { workspace_id: workspaceId } : {}) },
    }),
  cancelTurn: (sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'turn/cancel', params: { session_id: sessionId } }),

  // Session management
  createSession: (title, workspaceId) =>
    ipcRenderer.invoke('ws:call', {
      method: 'session/create',
      params: { title, ...(workspaceId ? { workspace_id: workspaceId } : {}) },
    }),
  listSessions: () =>
    ipcRenderer.invoke('ws:call', { method: 'session/list',   params: {} }),
  getSession: (sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'session/get',    params: { session_id: sessionId } }),
  deleteSession: (sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'session/delete', params: { session_id: sessionId } }),

  // Workspace management
  listWorkspaces: () =>
    ipcRenderer.invoke('ws:call', { method: 'workspace/list', params: {} }),
  createWorkspace: (name, path) =>
    ipcRenderer.invoke('ws:call', { method: 'workspace/create', params: { name, path } }),
  selectWorkspace: () =>
    ipcRenderer.invoke('workspace:select'),

  // Agent state
  agentStatus: () =>
    ipcRenderer.invoke('ws:call', { method: 'agent/status', params: {} }),

  // Window / edit controls (custom TopBar)
  windowControl: (action) => ipcRenderer.invoke('window:control', action),

  // Event subscriptions — on() returns an unsubscribe function
  on: (event, cb) => {
    const wrapper = (_e, data) => cb(data)
    ipcRenderer.on(`ws:event:${event}`, wrapper)
    return () => ipcRenderer.removeListener(`ws:event:${event}`, wrapper)
  },
  once: (event, cb) => ipcRenderer.once(`ws:event:${event}`, (_e, data) => cb(data)),
})

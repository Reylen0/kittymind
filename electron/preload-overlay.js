'use strict'
// Overlay (quick-input) window contextBridge
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('overlay', {
  sendMessage: (text, sessionId) =>
    ipcRenderer.invoke('ws:call', { method: 'turn/run',       params: { text, session_id: sessionId } }),
  createSession: (title) =>
    ipcRenderer.invoke('ws:call', { method: 'session/create', params: { title } }),
  agentStatus: () =>
    ipcRenderer.invoke('ws:call', { method: 'agent/status',   params: {} }),

  hide: () => ipcRenderer.send('overlay:hide'),

  on: (event, cb) => {
    const wrapper = (_e, data) => cb(data)
    ipcRenderer.on(`ws:event:${event}`, wrapper)
    return () => ipcRenderer.removeListener(`ws:event:${event}`, wrapper)
  },
  off: (event, cb) => ipcRenderer.removeListener(`ws:event:${event}`, cb),
})

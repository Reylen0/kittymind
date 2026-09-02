'use strict'
// Pet window contextBridge — receive agent events, control click-through
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('pet', {
  on:  (event, cb) => ipcRenderer.on(`ws:event:${event}`, (_e, data) => cb(data)),
  off: (event, cb) => ipcRenderer.removeListener(`ws:event:${event}`, cb),

  // Toggle mouse click-through (true = transparent to clicks, false = interactive)
  setClickThrough: (ignore) => ipcRenderer.send('pet:set-ignore-mouse', ignore),
})

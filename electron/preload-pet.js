'use strict'
// Pet window contextBridge — receive agent events, control click-through
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('pet', {
  on: (event, cb) => {
    const wrapper = (_e, data) => cb(data)
    ipcRenderer.on(`ws:event:${event}`, wrapper)
    return () => ipcRenderer.removeListener(`ws:event:${event}`, wrapper)
  },

  // Toggle mouse click-through (true = transparent to clicks, false = interactive)
  setClickThrough: (ignore) => ipcRenderer.send('pet:set-ignore-mouse', ignore),
})

'use strict'
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('pet', {
  // Agent event subscriptions — returns unsubscribe fn
  on: (event, cb) => {
    const wrapper = (_e, data) => cb(data)
    ipcRenderer.on(`ws:event:${event}`, wrapper)
    return () => ipcRenderer.removeListener(`ws:event:${event}`, wrapper)
  },

  // Window control
  setClickThrough: (ignore) => ipcRenderer.send('pet:set-ignore-mouse', ignore),

  // Drag — call on mousedown to get the current window position, then send move on mousemove
  getWindowBounds: () => ipcRenderer.invoke('pet:get-bounds'),
  move: (x, y)   => ipcRenderer.send('pet:move', { x, y }),
  snapToEdge: ()  => ipcRenderer.invoke('pet:snap-edge'),

  // Context menu
  showContextMenu: () => ipcRenderer.send('pet:context-menu'),
})

'use strict'
/**
 * Electron 主进程
 *
 * 启动流程:
 *   1. spawnPython() — 启动 `uv run python -m server.app`
 *   2. waitForReady() — 等待 stdout 出现 "[ready] ws://..." 行
 *   3. PythonBridge.connect() — 建立 WebSocket 连接
 *   4. 创建三个窗口（Chat / Pet / Overlay）
 *   5. 注册全局热键、系统托盘
 */

const {
  app, BrowserWindow, ipcMain, globalShortcut,
  Tray, Menu, nativeImage, screen, dialog,
} = require('electron')
const { spawn } = require('child_process')
const path      = require('path')
const fs        = require('fs')
const WebSocket = require('ws')
const { ensureTrayIcon, ensureAppIcon } = require('./icon-gen')

// ─── Constants ────────────────────────────────────────────────────────────────

const PROJECT_ROOT = path.join(__dirname, '..')
const IS_DEV       = process.argv.includes('--dev')

const WIN_SPEC = {
  // CHAT html is only used in prod; dev uses VITE_DEV_URL
  CHAT:    { w: 900, h: 650, html: path.join(__dirname, 'renderer-dist', 'index.html') },
  PET:     { w: 200, h: 323, html: path.join(__dirname, 'src', 'pet',      'index.html') },
  OVERLAY: { w: 500, h: 80,  html: path.join(__dirname, 'src', 'overlay',  'index.html') },
}

const VITE_DEV_URL = IS_DEV ? 'http://localhost:5173' : null

// Agent events that get relayed from Python → all renderer windows
const PUSH_EVENTS = [
  'agent.start', 'agent.thinking', 'agent.chunk',
  'agent.tool_call', 'agent.tool_result', 'agent.done', 'agent.error',
  'tool.permission_request',
]

// ─── Global State ─────────────────────────────────────────────────────────────

let chatWin    = null
let petWin     = null
let overlayWin = null
let tray       = null
let pythonProc = null
let bridge     = null

// ─── PythonBridge ─────────────────────────────────────────────────────────────

class PythonBridge {
  constructor() {
    this._ws            = null
    this._pending       = new Map()   // id → { resolve, reject, timer }
    this._eventHandlers = new Map()   // method → Set<fn>
    this._nextId        = 1
  }

  connect(url) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url)
      ws.once('open',  () => { this._ws = ws; resolve() })
      ws.once('error', reject)
      ws.on('message', (raw) => this._onMessage(raw))
      ws.on('close',   () => { this._ws = null })
    })
  }

  _onMessage(raw) {
    let msg
    try { msg = JSON.parse(raw) } catch { return }

    if (msg.id != null) {
      // RPC response
      const p = this._pending.get(msg.id)
      if (!p) return
      clearTimeout(p.timer)
      this._pending.delete(msg.id)
      msg.error ? p.reject(new Error(String(msg.error?.message ?? msg.error)))
                : p.resolve(msg.result)
    } else if (msg.method) {
      // Server-push event (no id field)
      const handlers = this._eventHandlers.get(msg.method)
      if (handlers) handlers.forEach(fn => { try { fn(msg.params) } catch {} })
    }
  }

  call(method, params = {}, timeoutMs = 30000) {
    return new Promise((resolve, reject) => {
      if (!this.isConnected) { reject(new Error('not connected')); return }
      const id    = this._nextId++
      const timer = setTimeout(() => {
        this._pending.delete(id)
        reject(new Error(`RPC timeout: ${method}`))
      }, timeoutMs)
      this._pending.set(id, { resolve, reject, timer })
      this._ws.send(JSON.stringify({ id, method, params }))
    })
  }

  onEvent(method, fn) {
    if (!this._eventHandlers.has(method)) this._eventHandlers.set(method, new Set())
    this._eventHandlers.get(method).add(fn)
  }

  get isConnected() {
    return this._ws != null && this._ws.readyState === WebSocket.OPEN
  }

  close() { this._ws?.close() }
}

// ─── Python Process ───────────────────────────────────────────────────────────

// PID 文件路径——用于跨进程清理僵尸服务
const PID_FILE = path.join(
  process.env.APPDATA || require('os').homedir(),
  '.kittymind', 'server.pid'
)

function writePid(pid) {
  try {
    fs.mkdirSync(path.dirname(PID_FILE), { recursive: true })
    fs.writeFileSync(PID_FILE, String(pid), 'utf8')
  } catch {}
}

function clearPid() {
  try { fs.unlinkSync(PID_FILE) } catch {}
}

// 杀掉上次残留的 Python 进程（开发调试时 will-quit 不一定触发）
function killOrphan() {
  try {
    const pid = parseInt(fs.readFileSync(PID_FILE, 'utf8'), 10)
    if (!pid || isNaN(pid)) return
    if (process.platform === 'win32') {
      spawn('taskkill', ['/F', '/T', '/PID', String(pid)], { stdio: 'ignore' })
    } else {
      process.kill(pid, 'SIGTERM')
    }
  } catch {}
  clearPid()
}

function spawnPython() {
  let cmd, args, cwd

  if (app.isPackaged) {
    // Production: use PyInstaller-bundled server.exe
    cmd  = path.join(process.resourcesPath, 'server', 'server.exe')
    args = []
    cwd  = path.join(process.resourcesPath, 'server')
  } else {
    // Dev / pnpm start: delegate to uv
    cmd  = 'uv'
    args = ['run', 'python', '-m', 'server.app']
    cwd  = PROJECT_ROOT
  }

  const proc = spawn(cmd, args, {
    cwd,
    env:   { ...process.env },
    stdio: ['ignore', 'pipe', 'pipe'],
  })

  proc.stderr.on('data', d => process.stderr.write(`[py] ${d}`))
  proc.on('error', e  => console.error('[py] spawn error:', e.message))
  proc.on('exit',  (code, sig) => {
    console.log(`[py] exited code=${code} sig=${sig}`)
    clearPid()
    pythonProc = null
  })

  writePid(proc.pid)

  return proc
}

function waitForReady(proc, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(
      () => reject(new Error('Python server did not start within 30s')),
      timeoutMs,
    )

    let buf = ''
    proc.stdout.on('data', chunk => {
      buf += chunk.toString()
      const lines = buf.split('\n')
      buf = lines.pop()
      for (const line of lines) {
        process.stdout.write(`[py] ${line}\n`)
        const m = line.match(/\[ready\]\s+(ws:\/\/\S+)/)
        if (m) { clearTimeout(t); resolve(m[1]) }
      }
    })

    proc.on('exit', code => {
      clearTimeout(t)
      reject(new Error(`Python exited early with code ${code}`))
    })
  })
}

// ─── Event Relay ─────────────────────────────────────────────────────────────

function broadcastToWindows(channel, data) {
  for (const win of [chatWin, petWin, overlayWin]) {
    if (win && !win.isDestroyed()) {
      win.webContents.send(channel, data)
    }
  }
}

function setupEventRelay(b) {
  for (const evt of PUSH_EVENTS) {
    b.onEvent(evt, params => broadcastToWindows(`ws:event:${evt}`, params))
  }
}

// ─── IPC ──────────────────────────────────────────────────────────────────────

function setupIpc(b) {
  // Relay RPC calls from any renderer → Python WS
  ipcMain.handle('ws:call', async (_e, { method, params }) => {
    if (!b?.isConnected) return { error: 'Python backend not connected' }
    try   { return await b.call(method, params) }
    catch (e) { return { error: e.message } }
  })

  // Window controls & edit actions from custom TopBar
  ipcMain.handle('window:control', (_e, action) => {
    switch (action) {
      case 'minimize':   chatWin?.minimize(); break
      case 'maximize':   chatWin?.isMaximized() ? chatWin.unmaximize() : chatWin?.maximize(); break
      case 'close':      chatWin?.close(); break
      case 'undo':       chatWin?.webContents.undo(); break
      case 'redo':       chatWin?.webContents.redo(); break
      case 'cut':        chatWin?.webContents.cut(); break
      case 'copy':       chatWin?.webContents.copy(); break
      case 'paste':      chatWin?.webContents.paste(); break
      case 'selectAll':  chatWin?.webContents.selectAll(); break
    }
  })

  // Overlay: hide on request or after sending a message
  ipcMain.on('overlay:hide', () => overlayWin?.hide())

  // Workspace: open native folder picker
  ipcMain.handle('workspace:select', async () => {
    const result = await dialog.showOpenDialog(chatWin, {
      properties: ['openDirectory'],
      title: '选择工作区目录',
    })
    return result.canceled ? null : result.filePaths[0] ?? null
  })

  // Pet: toggle click-through mode
  ipcMain.on('pet:set-ignore-mouse', (_e, ignore) => {
    petWin?.setIgnoreMouseEvents(Boolean(ignore), { forward: true })
  })

  // Pet: drag & edge snapping
  ipcMain.handle('pet:get-bounds', () => petWin?.getBounds() ?? { x: 0, y: 0, width: 180, height: 200 })
  ipcMain.on('pet:move', (_e, { x, y }) => petWin?.setPosition(Math.round(x), Math.round(y)))
  ipcMain.handle('pet:snap-edge', () => {
    if (!petWin) return
    const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize
    const [wx, wy] = petWin.getPosition()
    const { width: ww, height: wh } = petWin.getBounds()
    const snap = 30
    let nx = wx, ny = wy
    if (wx < snap)          nx = 0
    if (wx + ww > sw - snap) nx = sw - ww
    if (wy < snap)          ny = 0
    if (wy + wh > sh - snap) ny = sh - wh
    petWin.setPosition(nx, ny)
  })

  // Pet: native context menu
  ipcMain.on('pet:context-menu', () => {
    if (!petWin) return

    // Scan available character models at runtime
    const layersDir = path.join(__dirname, 'src', 'pet', 'layers')
    let models = []
    try {
      models = fs.readdirSync(layersDir).filter(d =>
        fs.existsSync(path.join(layersDir, d, 'manifest.json'))
      )
    } catch {}

    const template = [
      { label: '打开主窗口', click: () => { chatWin?.show(); chatWin?.focus() } },
      { label: petWin.isVisible() ? '隐藏桌宠' : '显示桌宠',
        click: () => petWin?.isVisible() ? petWin.hide() : petWin?.show() },
    ]

    if (models.length > 1) {
      template.push({
        label: '切换角色',
        submenu: models.map(m => ({
          label: m,
          click: () => petWin?.webContents.send('ws:event:pet.switch-model', { model: m }),
        })),
      })
    }

    template.push({ type: 'separator' }, { label: '退出 KittyMind', click: () => app.quit() })
    Menu.buildFromTemplate(template).popup({ window: petWin })
  })
}

// ─── Application Menu ─────────────────────────────────────────────────────────

function setupAppMenu() {
  const aboutClick = () => dialog.showMessageBox(chatWin, {
    type: 'info', title: 'KittyMind', message: 'KittyMind',
    detail: '版本 v0.1.0\n通用桌面 AI Agent', buttons: ['确定'],
  })

  const template = [
    {
      label: 'KittyMind',
      submenu: [
        { label: '关于 KittyMind', click: aboutClick },
        { type: 'separator' },
        { label: '退出', role: 'quit' },
      ],
    },
    {
      label: '编辑(E)',
      submenu: [
        { label: '撤销(U)',  role: 'undo',      accelerator: 'CmdOrCtrl+Z' },
        { label: '重做(R)',  role: 'redo',      accelerator: 'CmdOrCtrl+Y' },
        { type: 'separator' },
        { label: '剪切(T)',  role: 'cut',       accelerator: 'CmdOrCtrl+X' },
        { label: '复制(C)',  role: 'copy',      accelerator: 'CmdOrCtrl+C' },
        { label: '粘贴(P)',  role: 'paste',     accelerator: 'CmdOrCtrl+V' },
        { type: 'separator' },
        { label: '全选(A)',  role: 'selectAll', accelerator: 'CmdOrCtrl+A' },
      ],
    },
    {
      label: '窗口(W)',
      submenu: [
        { label: '最小化', role: 'minimize' },
        {
          label: '最大化 / 还原',
          click: () => {
            if (!chatWin) return
            chatWin.isMaximized() ? chatWin.unmaximize() : chatWin.maximize()
          },
        },
        { type: 'separator' },
        { label: '关闭窗口', role: 'close' },
      ],
    },
    {
      label: '帮助(H)',
      submenu: [
        { label: '关于 KittyMind', click: aboutClick },
      ],
    },
  ]

  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ─── Window Factories ─────────────────────────────────────────────────────────

function createChatWindow() {
  chatWin = new BrowserWindow({
    width:           WIN_SPEC.CHAT.w,
    height:          WIN_SPEC.CHAT.h,
    title:           'KittyMind',
    backgroundColor: '#f5f5f7',
    titleBarStyle:   'hidden',
    titleBarOverlay: {
      color:       '#f5f5f7',
      symbolColor: '#1c1c1e',
      height:      32,
    },
    webPreferences: {
      preload:          path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration:  false,
    },
  })
  if (VITE_DEV_URL) {
    chatWin.loadURL(VITE_DEV_URL)
    chatWin.webContents.openDevTools({ mode: 'detach' })
  } else {
    chatWin.loadFile(WIN_SPEC.CHAT.html)
  }
  chatWin.on('closed', () => { chatWin = null })
  return chatWin
}

function createPetWindow() {
  const { width: sw, height: sh } = screen.getPrimaryDisplay().workAreaSize
  petWin = new BrowserWindow({
    width:       WIN_SPEC.PET.w,
    height:      WIN_SPEC.PET.h,
    x:           sw - WIN_SPEC.PET.w - 20,
    y:           sh - WIN_SPEC.PET.h - 40,
    transparent: true,
    frame:       false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable:   false,
    webPreferences: {
      preload:          path.join(__dirname, 'preload-pet.js'),
      contextIsolation: true,
      nodeIntegration:  false,
    },
  })
  petWin.loadFile(WIN_SPEC.PET.html)
  petWin.on('closed', () => { petWin = null })
  return petWin
}

function createOverlayWindow() {
  const { width: sw } = screen.getPrimaryDisplay().workAreaSize
  overlayWin = new BrowserWindow({
    width:       WIN_SPEC.OVERLAY.w,
    height:      WIN_SPEC.OVERLAY.h,
    x:           Math.floor((sw - WIN_SPEC.OVERLAY.w) / 2),
    y:           120,
    transparent: true,
    frame:       false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable:   false,
    show:        false,   // starts hidden, Ctrl+Shift+Space to show
    webPreferences: {
      preload:          path.join(__dirname, 'preload-overlay.js'),
      contextIsolation: true,
      nodeIntegration:  false,
    },
  })
  overlayWin.loadFile(WIN_SPEC.OVERLAY.html)
  // Auto-hide when focus leaves overlay
  overlayWin.on('blur',   () => overlayWin?.hide())
  overlayWin.on('closed', () => { overlayWin = null })
  return overlayWin
}

// ─── Tray ─────────────────────────────────────────────────────────────────────

async function setupTray() {
  const icon = await app.getFileIcon(process.execPath, { size: 'small' })

  tray = new Tray(icon)
  tray.setToolTip('KittyMind')

  const buildMenu = () => Menu.buildFromTemplate([
    {
      label: chatWin?.isVisible() ? '隐藏主窗口' : '显示主窗口',
      click: () => {
        if (!chatWin) { createChatWindow(); return }
        chatWin.isVisible() ? chatWin.hide() : chatWin.show()
      },
    },
    {
      label: petWin?.isVisible() ? '隐藏桌宠' : '显示桌宠',
      click: () => petWin?.isVisible() ? petWin.hide() : petWin?.show(),
    },
    { type: 'separator' },
    { label: '退出 KittyMind', click: () => app.quit() },
  ])

  // Left-click: show/focus chat
  tray.on('click', () => {
    if (!chatWin) return
    chatWin.isVisible() ? chatWin.focus() : chatWin.show()
  })
  tray.on('right-click', () => tray.popUpContextMenu(buildMenu()))
}

// ─── Hotkeys ──────────────────────────────────────────────────────────────────

function setupHotkeys() {
  // Ctrl+Shift+Space — toggle quick-input overlay
  globalShortcut.register('Ctrl+Shift+Space', () => {
    if (!overlayWin) return
    if (overlayWin.isVisible()) {
      overlayWin.hide()
    } else {
      overlayWin.center()
      overlayWin.show()
      overlayWin.focus()
    }
  })

  // Ctrl+Shift+K — show/hide main chat window
  globalShortcut.register('Ctrl+Shift+K', () => {
    if (!chatWin) return
    chatWin.isVisible() ? chatWin.hide() : (chatWin.show(), chatWin.focus())
  })
}

// ─── App Lifecycle ────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  // Ensure app icons exist (no-op if already present)
  ensureAppIcon(path.join(__dirname, 'assets', 'icon.ico'))

  Menu.setApplicationMenu(null)   // 菜单移入自定义顶栏，去掉原生第二行

  // 清理上次残留的 Python 进程
  killOrphan()

  try {
    console.log('[main] Starting Python server...')
    pythonProc = spawnPython()
    const wsUrl = await waitForReady(pythonProc)
    console.log(`[main] Python ready at ${wsUrl}`)

    bridge = new PythonBridge()
    await bridge.connect(wsUrl)
    console.log('[main] WebSocket connected')

    setupIpc(bridge)
    setupEventRelay(bridge)
  } catch (err) {
    console.error('[main] Python/WS init failed:', err.message)
    // Continue without backend — windows still open, calls will return error
    setupIpc(null)
  }

  createChatWindow()
  createPetWindow()
  createOverlayWindow()
  await setupTray()
  setupHotkeys()
  console.log('[main] Ready')
})

// On Windows/Linux, closing all windows doesn't quit by default (tray stays)
app.on('window-all-closed', () => {
  if (process.platform === 'darwin') app.quit()
  // On Windows: keep running in tray
})

app.on('activate', () => {
  if (!chatWin) createChatWindow()
})

function killPython() {
  if (!pythonProc) return
  const proc = pythonProc
  pythonProc = null
  if (process.platform === 'win32') {
    // /T kills the entire process tree (uv → python → any children)
    spawn('taskkill', ['/F', '/T', '/PID', String(proc.pid)], { stdio: 'ignore' })
  } else {
    proc.kill('SIGTERM')
  }
}

app.on('will-quit', () => {
  globalShortcut.unregisterAll()
  bridge?.close()
  clearPid()
  killPython()
})

// 捕获 Node 进程直接退出的情况（如 Ctrl+C 在终端）
process.on('exit', () => { clearPid() })

// Ctrl+C in terminal sends SIGINT to the process group; handle it explicitly
// so will-quit fires and Python is cleaned up
process.on('SIGINT',  () => app.quit())
process.on('SIGTERM', () => app.quit())

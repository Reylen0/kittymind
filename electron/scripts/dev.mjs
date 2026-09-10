/**
 * dev.mjs — 替代 concurrently 的 Windows 友好开发脚本。
 *
 * 直接通过 Node.js 启动 Vite 和 Electron，不走 cmd.exe shell，
 * 因此 Ctrl+C 只触发一次 SIGINT，无「批处理终止(Y/N)?」提示，
 * 也无 SIGTERM 在 Windows 上被忽略导致的挂起问题。
 */

import { spawn, spawnSync } from 'child_process'
import { readFileSync }      from 'fs'
import { homedir }           from 'os'
import { createRequire }     from 'module'
import { fileURLToPath }     from 'url'
import { dirname, join }     from 'path'

const __dirname   = dirname(fileURLToPath(import.meta.url))
const require     = createRequire(import.meta.url)
const root        = join(__dirname, '..')
const electronBin = require('electron')
const viteBin     = join(root, 'node_modules', 'vite', 'bin', 'vite.js')

function run(cmd, args, opts = {}) {
  return spawn(cmd, args, { stdio: 'inherit', shell: false, cwd: root, ...opts })
}

// ── 启动 Vite，监听 stdout 等 ready 信号 ────────────────────────────
process.stdout.write('[dev] Starting Vite...\n')

const vite = spawn(process.execPath, [viteBin], {
  shell: false, cwd: root,
  stdio: ['inherit', 'pipe', 'inherit'],  // stdout pipe 以便检测 ready
})

// 把 Vite stdout 透传给终端
vite.stdout.pipe(process.stdout)

await new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Vite did not start within 30s')), 30_000)
  vite.stdout.on('data', chunk => {
    if (chunk.toString().includes('ready in') || chunk.toString().includes('Local:')) {
      clearTimeout(timer)
      resolve()
    }
  })
  vite.on('exit', code => { clearTimeout(timer); reject(new Error(`Vite exited with ${code}`)) })
})

process.stdout.write('[dev] Vite ready. Starting Electron...\n')

// ── 启动 Electron ────────────────────────────────────────────────
const electron = run(electronBin, ['.', '--dev'])

// ── 任一进程退出时清理另一个 ─────────────────────────────────────
// PID file written by main.js to track the Python/uv process
const PID_FILE = join(process.env.APPDATA || homedir(), '.kittymind', 'server.pid')

function taskkillTree(pid) {
  if (!pid) return
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/F', '/T', '/PID', String(pid)], { stdio: 'ignore' })
  } else {
    try { process.kill(pid, 'SIGTERM') } catch {}
  }
}

let cleanedUp = false
function cleanup(exitCode = 0) {
  if (cleanedUp) return
  cleanedUp = true

  // Kill Python/uv via PID file first — uv may create Python in a separate
  // process group, so it won't appear in Electron's /T tree.
  try {
    const pid = parseInt(readFileSync(PID_FILE, 'utf8'), 10)
    if (pid && !isNaN(pid)) taskkillTree(pid)
  } catch {}

  // Kill Electron tree (and whatever children survived)
  taskkillTree(electron?.pid)
  // Kill Vite
  taskkillTree(vite?.pid)

  process.exit(exitCode)
}

electron.on('exit', code => cleanup(code ?? 0))
vite.on('exit',     code => cleanup(code ?? 0))

process.on('SIGINT', () => cleanup(0))


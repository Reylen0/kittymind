/**
 * dev.mjs — 替代 concurrently 的 Windows 友好开发脚本。
 *
 * 直接通过 Node.js 启动 Vite 和 Electron，不走 cmd.exe shell，
 * 因此 Ctrl+C 只触发一次 SIGINT，无「批处理终止(Y/N)?」提示，
 * 也无 SIGTERM 在 Windows 上被忽略导致的挂起问题。
 */

import { spawn }          from 'child_process'
import { createConnection } from 'net'
import { createRequire }  from 'module'
import { fileURLToPath }  from 'url'
import { dirname, join }  from 'path'

const __dirname   = dirname(fileURLToPath(import.meta.url))
const require     = createRequire(import.meta.url)
const root        = join(__dirname, '..')
const electronBin = require('electron')        // electron npm 包直接导出二进制路径
const viteBin     = join(root, 'node_modules', 'vite', 'bin', 'vite.js')

function run(cmd, args) {
  return spawn(cmd, args, { stdio: 'inherit', shell: false, cwd: root })
}

async function waitForPort(port, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const ok = await new Promise(resolve => {
      const s = createConnection({ host: '127.0.0.1', port })
      s.once('connect', () => { s.destroy(); resolve(true)  })
      s.once('error',   () => { s.destroy(); resolve(false) })
    })
    if (ok) return
    await new Promise(r => setTimeout(r, 300))
  }
  throw new Error(`Port ${port} not ready within ${timeoutMs / 1000}s`)
}

// ── 启动 Vite（直接用 node 运行，不走 .cmd 包装） ────────────────
const vite = run(process.execPath, [viteBin])

process.stdout.write('[dev] Waiting for Vite on :5173...\n')
await waitForPort(5173)
process.stdout.write('[dev] Vite ready. Starting Electron...\n')

// ── 启动 Electron ────────────────────────────────────────────────
const electron = run(electronBin, ['.', '--dev'])

// ── 任一进程退出时清理另一个 ─────────────────────────────────────
function cleanup(exitCode = 0) {
  try { electron.kill() } catch {}
  try { vite.kill()     } catch {}
  process.exit(exitCode)
}

electron.on('exit', code => cleanup(code ?? 0))
vite.on('exit',     code => cleanup(code ?? 0))

// ── Ctrl+C：直接退出，无弹窗 ─────────────────────────────────────
process.on('SIGINT', () => cleanup(0))

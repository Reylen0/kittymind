/**
 * One-shot: launch the Electron app, wait for the pet window to load,
 * take a screenshot, then quit.
 */
import { _electron as electron } from 'playwright-core'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'
import { mkdirSync, existsSync } from 'fs'

const __dir = dirname(fileURLToPath(import.meta.url))
const APP_DIR = join(__dir, '..')
const SHOT_DIR = join(__dir, '../../../shots')
mkdirSync(SHOT_DIR, { recursive: true })

const electronBin = join(APP_DIR, 'node_modules/.pnpm/electron@44.1.0/node_modules/electron/dist/electron.exe')

console.log('Launching Electron…')
const app = await electron.launch({
  executablePath: electronBin,
  args: [APP_DIR],
  env: { ...process.env },
  timeout: 60_000,
})

// Wait for windows to appear
await new Promise(r => setTimeout(r, 8000))

console.log('Windows:', app.windows().length)
for (const w of app.windows()) console.log(' ', w.url())

// Screenshot the pet window (transparent, always-on-top, small)
for (const [i, win] of app.windows().entries()) {
  const url = win.url()
  const name = url.includes('pet') ? 'pet' : url.includes('overlay') ? 'overlay' : `win-${i}`
  const f = join(SHOT_DIR, `${name}.png`)
  await win.screenshot({ path: f }).catch(e => console.warn('ss failed:', e.message))
  console.log(`screenshot: ${f}`)
}

await app.close()
console.log('Done.')

/**
 * 切会话状态保留验证（审批弹窗 / 输入草稿 / 消息 / 事件归属）。
 *
 * 回归的 bug：会话 A 弹出审批 → 切到 B 再切回 A → 弹窗消失、界面像已结束、
 * 输入内容不见。成因是 App 用 key={currentId} 让「切会话」等于卸载重建 ChatView，
 * 而会话级状态（messages / input / isLoading / permQueue）全在组件内部；
 * 后端审批又是一次性推送，错过就再也拿不回来。
 *
 * 注意：会话视图现在常驻挂载（切换只切显隐），所以选择器必须用
 * `.chat-view:visible` 限定到当前显示的那一份，否则会把隐藏视图的内容也数进来。
 *
 * 用法：先 `npx vite`（端口 5173），再 `node scripts/verify-session-switch.mjs`
 */
import { chromium } from 'playwright-core'

const URL = 'http://localhost:5173/preview.html?switch'
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const DRAFT = '草稿不该被切会话吃掉'
const MSG = '.chat-view:visible .msg'
const INPUT = '.chat-view:visible .input-box'
const BANNER = '.chat-view:visible .perm-banner'

const results = []
function check(name, ok, extra = '') {
  results.push(ok)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `   [${extra}]` : ''}`)
}

const browser = await chromium.launch({ executablePath: CHROME, headless: true })
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
page.on('pageerror', e => console.log('PAGE ERROR:', e.message))

await page.goto(URL, { waitUntil: 'networkidle' })
await page.waitForSelector('.session-item', { timeout: 15000 })
await page.waitForTimeout(800)

const sessions = page.locator('.session-item')
const n = await sessions.count()
check('侧栏出现两个会话', n === 2, `count=${n}`)

const initialMsgs = await page.locator(MSG).count()
check('初始会话有消息', initialMsgs > 0, `msg=${initialMsgs}`)

// ── 输入草稿 ─────────────────────────────────────────────────────
await page.fill(INPUT, DRAFT)
check('草稿已输入', (await page.inputValue(INPUT)) === DRAFT)

// ── 模拟本轮正在流式生成的回复（后端尚未落库）────────────────────
// 切回后它必须还在：重拉历史拿不到——run 没结束就不会 append_turn，
// 所以「靠重拉历史恢复界面」这条路在等审批期间本来就是不通的。
await page.evaluate(() => {
  window.__permFire('agent.chunk', { delta: '正在生成的回复…', session_id: 'demo-1' })
})
await page.waitForTimeout(250)
const liveMsgs = await page.locator(MSG).count()
check('流式回复已进入当前会话', liveMsgs === initialMsgs + 1, `msg=${liveMsgs}`)

// ── 触发本会话（demo-1）的审批 ───────────────────────────────────
await page.evaluate(() => {
  window.__permFire('tool.permission_request', {
    request_id: 'r1', tool: 'bash', args: { command: 'rm -rf build' },
    reason: '命令包含删除操作', session_id: 'demo-1',
  })
})
await page.waitForSelector(BANNER, { timeout: 3000 })
check('审批弹窗出现', await page.locator(BANNER).isVisible())

// ── 别的会话（demo-2）的审批不得串到当前界面 ──────────────────────
await page.evaluate(() => {
  window.__permFire('tool.permission_request', {
    request_id: 'r2', tool: 'write_file', args: { path: 'x' },
    reason: '别的会话的审批', session_id: 'demo-2',
  })
})
await page.waitForTimeout(250)
let banner = (await page.locator(BANNER).first().innerText()).replace(/\n/g, ' | ')
check('别的会话的审批不串入当前界面', !banner.includes('write_file'), banner.slice(0, 70))

// ── 切到会话 B（此前从未挂载，弹窗只能靠补拉恢复）────────────────
await sessions.nth(1).click()
await page.waitForSelector(BANNER, { timeout: 4000 })
const bCount = await page.locator(BANNER).count()
const bText = (await page.locator(BANNER).first().innerText()).replace(/\n/g, ' | ')
check('会话 B 看到自己的审批（补拉）', bCount === 1 && bText.includes('write_file'), `count=${bCount}`)
const bMsgs = await page.locator(MSG).count()
check('会话 B 显示自己的消息', bMsgs === 1, `msg=${bMsgs}`)
check('会话 B 输入框为空（草稿归属 A）', (await page.inputValue(INPUT)) === '', await page.inputValue(INPUT))

// ── 切回会话 A ──────────────────────────────────────────────────
await sessions.nth(0).click()
await page.waitForTimeout(600)
check('切回后审批弹窗仍在', await page.locator(BANNER).isVisible())
banner = (await page.locator(BANNER).first().innerText()).replace(/\n/g, ' | ')
check('切回后弹窗仍是本会话的', banner.includes('bash'), banner.slice(0, 50))
check('切回后草稿还在', (await page.inputValue(INPUT)) === DRAFT, await page.inputValue(INPUT))
const backMsgs = await page.locator(MSG).count()
check('切回后本轮消息还在（未落库也不丢）', backMsgs === liveMsgs, `${liveMsgs} -> ${backMsgs}`)

// ── 超时撤回只撤自己那条 ─────────────────────────────────────────
await page.evaluate(() => {
  window.__permFire('tool.permission_expired', { request_id: 'r1', tool: 'bash', session_id: 'demo-1' })
})
await page.waitForTimeout(250)
check('本会话审批撤回后弹窗消失', (await page.locator(BANNER).count()) === 0)

await browser.close()
const passed = results.filter(Boolean).length
console.log(`\n${passed}/${results.length} passed`)
process.exit(passed === results.length ? 0 : 1)

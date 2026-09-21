// 会话归档交互验证（playwright + 本机 Chrome 无头）
// 前置：先 `npx vite` 起 5173，再 `node scripts/verify-session-archive.mjs`
//
// 覆盖：
//   1. 侧栏顶部有「显示已归档」开关，默认关闭
//   2. 默认列表不含已归档会话（后端过滤，不是前端藏）
//   3. 悬浮会话行 → 行尾出现归档按钮（与删除按钮并排，且在删除左边）
//   4. 点归档 → 会话从默认列表消失，但**不是删除**（开关打开后还在）
//   5. 打开开关 → 会话以灰显形式出现（opacity < 1）
//   6. 已归档行的按钮 title 变成「取消归档」
//   7. 点取消归档 → 灰显消失，回到普通列表
import { chromium } from 'playwright-core'
import { existsSync } from 'node:fs'

const candidates = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  process.env.LOCALAPPDATA + '/Google/Chrome/Application/chrome.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
]
const executablePath = candidates.find(p => p && existsSync(p))
if (!executablePath) { console.error('FATAL: no chrome/edge found'); process.exit(1) }

const URL = 'http://localhost:5173/preview.html'
let failed = 0
function check(name, ok, detail = '') {
  const tag = ok ? 'PASS' : 'FAIL'
  console.log(`[${tag}] ${name}${detail ? `  (${detail})` : ''}`)
  if (!ok) failed += 1
}

const browser = await chromium.launch({ executablePath, headless: true })
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
await page.goto(URL)
await page.waitForSelector('.session-list', { timeout: 8000 })

// 侧栏两级默认收起：「对话」因当前会话在其中会自动展开，「工作区」需要手动展开，
// 展开后才有足够多的行来做"归档前 / 归档后"的数量对比。
await page.locator('.session-section-label.collapsible', { hasText: '工作区' }).click()
await page.waitForSelector('.workspace-group-row', { timeout: 8000 })
await page.locator('.workspace-group-header').first().click()
await page.waitForTimeout(250)

const archivedToggle = page.locator('.sidebar-header-btns [aria-label*="已归档"]')
const items = page.locator('.session-item')
const byTitle = t => page.locator('.session-item', { hasText: t })

// ── 1. 开关存在且默认关闭 ───────────────────────────────────────
check('归档开关存在', await archivedToggle.count() === 1)
check('默认不显示已归档', await archivedToggle.getAttribute('aria-pressed') === 'false')

const baseCount = await items.count()
check('展开后列出全部会话', baseCount === 3, `count=${baseCount}`)

// ── 2. 悬浮会话行 → 归档按钮出现，且排在删除左边 ────────────────
const target = byTitle('调研桌宠动画方案')
await target.hover()
// 按钮是 opacity 过渡淡入的，hover 之后要等一下再读计算样式
await page.waitForTimeout(250)

const archBtn = target.locator('.session-archive')
const delBtn = target.locator('.session-delete')
check('悬浮出现归档按钮', await archBtn.count() === 1)
{
  const op = await archBtn.evaluate(el => getComputedStyle(el).opacity)
  check('归档按钮可见（opacity=1）', op === '1', `opacity=${op}`)
  const ab = await archBtn.boundingBox()
  const db = await delBtn.boundingBox()
  check('归档按钮在删除按钮左侧', !!ab && !!db && ab.x < db.x,
    ab && db ? `arch=${Math.round(ab.x)} del=${Math.round(db.x)}` : '缺 boundingBox')
}
check('未归档时 title 是「归档」', await archBtn.getAttribute('title') === '归档')

// ── 3. 点归档 → 从默认列表消失 ──────────────────────────────────
await archBtn.click()
await page.waitForFunction(
  n => document.querySelectorAll('.session-item').length === n,
  baseCount - 1, { timeout: 4000 },
)
check('归档后从默认列表消失', await byTitle('调研桌宠动画方案').count() === 0)

// ── 4. 打开开关 → 灰显出现（说明只是收起，不是删除）────────────
await archivedToggle.click()
await page.waitForFunction(
  n => document.querySelectorAll('.session-item').length === n,
  baseCount, { timeout: 4000 },
)
const revived = byTitle('调研桌宠动画方案')
check('打开开关后重新出现', await revived.count() === 1)
check('带 archived 样式类', await revived.evaluate(el => el.classList.contains('archived')))

{
  // 灰显：未 hover 时整体透明度应低于 1（hover 会恢复到 1）
  await page.mouse.move(5, 5)
  await page.waitForTimeout(250)
  const op = parseFloat(await revived.evaluate(el => getComputedStyle(el).opacity))
  check('已归档行灰显', op > 0 && op < 1, `opacity=${op}`)
}
check('开关状态已置为按下', await archivedToggle.getAttribute('aria-pressed') === 'true')

// ── 5. 已归档行的按钮 title 变成「取消归档」─────────────────────
await revived.hover()
check('已归档时 title 是「取消归档」',
  await revived.locator('.session-archive').getAttribute('title') === '取消归档')

// ── 6. 点取消归档 → 灰显消失 ────────────────────────────────────
await revived.locator('.session-archive').click()
await page.waitForFunction(
  () => !document.querySelector('.session-item.archived'),
  null, { timeout: 4000 },
)
check('取消归档后不再是 archived 样式',
  await byTitle('调研桌宠动画方案').evaluate(el => !el.classList.contains('archived')))

// ── 7. 另一条会话同样走通；归档与删除两个按钮并存 ───────────────
{
  // 先把开关关回默认态：开关打开时归档不会让行消失（那正是"收起"的反证）
  await archivedToggle.click()
  await page.waitForTimeout(250)

  const before = await items.count()
  const other = byTitle('修复 WebSocket 重连逻辑')
  await other.hover()
  await page.waitForTimeout(250)
  check('归档与删除按钮并存', await other.locator('.session-delete').count() === 1)

  await other.locator('.session-archive').click()
  await page.waitForFunction(
    n => document.querySelectorAll('.session-item').length === n,
    before - 1, { timeout: 4000 },
  )
  check('对另一条会话归档同样即时生效', true)

  // 归档不删数据：把开关打开，它应该原样回来（而不是像删除那样彻底没了）
  await archivedToggle.click()
  await page.waitForTimeout(250)
  check('归档的另一条仍在（不是被删除）',
    await byTitle('修复 WebSocket 重连逻辑').evaluate(el => el.classList.contains('archived')))
}

await browser.close()

console.log()
if (failed) {
  console.error(`FAILED: ${failed} 项未通过`)
  process.exit(1)
}
console.log('全部通过')

// 侧栏删除交互验证（playwright + 本机 Chrome 无头）
// 前置：先 `npx vite` 起 5173，再 `node scripts/verify-sidebar-delete.mjs`
//
// 覆盖：
//   1. 「对话 / 工作区」分区标签的箭头紧跟文字（而非贴最右）
//   2. 工作区行的箭头紧跟名称文字
//   3. 悬浮工作区行 → 行尾出现删除按钮；点击弹确认弹窗
//   4. 取消 → 什么都不发生
//   5. 确认删除工作区 → 工作区消失，其下会话移回「对话」分组（数量对上）
//   6. 悬浮会话 → 删除按钮出现；确认后该会话消失
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

// 工作区分组默认收起（当前会话是自由会话，不会自动展开「工作区」），先点开
await page.locator('.session-section-label.collapsible', { hasText: '工作区' }).click()
await page.waitForSelector('.workspace-group-row', { timeout: 8000 })

// ── 1. 分区标签箭头紧跟文字 ─────────────────────────────────────
{
  const label = page.locator('.session-section-label.collapsible').first()
  const lb = await label.boundingBox()
  const ab = await label.locator('.section-arrow').boundingBox()
  check('分区标签箭头存在', !!ab)
  if (lb && ab) {
    const offset = ab.x - lb.x
    // 「对话 (n)」文字很窄；若箭头贴最右 offset 会接近侧栏宽度(~230+)
    check('分区标签箭头紧跟文字', offset > 10 && offset < 160, `offset=${Math.round(offset)}px`)
  }
}

// ── 2. 工作区行箭头紧跟名称 ─────────────────────────────────────
{
  const row = page.locator('.workspace-group-row').first()
  const nb = await row.locator('.workspace-group-name').boundingBox()
  const ab = await row.locator('.workspace-group-arrow').boundingBox()
  check('工作区行箭头存在', !!ab)
  if (nb && ab) {
    const gap = ab.x - (nb.x + nb.width)
    check('工作区箭头紧跟名称', gap >= -4 && gap < 40, `gap=${Math.round(gap)}px`)
  }
}

// ── 3. 悬浮工作区行出现删除按钮，点击弹确认 ─────────────────────
{
  const row = page.locator('.workspace-group-row').first()
  const del = row.locator('.ws-delete')
  const before = await del.evaluate(el => getComputedStyle(el).opacity)
  check('未悬浮时删除按钮隐藏', before === '0', `opacity=${before}`)
  await row.hover()
  await page.waitForTimeout(150)
  const after = await del.evaluate(el => getComputedStyle(el).opacity)
  check('悬浮后删除按钮可见', Number(after) > 0.9, `opacity=${after}`)

  await del.click()
  await page.waitForSelector('.confirm-dialog', { timeout: 3000 })
  const text = await page.locator('.confirm-dialog').innerText()
  check('确认弹窗出现且含工作区名', text.includes('kittymind') && text.includes('删除工作区'))
  check('弹窗说明会话会保留', text.includes('保留'))

  // ── 4. 取消 ──
  await page.click('.btn-confirm-cancel')
  await page.waitForTimeout(120)
  check('取消后弹窗关闭', (await page.locator('.confirm-dialog').count()) === 0)
  check('取消后工作区还在', (await page.locator('.workspace-group-row').count()) === 1)

  // ── 5. 确认删除工作区 → 会话移回「对话」 ──
  await row.hover()
  await row.locator('.ws-delete').click()
  await page.waitForSelector('.confirm-dialog', { timeout: 3000 })
  await page.click('.btn-confirm-danger')
  await page.waitForTimeout(250)
  check('确认后工作区消失', (await page.locator('.workspace-group-row').count()) === 0)
  // demo-1/2/3 全部回到「对话」分组 → 标签显示 对话 (3)
  const dialogLabel = await page.locator('.session-section-label').first().innerText()
  check('其下会话移回对话分组', dialogLabel.includes('对话') && dialogLabel.includes('3'), dialogLabel.trim())
  check('原工作区会话仍可见', (await page.locator('.session-item', { hasText: '修复 WebSocket 重连逻辑' }).count()) === 1)
}

// ── 6. 删除单个会话 ────────────────────────────────────────────
{
  const item = page.locator('.session-item', { hasText: '修复 WebSocket 重连逻辑' }).first()
  await item.hover()
  const del = item.locator('.session-delete')
  await del.click()
  await page.waitForSelector('.confirm-dialog', { timeout: 3000 })
  const text = await page.locator('.confirm-dialog').innerText()
  check('会话确认弹窗含标题', text.includes('修复 WebSocket 重连逻辑'))
  await page.click('.btn-confirm-danger')
  await page.waitForTimeout(200)
  check('确认后该会话消失',
    (await page.locator('.session-item', { hasText: '修复 WebSocket 重连逻辑' }).count()) === 0)
  check('其他会话不受影响',
    (await page.locator('.session-item', { hasText: '演示：写诗与代码高亮' }).count()) === 1)
}

// ── 7. Escape 关闭弹窗 ─────────────────────────────────────────
{
  const item = page.locator('.session-item').first()
  await item.hover()
  await item.locator('.session-delete').click()
  await page.waitForSelector('.confirm-dialog', { timeout: 3000 })
  await page.keyboard.press('Escape')
  await page.waitForTimeout(120)
  check('Escape 关闭弹窗', (await page.locator('.confirm-dialog').count()) === 0)
}

await browser.close()
console.log(failed === 0 ? '\nALL PASS' : `\n${failed} FAILED`)
process.exit(failed === 0 ? 0 : 1)

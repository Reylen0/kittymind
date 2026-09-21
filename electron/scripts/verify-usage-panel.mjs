/**
 * 用量成本面板验证（Phase 16）。
 *
 * 验证点：顶栏「用量」按钮可打开面板；默认按模型分组展示 token 与成本；
 * 切换到「按日」分组后重新拉取并渲染；成本格式化（$ / — / <$0.01）不炸。
 *
 * 用法：先 `npx vite`（端口 5173），再 `node scripts/verify-usage-panel.mjs`
 */
import { chromium } from 'playwright-core'

const URL = 'http://localhost:5173/preview.html'
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'

const results = []
function check(name, ok, extra = '') {
  results.push(ok)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? `   [${extra}]` : ''}`)
}

const browser = await chromium.launch({ executablePath: CHROME, headless: true })
const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
page.on('pageerror', e => console.log('PAGE ERROR:', e.message))

await page.goto(URL, { waitUntil: 'networkidle' })
await page.waitForSelector('.topbar-usage-btn', { timeout: 15000 })

// 打开面板
await page.click('.topbar-usage-btn')
await page.waitForSelector('.usage-panel', { timeout: 5000 })
check('顶栏用量按钮打开面板', true)

// 打开时原生窗口控制按钮区域（titleBarOverlay）应被调成遮罩混合色——
// WCO 是系统层，遮罩画不到它上面，不改色会留一块亮色浮在弹窗之上
const callsOpen = await page.evaluate(() => window.__overlayCalls.map(c => c.color))
check('打开时 overlay 调成遮罩混合色', callsOpen.at(-1) === '#a8a9aa', callsOpen.join(' | '))

// 默认按模型分组，渲染出两行（mock 数据）
await page.waitForSelector('.usage-row', { timeout: 5000 })
const rows0 = await page.locator('.usage-row').count()
check('默认按模型分组渲染行', rows0 === 2, `rows=${rows0}`)

// 成本列非空（有 $ 值）
const costText = await page.locator('.usage-row .usage-cost').first().innerText()
check('模型分组显示成本', /^\$/.test(costText), costText)

// 切到「按日」分组
await page.click('.usage-tab:has-text("按日")')
await page.waitForTimeout(400)
const rows1 = await page.locator('.usage-row').count()
check('按日分组重渲染', rows1 === 2, `rows=${rows1}`)
const dayKey = await page.locator('.usage-row .usage-row-key').first().innerText()
check('按日分组的 key 是日期', /^2026-09-/.test(dayKey), dayKey)

// 按日分组的成本列应为 —（非 model 分组不估成本）
const dayCost = await page.locator('.usage-row .usage-cost').first().innerText()
check('按日分组成本显示为占位', dayCost === '—', dayCost)

// 关闭面板
await page.click('.usage-close')
await page.waitForTimeout(200)
const stillThere = await page.locator('.usage-panel').count()
check('关闭面板后消失', stillThere === 0)

// 关闭后 overlay 应回落到主题常态色
const callsClose = await page.evaluate(() => window.__overlayCalls.map(c => c.color))
check('关闭后 overlay 还原主题色', callsClose.at(-1) === '#f7f8fa', callsClose.join(' | '))

await browser.close()

const failed = results.filter(r => !r).length
console.log(`\n${results.length - failed}/${results.length} 通过`)
process.exit(failed ? 1 : 0)

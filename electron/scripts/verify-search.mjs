/**
 * 侧栏全文搜索 UI 验证（Phase 15）。
 *
 * 前端没有单测框架，这类交互只能靠 preview 沙箱 + 真浏览器跑。重点盯四件
 * 容易悄悄回归的事：
 *   1. 高亮偏移——marks 是后端按「片段」算的字符区间，截断/省略号/多行压缩
 *      任何一步算错，高亮就会整体偏一位，而页面看上去仍然「有高亮」；
 *   2. 防抖与请求竞态——连续输入时旧响应若晚于新响应到达，结果会闪回旧值；
 *   3. 片段必须能多行——复用 .session-title 的话会被 nowrap+ellipsis 截成一行；
 *   4. 深色模式下 <mark> 必须仍有可见背景（浅色配色在深色底上会糊掉）。
 *
 * 注意 favicon 的 404 是 preview.html 的既有状态（它没声明 favicon），
 * 与本功能无关，故在断言里排除。
 *
 * 用法：先 `npx vite`（端口 5173），再 `node scripts/verify-search.mjs`
 */
import { chromium } from 'playwright-core'

const URL = 'http://localhost:5173/preview.html?search'
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const results = []
function check(name, ok, extra = '') {
  results.push(ok)
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra ? '  — ' + extra : ''}`)
}

const browser = await chromium.launch({ executablePath: CHROME, headless: true })
const page = await browser.newPage({ viewport: { width: 1200, height: 800 } })
const errors = []
page.on('pageerror', e => errors.push('pageerror: ' + String(e)))
// 「Failed to load resource」由下面的 response 钩子负责——它能拿到 URL，
// 而 console 那条消息不带 URL，没法区分是 favicon 还是真的资源挂了。
page.on('console', m => {
  if (m.type() === 'error' && !/Failed to load resource/i.test(m.text())) {
    errors.push('console: ' + m.text())
  }
})
page.on('response', r => {
  if (r.status() >= 400 && !/favicon/i.test(r.url())) {
    errors.push(`HTTP ${r.status()} ${r.url()}`)
  }
})
page.on('requestfailed', r => {
  if (!/favicon/i.test(r.url())) {
    errors.push(`reqfail ${r.url()} ${r.failure()?.errorText}`)
  }
})

await page.goto(URL, { waitUntil: 'networkidle' })
await page.waitForSelector('.session-item', { timeout: 15000 })

const wsBefore = await page.locator('.session-section-label', { hasText: '工作区' }).count()
check('未搜索时「工作区」区块存在', wsBefore === 1)

await page.click('.sidebar-header-btns .sidebar-icon-btn:first-child')
await page.waitForSelector('.sidebar-search-input', { timeout: 5000 })
check('搜索框可打开', true)

const ph = await page.getAttribute('.sidebar-search-input', 'placeholder')
check('placeholder 含「内容」', /内容/.test(ph), ph)

await page.fill('.sidebar-search-input', '压缩')

await page.waitForTimeout(120)
const busy = await page.locator('.session-empty', { hasText: '搜索中' }).count()
check('防抖期间显示「搜索中…」', busy > 0, `count=${busy}`)

await page.waitForSelector('.search-hit', { timeout: 8000 })
check('渲染出命中片段', await page.locator('.search-hit').count() === 3)
check('结果按会话分组', await page.locator('.search-group').count() === 2)

const label = await page.locator('.session-section-label', { hasText: '内容匹配' }).innerText()
check('分段标题带命中总数', label.includes('(3)'), JSON.stringify(label))

// 查询「压缩」不命中任何工作区会话的标题——工作区区块整体应隐藏，而不是
// 展开了却一个分组都不渲染（那样看起来像「搜索把工作区弄丢了」）。
const wsDuring = await page.locator('.session-section-label', { hasText: '工作区' }).count()
check('搜索且标题无命中时「工作区」区块整体隐藏', wsDuring === 0, `count=${wsDuring}`)

const marks = await page.locator('.search-hit mark').allInnerTexts()
check('高亮精确落在查询词上',
      marks.length === 5 && marks.every(m => m === '压缩'),
      JSON.stringify(marks))

const firstHit = await page.locator('.search-hit').first().innerText()
check('片段文本完整（高亮未吞字）',
      firstHit.replace(/\s/g, '') === '我们来聊聊上下文压缩的实现思路',
      JSON.stringify(firstHit))

const ws = await page.locator('.search-hit').first().evaluate(el => getComputedStyle(el).whiteSpace)
check('片段允许多行（非 nowrap）', ws !== 'nowrap', `white-space=${ws}`)

// 点击结果切换到对应会话，搜索态本身**保持打开**（单击不应打断连续浏览结果）
await page.locator('.search-hit').last().click()
await page.waitForTimeout(400)
const activeAll  = await page.locator('.search-hit.active').count()
const activeLast = await page.locator('.search-group').last().locator('.search-hit.active').count()
check('点击结果切换到对应会话', activeAll === 1 && activeLast === 1,
      `active=${activeAll} inLastGroup=${activeLast}`)
check('单击后仍停留在搜索态', await page.locator('.sidebar-search-input').count() === 1)

await page.fill('.sidebar-search-input', '')
await page.waitForTimeout(400)
check('清空输入后结果清除', await page.locator('.search-hit').count() === 0)

await page.fill('.sidebar-search-input', '压缩')
await page.waitForSelector('.search-hit', { timeout: 8000 })
await page.press('.sidebar-search-input', 'Escape')
await page.waitForTimeout(300)
check('Esc 关闭搜索并清空结果', await page.locator('.search-hit').count() === 0)

// 快速连续输入：只有最后一次的结果该落地（防抖 + 请求序号）
await page.click('.sidebar-header-btns .sidebar-icon-btn:first-child')
for (const s of ['压', '压缩', '压缩管', '压缩']) {
  await page.fill('.sidebar-search-input', s)
  await page.waitForTimeout(60)
}
await page.waitForSelector('.search-hit', { timeout: 8000 })
await page.waitForTimeout(600)
check('连打不产生重复结果', await page.locator('.search-hit').count() === 3,
      `hits=${await page.locator('.search-hit').count()}`)

await page.screenshot({ path: 'C:/tmp/search-light.png' })

// 双击命中项：应退出搜索态（关闭输入框、清空结果），并在正常会话列表里
// 选中该会话——不是「切了会话但仍卡在搜索结果视图里」。
await page.dblclick('.search-hit >> nth=0')
await page.waitForTimeout(500)
check('双击后搜索框关闭', await page.locator('.sidebar-search-input').count() === 0)
check('双击后搜索结果清空', await page.locator('.search-hit').count() === 0)
const activeTitle = await page.locator('.session-item.active .session-title')
  .innerText().catch(() => '')
check('双击后会话列表中对应会话被选中', activeTitle.length > 0, JSON.stringify(activeTitle))
check('退出搜索后「工作区」区块恢复',
      await page.locator('.session-section-label', { hasText: '工作区' }).count() === 1)

check('无 JS 错误 / 资源加载失败', errors.length === 0, errors.slice(0, 3).join(' | '))

// 深色模式
await page.goto(URL + '&theme=dark', { waitUntil: 'networkidle' })
await page.waitForSelector('.session-item', { timeout: 15000 })
await page.click('.sidebar-header-btns .sidebar-icon-btn:first-child')
await page.fill('.sidebar-search-input', '压缩')
await page.waitForSelector('.search-hit mark', { timeout: 8000 })
const bg = await page.locator('.search-hit mark').first()
  .evaluate(el => getComputedStyle(el).backgroundColor)
check('深色模式高亮有背景色', bg !== 'rgba(0, 0, 0, 0)', bg)
await page.screenshot({ path: 'C:/tmp/search-dark.png' })

await browser.close()
const failed = results.filter(x => !x).length
console.log(`\n${results.length - failed}/${results.length} passed`)
process.exit(failed ? 1 : 0)

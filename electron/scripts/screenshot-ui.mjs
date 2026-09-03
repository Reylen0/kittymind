import { _electron as electron } from 'playwright-core';
import path from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APP_DIR   = path.resolve(__dirname, '..');
const SHOT_DIR  = 'C:\\tmp\\shots';
fs.mkdirSync(SHOT_DIR, { recursive: true });

const electronBin = path.join(
  APP_DIR,
  'node_modules\\.pnpm\\electron@44.1.0\\node_modules\\electron\\dist\\electron.exe'
);

const app = await electron.launch({ executablePath: electronBin, args: [APP_DIR], timeout: 45000 });

let page = null;
for (let i = 0; i < 60; i++) {
  await new Promise(r => setTimeout(r, 500));
  const wins = app.windows();
  page = wins.find(w => w.url().includes('index.html') && !w.url().includes('pet') && !w.url().includes('overlay'))
      ?? wins.find(w => !w.url().includes('devtools') && w.url() !== 'about:blank');
  if (page) break;
}

await page.waitForLoadState('domcontentloaded');
await new Promise(r => setTimeout(r, 1200));

// 1. sidebar open with session history
await page.screenshot({ path: path.join(SHOT_DIR, '01-sidebar-open.png') });
console.log('01-sidebar-open.png');

// 2. search active
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('.sidebar-icon-btn')];
  // first icon btn is search
  btns[0]?.click();
});
await new Promise(r => setTimeout(r, 400));
await page.screenshot({ path: path.join(SHOT_DIR, '02-search-active.png') });
console.log('02-search-active.png');

// 3. collapse sidebar
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('.sidebar-icon-btn')];
  // click the panel icon (second one)
  btns[1]?.click();
});
await new Promise(r => setTimeout(r, 400));
await page.screenshot({ path: path.join(SHOT_DIR, '03-sidebar-collapsed.png') });
console.log('03-sidebar-collapsed.png');

await app.close();
process.exit(0);

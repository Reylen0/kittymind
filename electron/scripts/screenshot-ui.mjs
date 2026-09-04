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
await new Promise(r => setTimeout(r, 1500));

// Full screenshot (initial state)
await page.screenshot({ path: path.join(SHOT_DIR, 'full.png') });

// Click 新对话 to enter landing view
await page.evaluate(() => {
  const btn = document.querySelector('.btn-new-text')
  btn?.click()
})
await new Promise(r => setTimeout(r, 400))
await page.screenshot({ path: path.join(SHOT_DIR, 'landing.png') })

console.log('done');
await app.close();
process.exit(0);

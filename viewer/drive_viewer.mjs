import pkg from '/home/clow/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.js';
const { chromium } = pkg;

const URL = 'http://localhost:8080/index.html';
const SHOT = '/tmp/streetforge_davis.png';

const logs = [];
const browser = await chromium.launch({
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
         '--ignore-gpu-blocklist', '--enable-webgl'],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
page.on('console', m => logs.push(`[console.${m.type()}] ${m.text()}`));
page.on('pageerror', e => logs.push(`[pageerror] ${e.message}`));

let loaded = false;
try {
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForFunction(
    () => document.body.innerText.includes('objects loaded'), { timeout: 90000 });
  loaded = true;
} catch (e) { logs.push(`[wait] no 'objects loaded' badge: ${e.message}`); }

await page.waitForTimeout(3000);
const badge = await page.evaluate(() => {
  const m = document.body.innerText.match(/objects loaded:[^\n]*/); return m ? m[0] : null;
});
await page.screenshot({ path: SHOT, fullPage: false });
console.log('=== LOADED:', loaded, '| BADGE:', badge, '===');
console.log(logs.filter(l => /leaf objects|lidar overlay|load failed|pageerror|error/i.test(l)).join('\n'));
console.log('=== SHOT:', SHOT);
await browser.close();

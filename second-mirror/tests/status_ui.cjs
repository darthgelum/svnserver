// Isolated live-UI/API fixture + interactive preview checks. Requires Playwright.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const vm = require('node:vm');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const web = path.join(__dirname, '../web');
  const context = { window: {}, Date };
  vm.runInNewContext(fs.readFileSync(path.join(web, 'demo.js'), 'utf8'), context);
  const data = context.window.MIRROR_DEMO;
  let failure = false;
  const server = http.createServer((req, res) => {
    if (req.url === '/svn-mirror-status/api') {
      res.writeHead(failure ? 503 : 200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(data)); return;
    }
    const name = req.url.split('/').pop() || 'index.html';
    if (!['index.html', 'app.js', 'style.css'].includes(name)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', name.endsWith('.css') ? 'text/css' : name.endsWith('.js') ? 'application/javascript' : 'text/html');
    res.end(fs.readFileSync(path.join(web, name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, ...(process.env.STATUS_TEST_BROWSER ? { executablePath: process.env.STATUS_TEST_BROWSER } : {}) });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 }, colorScheme: 'light' });
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/svn-mirror-status/`);
    await page.locator('#sm-repositories tr').first().waitFor();
    assert.equal(await page.locator('#sm-repositories tr').count(), 11);
    await page.locator('[data-filter="attention"]').click();
    assert.equal(await page.locator('#sm-repositories tr').count(), 2);
    await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.locator('.sm-copy').first().click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()),
                 `http://127.0.0.1:${server.address().port}/svn/Example`);
    await page.getByRole('button', { name: 'Animation', exact: true }).click();
    assert.match(await page.locator('#sm-detail').textContent(), /Не удалось установить/);
    assert.match(await page.locator('#sm-detail').textContent(), /svn\/Animation/);
    await page.locator('[data-view="users"]').click();
    assert.equal(await page.locator('#sm-users-table tr').count(), 4);
    await page.locator('[data-view="logs"]').click();
    await page.locator('#sm-log-level').selectOption('error');
    assert.match(await page.locator('#sm-log-output').textContent(), /Animation/);
    assert.doesNotMatch(await page.locator('#sm-log-output').textContent(), /lag=0/);
    failure = true;
    await page.locator('#sm-refresh').click();
    await page.locator('#sm-notice').waitFor({ state: 'visible' });
    assert.match(await page.locator('#sm-notice').textContent(), /503/);
    failure = false;
    await page.locator('#sm-refresh').click();
    await page.locator('#sm-notice').waitFor({ state: 'hidden' });
    // Server text must stay inert; no innerHTML interpolation.
    data.repositories[1].error = '<img src=x onerror="window.pwned=1">';
    await page.locator('#sm-refresh').click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.equal(await page.locator('#sm-detail img').count(), 0);

    const preview = process.argv[2];
    const output = process.argv[3];
    if (preview) {
      await page.setContent('<!doctype html><html lang="ru"><head><meta charset="utf-8"></head><body style="margin:0">' + fs.readFileSync(preview, 'utf8') + '</body></html>');
      await page.setViewportSize({ width: 1024, height: 1100 });
      await page.locator('#sm-repositories tr').first().waitFor();
      await page.locator('[data-view="connection"]').click();
      await page.locator('#sm-offline').click();
      assert.match(await page.locator('#sm-link-badge').textContent(), /Нет свежего/);
      await page.locator('#sm-offline').click();
      await page.locator('#sm-advance').click();
      await page.locator('[data-view="overview"]').click();
      assert.match(await page.locator('#sm-transfer').textContent(), /r2778/);
      const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(desktopOverflow, false);
      if (output) await page.screenshot({ path: path.join(output, 'mirror-status-desktop.png'), fullPage: true });
      await page.setViewportSize({ width: 390, height: 900 });
      if (output) await page.screenshot({ path: path.join(output, 'mirror-status-mobile.png'), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
        JSON.stringify(await page.evaluate(() => [...document.querySelectorAll('.sm-main,.sm-summary,.sm-heading,.sm-transfer,.sm-panel,.sm-top,.sm-actions,.sm-footer')].map(e => ({c:e.className,right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})))));
    }
    assert.deepEqual(errors, []);
    console.log('PASS: repositories, filters, details, users, logs, API failure/recovery, XSS text escaping, offline demo, transfer progress, desktop/mobile layout');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });

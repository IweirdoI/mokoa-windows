// Run against Vite and the isolated real API from serve_api.py.
// PLAYWRIGHT_MODULE can point at an existing Playwright installation.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const fixtures = process.argv[2];
if (!fixtures) throw new Error('Pass the FIXTURE_DIR printed by serve_api.py');
const base = process.env.FRONTEND_TEST_URL || 'http://127.0.0.1:5186';

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(15000);
    const errors = [], uploads = [], ingests = [], checks = [];
    page.on('pageerror', err => errors.push(err.message));
    page.on('request', req => {
      if (req.url().endsWith('/api/ingest/file')) uploads.push(req);
      if (req.url().endsWith('/api/ingest')) ingests.push(req);
    });
    const check = name => { checks.push(name); console.log('PASS', name); };
    await page.goto(base);
    await page.getByRole('button', { name: '实时 API 已连接', exact: true }).waitFor();
    const uid = 'ingestion_ui_' + Date.now();
    await page.getByTestId('user-input').fill('  ' + uid + '  ');
    await page.waitForFunction(() => !document.querySelector('[data-testid=sync-button]').disabled);
    await page.getByTestId('open-ingestion').click();
    await page.getByTestId('ingest-files').setInputFiles([
      path.join(fixtures, 'guide.docx'), path.join(fixtures, 'guide.pdf'), path.join(fixtures, 'blank.png'),
    ]);
    await page.getByTestId('ingest-submit').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-testid=ingest-result][data-state=accepted]').length === 2 && document.querySelectorAll('[data-testid=ingest-result][data-state=rejected]').length === 1);
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    assert.equal(uploads.length, 3);
    assert.equal(ingests.length, 0, 'files must not be resubmitted as JSON');
    assert(uploads.every(req => req.headers()['content-type'].startsWith('multipart/form-data; boundary=')));
    check('PDF/Word accepted; blank image rejected; multipart only');

    const blank = page.getByTestId('upload-item').filter({ hasText: 'blank.png' });
    await blank.getByRole('textbox').fill('桌面截图：终端 pip 安装超时，切换国内镜像解决。');
    await blank.getByRole('button', { name: '修改后加入待上传队列' }).click();
    await page.getByTestId('ingest-submit').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-testid=ingest-result][data-state=accepted]').length === 3);
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    assert.match(await page.getByTestId('ingest-result').first().innerText(), /使用用户说明/);
    check('blank image with caption succeeds and reports fallback honestly');

    await page.getByTestId('ingest-files').setInputFiles(path.join(fixtures, 'guide.docx'));
    await page.getByTestId('ingest-submit').click();
    await page.waitForFunction(() => document.querySelector('[data-testid=ingest-result]').textContent.includes('duplicate'));
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    check('duplicate upload shown as business rejection');

    for (const source of ['tool_result', 'user_behavior', 'manual_config', 'conversation']) {
      await page.getByTestId('ingest-source').selectOption(source);
      await page.getByRole('button', { name: '填入示例', exact: true }).click();
      const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/ingest') && r.request().method() === 'POST');
      await page.getByTestId('ingest-submit').click();
      const response = await responsePromise;
      assert.equal((await response.json()).accepted, true, source);
      assert.equal(response.request().postDataJSON().source, source);
      await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    }
    assert.equal(ingests.length, 4);
    assert(ingests.every(req => req.postDataJSON().user_id === uid));
    check('pasted user ID is normalized consistently');
    check('four structured source forms reach the real ingestion pipeline');

    await page.getByTestId('chat-input').fill('麒麟系统安装依赖失败时如何切换镜像');
    await page.getByTestId('chat-send').click();
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    await page.getByTestId('tab-memories').click();
    assert.match(await page.locator('.memory-content').innerText(), /guide.docx|guide.pdf|清华镜像/);
    check('uploaded knowledge is available to next-turn recall');

    await page.getByTestId('tab-ingestion').click();
    await page.getByTestId('ingest-source').selectOption('document');
    const beforeInvalid = uploads.length;
    await page.getByTestId('ingest-files').setInputFiles([
      { name: 'legacy.doc', mimeType: 'application/msword', buffer: Buffer.from('OLE') },
      { name: 'empty.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(0) },
      { name: 'large.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(8 * 1024 * 1024 + 1) },
    ]);
    assert.equal(await page.locator('[data-testid=upload-item][data-state=invalid]').count(), 3);
    assert.equal(uploads.length, beforeInvalid);
    check('old Word, empty file and oversize blocked before upload');
    await page.screenshot({ path: path.join(fixtures, 'desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflow, false, 'mobile horizontal overflow');
    await page.locator('.ingestion-panel').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(fixtures, 'mobile.png'), fullPage: true });
    check('mobile layout has no horizontal overflow');

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByTestId('user-input').fill('other_user_' + Date.now());
    assert.equal(await page.getByTestId('ingest-result').count(), 0);
    assert.equal(await page.getByTestId('upload-item').count(), 0);
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    check('switching users clears upload queue and result history');

    // Inject only transport/promotion failures to verify partial-outcome UI.
    await page.route('**/api/consolidate/*', route => route.fulfill({ status: 503, body: 'unavailable' }));
    await page.getByTestId('ingest-files').setInputFiles(path.join(fixtures, 'guide.docx'));
    await page.getByTestId('ingest-submit').click();
    await page.waitForFunction(() => document.querySelector('[data-testid=ingest-result]').textContent.includes('长期记忆同步失败'));
    assert.equal(await page.getByTestId('ingest-result').first().getAttribute('data-state'), 'accepted');
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    check('promotion failure preserves successful ingestion');
    await page.route('**/api/ingest/file', route => route.abort('failed'));
    await page.getByTestId('ingest-files').setInputFiles(path.join(fixtures, 'guide.pdf'));
    await page.getByTestId('ingest-submit').click();
    await page.waitForFunction(() => document.querySelector('[data-testid=ingest-result]').dataset.state === 'error');
    await page.waitForFunction(() => !document.querySelector('[data-testid=user-input]').disabled);
    check('network failure releases controls and reports unconfirmed result');

    await page.getByTestId('connection-button').click();
    await page.getByTestId('ingest-files').setInputFiles(path.join(fixtures, 'guide.docx'));
    assert.equal(await page.getByTestId('ingest-submit').isDisabled(), true);
    check('demo mode cannot submit files');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(fixtures, 'browser-report.json'), JSON.stringify({ checks, errors }, null, 2));
    console.log('All browser checks passed.');
  } finally { await browser.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });

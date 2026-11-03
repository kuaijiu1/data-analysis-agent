// Run against the local app. All API requests use disposable in-memory fixtures.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const makeItem = (id, name, status = 'ready') => ({ id, name, status, extension: '.csv', size: 15,
      storage_bytes: 32, progress: '测试数据', tables: [], sheets: [], profile_status: 'ready', dictionary: {} });
    const a = makeItem('a'.repeat(32), '正在查看.csv');
    const b = makeItem('b'.repeat(32), '待删除 <示例>.csv');
    let items = [a, b], failDeletion = false, cancellationCount = 0;
    const deleted = [];
    await page.route('**/api/**', async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const method = request.method();
      let status = 200, result;
      if (path === '/api/auth/status') result = { authenticated: true, email: 'fixture@example.com', legacy_available: false };
      else if (path === '/api/health') result = { model_ready: false, provider: '测试模型' };
      else if (path === '/api/datasets') result = items;
      else if (path.endsWith('/history')) result = { items: [], total: 0, has_more: false };
      else if (path.endsWith('/dashboard')) { status = 404; result = { detail: '尚未生成看板' }; }
      else {
        const id = path.split('/')[3];
        const item = items.find(item => item.id === id);
        if (!item) { status = 404; result = { detail: '文件不存在' }; }
        else if (path.endsWith('/cancel')) {
          cancellationCount++; item.status = 'cancelled'; item.profile_status = 'cancelled'; result = { ok: true };
        } else if (method === 'DELETE') {
          if (failDeletion) { status = 409; result = { detail: '测试占用：请关闭文件后重试' }; }
          else { deleted.push(id); items = items.filter(item => item.id !== id); result = { ok: true, released_bytes: 32 }; }
        } else result = item;
      }
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(result) });
    });
    await page.goto(process.env.DELETE_UI_URL || 'http://127.0.0.1:8000/');
    const row = name => page.locator('.dataset-row').filter({ has: page.locator('strong', { hasText: name }) });
    await page.waitForFunction(() => document.querySelectorAll('.dataset-delete').length === 2);
    await row(b.name).getByRole('button', { name: `删除文件 ${b.name}`, exact: true }).click();
    await page.locator('#deleteDialog').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#deleteFileName').textContent(), b.name);
    await page.locator('#cancelDeleteBtn').click();
    assert.equal(deleted.length, 0);
    assert.equal(items.length, 2);

    await row(b.name).locator('.dataset-delete').click();
    await page.locator('#confirmDeleteBtn').click();
    await page.waitForFunction(() => document.querySelectorAll('.dataset-delete').length === 1);
    assert.deepEqual(deleted, [b.id]);
    assert.equal(await page.locator('#datasetTitle').textContent(), a.name);
    console.log('PASS: cancel leaves files intact; deleting another file preserves the current view.');

    items.push(b); failDeletion = true;
    await page.evaluate(() => refresh());
    await row(b.name).locator('.dataset-delete').click();
    await page.locator('#confirmDeleteBtn').click();
    await page.locator('#deleteDialogError').waitFor({ state: 'visible' });
    assert.match(await page.locator('#deleteDialogError').textContent(), /测试占用/);
    assert(items.some(item => item.id === b.id));
    if (process.env.DELETE_UI_SCREENSHOT) await page.screenshot({ path: process.env.DELETE_UI_SCREENSHOT });
    await page.locator('#cancelDeleteBtn').click();
    failDeletion = false;
    console.log('PASS: backend failure stays visible and leaves the file in the list.');

    await row(a.name).locator('.dataset-delete').click();
    await page.locator('#confirmDeleteBtn').click();
    await page.waitForFunction(name => document.getElementById('datasetTitle').textContent === name, b.name);
    assert(!items.some(item => item.id === a.id));
    console.log('PASS: deleting the current file selects a remaining file.');

    const c = makeItem('c'.repeat(32), '导入中.csv', 'importing');
    items.push(c);
    await page.evaluate(() => refresh());
    await row(c.name).locator('.dataset-delete').click();
    await page.locator('#confirmDeleteBtn').click();
    await page.waitForFunction(() => !document.querySelector('.dataset-list').textContent.includes('导入中.csv'));
    assert.equal(cancellationCount, 1);
    assert(deleted.includes(c.id));
    assert.deepEqual(errors, []);
    console.log('PASS: an importing file is cancelled before deletion; no page errors.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

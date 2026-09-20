const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
(async () => {
  const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'sessions-'));
  let context, launches = 0;
  const open = async () => {
    context = await chromium.launchPersistentContext(folder, { executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
    await context.route('https://sessions.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ body: '<!doctype html><body></body>', contentType: 'text/html' });
      return route.fulfill({ body: await fs.readFile(path.join(root, url.pathname)), contentType: 'text/javascript' });
    });
    const page = await context.newPage();
    await page.clock.setFixedTime(new Date(launches++ ? '2040-01-01T12:00:00Z' : '2026-01-01T12:00:00Z'));
    await page.goto('https://sessions.test/');
    return page;
  };
  const read = page => page.evaluate(async () => {
    const line = await import('/01-northline/northline-auth.js');
    const aster = await import('/02-aster-observatory/aster-store.js');
    const morrow = await import('/03-morrow-coffee/morrow-auth.js');
    const answers = await import('/04-signal-works/answers-store.js');
    const svod = await import('/05-field-notes/svod-store.js');
    const loop = await import('/06-loop-archive/loop-store.js');
    const fokus = await import('/07-fokus-news/fokus-store.js');
    return [line.currentUser(line.readState())?.id, aster.user()?.id, (await morrow.restoreSession())?.id,
      answers.getSession(), (await svod.current())?.id, (await loop.current())?.id, fokus.account(await fokus.read())?.id];
  });
  try {
    let page = await open();
    await page.evaluate(async () => {
      const line = await import('/01-northline/northline-auth.js');
      const state = line.readState(); state.accounts.push({id:'line-reader',displayName:'Reader'}); line.createSession(state, 'line-reader');
      await (await import('/02-aster-observatory/aster-auth.js')).register('Reader', 'reader@example.test', 'test-password');
      const asterStore = await import('/02-aster-observatory/aster-store.js');
      asterStore.commit(state => {
        const active = state.accounts[0];
        state.accounts.unshift(...Array.from({ length: 30 }, (_, i) => ({ ...active, id: `older-${i}`, email: `older-${i}@example.test` })));
      });
      await (await import('/03-morrow-coffee/morrow-auth.js')).authenticate('reader', 'test-password', true);
      await (await import('/04-signal-works/answers-auth.js')).register('Reader', 'reader@example.test', 'test-password');
      for (const [site, prefix] of [['05-field-notes','svod'], ['06-loop-archive','loop'], ['07-fokus-news','fokus']])
        await (await import(`/${site}/${prefix}-auth.js`)).authenticate('reader', 'test-password', 'Reader');
    });
    const ids = await read(page); assert.ok(ids.every(Boolean));
    // Close the entire browser, not just reload the existing tab.
    await context.close(); page = await open();
    assert.deepEqual(await read(page), ids, 'All seven sessions survive browser restart and future date');
    await page.evaluate(async () => {
      const { persistentSession } = await import('/shared/session.js');
      sessionStorage.setItem('legacy-test', 'reader');
      const legacy = persistentSession('legacy-test');
      if (legacy.get() !== 'reader' || localStorage.getItem('legacy-test') !== 'reader') throw Error('Migration failed');
      legacy.set('');
      sessionStorage.setItem('legacy-test', 'reader');
      if (persistentSession('legacy-test').get() !== '') throw Error('Old tab resurrected logout');
      const current = persistentSession('failure-test'); current.set('reader');
      const get = Storage.prototype.getItem;
      Storage.prototype.getItem = () => { throw Error('temporary failure'); };
      try { if (current.get() !== 'reader') throw Error('Session lost on read failure'); }
      finally { Storage.prototype.getItem = get; }
      const line = await import('/01-northline/northline-auth.js'); line.logout(line.readState());
      (await import('/02-aster-observatory/aster-auth.js')).signOut();
      (await import('/03-morrow-coffee/morrow-auth.js')).logout();
      (await import('/04-signal-works/answers-auth.js')).logout();
      for (const [site,prefix] of [['05-field-notes','svod'],['06-loop-archive','loop'],['07-fokus-news','fokus']])
        (await import(`/${site}/${prefix}-store.js`)).signOut();
    });
    await context.close(); page = await open();
    assert.ok((await read(page)).every(id => !id), 'Explicit logout persists after restart');
    console.log('PASS: 7 sites, browser restart, future clock, legacy migration, explicit logout and transient storage failure.');
  } finally { await context?.close(); await fs.rm(folder, { recursive: true, force: true }); }
})().catch(error => { console.error(error); process.exitCode = 1; });

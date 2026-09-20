// Real IndexedDB recovery and account isolation, including delayed storage events.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const engines = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
(async () => {
  const engine = process.env.UI_ENGINE || 'chromium';
  const browser = await engines[engine].launch(engine === 'chromium' ? { executablePath: process.env.UI_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' } : {});
  try {
    const context = await browser.newContext({ locale: 'ru-RU' }), page = await context.newPage(), errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error('Page error:', error.message); });
    await context.route('https://recovery.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><body></body>' });
      if (url.pathname.startsWith('/_')) return route.fulfill({ contentType: 'application/json', body: '[]' });
      const file = path.join(root, url.pathname);
      return route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto('https://recovery.test/');
    const recovery = await page.evaluate(async () => {
      const native = indexedDB.open.bind(indexedDB), results = [];
      for (const [file, method, args, name] of [
        ['03-morrow-coffee/morrow-db.js', 'openDatabase', [], 'morrow:workspace:v1'],
        ['04-signal-works/answers-store.js', 'read', [], 'answers:workspace:v1'],
        ['05-field-notes/svod-store.js', 'database', [], 'svod:wikipedia:v1'],
        ['06-loop-archive/loop-store.js', 'db', [], 'loop:gifs:v1'],
      ]) {
        const module = await import('/' + file + '?recovery');
        indexedDB.open = () => { throw new DOMException('Temporarily unavailable', 'UnknownError'); };
        let rejected = false;
        try { await module[method](...args); } catch { rejected = true; }
        let opened, count = 0;
        indexedDB.open = (...args) => {
          count++;
          const request = native(...args);
          request.addEventListener('success', () => { opened = request.result; });
          return request;
        };
        await Promise.all(Array.from({ length: 8 }, () => module[method](...args)));
        if (!rejected || count !== 1) throw Error(file + ': no recovery or duplicate connections');
        opened.dispatchEvent(new Event('versionchange'));
        await module[method](...args);
        if (count !== 2) throw Error(file + ': closed connection reused');
        opened.dispatchEvent(new Event('close'));
        await module[method](...args);
        if (count !== 3) throw Error(file + ': forced close not recovered');
        opened.dispatchEvent(new Event('versionchange'));
        // A blocked request may succeed later, after the UI already reported failure.
        let blocked;
        indexedDB.open = () => {
          blocked = {};
          queueMicrotask(() => blocked.onblocked());
          return blocked;
        };
        try { await module[method](...args); throw Error('blocked request resolved'); }
        catch (error) { if (error.message === 'blocked request resolved') throw error; }
        const late = await new Promise((resolve, reject) => {
          const request = native(name); request.onsuccess = () => resolve(request.result); request.onerror = reject;
        });
        blocked.result = late; blocked.onsuccess();
        let closed = false;
        try { late.transaction(late.objectStoreNames[0]); } catch { closed = true; }
        if (!closed) throw Error(file + ': leaked blocked connection');
        indexedDB.open = native;
        await module[method](...args);
        results.push(file);
      }
      // A failing media request must not also leave an unhandled transaction promise.
      const getAll = IDBIndex.prototype.getAll;
      IDBIndex.prototype.getAll = function (...args) {
        const request = getAll.apply(this, args);
        this.objectStore.transaction.abort();
        return request;
      };
      try {
        for (const site of ['02-aster-observatory/aster', '03-morrow-coffee/morrow']) {
          const uploads = await import('/' + site + '-uploads.js');
          let failed = false;
          try { await uploads.videosFor('reader'); } catch { failed = true; }
          if (!failed) throw Error('Aborted media read succeeded');
        }
      } finally { IDBIndex.prototype.getAll = getAll; }
      await new Promise(resolve => setTimeout(resolve, 50));
      return results;
    });
    assert.equal(recovery.length, 4);
    await page.evaluate(async () => {
      const auth = await import('/02-aster-observatory/aster-auth.js');
      const store = await import('/02-aster-observatory/aster-store.js');
      await auth.register('Reader', 'reader@example.test', 'test-password');
      const key = 'disguise:aster:state:v1', id = 'a'.repeat(32);
      // Simulate a write from a background tab before its storage event arrives.
      let fresh = JSON.parse(localStorage.getItem(key));
      fresh.accounts[0].bio = 'Changed in another tab';
      localStorage.setItem(key, JSON.stringify(fresh));
      store.toggle('likes', id);
      fresh = JSON.parse(localStorage.getItem(key));
      if (fresh.accounts[0].bio !== 'Changed in another tab') throw Error('Lost a concurrent edit');
      fresh.session = null;
      localStorage.setItem(key, JSON.stringify(fresh));
      try { store.toggle('later', id); throw Error('Resurrected session'); }
      catch (e) { if (e.message !== 'needLogin') throw e; }
      if (JSON.parse(localStorage.getItem(key)).session !== null) throw Error('Logout overwritten');
      const get = Storage.prototype.getItem;
      Storage.prototype.getItem = () => { throw Error('Read unavailable'); };
      try {
        try { store.commit(s => { s.accounts = []; }); throw Error('Unsafe write'); }
        catch (e) { if (e.message !== 'storageError') throw e; }
      } finally { Storage.prototype.getItem = get; }
      if (JSON.parse(localStorage.getItem(key)).accounts.length !== 1) throw Error('Lost account');
    });
    // A story editor must not autosave the old user's text into the new user's account.
    await page.evaluate(async () => {
      const auth = await import('/01-northline/northline-auth.js');
      const state = auth.readState();
      state.accounts = ['alice', 'bob'].map(id => ({ id, username: id, displayName: id }));
      auth.createSession(state, 'alice');
    });
    await page.goto('https://recovery.test/01-northline/index.html#/me');
    await page.getByRole('button', { name: 'Написать', exact: true }).first().click();
    await page.getByLabel('Текст черновика', { exact: true }).fill('Private draft from Alice');
    const other = await context.newPage();
    await other.goto('https://recovery.test/');
    await other.evaluate(async () => {
      const auth = await import('/01-northline/northline-auth.js'); auth.createSession(auth.readState(), 'bob');
    });
    await page.waitForFunction(() => !document.querySelector('dialog[open]'));
    await page.waitForTimeout(650);
    const bob = await page.evaluate(() => JSON.parse(localStorage.getItem('disguise:northline:state')).profiles.bob?.reader);
    assert.ok(!bob?.drafts?.length && !bob?.localPosts?.length, 'Old editor does not modify Bob');
    assert.deepEqual(errors, []);
    console.log('PASS: IndexedDB retry/blocked/closed connections, upload aborts, cross-tab edits/logout and Line draft isolation.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

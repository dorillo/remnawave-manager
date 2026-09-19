// Real HTTP cache regression: old unversioned modules must not survive a CSP update.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const fixture = String.raw`
import json, signal, tempfile
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from remnawave_manager.site_assets import FreshAssetsHandler

with tempfile.TemporaryDirectory() as folder:
    site = Path(folder) / 'site'
    site.mkdir()
    upgraded = False
    (site / 'avatar.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    (site / 'app.js').write_text("import { render } from './ui.js'; render();")
    html = '<!doctype html><meta http-equiv="Content-Security-Policy" content="img-src {}"><script type="module" src="app.js"></script>'
    (site / 'index.html').write_text(html.format("'self' http:"))
    class Handler(FreshAssetsHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)
        def do_GET(self):
            global upgraded
            if self.path == '/upgrade':
                upgraded = True
                (site / 'ui.js').write_text("export function render() { const img=document.createElement('img'); img.src='/avatar.svg'; document.body.append(img); }")
                (site / 'index.html').write_text(html.format("'self'"))
                self.send_response(200); self.end_headers(); return
            super().do_GET()
        def send_head(self):
            return super().send_head() if upgraded else SimpleHTTPRequestHandler.send_head(self)
        def end_headers(self):
            if not upgraded:
                self.send_header('Cache-Control', 'public, max-age=3600' if '.js' in self.path else 'no-cache')
            super().end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_port
    (site / 'ui.js').write_text("export function render() { const img=document.createElement('img'); img.src='http://localhost:%d/avatar.svg'; document.body.append(img); }" % port)
    print(port, flush=True)
    def stop(*args):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
`;
(async () => {
  const server = spawn('python3', ['-u', '-c', fixture], { env: { ...process.env, PYTHONPATH: path.resolve(__dirname, '../src') }, stdio: ['ignore', 'pipe', 'inherit'] });
  let browser;
  try {
    const lines = readline.createInterface({ input: server.stdout });
    const port = await new Promise((resolve, reject) => {
      lines.once('line', line => resolve(Number(line)));
      server.once('exit', code => reject(new Error('Fixture exited: ' + code)));
    });
    browser = await chromium.launch({ executablePath: process.env.IMAGE_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
    const page = await browser.newPage();
    await page.addInitScript(() => {
      window.violations = [];
      document.addEventListener('securitypolicyviolation', event => window.violations.push(event.blockedURI));
    });
    const base = `http://127.0.0.1:${port}`;
    await page.goto(base);
    await page.waitForFunction(() => document.images[0]?.naturalWidth > 0);
    assert.ok((await page.locator('img').getAttribute('src')).includes('localhost'));
    await page.evaluate(() => fetch('/upgrade'));
    await page.reload();
    await page.waitForFunction(() => document.images[0]?.naturalWidth > 0);
    assert.equal(await page.locator('img').getAttribute('src'), '/avatar.svg');
    assert.deepEqual(await page.evaluate(() => window.violations), []);
    assert.ok((await page.locator('script').getAttribute('src')).includes('?v='));
    console.log('PASS: cached old modules are replaced after the CSP update; images load without clearing browser data.');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

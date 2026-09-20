// Real HTTP headers: extensionless avatars still render; upstream JS cannot execute.
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const path = require('node:path');
const readline = require('node:readline');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const fixture = String.raw`
import base64, io, signal, tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from remnawave_manager.image_preview import serve_preview_asset
from remnawave_manager.site_assets import FreshAssetsHandler

def source(request, **kwargs):
    if 'script' in request.full_url:
        payload = b'window.upstreamExecuted = true;'
    elif '.svg' in request.full_url:
        payload = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><script>parent.upstreamExecuted=true;</script></svg>'
    else:
        payload = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1XQAAAAASUVORK5CYII=')
    response = io.BytesIO(payload)
    response.headers = {'Content-Type': 'text/javascript'}
    return response

with tempfile.TemporaryDirectory() as folder:
    (Path(folder) / 'index.html').write_text('<!doctype html><body></body>')
    class Handler(FreshAssetsHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=folder, **kwargs)
        def do_GET(self):
            if not serve_preview_asset(self): super().do_GET()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    print(server.server_port, flush=True)
    def stop(*args): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        with patch('remnawave_manager.image_preview.OPENER.open', source): server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
`;
(async () => {
  const server = spawn('python3', ['-u', '-c', fixture], {
    env: { ...process.env, PYTHONPATH: path.resolve(__dirname, '../src') }, stdio: ['ignore', 'pipe', 'inherit'],
  });
  let browser;
  try {
    const lines = readline.createInterface({ input: server.stdout });
    const port = await new Promise((resolve, reject) => {
      lines.once('line', line => resolve(Number(line)));
      server.once('exit', code => reject(new Error('Fixture exited: ' + code)));
    });
    browser = await chromium.launch({ executablePath: process.env.IMAGE_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${port}`);
    const images = ['/_images/filin.mail.ru/pic?d=avatar', '/_images/pic.rtbcdn.ru/photo.png', '/_images/upload.wikimedia.org/test.svg'];
    for (const src of images) {
      assert.equal(await page.evaluate(src => new Promise(resolve => {
        const img = new Image();
        img.onload = () => resolve(img.naturalWidth > 0);
        img.onerror = () => resolve(false);
        img.src = src; document.body.append(img);
      }), src), true, src);
    }
    const script = '/_images/filin.mail.ru/pic?d=script';
    await page.evaluate(src => new Promise(resolve => {
      const el = document.createElement('script');
      el.src = src; el.onload = el.onerror = resolve; document.body.append(el);
    }), script);
    await page.evaluate(() => new Promise(resolve => {
      const frame = document.createElement('iframe');
      frame.onload = resolve; frame.src = '/_images/upload.wikimedia.org/test.svg'; document.body.append(frame);
    }));
    assert.equal(await page.evaluate(() => !!window.upstreamExecuted), false);
    const error = await page.request.get(`http://127.0.0.1:${port}/_images/unknown/a.png`);
    assert.equal(error.status(), 400);
    assert.equal(error.headers()['cache-control'], 'no-store');
    assert.equal(error.headers()['x-content-type-options'], 'nosniff');
    console.log('PASS: PNG/SVG and extensionless avatars render; upstream script/SVG execution blocked; errors are not cached.');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

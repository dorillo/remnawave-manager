/* Grid batches, manual continuation, source overlap and retry regressions. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises/03-morrow-coffee');
const id = n => n.toString(16).padStart(32, '0');
const author = { uuid:id(900), nickname:'creator', firstName:'Автор' };
const published = n => new Date(Date.UTC(2026, 8, 20) - n * 1000).toISOString();
const video = n => ({ uuid:id(n), creator:author, description:`Видео ${n}`, publishedAt:published(n), link:`https://vb-rtb.uma.media/vod/${n}.mp4`, thumbnail:'https://cdn-st.rutubelist.ru/poster.jpg' });
(async () => {
 const browser = await chromium.launch({executablePath:process.env.MORROW_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 try {
  const context = await browser.newContext({locale:'ru-RU'});
  const requests = [], errors = [];
  let failThird = false, pageSize = 17, failPage = 0;
  await context.route('**/*', async route => {
   const url = new URL(route.request().url()), p = url.pathname;
   if (p.startsWith('/_morrow/yappy/')) {
    requests.push(url.href);
    let result;
    if (p.includes('/author/')) result = author;
    else {
     const cursor = url.searchParams.get('created');
     const pageNumber = Number(url.searchParams.get('page') || 1);
     const start = cursor ? (Date.UTC(2026,8,20)-Date.parse(cursor))/1000+1 : (pageNumber-1)*pageSize+1;
     if (pageNumber === failPage) return route.fulfill({status:403,contentType:'application/json',body:'{}'});
     if (failThird && start === 35) return route.fulfill({status:502,contentType:'application/json',body:'{}'});
     const end = Math.min(65,start+pageSize-1);
     result = { results:Array.from({length:Math.max(0,end-start+1)},(_,i)=>video(start+i)), next:end<65?'true':null };
     if (start>1) result.results.unshift(video(start-1));
    }
    return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
   }
   if (p.startsWith('/_images/') || url.hostname==='cdn-st.rutubelist.ru') return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="9" height="16"/>'});
   const file = p.startsWith('/shared/') ? path.resolve(root,'..'+p) : path.join(root,p==='/'?'index.html':p);
   try { return route.fulfill({contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':p.endsWith('.svg')?'image/svg+xml':'text/html',body:await fs.readFile(file)}); }
   catch { return route.fulfill({status:404,body:''}); }
  });
  const page = await context.newPage();
  page.on('pageerror',error=>errors.push(error.message));
  for (const width of [360,1000,1600]) {
   await page.setViewportSize({width,height:900});
   for (const screen of ['author/remote/'+id(900),'explore','search?q=test']) {
    await page.goto('https://morrow.test/#/'+screen);
    const more = page.locator('.load-button');
    await page.waitForFunction(()=>document.querySelector('.video-tile') && !document.querySelector('.load-button').disabled);
    const initial = await page.locator('.video-tile').count();
    const columns = await page.locator('.video-grid').evaluate(grid=>getComputedStyle(grid).gridTemplateColumns.split(' ').length);
    assert.equal(initial % columns,0,'initial batch fills rows');
    assert.ok(initial>=24 && initial<65,'only part of the list is shown');
    const calls = requests.length;
    await page.locator('main').evaluate(main=>{main.scrollTop=main.scrollHeight; main.dispatchEvent(new Event('scroll'));});
    await page.waitForTimeout(100);
    assert.equal(requests.length,calls,'scroll does not request more videos');
    assert.equal(await page.locator('.video-tile').count(),initial);
    for (let step=0;step<5 && await more.isVisible();step++) {
     await more.click();
     await page.waitForFunction(()=>!document.querySelector('.load-button').disabled);
     const count=await page.locator('.video-tile').count();
     if (await more.isVisible()) assert.equal(count%columns,0,'continuation fills rows');
    }
    assert.equal(await page.locator('.video-grid').count(),1,'all batches share one grid');
    assert.equal(await page.locator('.video-tile').count(),65,'no videos lost or duplicated');
    assert.equal(await more.isVisible(),false);
   }
  }
  await page.evaluate(async()=>{(await import('/morrow-data.js')).clearCache();});
  failThird=true;
  await page.goto('https://morrow.test/#/search?q=retry');
  await page.waitForFunction(()=>document.querySelector('.video-tile') && !document.querySelector('.load-button').disabled);
  const before=await page.locator('.video-tile').count();
  await page.locator('.load-button').click();
  await page.waitForFunction(()=>!document.querySelector('.load-button').disabled);
  assert.equal(await page.locator('.video-tile').count(),34,'failed continuation reveals the buffered videos too');
  assert.ok(await page.locator('.video-tile').count()>=before,'failed continuation preserves visible videos');
  failThird=false;
  await page.locator('.load-button').click();
  await page.waitForFunction(()=>document.querySelectorAll('.video-tile').length>34&&!document.querySelector('.load-button').disabled);
  await page.evaluate(async()=>{(await import('/morrow-data.js')).clearCache();});
  pageSize=4;
  failPage=2;
  await page.goto('https://morrow.test/#/explore');
  await page.waitForFunction(()=>document.querySelector('.load-button')&&!document.querySelector('.load-button').disabled);
  assert.equal(await page.locator('.video-tile').count(),4,'a failure on page two must not hide page one');
  assert.match(await page.locator('.pagination-status').innerText(),/Не удалось/);
  failPage=0;
  const beforeRetry=requests.length;
  await page.locator('.load-button').click();
  await page.waitForFunction(()=>!document.querySelector('.load-button').disabled);
  assert.ok(requests[beforeRetry].endsWith('page=2'),'retry resumes at the failed page');
  assert.ok(requests.length-beforeRetry<=2,'one click requests at most two source pages');
  assert.ok(await page.locator('.video-tile').count()>4);
  await page.evaluate(async()=>{(await import('/morrow-data.js')).clearCache();});
  const beforeExplore=requests.length;
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('.video-tile')&&!document.querySelector('.load-button').disabled);
  assert.equal(requests.length-beforeExplore,2,'small pages do not trigger a burst to fill 24 tiles');
  assert.ok(await page.locator('.video-tile').count()>0);
  // Expired cached pages still appear, with an explicit stale-data notice.
  await page.evaluate(()=>{
   const key='morrow:video-cache:v1';
   const entries=JSON.parse(localStorage.getItem(key));
   for(const [,entry] of entries) entry.at=Date.now()-3600000;
   localStorage.setItem(key,JSON.stringify(entries));
  });
  failPage=1;
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('.video-tile')&&!document.querySelector('.load-button').disabled);
  assert.match(await page.locator('.pagination-status').innerText(),/сохранённая лента/);
  assert.equal(await page.locator('.load-button').innerText(),'Загрузить ещё');
  assert.deepEqual(errors,[]);
  console.log('Morrow grid pagination passed: complete rows, manual loading, overlap, exhaustion and retry.');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});

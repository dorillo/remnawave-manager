/* Transient source errors, incompatible responses and retry presentation. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises/04-signal-works');
const origin = 'https://answers.test';
const question = { id:1,title:'Сохранённый вопрос',author:{id:9,nick:'Автор'},content:{type:'doc',content:[]},spaces:[] };
(async () => {
 const browser = await chromium.launch({executablePath:process.env.ANSWERS_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 try {
  const context = await browser.newContext({locale:'ru-RU'});
  let mode='ok',calls=0;
  await context.route('**/*',async route=>{
   const url = new URL(route.request().url()), p=url.pathname;
   if (p.startsWith('/_answers/mail/')) {
    if (p.endsWith('/spaces')) return route.fulfill({contentType:'application/json',body:'{"result":[]}'});
    calls++;
    if (mode==='offline' || mode==='transient' && calls===1) return route.fulfill({status:502,contentType:'application/json',body:'{}'});
    if (mode==='invalid') return route.fulfill({contentType:'application/json',body:'{broken'});
    if (mode==='schema') return route.fulfill({contentType:'application/json',body:'{"result":{"unknown":[]}}'});
    if (mode==='limited') return route.fulfill({status:429,headers:{'Retry-After':'60'},contentType:'application/json',body:'{}'});
    const result=p.endsWith('/count')?{topics_count:1,replies_count:0}:{feed:[question],params:{}};
    return route.fulfill({contentType:'application/json',body:JSON.stringify({result})});
   }
   if(p==='/blank') return route.fulfill({contentType:'text/html',body:'<!doctype html><title>Data checks</title>'});
   const file=p.startsWith('/shared/')?path.resolve(root,'..'+p):path.join(root,p==='/'?'index.html':p);
   return route.fulfill({contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':p.endsWith('.svg')?'image/svg+xml':'text/html',body:await fs.readFile(file)});
  });
  const page=await context.newPage();
  page.setDefaultTimeout(6000);
  await page.goto(origin+'/blank');
  for(const scenario of ['transient','offline','invalid','schema','limited']) {
   mode=scenario; calls=0;
   const result=await page.evaluate(async scenario=>{
    const data=await import('/answers-data.js?case='+scenario);
    try {return {items:(await data.feed()).items.length};} catch(error) {return {error:error.message};}
   },scenario);
   assert.equal(calls,['transient','offline'].includes(scenario)?2:1,scenario+' retry limit');
   assert.deepEqual(result,scenario==='transient'?{items:1}:{error:scenario==='offline'?'network':scenario==='limited'?'rate-limit':'schema'});
   if(scenario==='limited') {
    await page.evaluate(async()=>{try{await (await import('/answers-data.js?case=limited')).feed();}catch{}});
    assert.equal(calls,1,'cooldown prevents another request');
   }
  }
  mode='ok';
  await page.goto(origin+'/');
  await page.getByRole('heading',{name:question.title}).waitFor();
  mode='offline';
  await page.reload();
  await page.getByRole('heading',{name:question.title}).waitFor();
  assert.equal(await page.locator('.load-error').count(),0,'cache has only its saved-feed notice');
  assert.match(await page.locator('main .notice-inline').innerText(),/Показана сохранённая лента/);
  await page.evaluate(()=>localStorage.clear());
  await page.reload();
  await page.locator('.load-error').waitFor();
  for(const width of [240,360,1280]) {
   await page.setViewportSize({width,height:850});
   for(const theme of ['light','dark']) {
    await page.evaluate(theme=>document.documentElement.dataset.theme=theme,theme);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.equal(await page.locator('.load-error').evaluate(panel=>{
     const button=panel.querySelector('button').getBoundingClientRect(),box=panel.getBoundingClientRect();
     return Math.abs(button.x+button.width/2-box.x-box.width/2)<2;
    }),true,'retry is centered');
    const bg=await page.locator('.load-error').evaluate(panel=>getComputedStyle(panel).backgroundColor);
    assert.equal(bg,theme==='dark'?'rgb(30, 41, 59)':'rgb(255, 255, 255)');
   }
  }
  mode='ok';
  await page.locator('[data-action="refresh"]').click();
  await page.waitForFunction(()=>!document.querySelector('.load-error') && !document.querySelector('main [aria-busy="true"]'));
  mode='schema';
  await page.goto(origin+'/#/user/mail/9');
  await page.locator('.load-error').waitFor();
  assert.match(await page.locator('.load-error').innerText(),/неподдерживаемом формате/);
  mode='ok';
  await page.locator('[data-action="more-profile"]').click();
  await page.getByRole('heading',{name:question.title}).waitFor();
  assert.equal(await page.locator('.load-error').count(),0,'profile recovers');
  mode='offline';
  await page.goto(origin+'/#/search/test');
  await page.locator('.load-error').waitFor();
  assert.equal(await page.getByText('Ничего не найдено.',{exact:true}).count(),0,'failure is not presented as empty results');
  mode='ok';
  await page.locator('[data-action="retry-search"]').click();
  await page.getByRole('heading',{name:question.title}).waitFor();
  console.log('Answers recovery passed: bounded retries, schema errors, rate limits, cache, themes and retry actions.');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});

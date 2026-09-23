/* Public discussion/profile pagination regressions. All API responses are fixtures. */
const assert=require('node:assert/strict'),fs=require('node:fs/promises'),path=require('node:path');
const {chromium}=require('./.tmp/node_modules/playwright-core');
const root=path.resolve(__dirname,'../src/remnawave_manager/data/disguises/04-signal-works');
const origin='https://spros.test';
const doc=text=>({type:'doc',content:[{type:'paragraph',content:[{type:'text',text}]}]});
const question=(id,total=4)=>({id,title:`Вопрос ${id}`,content:doc('Описание'),author:{id:9,nick:'Автор девять'},spaces:[],replies_count:total});
const answer=(id,parent=0)=>({id,topic_id:1,author:{id:9,nick:'Автор девять'},content:doc(`Ответ ${id}`),reply_to:parent,replyToReplyCount:id===10?2:0});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.ANSWERS_BROWSER||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 try {
  const ctx=await browser.newContext({locale:'ru-RU',viewport:{width:360,height:780}});
  let fail=false,branchFail=false,profileFail=true,rootProbeFail=true,releaseProfile;
  const profileGate=new Promise(resolve=>{releaseProfile=resolve;});
  const requests=[],errors=[];
  await ctx.route('**/*',async route=>{
   const url=new URL(route.request().url()),p=url.pathname,q=url.searchParams;
   assert.equal(route.request().method(),'GET');
   if(p.startsWith('/_answers/mail/')) {
    requests.push(p+url.search);let result;
    if(p.endsWith('/spaces'))result=[];
    else if(p.endsWith('/feed')) {
     if(q.has('space'))result=q.has('pos')?{feed:[{...question(2),spaces:[{title:'Тема',path:'topic'}]}],params:{}}:{feed:[{...question(1),spaces:[{adult:true}]}],params:{pos:100}};
     else result=q.get('pos')==='8'?{feed:[question(2)],params:{pos:7}}:q.has('pos')?{feed:null,params:{pos:8}}:{feed:[question(1,3)],params:{pos:7}};
    }
    else if(p.endsWith('/question/1'))result=question(1);
    else if(p.endsWith('/answers/1')) {
     if(fail || rootProbeFail && !q.has('reply_id') && q.has('pos') || branchFail && q.has('reply_id'))return route.fulfill({status:502,contentType:'application/json',body:'{}'});
     if(q.has('reply_id'))result=q.has('pos')?{replies:[answer(12,10)],params:{}}:{replies:[answer(11,10)],params:{last:11}};
     else result=q.get('pos')==='20'?{replies:null,params:{}}:q.has('pos')?{replies:[answer(20)],params:{last:20}}:{best_replies:answer(10),replies:[answer(10)],params:{last:10}};
    } else if(p.endsWith('/profile/9/count'))result={topics_count:3,replies_count:81};
    else if(p.endsWith('/profile/9/topics')) {
     await profileGate;
     if(profileFail && q.has('pos'))return route.fulfill({status:502,contentType:'application/json',body:'{}'});
     if(q.has('pos'))assert.equal(q.get('dir'),'1');else assert.equal(q.get('dir'),'0');
     result=q.get('pos')==='3'?{feed:[question(3)],params:{}}:q.get('pos')==='2'?{feed:[question(2)],params:{pos:3}}:{feed:[question(1)],params:{pos:q.has('pos')?2:1}};
    } else if(p.endsWith('/profile/9/replies')) {
     // Mail also returns short pages before exhaustion, without params.pos.
     const start=q.has('pos')?Number(q.get('pos'))-1:200;
     const limit=start===160?19:start===141?1:20;
     const ids=Array.from({length:Math.max(0,Math.min(limit,start-119))},(_,i)=>start-i);
     result={replies:ids.map(id=>answer(id)),params:q.has('pos')?{}:{pos:181}};
    }
    else return route.fulfill({status:404,body:'{}'});
    return route.fulfill({contentType:'application/json',body:JSON.stringify({result})});
   }
   const file=p.startsWith('/shared/')?path.resolve(root,'..'+p):path.join(root,p==='/'?'index.html':p);
   return route.fulfill({contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':p.endsWith('.svg')?'image/svg+xml':'text/html',body:await fs.readFile(file)});
  });
  const page=await ctx.newPage();page.on('pageerror',e=>errors.push(e.message));
  page.setDefaultTimeout(6000);
  await page.goto(origin+'/#/user/mail/9');
  await page.locator('main .load-row .ui-loading').waitFor();
  for(const width of [360,1280]) {
   await page.setViewportSize({width,height:780});
   assert.ok(await page.locator('main .load-row .ui-loading').evaluate(spinner=>{
    const box=spinner.getBoundingClientRect(),row=spinner.parentElement.getBoundingClientRect();
    return Math.abs(box.x+box.width/2-row.x-row.width/2)<2;
   }),'loading indicator is centered at '+width);
  }
  await page.setViewportSize({width:360,height:780});
  releaseProfile();
  await page.getByRole('link',{name:'Вопрос 1',exact:true}).waitFor();
  assert.ok(!requests.some(x=>x.includes('/feed?')),'direct profile does not depend on the home feed');
  profileFail=true;await page.locator('[data-action="more-profile"]').click();await page.locator('.notice-inline').filter({hasText:'Не удалось загрузить данные.'}).waitFor();
  assert.equal(await page.getByRole('link',{name:'Вопрос 1',exact:true}).count(),1,'failed continuation preserves loaded data');
  profileFail=false;
  for(let i=0;i<2;i++){await page.locator('[data-action="more-profile"]').click();await page.locator('.loading').count();await page.waitForFunction(()=>!document.querySelector('main [aria-busy="true"]'));await page.waitForTimeout(60);}
  await page.getByRole('link',{name:'Вопрос 3',exact:true}).waitFor();
  assert.equal(await page.locator('.question-card').count(),3,'duplicates do not terminate a progressing cursor');
  await page.locator('.profile-tab[href$="/answers"]').click();await page.getByText('Ответ 200',{exact:true}).waitFor();
  for (const last of [161,142,141,121,120]) {
   await page.locator('[data-action="more-profile"]').click();
   await page.getByText(`Ответ ${last}`,{exact:true}).waitFor();
  }
  assert.equal(await page.locator('.profile-answer').count(),81,'short and single-item pages do not terminate pagination');
  assert.equal(await page.getByText('Источник не вернул остальные записи.',{exact:true}).count(),0);
  await page.waitForTimeout(80);
  assert.equal(await page.locator('[data-action="more-profile"]').count(),0,'null profile page is end of results');
  await page.goto(origin+'/');await page.getByRole('link',{name:'Вопрос 1',exact:true}).waitFor();
  assert.equal(await page.locator('.reply-count').innerText(),'3');
  await page.getByRole('link',{name:'Вопрос 1',exact:true}).click();await page.getByText('Ответ 10',{exact:true}).waitFor();
  assert.equal(await page.getByText('Ответ 10',{exact:true}).count(),1,'pinned answer is deduplicated');
  assert.equal(await page.locator('.answers-title').innerText(),'4 ответов');
  await page.getByText('Ответ 11',{exact:true}).waitFor();
  await page.getByText('Ответ 12',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-action="more-replies"][data-parent]').count(),0,'threads load automatically');
  assert.equal(await page.getByText(/Загружено \d/).count(),0);
  assert.ok(await page.locator('[data-action="more-replies"]:not([data-parent])').evaluate(button=>[...document.querySelectorAll('.answer-card')].every(card=>Boolean(card.compareDocumentPosition(button)&Node.DOCUMENT_POSITION_FOLLOWING))),'continuation follows all comments');
  fail=true;await page.locator('[data-action="more-replies"]:not([data-parent])').click();await page.locator('.notice-inline').filter({hasText:'Ответы пока недоступны.'}).waitFor();
  assert.equal(await page.locator('.answer-card').count(),3,'failed reply continuation preserves loaded branches');
  fail=false;rootProbeFail=false;await page.locator('[data-action="more-replies"]:not([data-parent])').click();await page.getByText('Ответ 20',{exact:true}).waitFor();
  assert.equal(await page.locator('.answer-card').count(),4);
  await page.waitForTimeout(80);
  assert.equal(await page.locator('[data-action="more-replies"]:not([data-parent])').count(),0,'null replies page is end of results');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'nested replies fit mobile viewport');
  await page.evaluate(()=>{location.hash='#/home'});await page.getByRole('link',{name:'Вопрос 1',exact:true}).waitFor();
  assert.equal(await page.locator('.reply-count').innerText(),'4','detail updates feed count');
  await page.locator('[data-action="more"]').click();await page.getByRole('link',{name:'Вопрос 2',exact:true}).waitFor();
  assert.equal(await page.locator('[data-action="more"]').count(),0,'cyclic cursor terminates safely');
  await page.evaluate(()=>{location.hash='#/space/topic'});await page.getByRole('link',{name:'Вопрос 2',exact:true}).waitFor();
  assert.equal(await page.locator('[data-action="more-space"]').count(),0,'filtered pages are skipped without an empty click');
  branchFail=true;await page.goto(origin+'/#/question/mail/1');
  await page.locator('[data-action="more-replies"][data-parent="10"]').waitFor();
  assert.equal(await page.locator('.answer-card .ui-loading').count(),0,'failed branch stops its spinner');
  branchFail=false;await page.locator('[data-action="more-replies"][data-parent="10"]').click();
  await page.getByText('Ответ 12',{exact:true}).waitFor();
  assert.equal(await page.locator('[data-action="more-replies"][data-parent]').count(),0,'retry resumes automatic branch pagination');
  fail=true;await page.reload();await page.locator('.notice-inline').filter({hasText:'Ответы пока недоступны.'}).waitFor();
  assert.equal(await page.locator('.answers-title').innerText(),'4 ответов','failed fetch is not zero responses');
  assert.deepEqual(errors,[]);console.log('Spros pagination/profile/nested replies/count/error regressions passed');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

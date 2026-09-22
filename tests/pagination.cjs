const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
(async () => {
  const source = await fs.readFile('src/remnawave_manager/data/disguises/shared/pagination.js', 'utf8');
  const { confirmedPages } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
  const item = id => ({id});
  const calls = [];
  const pages = {
    first: {items: [item(1)], pos: 'duplicate'},
    duplicate: {items: [item(1)], pos: 'empty'},
    empty: {items: [], pos: 'last'},
    last: {items: [item(2)], pos: 'end'},
    end: {items: [], pos: null},
  };
  const load = confirmedPages(async (scope, cursor) => { calls.push(cursor); return pages[cursor || 'first']; });
  assert.deepEqual(await load('a'), {items: [item(1)], pos: 'last'});
  assert.deepEqual(await load('a', 'last'), {items: [item(2)], pos: null});
  assert.equal(calls.filter(x => x === 'last').length, 1, 'prefetched page is reused');
  const single = confirmedPages(async (_, cursor) => cursor ? {items: [], pos: null} : {items: [item(1)], pos: 'end'});
  assert.equal((await single('one')).pos, null, 'last item does not leave an empty button');
  const cycle = confirmedPages(async (_, cursor) => ({items: [item(1)], pos: cursor === 'a' ? 'b' : 'a'}));
  assert.equal((await cycle('cycle')).pos, null, 'duplicate cursor cycles stop');
  let fail = true;
  const retry = confirmedPages(async (_, cursor) => {
    if (cursor && fail) throw new Error('offline');
    return cursor ? {items: [item(2)], pos: null} : {items: [item(1)], pos: 'next'};
  });
  assert.equal((await retry('retry')).pos, 'next', 'failed probe preserves retry');
  fail = false;
  assert.deepEqual(await retry('retry', 'next'), {items: [item(2)], pos: null});
  const noLoss = confirmedPages(async (_, cursor) => cursor === 'skip' ? {items: [], pos: 'real'} : cursor === 'real' ? {items: [item(3)], pos: null} : {items: [], pos: 'skip'});
  assert.deepEqual(await noLoss('filtered'), {items: [item(3)], pos: null}, 'filtered first pages do not require empty clicks');
  const aggregated = confirmedPages(async (_, cursor) => cursor
    ? {items: [item(2)], pos: null} : {items: [item(1), item(2)], pos: null}, {trackHistory: false});
  await aggregated('author');
  assert.deepEqual((await aggregated('author', 'boundary')).items, [item(2)], 'merged feeds can retain items not yet displayed by their caller');
  const fullLast = confirmedPages(async (_, cursor) => ({items: cursor ? [] : Array.from({length:48}, (_, i) => item(i)), pos: cursor ? null : 48}));
  assert.equal((await fullLast('exact-multiple')).pos, null, 'a full final page is also terminal');
  const aborted = new AbortController();
  const cancel = confirmedPages(async (_, cursor) => {if(cursor) {aborted.abort();throw new DOMException('Aborted','AbortError');}return {items:[item(1)],pos:'next'};});
  await assert.rejects(cancel('cancel',null,{signal:aborted.signal}), {name:'AbortError'});
  console.log('Confirmed pagination: last pages, duplicates, gaps, cycles, cache, errors and cancellation passed');
})().catch(e => {console.error(e);process.exitCode=1;});

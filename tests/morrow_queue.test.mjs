import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createVideoQueue } from '../src/remnawave_manager/data/disguises/03-morrow-coffee/morrow-queue.js';

const videos = (...ids) => ids.map(id => ({ id }));

test('a viewer keeps loaded and pending videos in order and continues the source cursor', async () => {
  const calls = [];
  const grid = createVideoQueue({ cursor: 1, ended: false, fetchPage: async (cursor, signal) => {
    calls.push([cursor, signal]);
    return { items: videos(cursor * 2 - 1, cursor * 2), next: cursor < 3 ? cursor + 1 : null };
  } });
  const gridSignal = new AbortController();
  await grid.load(gridSignal.signal);
  await grid.load(gridSignal.signal);
  gridSignal.abort();
  const viewer = createVideoQueue(grid.snapshot());
  const viewerSignal = new AbortController().signal;
  assert.deepEqual(viewer.items, videos(1, 2, 3, 4));
  assert.deepEqual(await viewer.load(viewerSignal), { items: videos(5, 6), next: false, stale: false });
  assert.deepEqual(calls.map(([cursor]) => cursor), [1, 2, 3]);
  assert.equal(calls[2][1], viewerSignal);
  assert.deepEqual(grid.items, videos(1, 2, 3, 4));
});

test('finite profile lists end without fetching recommendations', async () => {
  const queue = createVideoQueue({ items: videos('saved-3', 'saved-1', 'saved-2') });
  assert.deepEqual(queue.items, videos('saved-3', 'saved-1', 'saved-2'));
  assert.equal(queue.ended, true);
  assert.deepEqual(await queue.load(), { items: [], next: null, stale: false });
});

test('deduplicates pages and ends a repeated cursor', async () => {
  const queue = createVideoQueue({ items: videos('a'), cursor: 'cursor-a', ended: false,
    fetchPage: async () => ({ items: videos('a', 'b', 'b'), next: 'cursor-a' }) });
  assert.deepEqual((await queue.load()).items, videos('b'));
  assert.equal(queue.ended, true);
  assert.deepEqual(queue.items, videos('a', 'b'));
});

test('failed pages retain the cursor for retry', async () => {
  let fail = true;
  const calls = [];
  const queue = createVideoQueue({ cursor: 'next-page', ended: false, fetchPage: async cursor => {
    calls.push(cursor);
    if (fail) throw new Error('network');
    return { items: videos('next'), next: null };
  } });
  await assert.rejects(queue.load(), /network/);
  assert.deepEqual(queue.items, []);
  assert.equal(queue.ended, false);
  fail = false;
  await queue.load();
  assert.deepEqual(calls, ['next-page', 'next-page']);
  assert.deepEqual(queue.items, videos('next'));
});

test('a response arriving after navigation does not consume a page', async () => {
  const controller = new AbortController();
  const queue = createVideoQueue({ cursor: 2, ended: false, fetchPage: async () => {
    controller.abort();
    return { items: videos('late'), next: 3 };
  } });
  await assert.rejects(queue.load(controller.signal), { name: 'AbortError' });
  assert.deepEqual(queue.items, []);
  assert.equal(queue.snapshot().cursor, 2);
});

test('empty pages with a new cursor can continue; stale state survives a snapshot', async () => {
  const queue = createVideoQueue({ cursor: 1, ended: false, fetchPage: async cursor =>
    cursor === 1 ? { items: [], next: 2, stale: true } : { items: videos('found'), next: null } });
  assert.equal((await queue.load()).next, true);
  const restored = createVideoQueue(queue.snapshot());
  assert.equal(restored.stale, true);
  assert.deepEqual((await restored.load()).items, videos('found'));
  assert.equal(restored.ended, true);
});

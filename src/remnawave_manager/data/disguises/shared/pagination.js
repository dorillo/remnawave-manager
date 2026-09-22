// Some sources return a cursor for their final page. Confirm that it leads to
// new items; retain the prefetched page for the next click. No persistent cache.
export function confirmedPages(fetchPage, { items = 'items', cursor = 'pos', trackHistory = true } = {}) {
  const prepared = new Map();
  const histories = new Map();
  const keyOf = (scope, position) => JSON.stringify([scope, position]);
  const remember = (key, page) => {
    prepared.set(key, { page, at: Date.now() });
    while (prepared.size > 40) prepared.delete(prepared.keys().next().value);
  };
  return async (scope, position = null, options = {}) => {
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const scopeKey = JSON.stringify(scope);
    if (!trackHistory || !position || options.force || !histories.has(scopeKey)) {
      histories.set(scopeKey, new Set());
      while (histories.size > 40) histories.delete(histories.keys().next().value);
    }
    const history = histories.get(scopeKey);
    const key = keyOf(scope, position);
    const saved = prepared.get(key);
    prepared.delete(key);
    let page = saved && !options.force && Date.now() - saved.at < 60000
      ? saved.page : await fetchPage(scope, position, options);
    // A retry may start on a duplicate-only page that could not be checked
    // earlier. Advance to new content without requiring an empty user click.
    const skipped = new Set([position]);
    for (let step = 0; step < 20 && page[items].every(item => history.has(item.id)); step++) {
      const continuation = page[cursor];
      if (!continuation || skipped.has(continuation) || page.stale) break;
      skipped.add(continuation);
      page = await fetchPage(scope, continuation, options);
      position = continuation;
    }
    for (const item of page[items]) history.add(item.id);
    const known = history;
    if (page.stale) return page;
    const visited = new Set([position]);
    let next = page[cursor];
    // Bound work even when an upstream sends an endless sequence of empty pages.
    for (let step = 0; next !== null && next !== undefined && next !== '' && next !== false && step < 20; step++) {
      if (visited.has(next)) return { ...page, [cursor]: null };
      visited.add(next);
      try {
        const probe = await fetchPage(scope, next, options);
        if (probe.stale) break; // Offline cache is not evidence of exhaustion.
        if (probe[items].some(item => !known.has(item.id))) {
          remember(keyOf(scope, next), probe);
          return { ...page, [cursor]: next };
        }
        next = probe[cursor];
      } catch (error) {
        if (options.signal?.aborted || error.name === 'AbortError') throw error;
        // Keep continuation retryable; a network failure does not mean the end.
        break;
      }
    }
    return { ...page, [cursor]: next ?? null };
  };
}

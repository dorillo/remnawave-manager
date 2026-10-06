// A list and its continuation travel together from the grid to the player.
// Each viewer gets its own cursor and uses its own AbortSignal.
export function createVideoQueue({
  items = [], cursor = null, ended = true, stale = false,
  visited = [], fetchPage = null,
} = {}) {
  items = [...new Map(items.map((video) => [video.id, video])).values()];
  const seen = new Set(items.map((video) => video.id));
  const cursors = new Set(visited);
  return {
    get items() { return items; },
    get ended() { return ended; },
    get stale() { return stale; },
    snapshot: () => ({ items: items.slice(), cursor, ended, stale, visited: [...cursors], fetchPage }),
    async load(signal) {
      if (ended || !fetchPage) return { items: [], next: null, stale };
      signal?.throwIfAborted();
      const result = await fetchPage(cursor, signal);
      // Never consume a page that the departed view cannot display.
      signal?.throwIfAborted();
      const fresh = result.items.filter((video) =>
        !seen.has(video.id) && (seen.add(video.id), true));
      items.push(...fresh);
      cursors.add(cursor);
      ended = !result.next || cursors.has(result.next);
      cursor = result.next;
      stale ||= !!result.stale;
      return { items: fresh, next: !ended, stale };
    },
  };
}

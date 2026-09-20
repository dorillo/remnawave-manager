/* Anonymous first-party requests used only while preparing the static snapshot. */
const { existsSync } = require('node:fs');
const { homedir } = require('node:os');
const path = require('node:path');

function playwright() {
  for (const module of ['playwright-core', '../tests/.tmp/node_modules/playwright-core']) {
    try { return require(module); }
    catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  }
  throw new Error('Install playwright-core: npm install --prefix tests/.tmp --no-save playwright-core');
}
function browserPath(chromium) {
  if (process.env.ASTER_BROWSER) {
    if (!existsSync(process.env.ASTER_BROWSER)) throw new Error('ASTER_BROWSER does not point to a browser executable');
    return process.env.ASTER_BROWSER;
  }
  const candidates = process.platform === 'darwin' ? [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    path.join(homedir(), 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
  ] : process.platform === 'win32' ? [
    ...['PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'].filter(key => process.env[key])
      .map(key => path.join(process.env[key], 'Google/Chrome/Application/chrome.exe')),
  ] : ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/snap/bin/chromium'];
  const executable = [...candidates, chromium.executablePath()].find(existsSync);
  if (!executable) throw new Error('Chrome/Chromium was not found. Set ASTER_BROWSER to its executable path.');
  return executable;
}
async function main() {
  const { chromium } = playwright();
  const browser = await chromium.launch({
    executablePath: browserPath(chromium),
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.goto('https://rutube.ru/', {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    });
    const result = await page.evaluate(async () => {
      const get = async (url, timeout = 15000) => {
        let failure;
        for (let attempt = 0; attempt < 3; attempt += 1) {
          try {
            const response = await fetch(url, {
              credentials: 'omit',
              signal: AbortSignal.timeout(timeout),
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return await response.json();
          } catch (error) {
            failure = error;
            if (attempt < 2)
              await new Promise((resolve) =>
                setTimeout(resolve, 250 * (attempt + 1)),
              );
          }
        }
        throw failure;
      };
      const pool = async (items, size, task) => {
        let cursor = 0;
        async function worker() {
          while (cursor < items.length) {
            const item = items[cursor++];
            await task(item);
          }
        }
        await Promise.all(Array.from({ length: size }, worker));
      };
      const compactVideo = (video) => ({
        id: video.id,
        title: video.title,
        description: video.description,
        thumbnail_url: video.thumbnail_url,
        duration: video.duration,
        created_ts: video.created_ts,
        publication_ts: video.publication_ts,
        author: video.author,
        category: video.category,
        hits: video.hits,
        pg_rating: video.pg_rating,
        is_adult: video.is_adult,
        is_paid: video.is_paid,
        is_club: video.is_club,
        is_deleted: video.is_deleted,
        is_hidden: video.is_hidden,
        is_livestream: video.is_livestream,
      });
      const feed = await get(
        '/api/v2/video/recommendation/main?limit=60&offset=0',
      );
      const videos = Array.isArray(feed.results) ? feed.results : [];
      const channelMap = new Map();
      for (const video of videos) {
        const author = video.author;
        if (author?.id)
          channelMap.set(String(author.id), {
            id: String(author.id),
            name: author.name,
            avatar: author.avatar_url,
            videos: [],
          });
      }

      const enrichDiscussion = async (video) => {
        try {
          const base = `/api/v2/comments/video/${video.id}/?client=wdp&sort_by=date_added_desc`;
          const roots = [];
          let commentId = '';
          let commentsCount = 0;
          for (let pageNumber = 0; pageNumber < 4; pageNumber += 1) {
            const suffix = commentId
              ? `&direction=earliest&comment_id=${encodeURIComponent(commentId)}`
              : '&direction=earliest';
            const data = await get(base + suffix, 12000);
            commentsCount = Number(data.comments_count) || commentsCount;
            const rows = Array.isArray(data.results) ? data.results : [];
            if (!rows.length) break;
            const known = new Set(roots.map((comment) => String(comment.id)));
            roots.push(
              ...rows.filter((comment) => !known.has(String(comment.id))),
            );
            commentId = String(rows.at(-1).id);
            if (!data.has_next || roots.length >= 80) break;
          }
          const parents = roots
            .filter(
              (comment) => !comment.parent_id && comment.replies_number > 0,
            )
            .slice(0, 10);
          const replies = [];
          await pool(parents, 4, async (parent) => {
            try {
              const data = await get(
                `${base}&direction=earliest&parent_id=${encodeURIComponent(parent.id)}`,
                10000,
              );
              if (Array.isArray(data.results))
                replies.push(...data.results.slice(0, 10));
            } catch {
              /* A missing reply branch does not discard the discussion. */
            }
          });
          video.comments_count = commentsCount;
          video._public_comments = [...roots, ...replies].slice(0, 100);
        } catch {
          /* One unavailable discussion must not discard the feed. */
        }
      };
      const enrichVote = async (video) => {
        try {
          const vote = await get(
            `/api/numerator/video/${video.id}/vote?client=wdp`,
            10000,
          );
          video.positive_votes = Number(vote.positive) || 0;
          video.negative_votes = Number(vote.negative) || 0;
        } catch {
          /* Keep the video when the optional counter is unavailable. */
        }
      };
      const loadChannel = async (channel) => {
        try {
          const first = await get(
            `/api/video/person/${channel.id}/?page=1`,
          );
          const rows = Array.isArray(first.results) ? [...first.results] : [];
          const pages = Math.min(10, Math.max(1, Number(first.num_pages) || 1));
          for (let number = 2; number <= pages; number += 1) {
            const data = await get(
              `/api/video/person/${channel.id}/?page=${number}`,
            );
            if (Array.isArray(data.results)) rows.push(...data.results);
          }
          const unique = [
            ...new Map(
              rows
                .filter((video) => video?.id)
                .map((video) => [video.id, compactVideo(video)]),
            ).values(),
          ];
          channel.videos = unique;
        } catch {
          channel.videos = videos
            .filter((video) => String(video.author?.id) === channel.id)
            .map(compactVideo);
        }
      };

      await Promise.all([
        pool(videos, 4, enrichDiscussion),
        pool(videos, 6, enrichVote),
      ]);
      await pool([...channelMap.values()], 2, loadChannel);
      const channelVideos = [
        ...new Map(
          [...channelMap.values()]
            .flatMap((channel) => channel.videos)
            .map((video) => [video.id, video]),
        ).values(),
      ];
      await pool(channelVideos, 6, enrichVote);
      feed.channels = [...channelMap.values()];
      return feed;
    });
    process.stdout.write(JSON.stringify(result));
  } finally {
    await browser.close();
  }
}
module.exports = { browserPath, playwright };
if (require.main === module) main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});

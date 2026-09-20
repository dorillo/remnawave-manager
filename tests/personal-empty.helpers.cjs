const assert = require('node:assert/strict');
module.exports = async function checkPersonalEmpty(page, routes) {
  const previous = page.url(), viewport = page.viewportSize(), headings = new Set();
  for (const [hash, kind] of routes) {
    await page.evaluate(hash => { location.hash = hash; }, hash);
    const panel = page.locator(`[data-empty-section="${kind}"]`);
    await panel.waitFor();
    const title = await panel.locator('h2').innerText();
    assert(!headings.has(title), `Distinct empty title for ${hash}`);
    headings.add(title);
    assert((await panel.locator('p').innerText()).length > 20);
    await page.setViewportSize({ width: 320, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, hash);
    await page.screenshot({ path: `/tmp/empty-${new URL(page.url()).hostname}-${kind}.png` });
  }
  await page.setViewportSize(viewport);
  await page.goto(previous);
};

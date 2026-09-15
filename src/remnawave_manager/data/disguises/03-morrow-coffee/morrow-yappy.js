// Untrusted provider data is reduced to this explicit, text-only schema.
export const validId = (value) =>
  typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
export const cleanId = (value) =>
  String(value || "")
    .replaceAll("-", "")
    .toLowerCase();
const text = (v, max = 5000) => (typeof v === "string" ? v.slice(0, max) : "");
export function safeURL(value, media = false) {
  try {
    const u = new URL(value);
    const hosts = media
      ? ["vb-rtb.uma.media"]
      : ["cdn-st.rutubelist.ru", "cdn-st.yappy.media", "pic.rtbcdn.ru"];
    return u.protocol === "https:" &&
      !u.username &&
      !u.password &&
      !u.port &&
      hosts.includes(u.hostname)
      ? u.href
      : "";
  } catch {
    return "";
  }
}
const count = (v) => (Number.isFinite(v) && v >= 0 ? Math.floor(v) : null);
export function author(raw = {}) {
  const id = cleanId(raw.uuid || raw.hex);
  if (!validId(id)) return null;
  return {
    id,
    name: text(raw.firstName || raw.name || raw.nickname, 80) || "Yappy",
    nickname: text(raw.nickname || raw.name, 80),
    avatar: safeURL(raw.avatar144 || raw.avatar || raw.photo),
    about: text(raw.about, 2000),
    followers: count(raw.subscribers),
  };
}
export function video(raw) {
  if (
    !raw ||
    !validId(cleanId(raw.uuid)) ||
    raw.longvideoMetainfo ||
    raw.streamInfo ||
    raw.deleted ||
    raw.isAvailable === false
  )
    return null;
  const sources = Object.fromEntries(
    Object.entries({
      sd: raw.videoLinks?.sd,
      hd: raw.videoLinks?.hd || raw.link,
      fullHd: raw.videoLinks?.fullHd,
    })
      .map(([k, v]) => [k, safeURL(v, true)])
      .filter(([, v]) => v),
  );
  if (!Object.keys(sources).length) return null;
  const id = cleanId(raw.uuid),
    creator = author(raw.creator);
  if (!creator) return null;
  return {
    id,
    provider: "yappy",
    sourceUrl: `https://yappy.media/video/${id}`,
    author: creator,
    description: text(raw.description),
    poster: safeURL(raw.thumbnail),
    sources,
    publishedAt: text(raw.publishedAt, 50),
    duration: count(raw.duration),
    music: text(raw.audio?.title, 160),
    stats: {
      likes: count(raw.likesCount),
      comments: count(raw.commentsCount),
      views: count(raw.viewsCount),
    },
    fetchedAt: Date.now(),
  };
}
export function comment(raw) {
  if (!raw || !/^[a-f0-9]{1,128}$/.test(raw.hex || "")) return null;
  return {
    id: `public:${raw.hex}`,
    text: raw.deleted ? "" : text(raw.markdown),
    deleted: !!raw.deleted,
    author: author({
      ...raw.commenter,
      hex: raw.commenterHex || raw.commenter?.hex,
    }),
    created: text(raw.creationDate, 50),
    likes: count(raw.likeCount),
    parentId: null,
  };
}
export const capabilities = Object.freeze({
  search: true,
  authors: true,
  comments: true,
  replies: false,
});

import { read, writeProfile } from "./morrow-db.js";
import { validId, safeURL } from "./morrow-yappy.js";
export const blankProfile = (name = "") => ({
  version: 2,
  profile: { name, about: "", avatar: "" },
  likes: [],
  saved: [],
  following: [],
  hidden: [],
  hiddenAuthors: [],
  commentLikes: [],
  comments: [],
  history: [],
  videos: [],
  authors: [],
  economy: false,
});
export function validateProfile(raw) {
  const fail = () => {
    throw new Error("invalidData");
  };
  if (!raw || raw.version !== 2 || JSON.stringify(raw).length > 8000000) fail();
  const str = (v, n) => {
    if (typeof v !== "string" || v.length > n) fail();
    return v;
  };
  const ids = (v, max, regex) => {
    if (
      !Array.isArray(v) ||
      v.length > max ||
      v.some((x) => typeof x !== "string" || !regex.test(x)) ||
      new Set(v).size !== v.length
    )
      fail();
    return [...v];
  };
  const out = blankProfile();
  out.profile = {
    name: str(raw.profile?.name, 80),
    about: str(raw.profile?.about, 2000),
    avatar: str(raw.profile?.avatar, 180000),
  };
  if (
    out.profile.avatar &&
    !/^data:image\/(png|jpeg|webp);base64,[a-zA-Z0-9+/=]+$/.test(
      out.profile.avatar,
    )
  )
    fail();
  for (const key of ["likes", "saved", "following", "hidden", "hiddenAuthors"])
    out[key] = ids(raw[key], 5000, /^[a-f0-9]{32}$/);
  out.commentLikes = ids(
    raw.commentLikes,
    5000,
    /^(public:[a-f0-9]{1,128}|local:[a-f0-9-]{36})$/,
  );
  if (!Array.isArray(raw.comments) || raw.comments.length > 5000) fail();
  out.comments = raw.comments.map((c) => {
    if (
      !/^local:[a-f0-9-]{36}$/.test(c.id) ||
      !validId(c.videoId) ||
      !Number.isFinite(c.created) ||
      !Number.isFinite(c.updated)
    )
      fail();
    if (
      c.parentId !== null &&
      !/^(public:[a-f0-9]{1,128}|local:[a-f0-9-]{36})$/.test(c.parentId)
    )
      fail();
    return {
      id: c.id,
      videoId: c.videoId,
      parentId: c.parentId,
      recipient: str(c.recipient, 80),
      text: str(c.text, 5000),
      created: c.created,
      updated: c.updated,
      deleted: !!c.deleted,
    };
  });
  const seen = new Map(out.comments.map((c) => [c.id, c]));
  if (seen.size !== out.comments.length) fail();
  for (const c of out.comments) {
    const chain = new Set([c.id]);
    let p = c.parentId;
    while (p?.startsWith("local:")) {
      if (
        chain.size > 50 ||
        chain.has(p) ||
        !seen.has(p) ||
        seen.get(p).videoId !== c.videoId
      )
        fail();
      chain.add(p);
      p = seen.get(p).parentId;
    }
  }
  if (!Array.isArray(raw.history) || raw.history.length > 1000) fail();
  out.history = raw.history.map((h) => {
    if (!validId(h.id) || !Number.isFinite(h.at)) fail();
    return { id: h.id, at: h.at };
  });
  if (!Array.isArray(raw.videos) || raw.videos.length > 5000) fail();
  out.videos = raw.videos.map((v) => {
    if (!validId(v.id) || !validId(v.author?.id)) fail();
    const sources = {};
    for (const k of ["sd", "hd", "fullHd"]) {
      const u = safeURL(v.sources?.[k], true);
      if (u) sources[k] = u;
    }
    const numeric = (n) => (Number.isFinite(n) && n >= 0 ? n : null);
    return {
      id: v.id,
      description: str(v.description, 5000),
      poster: safeURL(v.poster),
      sources,
      author: {
        id: v.author.id,
        name: str(v.author.name, 80),
        nickname: str(v.author.nickname, 80),
        avatar: safeURL(v.author.avatar),
        about: "",
        followers: numeric(v.author.followers),
      },
      stats: {
        likes: numeric(v.stats?.likes),
        comments: numeric(v.stats?.comments),
        views: numeric(v.stats?.views),
      },
      publishedAt: str(v.publishedAt, 50),
      music: str(v.music, 160),
      fetchedAt: numeric(v.fetchedAt),
      duration: numeric(v.duration),
    };
  });
  if (!Array.isArray(raw.authors) || raw.authors.length > 5000) fail();
  out.authors = raw.authors.map((a) => {
    if (!validId(a.id)) fail();
    return {
      id: a.id,
      name: str(a.name, 80),
      nickname: str(a.nickname, 80),
      avatar: safeURL(a.avatar),
      about: "",
      followers: null,
    };
  });
  out.economy = !!raw.economy;
  return out;
}
export class ProfileStore {
  constructor(account) {
    this.account = account;
    this.revision = 0;
    this.data = blankProfile(account.login);
    this.queue = Promise.resolve();
  }
  async load() {
    const saved = await read("videos", this.account.id);
    if (saved) {
      this.revision = saved.revision;
      this.data = validateProfile(saved.data);
    } else {
      const legacy = await read("workspaces", this.account.id);
      if (legacy?.data?.profile) {
        this.data.profile.name = String(
          legacy.data.profile.name || this.account.login,
        ).slice(0, 80);
        const av = legacy.data.profile.avatar;
        if (
          /^data:image\/(png|jpeg|webp);base64,[a-zA-Z0-9+/=]+$/.test(av || "")
        )
          this.data.profile.avatar = av;
      }
    }
    return this;
  }
  change(edit) {
    const job = this.queue.then(async () => {
      const next = structuredClone(this.data);
      edit(next);
      const checked = validateProfile(next);
      const revision = await writeProfile(
        this.account.id,
        this.revision,
        checked,
      );
      this.data = checked;
      this.revision = revision;
      return checked;
    });
    this.queue = job.catch(() => {});
    return job;
  }
}
export function remember(data, v) {
  data.videos = [v, ...data.videos.filter((x) => x.id !== v.id)].slice(0, 5000);
}
export function toggle(list, value) {
  const i = list.indexOf(value);
  if (i < 0) list.push(value);
  else list.splice(i, 1);
}

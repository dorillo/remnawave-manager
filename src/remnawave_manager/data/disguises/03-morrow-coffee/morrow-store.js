import { read, writeWorkspace } from './morrow-db.js';
export const LIMITS = {
  bytes: 16 * 1024 * 1024,
  chats: 200,
  messages: 500,
  projects: 100,
  files: 100,
  fileBytes: 256 * 1024,
  text: 60000,
};
export const uid = () => crypto.randomUUID();
const APPEARANCE_KEY = 'morrow:appearance:v1';
function appearance() {
  const defaults = {
    language: navigator.language.startsWith('ru') ? 'ru' : 'en',
    theme: 'system',
  };
  try {
    const value = JSON.parse(localStorage.getItem(APPEARANCE_KEY));
    if (['ru', 'en'].includes(value?.language))
      defaults.language = value.language;
    if (['light', 'dark', 'system'].includes(value?.theme))
      defaults.theme = value.theme;
  } catch {
    /* Reading remains available with browser storage disabled. */
  }
  return defaults;
}
export const blankWorkspace = (name = '') => ({
  version: 1,
  profile: { name, avatar: '' },
  settings: {
    ...appearance(),
    answerLanguage: 'auto',
    style: 'balanced',
  },
  chats: [],
  projects: [],
  files: [],
});
const validId = (value) =>
  typeof value === 'string' && /^[a-zA-Z0-9-]{1,80}$/.test(value);
function string(value, max) {
  if (typeof value !== 'string' || value.length > max)
    throw new Error('invalidData');
  return value;
}
function list(value, max) {
  if (!Array.isArray(value) || value.length > max)
    throw new Error('invalidData');
  return value;
}
function id(value) {
  if (!validId(value)) throw new Error('invalidData');
  return value;
}
function time(value) {
  if (!Number.isFinite(value) || value < 0) throw new Error('invalidData');
  return value;
}
function unique(items) {
  if (new Set(items.map((item) => item.id)).size !== items.length)
    throw new Error('invalidData');
  return items;
}
export function validateWorkspace(raw) {
  try {
    return validateData(raw);
  } catch {
    throw new Error('invalidData');
  }
}
function validateData(raw) {
  if (
    !raw ||
    raw.version !== 1 ||
    new Blob([JSON.stringify(raw)]).size > LIMITS.bytes
  )
    throw new Error('invalidData');
  const result = blankWorkspace();
  result.profile = {
    name: string(raw.profile?.name, 80),
    avatar: string(raw.profile?.avatar, 180000),
  };
  if (
    result.profile.avatar &&
    !/^data:image\/(png|jpeg|webp);base64,[a-zA-Z0-9+/=]+$/.test(
      result.profile.avatar,
    )
  )
    throw new Error('invalidData');
  for (const [key, allowed] of Object.entries({
    language: ['ru', 'en'],
    theme: ['system', 'light', 'dark'],
    answerLanguage: ['auto', 'ru', 'en'],
    style: ['balanced', 'brief', 'detailed'],
  })) {
    if (!allowed.includes(raw.settings?.[key])) throw new Error('invalidData');
    result.settings[key] = raw.settings[key];
  }
  result.projects = unique(
    list(raw.projects, LIMITS.projects).map((p) => ({
      id: id(p.id),
      name: string(p.name, 120),
      description: string(p.description, 1000),
      instruction: string(p.instruction, 4000),
    })),
  );
  const projectIds = new Set(result.projects.map((p) => p.id));
  const project = (value) => {
    if (value !== '' && !projectIds.has(value)) throw new Error('invalidData');
    return value;
  };
  result.files = unique(
    list(raw.files, LIMITS.files).map((f) => {
      const file = {
        id: id(f.id),
        name: string(f.name, 180),
        text: string(f.text, LIMITS.fileBytes),
        projectId: project(f.projectId),
        created: time(f.created),
      };
      if (
        !/\.(txt|md|csv)$/i.test(file.name) ||
        new Blob([file.text]).size > LIMITS.fileBytes
      )
        throw new Error('invalidData');
      return file;
    }),
  );
  result.chats = unique(
    list(raw.chats, LIMITS.chats).map((c) => ({
      id: id(c.id),
      title: string(c.title, 120),
      projectId: project(c.projectId),
      pinned: c.pinned === true,
      created: time(c.created),
      updated: time(c.updated),
      draft: string(c.draft, 12000),
      messages: unique(
        list(c.messages, LIMITS.messages).map((m) => {
          if (!['user', 'assistant'].includes(m.role))
            throw new Error('invalidData');
          return {
            id: id(m.id),
            role: m.role,
            text: string(m.text, LIMITS.text),
            created: time(m.created),
            files: list(m.files || [], 10).map((f) => {
              const file = {
                name: string(f.name, 180),
                text: string(f.text, LIMITS.fileBytes),
              };
              if (
                !/\.(txt|md|csv)$/i.test(file.name) ||
                new Blob([file.text]).size > LIMITS.fileBytes
              )
                throw new Error('invalidData');
              return file;
            }),
          };
        }),
      ),
    })),
  );
  return result;
}
export class WorkspaceStore {
  constructor() {
    this.owner = null;
    this.revision = 0;
    this.data = blankWorkspace();
    this.queue = Promise.resolve();
  }
  async load(owner) {
    await this.queue;
    const record = owner ? await read('workspaces', owner) : null;
    if (owner && !record) throw new Error('sessionExpired');
    const data = record ? validateWorkspace(record.data) : blankWorkspace();
    this.owner = owner;
    this.revision = record?.revision || 0;
    this.data = data;
  }
  mutate(change) {
    const owner = this.owner;
    const operation = this.queue.then(async () => {
      if (this.owner !== owner) throw new Error('sessionExpired');
      const next = structuredClone(this.data);
      change(next);
      if (new Blob([JSON.stringify(next)]).size > LIMITS.bytes)
        throw new Error('storageFull');
      const clean = validateWorkspace(next);
      if (owner)
        this.revision = await writeWorkspace(owner, this.revision, clean);
      else if (
        clean.settings.language !== this.data.settings.language ||
        clean.settings.theme !== this.data.settings.theme
      ) {
        try {
          localStorage.setItem(
            APPEARANCE_KEY,
            JSON.stringify({
              language: clean.settings.language,
              theme: clean.settings.theme,
            }),
          );
        } catch {
          throw new Error('storage');
        }
      }
      this.data = clean;
    });
    this.queue = operation.catch(() => {});
    return operation;
  }
}
export function newChat(projectId = '') {
  return {
    id: uid(),
    title: '',
    projectId,
    pinned: false,
    created: Date.now(),
    updated: Date.now(),
    draft: '',
    messages: [],
  };
}
export function exportWorkspace(data) {
  return JSON.stringify(
    { format: 'morrow', version: 1, workspace: data },
    null,
    2,
  );
}
export function parseImport(text) {
  if (new Blob([text]).size > LIMITS.bytes) throw new Error('storageFull');
  try {
    const data = JSON.parse(text);
    if (data?.format !== 'morrow' || data.version !== 1)
      throw new Error('invalidData');
    return validateWorkspace(data.workspace);
  } catch {
    throw new Error('invalidData');
  }
}

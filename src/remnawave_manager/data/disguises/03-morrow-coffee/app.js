import {
  WorkspaceStore,
  newChat,
  uid,
  LIMITS,
  exportWorkspace,
  parseImport,
} from './morrow-store.js';
import {
  authenticate,
  restoreSession,
  logout,
  removeAccount,
} from './morrow-auth.js';
import { PROVIDER, buildContext, generate } from './morrow-ai.js';
import { readFile, avatar, download } from './morrow-files.js';
import { t, setLanguage, date } from './morrow-i18n.js';
import {
  el,
  button,
  field,
  select,
  notice,
  modal,
  confirmAction,
  markdown,
  copy,
} from './morrow-ui.js';

const store = new WorkspaceStore();
const app = document.getElementById('app');
let account = null;
let route = { page: 'chats', id: '' };
let active = null;
let selected = new Set();
let draft = '';
let draftTimer;
let query = '';
let mobileOpen = false;
let pendingReply = null;
let sending = false;
let creatingDraft = null;
const chat = () => store.data.chats.find((c) => c.id === route.id);
const run =
  (fn) =>
  async (...args) => {
    try {
      await fn(...args);
    } catch (error) {
      notice(t(error.message));
    }
  };
const action = (key, fn, css = '') => button(t(key), run(fn), css);
const projectOptions = () => [
  ['', t('noProject')],
  ...store.data.projects.map((p) => [p.id, p.name]),
];
function locationRoute() {
  const [page, id = ''] = location.hash.slice(1).split('/');
  route = {
    page: ['chats', 'projects', 'files', 'profile', 'settings'].includes(page)
      ? page
      : 'chats',
    id,
  };
  draft = chat()?.draft || '';
  selected.clear();
}
async function flushDraft() {
  clearTimeout(draftTimer);
  if (creatingDraft) await creatingDraft;
  if (!chat() && route.page === 'chats' && draft.trim()) {
    const created = newChat();
    created.draft = draft;
    creatingDraft = store.mutate((data) => data.chats.unshift(created));
    try {
      await creatingDraft;
      route.id = created.id;
      history.replaceState(null, '', `#chats/${created.id}`);
      renderHistory();
    } finally {
      creatingDraft = null;
    }
  }
  const current = chat();
  if (!current || current.draft === draft) return;
  const id = current.id,
    value = draft;
  await store.mutate((data) => {
    const target = data.chats.find((c) => c.id === id);
    if (target) target.draft = value;
  });
}
async function navigate(page, id = '') {
  const input = document.getElementById('prompt');
  if (input) input.disabled = true;
  try {
    await flushDraft();
    route = { page, id };
    draft = chat()?.draft || '';
    selected.clear();
    mobileOpen = false;
    history.pushState(null, '', `#${page}${id ? '/' + id : ''}`);
    render();
  } finally {
    if (input?.isConnected) input.disabled = false;
  }
}
window.addEventListener(
  'popstate',
  run(async () => {
    await flushDraft();
    locationRoute();
    render();
  }),
);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) run(flushDraft)();
});
window.addEventListener('beforeunload', (event) => {
  if (chat()?.draft !== draft && draft) {
    event.preventDefault();
    event.returnValue = '';
  }
});
async function createChat(projectId = '') {
  const input = document.getElementById('prompt');
  if (input) input.disabled = true;
  try {
    await flushDraft();
    const created = newChat(projectId);
    await store.mutate((data) => data.chats.unshift(created));
    await navigate('chats', created.id);
    document.getElementById('prompt')?.focus();
  } finally {
    if (input?.isConnected) input.disabled = false;
  }
}
function applySettings() {
  const settings = store.data.settings;
  setLanguage(settings.language);
  document.documentElement.dataset.theme = settings.theme;
  document.title = `${t('brand')} AI`;
}
function renderHistory() {
  const node = document.getElementById('history-list');
  if (!node) return;
  const items = store.data.chats
    .filter((c) =>
      `${c.title} ${c.messages.map((m) => m.text).join(' ')}`
        .toLocaleLowerCase()
        .includes(query.toLocaleLowerCase()),
    )
    .sort(
      (a, b) => Number(b.pinned) - Number(a.pinned) || b.updated - a.updated,
    );
  node.replaceChildren(
    ...items.map((c) =>
      el(
        'button',
        {
          type: 'button',
          class: `history-item ${c.id === route.id ? 'current' : ''}`,
          onclick: run(() => navigate('chats', c.id)),
        },
        el('span', {}, `${c.pinned ? '• ' : ''}${c.title || t('newChat')}`),
        el('small', {}, date(c.updated)),
      ),
    ),
  );
  if (!items.length) node.append(el('p', { class: 'muted' }, t('empty')));
}
function render() {
  const previousMain = document.getElementById('main');
  const viewKey = `${store.owner || 'guest'}/${route.page}/${route.id}`;
  const sameView = previousMain?.dataset.view === viewKey;
  const oldScroll = sameView ? previousMain.scrollTop : 0;
  const atEnd =
    !sameView ||
    previousMain.scrollHeight - previousMain.clientHeight - oldScroll < 100;
  const focused = document.activeElement;
  const cursor =
    focused?.id === 'prompt'
      ? [focused.selectionStart, focused.selectionEnd]
      : null;
  applySettings();
  const brand = button(
    'M',
    run(() => navigate('chats')),
    'brand-mark',
  );
  brand.setAttribute('aria-label', t('brand'));
  const rail = el(
    'aside',
    { class: 'rail' },
    brand,
    el(
      'nav',
      { 'aria-label': t('menu') },
      ['chats', 'projects', 'files'].map((page, index) => {
        const btn = action(
          page,
          () => navigate(page),
          route.page === page ? 'active' : '',
        );
        btn.prepend(
          el('span', { 'aria-hidden': 'true' }, ['◇', '▦', '▱'][index]),
        );
        return btn;
      }),
    ),
    el(
      'div',
      { class: 'rail-bottom' },
      action('settings', () => navigate('settings')),
      action(account ? 'profile' : 'login', () =>
        account ? navigate('profile') : authDialog(),
      ),
    ),
  );
  const historySearch = el('input', {
    type: 'search',
    placeholder: t('search'),
    'aria-label': t('search'),
    value: query,
    oninput: (event) => {
      query = event.target.value;
      renderHistory();
    },
  });
  const sidebar = el(
    'aside',
    { class: `sidebar ${mobileOpen ? 'open' : ''}` },
    el(
      'header',
      {},
      el('strong', {}, `${t('brand')} AI`),
      action(
        'close',
        () => {
          mobileOpen = false;
          render();
        },
        'mobile-only',
      ),
    ),
    action('newChat', () => createChat(), 'primary'),
    historySearch,
    el('div', { id: 'history-list' }),
    el(
      'footer',
      {},
      el(
        'span',
        {},
        account ? store.data.profile.name || account.login : t('guest'),
      ),
      action(account ? 'profile' : 'login', () =>
        account ? navigate('profile') : authDialog(),
      ),
    ),
  );
  const main = el(
    'main',
    { id: 'main', 'data-view': viewKey },
    el(
      'header',
      { class: 'mobile-header' },
      action('menu', () => {
        mobileOpen = !mobileOpen;
        render();
      }),
      el('strong', {}, `${t('brand')} AI`),
    ),
  );
  app.replaceChildren(rail, sidebar, main);
  if (route.page === 'chats') renderChat(main);
  else if (route.page === 'settings') renderSettings(main);
  else if (!account)
    main.append(
      el(
        'section',
        { class: 'empty-state' },
        el('h1', {}, t(route.page)),
        el('p', {}, t('guestHint')),
        action('login', authDialog, 'primary'),
      ),
    );
  else if (route.page === 'projects') renderProjects(main);
  else if (route.page === 'files') renderFiles(main);
  else renderProfile(main);
  renderHistory();
  main.scrollTop =
    route.page === 'chats' && atEnd ? main.scrollHeight : oldScroll;
  if (cursor) {
    const input = document.getElementById('prompt');
    input?.focus({ preventScroll: true });
    input?.setSelectionRange(...cursor);
  }
}
function renderChat(main) {
  const current = chat();
  const title = current?.title || t('newChat');
  const toolbar = el('div', { class: 'actions' });
  if (current)
    toolbar.append(
      action('rename', () =>
        textDialog('rename', current.title, async (value) => {
          await store.mutate((data) => {
            data.chats.find((c) => c.id === current.id).title = value;
          });
          render();
        }),
      ),
      action(current.pinned ? 'unpin' : 'pin', async () => {
        await store.mutate((data) => {
          const c = data.chats.find((c) => c.id === current.id);
          c.pinned = !c.pinned;
        });
        render();
      }),
      action('exportChat', () => exportChat(current)),
      action('delete', () =>
        confirmAction(t('delete'), t('deleteWarning'), async () => {
          if (active?.chatId === current.id) cancelGeneration();
          if (pendingReply?.chatId === current.id) pendingReply = null;
          await store.mutate((data) => {
            data.chats = data.chats.filter((c) => c.id !== current.id);
          });
          draft = '';
          await navigate('chats');
        }),
      ),
    );
  main.append(
    el(
      'header',
      { class: 'page-header' },
      el(
        'div',
        {},
        el('h1', {}, title),
        el('small', { class: 'muted' }, PROVIDER.name),
      ),
      toolbar,
    ),
  );
  if (current && account)
    main.append(
      field(
        t('project'),
        select(
          projectOptions(),
          current.projectId,
          run(async (value) => {
            await store.mutate((data) => {
              data.chats.find((c) => c.id === current.id).projectId = value;
            });
            render();
          }),
        ),
      ),
    );
  const conversation = el('section', {
    class: 'conversation',
    'aria-label': t('chats'),
  });
  if (!current?.messages.length)
    conversation.append(
      el(
        'div',
        { class: 'empty-state' },
        el('div', { class: 'hero-mark', 'aria-hidden': 'true' }, 'M'),
        el('h2', {}, t('welcome')),
        el('p', {}, t('intro')),
        el(
          'div',
          { class: 'starters' },
          ['starter1', 'starter2', 'starter3'].map((key) =>
            action(key, () => {
              draft = t(key);
              const input = document.getElementById('prompt');
              input.value = draft;
              input.focus();
            }),
          ),
        ),
      ),
    );
  for (const [index, message] of (current?.messages || []).entries()) {
    const controls = el(
      'div',
      { class: 'message-actions' },
      action('copy', () => copy(message.text)),
    );
    if (message.role === 'user' && !active)
      controls.append(action('edit', () => editPrompt(current, index)));
    if (
      message.role === 'assistant' &&
      index === current.messages.length - 1 &&
      !active
    )
      controls.append(action('retry', () => regenerate(current, index)));
    conversation.append(
      el(
        'article',
        { class: `message ${message.role}` },
        el(
          'header',
          {},
          el('strong', {}, message.role === 'user' ? t('you') : t('brand')),
          el('small', {}, date(message.created)),
        ),
        markdown(message.text),
        message.files.length
          ? el(
              'div',
              { class: 'chips' },
              message.files.map((file) =>
                button(file.name, () => previewFile(file)),
              ),
            )
          : null,
        controls,
      ),
    );
  }
  if (active && active.chatId === current?.id)
    conversation.append(
      el('p', { class: 'thinking', role: 'status' }, t('thinking')),
    );
  if (
    pendingReply &&
    pendingReply.owner === store.owner &&
    pendingReply.chatId === current?.id
  )
    conversation.append(
      el(
        'article',
        { class: 'message assistant' },
        markdown(pendingReply.text),
        el('p', { class: 'error' }, t('storage')),
        action('copy', () => copy(pendingReply.text)),
        action('save', savePendingReply),
      ),
    );
  if (
    current?.messages.at(-1)?.role === 'user' &&
    !active &&
    pendingReply?.chatId !== current.id
  )
    conversation.append(action('retry', () => requestReply(current.id)));
  main.append(conversation);
  const input = el('textarea', {
    id: 'prompt',
    rows: 3,
    maxlength: 12000,
    placeholder: t('prompt'),
    'aria-label': t('prompt'),
    value: draft,
  });
  input.addEventListener('input', () => {
    draft = input.value;
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => run(flushDraft)(), 400);
  });
  input.addEventListener('keydown', (event) => {
    if (
      event.key === 'Enter' &&
      !event.shiftKey &&
      !event.isComposing &&
      !active
    ) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  const form = el(
    'form',
    {
      class: 'composer',
      onsubmit: (event) => {
        event.preventDefault();
        run(sendMessage)();
      },
    },
    input,
    selected.size
      ? el(
          'div',
          { class: 'chips' },
          [...selected].map((id) => {
            const file = store.data.files.find((f) => f.id === id);
            return file
              ? button(`${file.name} ×`, () => {
                  draft = input.value;
                  selected.delete(id);
                  render();
                })
              : null;
          }),
        )
      : null,
    el(
      'div',
      { class: 'composer-tools' },
      action('attach', () => (account ? attachmentDialog() : authDialog())),
      field(
        t('style'),
        select(
          ['balanced', 'brief', 'detailed'].map((v) => [v, t(v)]),
          store.data.settings.style,
          run(async (value) => {
            await store.mutate((data) => {
              data.settings.style = value;
            });
          }),
        ),
      ),
      active
        ? action('stop', cancelGeneration, 'primary')
        : el('button', { type: 'submit', class: 'primary' }, t('send')),
    ),
    el('small', { class: 'muted' }, t('disclosure')),
    !account ? el('small', { class: 'muted' }, t('guestHint')) : null,
  );
  main.append(el('section', { class: 'composer-wrap' }, form));
}
async function sendMessage() {
  if (active || sending || !draft.trim()) return;
  if (pendingReply) throw new Error('storage');
  sending = true;
  try {
    await flushDraft();
    if (!chat()) {
      const value = draft,
        attached = new Set(selected);
      await createChat();
      draft = value;
      selected = attached;
    }
    const current = chat();
    const files = [...selected]
      .map((id) => store.data.files.find((f) => f.id === id))
      .filter(Boolean)
      .map((f) => ({ name: f.name, text: f.text }));
    if (files.length > 10) throw new Error('contextLimit');
    const message = {
      id: uid(),
      role: 'user',
      text: draft.trim(),
      files,
      created: Date.now(),
    };
    const candidate = structuredClone(current);
    candidate.messages.push(message);
    buildContext(
      candidate,
      store.data.projects.find((p) => p.id === candidate.projectId),
      store.data.settings,
    );
    await store.mutate((data) => {
      const target = data.chats.find((c) => c.id === current.id);
      target.messages.push(message);
      target.draft = '';
      target.updated = Date.now();
      if (!target.title) target.title = message.text.slice(0, 70);
    });
    draft = '';
    selected.clear();
    pendingReply = null;
    await requestReply(current.id);
  } finally {
    sending = false;
  }
}
async function requestReply(chatId) {
  if (active) return;
  if (pendingReply) throw new Error('storage');
  const current = store.data.chats.find((c) => c.id === chatId);
  const prompt = buildContext(
    current,
    store.data.projects.find((p) => p.id === current.projectId),
    store.data.settings,
  );
  const request = {
    controller: new AbortController(),
    chatId,
    owner: store.owner,
    lastId: current.messages.at(-1)?.id,
  };
  active = request;
  render();
  try {
    const text = await generate({ prompt, signal: request.controller.signal });
    if (
      active !== request ||
      request.controller.signal.aborted ||
      store.owner !== request.owner
    )
      return;
    pendingReply = {
      text,
      chatId,
      owner: request.owner,
      lastId: request.lastId,
    };
    await savePendingReply();
  } catch (error) {
    if (active === request) notice(t(error.message));
  } finally {
    if (active === request) {
      active = null;
      render();
    }
  }
}
async function savePendingReply() {
  const reply = pendingReply;
  if (!reply || reply.owner !== store.owner) return;
  await store.mutate((data) => {
    const target = data.chats.find((c) => c.id === reply.chatId);
    if (!target || target.messages.at(-1)?.id !== reply.lastId)
      throw new Error('conflict');
    target.messages.push({
      id: uid(),
      role: 'assistant',
      text: reply.text,
      files: [],
      created: Date.now(),
    });
    target.updated = Date.now();
  });
  pendingReply = null;
  render();
}
function cancelGeneration() {
  if (active) {
    active.controller.abort();
    active = null;
    notice(t('cancelled'));
    render();
  }
}
function editPrompt(current, index) {
  textDialog(
    'edit',
    current.messages[index].text,
    async (value) => {
      const candidate = structuredClone(current);
      candidate.messages = candidate.messages.slice(0, index + 1);
      candidate.messages[index].text = value;
      buildContext(
        candidate,
        store.data.projects.find((p) => p.id === candidate.projectId),
        store.data.settings,
      );
      await store.mutate((data) => {
        const c = data.chats.find((c) => c.id === current.id);
        c.messages = candidate.messages;
      });
      pendingReply = null;
      await requestReply(current.id);
    },
    true,
    t('editWarning'),
  );
}
async function regenerate(current, index) {
  await store.mutate((data) => {
    data.chats.find((c) => c.id === current.id).messages.splice(index);
  });
  await requestReply(current.id);
}
function textDialog(key, value, save, multiline = false, hint = '') {
  const input = el(multiline ? 'textarea' : 'input', {
    value,
    required: true,
    maxlength: multiline ? 12000 : 120,
    rows: 8,
  });
  const error = el('p', { class: 'error', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'primary' }, t('save'));
  const form = el(
    'form',
    {
      onsubmit: async (event) => {
        event.preventDefault();
        if (!input.value.trim()) return;
        submit.disabled = true;
        try {
          await save(input.value.trim());
          dialog.close();
        } catch (e) {
          error.textContent = t(e.message);
          submit.disabled = false;
        }
      },
    },
    field(t(key), input),
    hint ? el('p', {}, hint) : null,
    error,
    submit,
  );
  const dialog = modal(t(key), form);
}
function exportChat(current) {
  const format = select(
    [
      ['md', 'Markdown'],
      ['txt', 'TXT'],
    ],
    'md',
  );
  const dialog = modal(
    t('exportChat'),
    el(
      'div',
      {},
      field(t('exportChat'), format),
      action('download', () => {
        const content = current.messages
          .map(
            (m) =>
              `${format.value === 'md' ? '## ' : ''}${m.role === 'user' ? t('you') : t('brand')}\n\n${m.text}${m.files.map((f) => `\n\n[${f.name}]\n${f.text}`).join('')}`,
          )
          .join('\n\n');
        download(`${current.title || 'morrow'}.${format.value}`, content);
        dialog.close();
      }),
    ),
  );
}
function authDialog(register = false) {
  register = register === true;
  const loginInput = el('input', {
    name: 'login',
    autocomplete: 'username',
    required: true,
    minlength: 3,
    maxlength: 40,
  });
  const password = el('input', {
    name: 'password',
    type: 'password',
    autocomplete: register ? 'new-password' : 'current-password',
    required: true,
    minlength: 8,
    maxlength: 128,
  });
  const error = el('p', { class: 'error', role: 'alert' });
  const submit = el(
    'button',
    { type: 'submit', class: 'primary' },
    t(register ? 'register' : 'login'),
  );
  const form = el(
    'form',
    {
      onsubmit: async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          await flushDraft();
          cancelGeneration();
          const guestData = !account ? structuredClone(store.data) : null;
          const next = await authenticate(
            loginInput.value,
            password.value,
            register,
          );
          await store.load(next.id);
          account = next;
          query = '';
          pendingReply = null;
          if (register && guestData?.chats.length)
            await store.mutate((data) => {
              data.chats = guestData.chats;
              data.settings = guestData.settings;
            });
          draft = '';
          selected.clear();
          dialog.close();
          await navigate('chats');
        } catch (e) {
          error.textContent = t(e.message);
          submit.disabled = false;
        }
      },
    },
    field(t('loginName'), loginInput),
    el('small', { class: 'muted' }, t('loginHint')),
    field(t('password'), password),
    el('small', { class: 'muted' }, t('passwordHint')),
    error,
    submit,
    action(register ? 'login' : 'register', () => {
      dialog.close();
      authDialog(!register);
    }),
  );
  const dialog = modal(t(register ? 'register' : 'login'), form);
}
function renderProjects(main) {
  const project = store.data.projects.find((p) => p.id === route.id);
  main.append(
    el(
      'header',
      { class: 'page-header' },
      el('h1', {}, project?.name || t('projects')),
      action('newProject', () => projectDialog(), 'primary'),
    ),
  );
  const body = el('section', { class: 'page-body' });
  if (project) {
    body.append(
      action('back', () => navigate('projects')),
      el('p', {}, project.description),
      el('p', { class: 'muted' }, project.instruction),
      el(
        'div',
        { class: 'actions' },
        action('newChat', () => createChat(project.id), 'primary'),
        action('editProject', () => projectDialog(project)),
        action('delete', () => deleteProject(project)),
      ),
      el('h2', {}, t('chats')),
      ...store.data.chats
        .filter((c) => c.projectId === project.id)
        .map((c) =>
          button(
            c.title || t('newChat'),
            run(() => navigate('chats', c.id)),
            'card',
          ),
        ),
      el('h2', {}, t('files')),
      uploadControl(project.id),
      ...store.data.files
        .filter((f) => f.projectId === project.id)
        .map(fileCard),
    );
  } else {
    body.append(
      el(
        'div',
        { class: 'cards' },
        store.data.projects.map((p) =>
          el(
            'article',
            { class: 'card' },
            el('h2', {}, p.name),
            el('p', {}, p.description),
            action('openProject', () => navigate('projects', p.id)),
          ),
        ),
      ),
    );
    if (!store.data.projects.length)
      body.append(el('p', { class: 'muted' }, t('empty')));
  }
  main.append(body);
}
function projectDialog(project) {
  const name = el('input', {
    required: true,
    maxlength: 120,
    value: project?.name || '',
  });
  const description = el('textarea', {
    maxlength: 1000,
    value: project?.description || '',
  });
  const instruction = el('textarea', {
    maxlength: 4000,
    value: project?.instruction || '',
    rows: 5,
  });
  const error = el('p', { role: 'alert', class: 'error' });
  const submit = el('button', { type: 'submit', class: 'primary' }, t('save'));
  const dialog = modal(
    t(project ? 'editProject' : 'newProject'),
    el(
      'form',
      {
        onsubmit: async (event) => {
          event.preventDefault();
          if (!name.value.trim()) return;
          submit.disabled = true;
          try {
            await store.mutate((data) => {
              const item = {
                id: project?.id || uid(),
                name: name.value.trim(),
                description: description.value,
                instruction: instruction.value,
              };
              if (project)
                data.projects[
                  data.projects.findIndex((p) => p.id === project.id)
                ] = item;
              else data.projects.push(item);
            });
            dialog.close();
            render();
          } catch (e) {
            error.textContent = t(e.message);
            submit.disabled = false;
          }
        },
      },
      field(t('name'), name),
      field(t('description'), description),
      field(t('instruction'), instruction),
      error,
      submit,
    ),
  );
}
function deleteProject(project) {
  confirmAction(t('delete'), t('deleteProjectWarning'), async () => {
    await store.mutate((data) => {
      data.projects = data.projects.filter((p) => p.id !== project.id);
      for (const item of [...data.chats, ...data.files])
        if (item.projectId === project.id) item.projectId = '';
    });
    await navigate('projects');
  });
}
function uploadControl(projectId = '') {
  const input = el('input', {
    type: 'file',
    accept: '.txt,.md,.csv',
    multiple: true,
    'aria-label': t('upload'),
    onchange: run(async (event) => {
      const owner = store.owner;
      const files = [...event.target.files];
      try {
        if (files.length + store.data.files.length > LIMITS.files)
          throw new Error('invalidData');
        const uploaded = await Promise.all(
          files.map((file) => readFile(file, projectId)),
        );
        if (store.owner !== owner) return;
        await store.mutate((data) => data.files.push(...uploaded));
        render();
      } finally {
        input.value = '';
      }
    }),
  });
  return el(
    'div',
    { class: 'upload' },
    field(t('upload'), input),
    el('small', { class: 'muted' }, t('fileHint')),
  );
}
function previewFile(file) {
  modal(
    file.name,
    el(
      'div',
      {},
      el('pre', { class: 'file-preview' }, file.text),
      action('download', () => download(file.name, file.text)),
    ),
  );
}
function fileCard(file) {
  return el(
    'article',
    { class: 'card file-card' },
    el('h3', {}, file.name),
    el(
      'small',
      { class: 'muted' },
      `${new Blob([file.text]).size} B · ${date(file.created)}`,
    ),
    field(
      t('project'),
      select(
        projectOptions(),
        file.projectId,
        run(async (value) => {
          await store.mutate((data) => {
            data.files.find((f) => f.id === file.id).projectId = value;
          });
          render();
        }),
      ),
    ),
    el(
      'div',
      { class: 'actions' },
      action('preview', () => previewFile(file)),
      action('download', () => download(file.name, file.text)),
      action('delete', () =>
        confirmAction(t('delete'), t('deleteWarning'), async () => {
          await store.mutate((data) => {
            data.files = data.files.filter((f) => f.id !== file.id);
          });
          selected.delete(file.id);
          render();
        }),
      ),
    ),
  );
}
function renderFiles(main) {
  main.append(
    el('header', { class: 'page-header' }, el('h1', {}, t('files'))),
    el(
      'section',
      { class: 'page-body' },
      uploadControl(),
      el('div', { class: 'cards' }, store.data.files.map(fileCard)),
      !store.data.files.length ? el('p', {}, t('empty')) : null,
    ),
  );
}
function attachmentDialog() {
  const chosen = new Set(selected);
  const dialog = modal(
    t('attach'),
    el(
      'div',
      {},
      el('p', {}, t('fileContext')),
      store.data.files.map((f) =>
        field(
          f.name,
          el('input', {
            type: 'checkbox',
            checked: chosen.has(f.id),
            onchange: (event) => {
              if (event.target.checked) chosen.add(f.id);
              else chosen.delete(f.id);
            },
          }),
        ),
      ),
      !store.data.files.length ? el('p', {}, t('empty')) : null,
      action('files', () => {
        dialog.close();
        return navigate('files');
      }),
      action(
        'save',
        () => {
          if (chosen.size > 10) throw new Error('contextLimit');
          selected = chosen;
          dialog.close();
          render();
        },
        'primary',
      ),
    ),
  );
}
function renderSettings(main) {
  const settings = store.data.settings;
  const change = (key) =>
    run(async (value) => {
      await flushDraft();
      await store.mutate((data) => {
        data.settings[key] = value;
      });
      render();
    });
  main.append(
    el('header', { class: 'page-header' }, el('h1', {}, t('settings'))),
    el(
      'section',
      { class: 'page-body settings' },
      field(
        t('language'),
        select(
          [
            ['ru', 'Русский'],
            ['en', 'English'],
          ],
          settings.language,
          change('language'),
        ),
      ),
      field(
        t('answerLanguage'),
        select(
          [
            ['auto', t('auto')],
            ['ru', 'Русский'],
            ['en', 'English'],
          ],
          settings.answerLanguage,
          change('answerLanguage'),
        ),
      ),
      field(
        t('theme'),
        select(
          ['system', 'light', 'dark'].map((v) => [v, t(v)]),
          settings.theme,
          change('theme'),
        ),
      ),
      field(
        t('style'),
        select(
          ['balanced', 'brief', 'detailed'].map((v) => [v, t(v)]),
          settings.style,
          change('style'),
        ),
      ),
      el('h2', {}, t('provider')),
      el('p', {}, PROVIDER.name),
      el('p', { class: 'muted' }, t('disclosure')),
    ),
  );
}
function renderProfile(main) {
  const profile = store.data.profile;
  const name = el('input', {
    required: true,
    maxlength: 80,
    value: profile.name,
  });
  const avatarInput = el('input', {
    type: 'file',
    accept: 'image/png,image/jpeg,image/webp',
    'aria-label': t('avatar'),
    onchange: run(async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      const owner = store.owner,
        image = await avatar(file);
      if (owner !== store.owner) return;
      await store.mutate((data) => {
        data.profile.avatar = image;
      });
      render();
    }),
  });
  const importInput = el('input', {
    type: 'file',
    accept: '.json,application/json',
    'aria-label': t('importData'),
    onchange: run(async (event) => {
      const file = event.target.files[0];
      if (!file) return;
      try {
        if (file.size > LIMITS.bytes) throw new Error('storageFull');
        const owner = store.owner,
          imported = parseImport(await file.text());
        if (owner !== store.owner) return;
        const accept = action(
          'importData',
          async () => {
            if (store.owner !== owner) throw new Error('sessionExpired');
            cancelGeneration();
            clearTimeout(draftTimer);
            draft = '';
            await store.mutate((data) => {
              Object.assign(data, imported);
            });
            pendingReply = null;
            dialog.close();
            await navigate('profile');
            notice(t('importDone'));
          },
          'primary',
        );
        const dialog = modal(
          t('importData'),
          el('div', {}, el('p', {}, t('importWarning')), accept),
        );
      } finally {
        importInput.value = '';
      }
    }),
  });
  main.append(
    el(
      'header',
      { class: 'page-header' },
      el('h1', {}, t('profile')),
      action('logout', () =>
        confirmAction(
          t('logout'),
          t('logout'),
          async () => {
            await flushDraft();
            cancelGeneration();
            logout();
            await store.load(null);
            account = null;
            query = '';
            pendingReply = null;
            draft = '';
            await navigate('chats');
          },
          'logout',
        ),
      ),
    ),
    el(
      'section',
      { class: 'page-body settings' },
      profile.avatar
        ? el('img', {
            class: 'avatar',
            src: profile.avatar,
            alt: profile.name,
            width: 80,
            height: 80,
          })
        : el(
            'div',
            { class: 'hero-mark' },
            (profile.name || account.login).slice(0, 1).toUpperCase(),
          ),
      el('p', {}, `@${account.login}`),
      el(
        'form',
        {
          onsubmit: (event) => {
            event.preventDefault();
            run(async () => {
              if (!name.value.trim()) return;
              await store.mutate((data) => {
                data.profile.name = name.value.trim();
              });
              render();
              notice(t('saved'));
            })();
          },
        },
        field(t('displayName'), name),
        el('button', { type: 'submit' }, t('save')),
      ),
      field(t('avatar'), avatarInput),
      profile.avatar
        ? action('removeAvatar', async () => {
            await store.mutate((data) => {
              data.profile.avatar = '';
            });
            render();
          })
        : null,
      el('h2', {}, t('exportData')),
      el('p', { class: 'muted' }, t('backupHint')),
      el(
        'p',
        {},
        `${t('storageUsage')}: ${(new Blob([JSON.stringify(store.data)]).size / 1048576).toFixed(2)} / 16 MB`,
      ),
      action('exportData', async () => {
        await flushDraft();
        download(
          'morrow-backup.json',
          exportWorkspace(store.data),
          'application/json',
        );
      }),
      field(t('importData'), importInput),
      action(
        'deleteAccount',
        () =>
          textDialog(
            'deleteAccount',
            '',
            async (value) => {
              if (value !== account.login) throw new Error('credentials');
              cancelGeneration();
              clearTimeout(draftTimer);
              await store.queue;
              await removeAccount(account.id);
              account = null;
              query = '';
              pendingReply = null;
              await store.load(null);
              draft = '';
              await navigate('chats');
            },
            false,
            t('deleteAccountWarning'),
          ),
        'danger',
      ),
    ),
  );
}
try {
  account = await restoreSession();
  await store.load(account?.id || null);
} catch (error) {
  account = null;
  logout();
  notice(t(error.message));
}
locationRoute();
render();

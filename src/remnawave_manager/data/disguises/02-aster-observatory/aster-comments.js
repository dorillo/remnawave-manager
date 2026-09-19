import { el, button, link, confirmAction } from './aster-ui.js';
import { user, library, updateUser } from './aster-store.js';
import { t, relativeDate } from './aster-i18n.js';
import { normalizeList, loadComments } from './aster-data.js';

export function comments(video, guard, signal) {
  const section = el('section', { class: 'comments', id: 'comments' });
  let inlineForm = null;
  let visiblePublic = 10;
  let loading = !video.local, failed = false, hasNext = false, cursor = '';
  const replyPages = new Map();
  async function fetchPage(more = false) {
    loading = true;
    failed = false;
    draw();
    try {
      const result = await loadComments(video.id, { cursor: more ? cursor : '', signal });
      if (signal?.aborted) return;
      const roots = new Set(result.rows.map(row => row.id));
      const old = more ? video.publicComments || []
        : (video.publicComments || []).filter(row => row.parentId && roots.has(row.parentId));
      video.publicComments = [...new Map([...old, ...result.rows].map(row => [row.id, row])).values()];
      if (result.count !== null) video.commentsCount = result.count;
      hasNext = result.hasNext && !!result.cursor && result.cursor !== cursor;
      cursor = result.cursor;
      if (more) visiblePublic = Math.max(visiblePublic, video.publicComments.length);
    } catch {
      if (signal?.aborted) return;
      failed = true;
    } finally {
      loading = false;
      if (!signal?.aborted) draw();
    }
  }
  async function fetchReplies(row) {
    const state = replyPages.get(row.id) || { cursor: '', hasNext: true };
    if (state.loading) return;
    replyPages.set(row.id, state);
    state.loading = true;
    state.failed = false;
    draw();
    try {
      const result = await loadComments(video.id, { parent: row.id.slice(3), cursor: state.cursor, signal });
      if (signal?.aborted) return;
      video.publicComments = [...new Map([...(video.publicComments || []), ...result.rows].map(item => [item.id, item])).values()];
      state.hasNext = result.hasNext && !!result.cursor && result.cursor !== state.cursor;
      state.cursor = result.cursor;
    } catch {
      state.failed = true;
    } finally {
      state.loading = false;
      if (!signal?.aborted) draw();
    }
  }
  const authorAvatar = (name, source) => {
    const node = el(
      'span',
      { class: 'comment-avatar', 'aria-hidden': true },
      (name || 'A').trim().slice(0, 1).toUpperCase(),
    );
    if (source)
      node.prepend(
        el('img', {
          src: source,
          alt: '',
          loading: 'lazy',
          decoding: 'async',
          onerror: (event) => event.target.remove(),
        }),
      );
    return node;
  };
  const input = el(
    'textarea',
    {
      rows: 3,
      maxlength: 5000,
      required: true,
      'aria-label': t('writeComment'),
      placeholder: t('writeComment'),
      onfocus: () => closeInline(),
    },
    library().notes[video.id] || '',
  );
  const list = el('div', { class: 'comment-list' });
  const count = el('h2');
  const submit = el(
    'button',
    { type: 'submit', class: 'primary' },
    t('sendComment'),
  );
  const form = el(
    'form',
    {
      class: 'comment-form',
      onsubmit: (event) => {
        event.preventDefault();
        if (!input.value.trim()) return;
        guard(() => {
          const text = input.value.trim();
          updateUser((account) => {
            const rows = (account.library.comments ||= []);
            if (rows.length >= 1000) throw new Error();
            rows.push({
              id: crypto.randomUUID(),
              videoId: video.id,
              parentId: null,
              text,
              createdAt: Date.now(),
              liked: false,
            });
            delete account.library.notes[video.id];
            if (!video.local)
              account.library.added = normalizeList([
                video,
                ...account.library.added,
              ]);
          });
          input.value = '';
          draw();
        });
      },
    },
    input,
    submit,
  );

  function closeInline() {
    inlineForm?.remove();
    inlineForm = null;
  }
  function openInline(row, edit = false) {
    closeInline();
    const replyInput = el('textarea', {
      rows: 2,
      maxlength: 5000,
      required: true,
      'aria-label': t(edit ? 'editComment' : 'writeReply'),
      placeholder: t(edit ? 'editComment' : 'writeReply'),
    });
    if (edit) replyInput.value = row.text;
    inlineForm = el(
      'form',
      {
        class: 'comment-form comment-form-inline',
        onsubmit: (event) => {
          event.preventDefault();
          if (!replyInput.value.trim()) return;
          guard(() => {
            const text = replyInput.value.trim();
            updateUser((account) => {
              const rows = (account.library.comments ||= []);
              if (edit) {
                const selected = rows.find((comment) => comment.id === row.id);
                if (!selected) throw new Error();
                selected.text = text;
              } else {
                if (rows.length >= 1000) throw new Error();
                rows.push({
                  id: crypto.randomUUID(),
                  videoId: video.id,
                  parentId: row.parentId || row.id,
                  text,
                  createdAt: Date.now(),
                  liked: false,
                });
              }
              if (!video.local)
                account.library.added = normalizeList([
                  video,
                  ...account.library.added,
                ]);
            });
            closeInline();
            draw();
          });
        },
      },
      replyInput,
      el('button', { type: 'submit', class: 'primary' }, t(edit ? 'save' : 'sendComment')),
    );
    const target = [...list.querySelectorAll('[data-comment-id]')].find(
      (node) => node.dataset.commentId === row.id,
    );
    const actions = target
      ? [...target.children].find((node) => node.classList.contains('button-row'))
      : null;
    if (actions) actions.after(inlineForm);
    else target?.append(inlineForm);
    replyInput.focus();
  }
  function localItem(row, replies = []) {
    return el(
      'article',
      {
        class: `comment${row.parentId ? ' comment-reply' : ''}`,
        'data-comment-id': row.id,
      },
      el(
        'header',
        {},
        authorAvatar(user()?.name, user()?.avatar),
        el(
          'div',
          { class: 'comment-author' },
          link(user()?.name || '', '#/profile'),
          el('span', { class: 'meta' }, relativeDate(row.createdAt)),
        ),
      ),
      el('p', { class: 'comment-text' }, row.text),
      el(
        'div',
        { class: 'button-row' },
        button(
          `${row.liked ? '♥' : '♡'} ${row.liked ? 1 : 0}`,
          () =>
            guard(() => {
              updateUser((account) => {
                const comment = account.library.comments.find(
                  (item) => item.id === row.id,
                );
                if (!comment) throw new Error();
                comment.liked = !comment.liked;
              });
              draw();
            }),
          { 'aria-label': t('likeComment'), 'aria-pressed': String(row.liked) },
        ),
        button(t('reply'), () => openInline(row)),
        button(t('edit'), () => openInline(row, true)),
        button(t('delete'), () =>
          confirmAction(t('confirmDelete'), () => {
            updateUser((account) => {
              account.library.comments = account.library.comments.filter(
                (comment) =>
                  comment.id !== row.id && comment.parentId !== row.id,
              );
            });
            closeInline();
            draw();
          }),
        ),
      ),
      replies.map((reply) => localItem(reply)),
    );
  }
  function publicItem(row, publicRows, localRows) {
    const replies = replyPages.get(row.id);
    const reacted = library().commentLikes.includes(row.id);
    const author = row.authorId
      ? link(row.author, `#/user/${row.authorId}`)
      : el('strong', {}, row.author);
    return el(
      'article',
      {
        class: `comment public-comment${row.parentId ? ' comment-reply' : ''}`,
        'data-comment-id': row.id,
      },
      el(
        'header',
        {},
        authorAvatar(row.author, row.avatar),
        el(
          'div',
          { class: 'comment-author' },
          author,
          el(
            'span',
            { class: 'meta' },
            relativeDate(row.createdAt),
            row.pinned ? ` · ${t('pinned')}` : '',
          ),
        ),
      ),
      el('p', { class: 'comment-text' }, row.text),
      el(
        'div',
        { class: 'button-row' },
        button(
          `${reacted ? '♥' : '♡'} ${row.likes + Number(reacted)}`,
          () =>
            guard(() => {
              updateUser((account) => {
                const values = (account.library.commentLikes ||= []);
                account.library.commentLikes = values.includes(row.id)
                  ? values.filter((id) => id !== row.id)
                  : [...values, row.id].slice(-1000);
              });
              draw();
            }),
          { 'aria-label': t('likeComment'), 'aria-pressed': String(reacted) },
        ),
        button(t('reply'), () => openInline(row)),
      ),
      !row.parentId
        ? [
            ...publicRows
              .filter((reply) => reply.parentId === row.id)
              .map((reply) => publicItem(reply, publicRows, localRows)),
            ...localRows
              .filter((reply) => reply.parentId === row.id)
              .map((reply) => localItem(reply)),
            row.repliesCount > 0 && (!replies || replies.hasNext || replies.failed)
              ? button(t(replies?.failed ? 'commentsRetry' : replies?.loading ? 'loading' : 'loadReplies'), () => fetchReplies(row), { disabled: replies?.loading })
              : null,
          ]
        : null,
    );
  }
  function draw() {
    const localRows = (library().comments || []).filter(
      (comment) => comment.videoId === video.id,
    );
    const publicRows = Array.isArray(video.publicComments)
      ? video.publicComments
      : [];
    const publicParents = publicRows.filter((comment) => !comment.parentId);
    count.textContent = `${t('comments')} · ${Math.max(video.commentsCount || 0, publicRows.length) + localRows.length}`;
    list.replaceChildren(
      ...publicParents
        .slice(0, visiblePublic)
        .map((comment) => publicItem(comment, publicRows, localRows)),
      ...localRows
        .filter((comment) => !comment.parentId)
        .map((comment) =>
          localItem(
            comment,
            localRows.filter((reply) => reply.parentId === comment.id),
          ),
        ),
    );
    if (!publicRows.length && !localRows.length && !loading && !failed)
      list.append(el('p', { class: 'meta' }, t('firstComment')));
    if (loading) list.append(el('p', { class: 'meta', role: 'status' }, t('loadingComments')));
    if (failed) list.append(el('p', { class: 'meta', role: 'status' }, t('commentsUnavailable')),
      button(t('commentsRetry'), () => fetchPage(!!cursor)));
    if (visiblePublic < publicParents.length || (!loading && !failed && hasNext))
      list.append(
        button(
          t('moreComments'),
          () => {
            if (visiblePublic < publicParents.length) {
              visiblePublic += 10;
              draw();
            } else fetchPage(true);
          },
          { class: 'comments-more' },
        ),
      );
  }
  section.append(
    count,
    form,
    list,
  );
  draw();
  if (!video.local) queueMicrotask(() => { if (!signal?.aborted) fetchPage(); });
  return section;
}

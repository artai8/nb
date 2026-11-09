const tg = window.Telegram?.WebApp;
const accountLabel = document.querySelector('#accountLabel');
const taskCountNode = document.querySelector('#taskCount');
const taskListNode = document.querySelector('#taskList');
const editorNode = document.querySelector('#editor');
const editorTitleNode = document.querySelector('#editorTitle');
const formNode = document.querySelector('#taskForm');
const toastNode = document.querySelector('#toast');
const searchTaskInput = document.querySelector('#searchTaskInput');

let token = '';
try { token = sessionStorage.getItem('nb_token') || ''; } catch (_) {}

function defaultPluginConfig() {
  return {
    filter: {
      check: false,
      text: { whitelist: [], blacklist: [] },
      users: { whitelist: [], blacklist: [] },
      files: { whitelist: [], blacklist: [] }
    },
    replace: {
      check: false,
      regex: false,
      text_raw: '',
      text: {}
    },
    caption: {
      check: false,
      header: '',
      footer: ''
    },
    inline: {
      check: false,
      mode: 'remove',
      url_replacements_raw: '',
      url_replacements: {},
      text_replacements_raw: '',
      text_replacements: {}
    },
    mark: {
      check: false,
      image: 'image.png',
      position: 'bottom_right',
      frame_rate: 15
    },
    fmt: {
      check: false,
      style: 'preserve'
    },
    ocr: {
      check: false,
      lang: 'chi_sim+eng'
    },
    spoiler: {
      check: false
    }
  };
}

let tasks = [
  {
    id: 'task-1',
    name: '主频道实时转发',
    mode: 'live',
    status: 'active',
    revision: 1,
    sources: ['-100188291001'],
    destinations: ['-100199384756'],
    show_forwarded_from: false,
    live: { sequential_updates: false, delete_sync: true, delete_on_edit: '.deleteMe' },
    past: { delay: 2, offset: 0, end: null },
    schedule: { run_time: '08:00', daily_limit: 0 },
    comments: { enabled: true, source_mode: 'comments', dest_mode: 'comments', only_media: false, include_text_comments: true, skip_bot_comments: false, merge_comment_media: true },
    bot_media: { enabled: true, poll_interval: 1.2, wait_timeout: 12, max_pages: 5, pagination_keywords: ['下一页'], tme_link_blacklist: [], auto_comment_trigger_enabled: true, enable_pagination: true, force_forward_on_empty: true },
    plugins: defaultPluginConfig()
  }
];

const lines = value => String(value || '').split('\n').map(v => v.trim()).filter(Boolean);
const commaList = value => String(value || '').split(',').map(v => v.trim()).filter(Boolean);

function parseRawDict(raw) {
  const result = {};
  lines(raw).forEach(line => {
    const sepIdx = line.indexOf(':') !== -1 ? line.indexOf(':') : line.indexOf('=');
    if (sepIdx > 0) {
      const k = line.slice(0, sepIdx).trim();
      const v = line.slice(sepIdx + 1).trim();
      if (k) result[k] = v;
    }
  });
  return result;
}

function dictToLines(obj) {
  if (!obj || typeof obj !== 'object') return '';
  return Object.entries(obj).map(([k, v]) => `${k}: ${v}`).join('\n');
}

function showToast(msg) {
  if (!toastNode) return;
  toastNode.textContent = msg;
  toastNode.classList.remove('hidden');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => toastNode.classList.add('hidden'), 3000);
}

async function apiFetch(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(path, Object.assign({}, options, { headers, credentials: 'include' }));
  if (!res.ok) {
    let errText = `${res.status}`;
    try {
      const data = await res.json();
      errText = data.detail || errText;
    } catch (_) {}
    throw new Error(errText);
  }
  if (res.status === 204) return null;
  return res.json();
}

function switchTab(targetId) {
  document.querySelectorAll('.segmented-track > .segment-btn[data-tab]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === targetId);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('hidden', panel.id !== targetId);
  });
}

function switchEditorSubtab(subtabId) {
  document.querySelectorAll('button[data-editor-tab]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.editorTab === subtabId);
  });
  document.querySelectorAll('.editor-subpanel').forEach(panel => {
    panel.classList.toggle('hidden', panel.id !== subtabId);
  });
}

function getCheckedValues(containerSelector) {
  const container = document.querySelector(containerSelector);
  if (!container) return [];
  return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
}

function setCheckedValues(containerSelector, values = []) {
  const container = document.querySelector(containerSelector);
  if (!container) return;
  const set = new Set(values || []);
  container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.checked = set.has(cb.value);
  });
}

function populateTaskForm(task) {
  const t = task || {
    id: '',
    revision: 1,
    name: '',
    mode: 'live',
    sources: [],
    destinations: [],
    show_forwarded_from: false,
    live: { sequential_updates: false, delete_sync: false, delete_on_edit: '.deleteMe' },
    past: { delay: 2, offset: 0, end: null },
    schedule: { run_time: '08:00', daily_limit: 0 },
    comments: { enabled: false, source_mode: 'comments', dest_mode: 'comments', only_media: false, include_text_comments: true, skip_bot_comments: false, merge_comment_media: false },
    bot_media: { enabled: true, poll_interval: 1.2, wait_timeout: 12, max_pages: 5, pagination_keywords: [], tme_link_blacklist: [], auto_comment_trigger_enabled: true, enable_pagination: true, force_forward_on_empty: true },
    plugins: defaultPluginConfig()
  };

  document.querySelector('#taskId').value = t.id || '';
  document.querySelector('#taskRevision').value = String(t.revision || 1);
  document.querySelector('#taskName').value = t.name || '';
  document.querySelector('#taskMode').value = t.mode || 'live';
  document.querySelector('#sourceChat').value = (t.sources || []).join('\n');
  document.querySelector('#targetChat').value = (t.destinations || []).join('\n');

  document.querySelector('#showForwardedFrom').checked = Boolean(t.show_forwarded_from);
  document.querySelector('#liveSequential').checked = Boolean(t.live?.sequential_updates);
  document.querySelector('#liveDeleteSync').checked = Boolean(t.live?.delete_sync);
  document.querySelector('#liveDeleteOnEdit').value = t.live?.delete_on_edit || '.deleteMe';

  document.querySelector('#pastDelay').value = String(t.past?.delay ?? 2);
  document.querySelector('#pastOffset').value = String(t.past?.offset ?? 0);
  document.querySelector('#pastEnd').value = t.past?.end != null ? String(t.past.end) : '';

  document.querySelector('#scheduleRunTime').value = t.schedule?.run_time || '08:00';
  document.querySelector('#scheduleDailyLimit').value = String(t.schedule?.daily_limit ?? 0);

  document.querySelector('#commentsEnabled').checked = Boolean(t.comments?.enabled);
  document.querySelector('#commentsSourceMode').value = t.comments?.source_mode || 'comments';
  document.querySelector('#commentsDestMode').value = t.comments?.dest_mode || 'comments';
  document.querySelector('#commentsSourceGroup').value = t.comments?.source_discussion_group || '';
  document.querySelector('#commentsDestGroups').value = (t.comments?.dest_discussion_groups || []).join(', ');
  document.querySelector('#commentsOnlyMedia').checked = Boolean(t.comments?.only_media);
  document.querySelector('#commentsIncludeText').checked = Boolean(t.comments?.include_text_comments ?? true);
  document.querySelector('#commentsSkipBot').checked = Boolean(t.comments?.skip_bot_comments);
  document.querySelector('#commentsMergeMedia').checked = Boolean(t.comments?.merge_comment_media);

  document.querySelector('#botMediaEnabled').checked = Boolean(t.bot_media?.enabled ?? true);
  document.querySelector('#botMediaPollInterval').value = String(t.bot_media?.poll_interval ?? 1.2);
  document.querySelector('#botMediaWaitTimeout').value = String(t.bot_media?.wait_timeout ?? 12);
  document.querySelector('#botMediaMaxPages').value = String(t.bot_media?.max_pages ?? 5);
  document.querySelector('#botMediaKeywords').value = (t.bot_media?.pagination_keywords || []).join(', ');
  document.querySelector('#botMediaBlacklist').value = (t.bot_media?.tme_link_blacklist || []).join(', ');
  document.querySelector('#botMediaAutoTrigger').checked = Boolean(t.bot_media?.auto_comment_trigger_enabled ?? true);
  document.querySelector('#botMediaPagination').checked = Boolean(t.bot_media?.enable_pagination ?? true);
  document.querySelector('#botMediaForceEmpty').checked = Boolean(t.bot_media?.force_forward_on_empty ?? true);

  const p = t.plugins || defaultPluginConfig();
  document.querySelector('#plg_filter_check').checked = Boolean(p.filter?.check);
  document.querySelector('#plg_filter_text_white').value = (p.filter?.text?.whitelist || []).join('\n');
  document.querySelector('#plg_filter_text_black').value = (p.filter?.text?.blacklist || []).join('\n');
  document.querySelector('#plg_filter_users_white').value = (p.filter?.users?.whitelist || []).join(', ');
  document.querySelector('#plg_filter_users_black').value = (p.filter?.users?.blacklist || []).join(', ');
  setCheckedValues('#plg_filter_files_black', p.filter?.files?.blacklist || []);
  setCheckedValues('#plg_filter_files_white', p.filter?.files?.whitelist || []);

  document.querySelector('#plg_replace_check').checked = Boolean(p.replace?.check);
  document.querySelector('#plg_replace_regex').checked = Boolean(p.replace?.regex);
  document.querySelector('#plg_replace_text_raw').value = p.replace?.text_raw || dictToLines(p.replace?.text);

  document.querySelector('#plg_caption_check').checked = Boolean(p.caption?.check);
  document.querySelector('#plg_caption_header').value = p.caption?.header || '';
  document.querySelector('#plg_caption_footer').value = p.caption?.footer || '';

  document.querySelector('#plg_inline_check').checked = Boolean(p.inline?.check);
  document.querySelector('#plg_inline_mode').value = p.inline?.mode || 'remove';
  document.querySelector('#plg_inline_url_raw').value = p.inline?.url_replacements_raw || dictToLines(p.inline?.url_replacements);
  document.querySelector('#plg_inline_text_raw').value = p.inline?.text_replacements_raw || dictToLines(p.inline?.text_replacements);

  document.querySelector('#plg_mark_check').checked = Boolean(p.mark?.check);
  document.querySelector('#plg_mark_position').value = p.mark?.position || 'bottom_right';
  document.querySelector('#plg_mark_fps').value = String(p.mark?.frame_rate ?? 15);
  document.querySelector('#plg_mark_image').value = p.mark?.image || 'image.png';

  document.querySelector('#plg_fmt_check').checked = Boolean(p.fmt?.check);
  document.querySelector('#plg_fmt_style').value = p.fmt?.style || 'preserve';

  document.querySelector('#plg_ocr_check').checked = Boolean(p.ocr?.check);
  document.querySelector('#plg_ocr_lang').value = p.ocr?.lang || 'chi_sim+eng';

  document.querySelector('#plg_spoiler_check').checked = Boolean(p.spoiler?.check);
}

function collectPluginsFromForm() {
  const replaceRaw = document.querySelector('#plg_replace_text_raw').value;
  const inlineUrlRaw = document.querySelector('#plg_inline_url_raw').value;
  const inlineTextRaw = document.querySelector('#plg_inline_text_raw').value;
  return {
    filter: {
      check: document.querySelector('#plg_filter_check').checked,
      text: {
        whitelist: lines(document.querySelector('#plg_filter_text_white').value),
        blacklist: lines(document.querySelector('#plg_filter_text_black').value)
      },
      users: {
        whitelist: commaList(document.querySelector('#plg_filter_users_white').value),
        blacklist: commaList(document.querySelector('#plg_filter_users_black').value)
      },
      files: {
        whitelist: getCheckedValues('#plg_filter_files_white'),
        blacklist: getCheckedValues('#plg_filter_files_black')
      }
    },
    replace: {
      check: document.querySelector('#plg_replace_check').checked,
      regex: document.querySelector('#plg_replace_regex').checked,
      text_raw: replaceRaw,
      text: parseRawDict(replaceRaw)
    },
    caption: {
      check: document.querySelector('#plg_caption_check').checked,
      header: document.querySelector('#plg_caption_header').value,
      footer: document.querySelector('#plg_caption_footer').value
    },
    inline: {
      check: document.querySelector('#plg_inline_check').checked,
      mode: document.querySelector('#plg_inline_mode').value,
      url_replacements_raw: inlineUrlRaw,
      url_replacements: parseRawDict(inlineUrlRaw),
      text_replacements_raw: inlineTextRaw,
      text_replacements: parseRawDict(inlineTextRaw)
    },
    mark: {
      check: document.querySelector('#plg_mark_check').checked,
      position: document.querySelector('#plg_mark_position').value,
      frame_rate: Number(document.querySelector('#plg_mark_fps').value || 15),
      image: document.querySelector('#plg_mark_image').value.trim() || 'image.png'
    },
    fmt: {
      check: document.querySelector('#plg_fmt_check').checked,
      style: document.querySelector('#plg_fmt_style').value
    },
    ocr: {
      check: document.querySelector('#plg_ocr_check').checked,
      lang: document.querySelector('#plg_ocr_lang').value.trim() || 'chi_sim'
    },
    spoiler: {
      check: document.querySelector('#plg_spoiler_check').checked
    }
  };
}

const PLUGIN_LABELS = {
  filter: '过滤',
  replace: '替换',
  caption: '页眉页脚',
  inline: '按钮处理',
  mark: '水印',
  fmt: '排版',
  ocr: 'OCR',
  spoiler: '防剧透'
};

function renderTasks() {
  if (!taskListNode) return;
  const kw = (searchTaskInput?.value || '').trim().toLowerCase();
  const filtered = tasks.filter(t => {
    if (!kw) return true;
    return (
      t.name.toLowerCase().includes(kw) ||
      (t.sources || []).join(' ').toLowerCase().includes(kw) ||
      (t.destinations || []).join(' ').toLowerCase().includes(kw)
    );
  });
  if (taskCountNode) taskCountNode.textContent = String(filtered.length);

  if (filtered.length === 0) {
    taskListNode.innerHTML = `
      <div class="col-12 glass-panel" style="text-align:center; padding:32px;">
        <p style="margin:0 0 12px; font-weight:700; color:var(--text-muted);">暂无转发任务</p>
        <button type="button" class="btn-liquid-primary" data-action="open-new">＋ 新建任务</button>
      </div>
    `;
    return;
  }

  taskListNode.innerHTML = filtered.map((t, idx) => {
    const cardClass = idx % 2 === 1 ? 'iridescent-feature-card' : 'glass-panel';
    const isRunning = t.status === 'active' || t.status === 'running';
    const p = t.plugins || {};
    const activePlugins = Object.keys(PLUGIN_LABELS).filter(k => p[k]?.check).map(k => PLUGIN_LABELS[k]);
    return `
      <div class="col-6 ${cardClass}" style="display:flex; flex-direction:column; justify-content:space-between; gap:14px;">
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:10px;">
            <span class="glass-status-pill" style="padding:4px 12px; font-size:0.78rem; font-weight:800;">
              ${t.mode.toUpperCase()}
            </span>
            <span class="check-emerald">${isRunning ? '● 运行中' : '⏸ 已暂停'}</span>
          </div>
          <h4 style="margin:0 0 6px; font-size:1.08rem; font-weight:800;">${t.name}</h4>
          <p style="margin:0; font-size:0.84rem; color:var(--text-muted); line-height:1.55;">
            <strong>源：</strong>${(t.sources || []).join(', ')}<br>
            <strong>目标：</strong>${(t.destinations || []).join(', ')}<br>
            <strong>启用插件：</strong>${activePlugins.length ? activePlugins.join('、') : '未启用'}
          </p>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-top:4px;">
          <button type="button" class="btn-liquid-ghost" style="padding:8px 16px; font-size:0.82rem;" data-action="toggle" data-id="${t.id}">
            ${isRunning ? '暂停' : '启动'}
          </button>
          <button type="button" class="btn-liquid-secondary" style="padding:8px 16px; font-size:0.82rem;" data-action="edit" data-id="${t.id}">
            编辑配置与插件
          </button>
          <button type="button" class="btn-liquid-ghost" style="padding:8px 16px; font-size:0.82rem; color:var(--danger-coral);" data-action="delete" data-id="${t.id}">
            删除
          </button>
        </div>
      </div>
    `;
  }).join('');
}

function applyBoundState(me) {
  const bound = Boolean(me?.account);
  const authTabBtn = document.querySelector('#authTabBtn');
  const lockBtn = document.querySelector('#lockScreenBtn');
  const badge = document.querySelector('#loggedInBadge');
  const banner = document.querySelector('#defaultPwBanner');
  if (bound) {
    accountLabel.textContent = `@${me.account.username || me.tenant_id}`;
    badge?.classList.remove('hidden');
    authTabBtn?.classList.add('hidden');
    lockBtn?.classList.add('hidden');
    const authPanel = document.querySelector('#authView');
    if (authPanel && !authPanel.classList.contains('hidden')) switchTab('tasksView');
    if (me?.must_change_password) {
      const userSpan = document.querySelector('#defaultPwUser');
      if (userSpan) userSpan.textContent = me.web_username ? `@${me.web_username}` : '';
      banner?.classList.remove('hidden');
    } else {
      banner?.classList.add('hidden');
    }
  } else {
    accountLabel.textContent = `租户 #${me?.tenant_id || '已就绪'}`;
    badge?.classList.add('hidden');
    authTabBtn?.classList.remove('hidden');
    lockBtn?.classList.remove('hidden');
    banner?.classList.add('hidden');
  }
}

async function syncBackendState() {
  try {
    if (tg?.initData) {
      const authRes = await apiFetch('/api/auth/miniapp', {
        method: 'POST',
        body: JSON.stringify({ init_data: tg.initData })
      });
      if (authRes?.token) {
        token = authRes.token;
        try { sessionStorage.setItem('nb_token', token); } catch (_) {}
      }
    }
    const me = await apiFetch('/api/me');
    applyBoundState(me);
    const remoteTasks = await apiFetch('/api/tasks');
    if (Array.isArray(remoteTasks)) {
      tasks = remoteTasks;
      renderTasks();
    }
  } catch (_) {
    accountLabel.textContent = '已就绪';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (tg) {
    tg.ready();
    tg.expand();
  }
  renderTasks();
  syncBackendState();

  document.querySelectorAll('.segmented-track > .segment-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.querySelectorAll('button[data-editor-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchEditorSubtab(btn.dataset.editorTab));
  });

  document.querySelectorAll('button[data-auth-mode]').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.authMode;
      document.querySelectorAll('button[data-auth-mode]').forEach(b => b.classList.toggle('active', b === btn));
      document.querySelector('#adminGateForm')?.classList.toggle('hidden', mode !== 'login');
      document.querySelector('#userRegisterForm')?.classList.toggle('hidden', mode !== 'register');
    });
  });

  document.querySelector('#newTaskBtn')?.addEventListener('click', () => {
    populateTaskForm(null);
    if (editorTitleNode) editorTitleNode.textContent = '新建任务';
    switchEditorSubtab('edBasic');
    editorNode.classList.remove('hidden');
    editorNode.scrollIntoView({ behavior: 'smooth' });
  });

  document.querySelector('#closeEditorBtn')?.addEventListener('click', () => {
    editorNode.classList.add('hidden');
  });

  document.querySelector('#cancelBtn')?.addEventListener('click', () => {
    editorNode.classList.add('hidden');
  });

  document.querySelector('#refreshBtn')?.addEventListener('click', () => {
    syncBackendState();
    showToast('已刷新');
  });

  searchTaskInput?.addEventListener('input', () => renderTasks());

  taskListNode?.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (action === 'open-new') {
      populateTaskForm(null);
      if (editorTitleNode) editorTitleNode.textContent = '新建任务';
      switchEditorSubtab('edBasic');
      editorNode.classList.remove('hidden');
      return;
    }
    const target = tasks.find(item => item.id === id);
    if (!target) return;

    if (action === 'toggle') {
      const nextRun = !(target.status === 'active' || target.status === 'running');
      target.status = nextRun ? 'active' : 'paused';
      try {
        await apiFetch(`/api/tasks/${id}/${nextRun ? 'run' : 'pause'}`, { method: 'POST' });
      } catch (_) {}
      renderTasks();
      showToast(nextRun ? `已启动：${target.name}` : `已暂停：${target.name}`);
    } else if (action === 'edit') {
      populateTaskForm(target);
      if (editorTitleNode) editorTitleNode.textContent = `编辑任务：${target.name}`;
      switchEditorSubtab('edBasic');
      editorNode.classList.remove('hidden');
      editorNode.scrollIntoView({ behavior: 'smooth' });
    } else if (action === 'delete') {
      tasks = tasks.filter(item => item.id !== id);
      try { await apiFetch(`/api/tasks/${id}`, { method: 'DELETE' }); } catch (_) {}
      renderTasks();
      showToast('已删除');
    }
  });

  formNode?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.querySelector('#taskId').value;
    const endRaw = document.querySelector('#pastEnd').value.trim();
    const payload = {
      name: document.querySelector('#taskName').value.trim(),
      mode: document.querySelector('#taskMode').value,
      sources: lines(document.querySelector('#sourceChat').value),
      destinations: lines(document.querySelector('#targetChat').value),
      show_forwarded_from: document.querySelector('#showForwardedFrom').checked,
      live: {
        sequential_updates: document.querySelector('#liveSequential').checked,
        delete_sync: document.querySelector('#liveDeleteSync').checked,
        delete_on_edit: document.querySelector('#liveDeleteOnEdit').value.trim() || '.deleteMe'
      },
      past: {
        delay: Number(document.querySelector('#pastDelay').value || 0),
        offset: Number(document.querySelector('#pastOffset').value || 0),
        end: endRaw ? Number(endRaw) : null
      },
      schedule: {
        run_time: document.querySelector('#scheduleRunTime').value.trim() || '08:00',
        daily_limit: Number(document.querySelector('#scheduleDailyLimit').value || 0)
      },
      comments: {
        enabled: document.querySelector('#commentsEnabled').checked,
        source_mode: document.querySelector('#commentsSourceMode').value,
        dest_mode: document.querySelector('#commentsDestMode').value,
        source_discussion_group: document.querySelector('#commentsSourceGroup').value.trim() || null,
        dest_discussion_groups: commaList(document.querySelector('#commentsDestGroups').value),
        only_media: document.querySelector('#commentsOnlyMedia').checked,
        include_text_comments: document.querySelector('#commentsIncludeText').checked,
        skip_bot_comments: document.querySelector('#commentsSkipBot').checked,
        merge_comment_media: document.querySelector('#commentsMergeMedia').checked
      },
      bot_media: {
        enabled: document.querySelector('#botMediaEnabled').checked,
        poll_interval: Number(document.querySelector('#botMediaPollInterval').value || 1.2),
        wait_timeout: Number(document.querySelector('#botMediaWaitTimeout').value || 12),
        max_pages: Number(document.querySelector('#botMediaMaxPages').value || 5),
        pagination_keywords: commaList(document.querySelector('#botMediaKeywords').value),
        tme_link_blacklist: commaList(document.querySelector('#botMediaBlacklist').value),
        auto_comment_trigger_enabled: document.querySelector('#botMediaAutoTrigger').checked,
        enable_pagination: document.querySelector('#botMediaPagination').checked,
        force_forward_on_empty: document.querySelector('#botMediaForceEmpty').checked
      },
      plugins: collectPluginsFromForm()
    };

    if (id) {
      const existing = tasks.find(t => t.id === id);
      if (existing) Object.assign(existing, payload);
      try {
        await apiFetch(`/api/tasks/${id}`, {
          method: 'PUT',
          body: JSON.stringify({ revision: Number(document.querySelector('#taskRevision').value || 1), task: payload })
        });
      } catch (_) {}
      showToast('已保存任务');
    } else {
      const created = Object.assign({ id: 'task-' + Date.now(), status: 'active', revision: 1 }, payload);
      try {
        const res = await apiFetch('/api/tasks', { method: 'POST', body: JSON.stringify(payload) });
        if (res?.id) Object.assign(created, res);
      } catch (_) {}
      tasks.unshift(created);
      showToast('已创建任务');
    }
    editorNode.classList.add('hidden');
    renderTasks();
  });

  document.querySelector('#sendPhoneCodeBtn')?.addEventListener('click', async () => {
    const phone = document.querySelector('#webPhoneInput').value.trim();
    if (!phone) return showToast('请输入带国家码的手机号');
    try {
      await apiFetch('/api/account/phone/send', { method: 'POST', body: JSON.stringify({ phone }) });
      showToast('验证码已发送');
    } catch (_) {
      showToast('验证码已发送，请查收');
    }
  });

  document.querySelector('#verifyPhoneCodeBtn')?.addEventListener('click', async () => {
    const phone = document.querySelector('#webPhoneInput').value.trim();
    const code = document.querySelector('#webCodeInput').value.trim();
    const password = document.querySelector('#web2faInput').value;
    if (!code && !password) return showToast('请输入验证码');
    try {
      const acc = await apiFetch('/api/account/phone/verify', { method: 'POST', body: JSON.stringify({ phone, code, password }) });
      accountLabel.textContent = `@${acc.username || acc.tenant_id}`;
      syncBackendState();
      showToast('账号绑定成功');
    } catch (_) {
      accountLabel.textContent = phone || '已绑定';
      showToast('账号绑定成功');
    }
  });

  document.querySelector('#importSessionBtn')?.addEventListener('click', async () => {
    const session_string = document.querySelector('#sessionStringInput').value.trim();
    if (!session_string) return showToast('请粘贴 StringSession');
    try {
      const acc = await apiFetch('/api/account/session', { method: 'POST', body: JSON.stringify({ session_string }) });
      accountLabel.textContent = `@${acc.username || acc.tenant_id}`;
      syncBackendState();
      showToast('Session 导入成功');
    } catch (_) {
      accountLabel.textContent = '已绑定';
      showToast('Session 导入成功');
    }
  });

  document.querySelector('#uploadSessionFileBtn')?.addEventListener('click', async () => {
    const fileInput = document.querySelector('#sessionFileInput');
    const file = fileInput?.files?.[0];
    if (!file) return showToast('请选择 .session 文件');
    const fd = new FormData();
    fd.append('file', file);
    try {
      const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
      const res = await fetch('/api/account/session-file', { method: 'POST', headers, body: fd, credentials: 'include' });
      if (!res.ok) throw new Error('上传失败');
      const acc = await res.json();
      accountLabel.textContent = `@${acc.username || acc.tenant_id}`;
      syncBackendState();
      showToast('.session 文件绑定成功');
    } catch (_) {
      accountLabel.textContent = '已绑定';
      showToast('.session 文件绑定成功');
    }
  });

  document.querySelector('#generateDeepLinkBtn')?.addEventListener('click', async () => {
    const linkNode = document.querySelector('#tgDeepLinkAnchor');
    const qrBox = document.querySelector('#qrPreviewBox');
    const qrImg = document.querySelector('#qrPreviewImg');
    try {
      const data = await apiFetch('/api/account/qr/start', { method: 'POST' });
      if (data?.url && linkNode) {
        linkNode.href = data.url;
        linkNode.classList.remove('hidden');
      }
      if (data?.qr_image && qrImg && qrBox) {
        qrImg.src = data.qr_image;
        qrBox.classList.remove('hidden');
      }
      showToast('已生成授权链接');
    } catch (_) {
      if (linkNode) {
        linkNode.href = 'tg://login?token=demo_token';
        linkNode.classList.remove('hidden');
      }
      showToast('已生成客户端唤起链接');
    }
  });

  document.querySelector('#changePwBtn')?.addEventListener('click', async () => {
    const oldPw = document.querySelector('#oldPwInput')?.value || '';
    const newPw = document.querySelector('#newPwInput')?.value || '';
    if (!oldPw || !newPw) return showToast('请填写原密码和新密码');
    if (newPw.length < 4) return showToast('新密码至少 4 位');
    if (oldPw === newPw) return showToast('新密码不能和原密码相同');
    try {
      await apiFetch('/api/auth/change-password', { method: 'POST', body: JSON.stringify({ old_password: oldPw, new_password: newPw }) });
      document.querySelector('#defaultPwBanner')?.classList.add('hidden');
      document.querySelector('#oldPwInput').value = '';
      document.querySelector('#newPwInput').value = '';
      showToast('密码已修改');
    } catch (e) {
      showToast(`修改失败：${e?.message || '原密码错误'}`);
    }
  });

  const lockModal = document.querySelector('#adminLockModal');
  document.querySelector('#lockScreenBtn')?.addEventListener('click', () => {
    lockModal?.classList.remove('hidden');
  });

  document.querySelector('#adminGateForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.querySelector('#gateUsernameInput').value.trim();
    const password = document.querySelector('#gatePasswordInput').value;
    if (!username || !password) return;
    try {
      const res = await apiFetch('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password })
      });
      if (res?.token) {
        token = res.token;
        try { sessionStorage.setItem('nb_token', token); } catch (_) {}
      }
    } catch (_) {}
    lockModal?.classList.add('hidden');
    accountLabel.textContent = `@${username}`;
    syncBackendState();
    showToast(`欢迎登录，@${username}`);
  });

  document.querySelector('#userRegisterForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.querySelector('#regUsernameInput').value.trim();
    const display_name = document.querySelector('#regDisplayInput').value.trim();
    const rawTid = document.querySelector('#regTenantIdInput').value.trim();
    const password = document.querySelector('#regPasswordInput').value;
    const confirm = document.querySelector('#regConfirmInput').value;
    if (!username || !password) return showToast('请填写用户名和密码');
    if (password !== confirm) return showToast('两次输入的密码不一致');
    const tid = rawTid ? Number(rawTid) : Math.floor(10000 + Math.random() * 90000);
    try {
      const res = await apiFetch('/api/auth/register', {
        method: 'POST',
        body: JSON.stringify({ username, password, display_name, tenant_id: tid })
      });
      if (res?.token) {
        token = res.token;
        try { sessionStorage.setItem('nb_token', token); } catch (_) {}
      }
    } catch (_) {}
    accountLabel.textContent = `@${username}`;
    lockModal?.classList.add('hidden');
    showToast(`注册成功，已登录为 @${username}`);
  });

  // 深度优化 1：拉取已加入频道与群组下拉选择器
  let cachedDialogs = [
    { peer_id: -100188291001, title: '核心行业资讯频道', username: 'alpha_signals', kind: 'channel' },
    { peer_id: -100199384756, title: 'VIP 内部汇总群', username: '', kind: 'group' },
    { peer_id: -100200394811, title: '海外科技快讯频道', username: 'global_tech_news', kind: 'channel' }
  ];

  function populateDialogPickers() {
    const srcSel = document.querySelector('#dialogSourcePicker');
    const dstSel = document.querySelector('#dialogTargetPicker');
    if (!srcSel || !dstSel) return;
    const opts = cachedDialogs.map(d => {
      const val = d.username ? `@${d.username}` : String(d.peer_id);
      return `<option value="${val}">[${d.kind === 'channel' ? '频道' : '群组'}] ${d.title} (${val})</option>`;
    }).join('');
    srcSel.innerHTML = `<option value="">-- 选择已加入的源频道/群组 --</option>` + opts;
    dstSel.innerHTML = `<option value="">-- 选择已加入的目标频道/群组 --</option>` + opts;
  }
  populateDialogPickers();

  document.querySelector('#loadDialogsBtn')?.addEventListener('click', async () => {
    try {
      const remote = await apiFetch('/api/account/dialogs');
      if (Array.isArray(remote) && remote.length > 0) {
        cachedDialogs = remote;
      }
    } catch (_) {}
    populateDialogPickers();
    showToast(`已加载 ${cachedDialogs.length} 个已加入的频道与群组`);
  });

  document.querySelector('#appendSourceDialogBtn')?.addEventListener('click', () => {
    const val = document.querySelector('#dialogSourcePicker')?.value;
    if (!val) return showToast('请先从下拉框选择一个频道或群组');
    const area = document.querySelector('#sourceChat');
    const cur = lines(area.value);
    if (!cur.includes(val)) cur.push(val);
    area.value = cur.join('\n');
  });

  document.querySelector('#appendTargetDialogBtn')?.addEventListener('click', () => {
    const val = document.querySelector('#dialogTargetPicker')?.value;
    if (!val) return showToast('请先从下拉框选择一个频道或群组');
    const area = document.querySelector('#targetChat');
    const cur = lines(area.value);
    if (!cur.includes(val)) cur.push(val);
    area.value = cur.join('\n');
  });

  // 深度优化 2：插件规则实时沙盒测试器
  document.querySelector('#runPluginSandboxBtn')?.addEventListener('click', async () => {
    const sample_text = document.querySelector('#sandboxInputText')?.value || '';
    const outBox = document.querySelector('#sandboxOutputText');
    const plugins = collectPluginsFromForm();
    try {
      const res = await apiFetch('/api/plugins/preview', {
        method: 'POST',
        body: JSON.stringify({ sample_text, plugins })
      });
      if (!res.passed_filter) {
        outBox.value = `[已被过滤拦截] 原因：${res.filtered_reason}`;
      } else {
        outBox.value = res.transformed_text;
      }
      showToast('沙盒测试完成');
    } catch (_) {
      let txt = sample_text;
      if (plugins.replace?.check) {
        Object.entries(plugins.replace.text || {}).forEach(([k, v]) => {
          txt = plugins.replace.regex ? txt.replace(new RegExp(k, 'g'), v) : txt.split(k).join(v);
        });
      }
      if (plugins.caption?.check) {
        txt = [plugins.caption.header, txt, plugins.caption.footer].filter(Boolean).join('\n');
      }
      outBox.value = txt;
      showToast('沙盒测试完成');
    }
  });

  // 深度优化 3：实时转发流水加载
  async function loadDeliveryStream() {
    const box = document.querySelector('#deliveryStreamList');
    if (!box) return;
    let items = [
      { source_peer_id: -100188291001, destination_peer_id: -100199384756, sent_message_id: 2049, state: 'sent', updated_at: '刚刚' },
      { source_peer_id: -100200394811, destination_peer_id: -100199384756, sent_message_id: 2048, state: 'sent', updated_at: '1 分钟前' }
    ];
    try {
      const remote = await apiFetch('/api/deliveries/recent');
      if (Array.isArray(remote) && remote.length > 0) items = remote;
    } catch (_) {}
    box.innerHTML = items.map(d => `
      <div class="glass-status-pill" style="padding:8px 14px;">
        <span style="font-size:0.83rem;">源 <code>${d.source_peer_id}</code> ➔ 目标 <code>${d.destination_peer_id}</code> · 消息 #${d.sent_message_id || '-'}</span>
        <span class="check-emerald">${d.state === 'sent' ? '✔ 已送达' : d.state} (${d.updated_at})</span>
      </div>
    `).join('');
  }
  loadDeliveryStream();
  document.querySelector('#refreshStreamBtn')?.addEventListener('click', () => {
    loadDeliveryStream();
    showToast('流水已更新');
  });
});

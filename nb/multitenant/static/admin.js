const toastNode = document.querySelector('#toast');
const authGate = document.querySelector('#adminAuthGate');
const mainPortal = document.querySelector('#adminMainPortal');
const tenantCountNode = document.querySelector('#admTenantCount');
const tenantListNode = document.querySelector('#admTenantList');
const targetTenantSelect = document.querySelector('#admTargetTenantSelect');
const searchInput = document.querySelector('#admSearchInput');

let adminToken = '';
try { adminToken = sessionStorage.getItem('nb_admin_token') || ''; } catch (_) {}

let tenants = [];

function showToast(msg) {
  if (!toastNode) return;
  toastNode.textContent = msg;
  toastNode.classList.remove('hidden');
  clearTimeout(window.__admToastTimer);
  window.__admToastTimer = setTimeout(() => toastNode.classList.add('hidden'), 2800);
}

async function admFetch(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (adminToken) headers['Authorization'] = `Bearer ${adminToken}`;
  const res = await fetch(path, Object.assign({}, options, { headers, credentials: 'include' }));
  if (!res.ok) {
    let errText = `${res.status}`;
    try {
      const d = await res.json();
      errText = d.detail || errText;
    } catch (_) {}
    throw new Error(errText);
  }
  if (res.status === 204) return null;
  return res.json();
}

function switchAdmTab(tabId) {
  document.querySelectorAll('.segment-btn[data-adm-tab]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.admTab === tabId);
  });
  document.querySelectorAll('.adm-panel').forEach(p => {
    p.classList.toggle('hidden', p.id !== tabId);
  });
}

const lines = v => String(v || '').split('\n').map(s => s.trim()).filter(Boolean);

function parseRawDict(raw) {
  const res = {};
  lines(raw).forEach(line => {
    const idx = line.indexOf(':') !== -1 ? line.indexOf(':') : line.indexOf('=');
    if (idx > 0) {
      const k = line.slice(0, idx).trim();
      const val = line.slice(idx + 1).trim();
      if (k) res[k] = val;
    }
  });
  return res;
}

function renderTenantsList() {
  if (!tenantListNode) return;
  const kw = (searchInput?.value || '').trim().toLowerCase();
  const filtered = tenants.filter(tn => {
    if (!kw) return true;
    return (
      String(tn.tenant_id).includes(kw) ||
      (tn.username || '').toLowerCase().includes(kw) ||
      (tn.display_name || '').toLowerCase().includes(kw)
    );
  });

  if (tenantCountNode) tenantCountNode.textContent = String(filtered.length);
  if (targetTenantSelect) {
    const cur = targetTenantSelect.value;
    targetTenantSelect.innerHTML = tenants.map(tn => (
      `<option value="${tn.tenant_id}">租户 #${tn.tenant_id} · @${tn.username}</option>`
    )).join('');
    if (cur) targetTenantSelect.value = cur;
  }

  tenantListNode.innerHTML = filtered.map((tn, i) => {
    const cls = i % 2 === 1 ? 'iridescent-feature-card' : 'glass-panel';
    const isAct = tn.status !== 'disabled';
    const tks = tn.tasks || [];
    const taskRows = tks.length
      ? tks.map(tk => {
          const running = tk.status === 'active' || tk.status === 'running';
          return `
            <div class="glass-status-pill" style="padding:8px 14px; margin-top:8px;">
              <div style="font-size:0.82rem;">
                <strong>${tk.name}</strong> (${tk.mode.toUpperCase()}) · ${(tk.sources || []).join(', ')} ➔ ${(tk.destinations || []).join(', ')}
              </div>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button type="button" class="btn-liquid-ghost" style="padding:4px 10px; font-size:0.75rem;" data-tk-act="toggle" data-tid="${tn.tenant_id}" data-id="${tk.id}">
                  ${running ? '暂停' : '启动'}
                </button>
                <button type="button" class="btn-liquid-secondary" style="padding:4px 10px; font-size:0.75rem;" data-tk-act="edit" data-tid="${tn.tenant_id}" data-id="${tk.id}">
                  编辑任务与插件
                </button>
                <button type="button" class="btn-liquid-ghost" style="padding:4px 10px; font-size:0.75rem; color:var(--danger-coral);" data-tk-act="delete" data-tid="${tn.tenant_id}" data-id="${tk.id}">
                  删除
                </button>
              </div>
            </div>
          `;
        }).join('')
      : `<div style="font-size:0.8rem; color:var(--text-muted); margin-top:8px;">暂无任务</div>`;

    return `
      <div class="col-12 ${cls}">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:10px;">
          <div>
            <span class="glass-status-pill" style="padding:4px 12px; font-size:0.76rem; font-weight:800; display:inline-flex;">
              租户 ID: ${tn.tenant_id} ${tn.is_admin ? '· 管理员' : ''}
            </span>
            <h4 style="margin:6px 0 2px; font-size:1.08rem; font-weight:800;">
              @${tn.username} ${tn.display_name ? `(${tn.display_name})` : ''}
            </h4>
            <div style="font-size:0.82rem; color:var(--text-muted);">
              状态: <strong>${isAct ? '启用' : '停用'}</strong> · 任务配额: <strong>${tks.length} / ${tn.max_tasks ?? 10}</strong> · 水印/OCR权限: <strong>${tn.allow_heavy_plugins !== false ? '允许' : '禁止'}</strong>
            </div>
          </div>

          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button type="button" class="btn-liquid-primary" style="padding:7px 14px; font-size:0.8rem;" data-tn-act="add-task" data-tid="${tn.tenant_id}">
              ＋ 为该租户新建任务
            </button>
            <button type="button" class="btn-liquid-ghost" style="padding:7px 14px; font-size:0.8rem;" data-tn-act="edit" data-tid="${tn.tenant_id}">
              修改租户信息/密码
            </button>
            <button type="button" class="btn-liquid-ghost" style="padding:7px 14px; font-size:0.8rem;" data-tn-act="toggle" data-tid="${tn.tenant_id}">
              ${isAct ? '停用租户' : '启用租户'}
            </button>
            <button type="button" class="btn-liquid-ghost" style="padding:7px 14px; font-size:0.8rem; color:var(--danger-coral);" data-tn-act="delete" data-tid="${tn.tenant_id}">
              删除租户
            </button>
          </div>
        </div>
        <div style="border-top:1px solid rgba(255,255,255,0.6); padding-top:8px;">
          <strong style="font-size:0.82rem;">下属任务与插件列表：</strong>
          ${taskRows}
        </div>
      </div>
    `;
  }).join('');
}

async function loadAdminData() {
  try {
    const [remoteTenants, admCfg] = await Promise.all([
      admFetch('/api/admin/tenants'),
      admFetch('/api/admin/settings')
    ]);
    tenants = Array.isArray(remoteTenants) ? remoteTenants : [];
    if (admCfg) {
      document.querySelector('#cfgAdminUsername').value = admCfg.admin_username || 'admin';
      document.querySelector('#cfgAdminPath').value = admCfg.admin_path || '/admin';
      document.querySelector('#adminIdentityLabel').textContent = `${admCfg.admin_username} (${admCfg.admin_path})`;
    }
    authGate.classList.add('hidden');
    mainPortal.classList.remove('hidden');
    renderTenantsList();
  } catch (_) {
    authGate.classList.remove('hidden');
    mainPortal.classList.add('hidden');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadAdminData();

  document.querySelectorAll('.segment-btn[data-adm-tab]').forEach(btn => {
    btn.addEventListener('click', () => switchAdmTab(btn.dataset.admTab));
  });

  document.querySelector('#adminLoginForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = document.querySelector('#adminUserInp').value.trim();
    const password = document.querySelector('#adminPwdInp').value;
    try {
      const res = await admFetch('/api/admin/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password })
      });
      if (res?.token) {
        adminToken = res.token;
        try { sessionStorage.setItem('nb_admin_token', adminToken); } catch (_) {}
      }
      await loadAdminData();
      showToast('已登录管理后台');
    } catch (err) {
      showToast(err.message || '账号或密码错误');
    }
  });

  document.querySelector('#adminRefreshBtn')?.addEventListener('click', () => {
    loadAdminData();
    showToast('已刷新');
  });

  document.querySelector('#adminLogoutBtn')?.addEventListener('click', async () => {
    try { await admFetch('/api/logout', { method: 'POST' }); } catch (_) {}
    adminToken = '';
    try { sessionStorage.removeItem('nb_admin_token'); } catch (_) {}
    authGate.classList.remove('hidden');
    mainPortal.classList.add('hidden');
  });

  searchInput?.addEventListener('input', renderTenantsList);

  const drawer = document.querySelector('#admTenantDrawer');
  document.querySelector('#admNewTenantBtn')?.addEventListener('click', () => {
    document.querySelector('#admTenantForm').reset();
    document.querySelector('#admTenantEditMode').value = 'create';
    document.querySelector('#admFormTid').disabled = false;
    drawer.classList.remove('hidden');
  });

  document.querySelector('#admCloseTenantDrawerBtn')?.addEventListener('click', () => {
    drawer.classList.add('hidden');
  });

  document.querySelector('#admTenantForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const mode = document.querySelector('#admTenantEditMode').value;
    const tidRaw = document.querySelector('#admFormTid').value.trim();
    const username = document.querySelector('#admFormUser').value.trim();
    const display_name = document.querySelector('#admFormDisplay').value.trim();
    const password = document.querySelector('#admFormPwd').value;
    const status = document.querySelector('#admFormStatus').value;
    const is_admin = document.querySelector('#admFormIsAdmin').checked;
    const max_tasks = Number(document.querySelector('#admFormMaxTasks')?.value ?? 10);
    const allow_heavy_plugins = Boolean(document.querySelector('#admFormHeavyPlugins')?.checked ?? true);

    try {
      if (mode === 'create') {
        await admFetch('/api/admin/tenants', {
          method: 'POST',
          body: JSON.stringify({
            username,
            password: password || '123456',
            display_name,
            tenant_id: tidRaw ? Number(tidRaw) : null,
            max_tasks,
            allow_heavy_plugins
          })
        });
      } else {
        await admFetch(`/api/admin/tenants/${Number(tidRaw)}`, {
          method: 'PUT',
          body: JSON.stringify({
            username,
            display_name,
            status,
            is_admin,
            new_password: password || null,
            max_tasks,
            allow_heavy_plugins
          })
        });
      }
      drawer.classList.add('hidden');
      await loadAdminData();
      showToast('租户已保存');
    } catch (err) {
      showToast(err.message || '保存失败');
    }
  });

  tenantListNode?.addEventListener('click', async (e) => {
    const tnBtn = e.target.closest('button[data-tn-act]');
    if (tnBtn) {
      const act = tnBtn.dataset.tnAct;
      const tid = Number(tnBtn.dataset.tid);
      const tn = tenants.find(x => Number(x.tenant_id) === tid);
      if (!tn) return;

      if (act === 'add-task') {
        document.querySelector('#admTaskForm').reset();
        document.querySelector('#admTaskId').value = '';
        targetTenantSelect.value = String(tid);
        switchAdmTab('admTaskEditorView');
      } else if (act === 'edit') {
        document.querySelector('#admTenantEditMode').value = 'edit';
        document.querySelector('#admFormTid').value = String(tn.tenant_id);
        document.querySelector('#admFormTid').disabled = true;
        document.querySelector('#admFormUser').value = tn.username || '';
        document.querySelector('#admFormDisplay').value = tn.display_name || '';
        document.querySelector('#admFormPwd').value = '';
        document.querySelector('#admFormStatus').value = tn.status || 'active';
        document.querySelector('#admFormIsAdmin').checked = Boolean(tn.is_admin);
        drawer.classList.remove('hidden');
      } else if (act === 'toggle') {
        const nextStatus = tn.status === 'disabled' ? 'active' : 'disabled';
        await admFetch(`/api/admin/tenants/${tid}`, {
          method: 'PUT',
          body: JSON.stringify({ status: nextStatus })
        });
        await loadAdminData();
        showToast('状态已更新');
      } else if (act === 'delete') {
        await admFetch(`/api/admin/tenants/${tid}`, { method: 'DELETE' });
        await loadAdminData();
        showToast('已删除租户');
      }
      return;
    }

    const tkBtn = e.target.closest('button[data-tk-act]');
    if (tkBtn) {
      const act = tkBtn.dataset.tkAct;
      const tid = Number(tkBtn.dataset.tid);
      const id = tkBtn.dataset.id;
      const tn = tenants.find(x => Number(x.tenant_id) === tid);
      const tk = (tn?.tasks || []).find(item => item.id === id);
      if (!tn || !tk) return;

      if (act === 'toggle') {
        const nextRun = !(tk.status === 'active' || tk.status === 'running');
        await admFetch(`/api/admin/tenants/${tid}/tasks/${id}/${nextRun ? 'run' : 'pause'}`, { method: 'POST' });
        await loadAdminData();
        showToast(nextRun ? '已启动任务' : '已暂停任务');
      } else if (act === 'edit') {
        targetTenantSelect.value = String(tid);
        document.querySelector('#admTaskId').value = tk.id;
        document.querySelector('#admTaskRev').value = String(tk.revision || 1);
        document.querySelector('#admTaskName').value = tk.name;
        document.querySelector('#admTaskMode').value = tk.mode || 'live';
        document.querySelector('#admTaskSources').value = (tk.sources || []).join('\n');
        document.querySelector('#admTaskDests').value = (tk.destinations || []).join('\n');
        document.querySelector('#adm_filter_check').checked = Boolean(tk.plugins?.filter?.check);
        document.querySelector('#adm_replace_check').checked = Boolean(tk.plugins?.replace?.check);
        document.querySelector('#adm_caption_check').checked = Boolean(tk.plugins?.caption?.check);
        document.querySelector('#adm_inline_check').checked = Boolean(tk.plugins?.inline?.check);
        document.querySelector('#adm_mark_check').checked = Boolean(tk.plugins?.mark?.check);
        document.querySelector('#adm_fmt_check').checked = Boolean(tk.plugins?.fmt?.check);
        document.querySelector('#adm_ocr_check').checked = Boolean(tk.plugins?.ocr?.check);
        document.querySelector('#adm_spoiler_check').checked = Boolean(tk.plugins?.spoiler?.check);
        document.querySelector('#adm_comments_check').checked = Boolean(tk.comments?.enabled);
        switchAdmTab('admTaskEditorView');
      } else if (act === 'delete') {
        await admFetch(`/api/admin/tenants/${tid}/tasks/${id}`, { method: 'DELETE' });
        await loadAdminData();
        showToast('已删除任务');
      }
    }
  });

  document.querySelector('#admBackToListBtn')?.addEventListener('click', () => {
    switchAdmTab('admTenantsView');
  });

  document.querySelector('#admTaskForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const tid = Number(targetTenantSelect.value);
    const id = document.querySelector('#admTaskId').value;
    const rev = Number(document.querySelector('#admTaskRev').value || 1);
    const repRaw = document.querySelector('#adm_replace_raw').value;
    const payload = {
      name: document.querySelector('#admTaskName').value.trim(),
      mode: document.querySelector('#admTaskMode').value,
      sources: lines(document.querySelector('#admTaskSources').value),
      destinations: lines(document.querySelector('#admTaskDests').value),
      comments: { enabled: document.querySelector('#adm_comments_check').checked },
      plugins: {
        filter: {
          check: document.querySelector('#adm_filter_check').checked,
          text: {
            whitelist: lines(document.querySelector('#adm_filter_white').value),
            blacklist: lines(document.querySelector('#adm_filter_black').value)
          }
        },
        replace: {
          check: document.querySelector('#adm_replace_check').checked,
          regex: document.querySelector('#adm_replace_regex').checked,
          text_raw: repRaw,
          text: parseRawDict(repRaw)
        },
        caption: {
          check: document.querySelector('#adm_caption_check').checked,
          header: document.querySelector('#adm_caption_header').value,
          footer: document.querySelector('#adm_caption_footer').value
        },
        inline: { check: document.querySelector('#adm_inline_check').checked },
        mark: { check: document.querySelector('#adm_mark_check').checked },
        fmt: { check: document.querySelector('#adm_fmt_check').checked },
        ocr: { check: document.querySelector('#adm_ocr_check').checked },
        spoiler: { check: document.querySelector('#adm_spoiler_check').checked }
      }
    };

    try {
      if (id) {
        await admFetch(`/api/admin/tenants/${tid}/tasks/${id}`, {
          method: 'PUT',
          body: JSON.stringify({ revision: rev, task: payload })
        });
      } else {
        await admFetch(`/api/admin/tenants/${tid}/tasks`, {
          method: 'POST',
          body: JSON.stringify(payload)
        });
      }
      await loadAdminData();
      switchAdmTab('admTenantsView');
      showToast('已保存该租户的任务与插件配置');
    } catch (err) {
      showToast(err.message || '保存失败');
    }
  });

  document.querySelector('#admSelfConfigForm')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const admin_username = document.querySelector('#cfgAdminUsername').value.trim();
    const admin_path = document.querySelector('#cfgAdminPath').value.trim();
    const admin_password = document.querySelector('#cfgAdminPassword').value;
    try {
      const updated = await admFetch('/api/admin/settings', {
        method: 'PUT',
        body: JSON.stringify({
          admin_username,
          admin_path,
          admin_password: admin_password || null
        })
      });
      document.querySelector('#adminIdentityLabel').textContent = `${updated.admin_username} (${updated.admin_path})`;
      showToast(`已更新：管理员账号为 ${updated.admin_username}，入口路径为 ${updated.admin_path}`);
    } catch (err) {
      showToast(err.message || '更新失败');
    }
  });
});

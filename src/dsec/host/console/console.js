'use strict';
// No credential persistence, duplicate VM registry, mutation replay or background polling.
const $ = (id) => document.getElementById(id);
const state = {token: '', project: 'root', busy: false, identity: null, selected: null,
  pages: {}, results: {}, nodes: [], nodesFetched: 0};
const names = {overview: '总览', nodes: '节点', sandboxes: '沙盒', projects: '项目与 IAM', requests: '请求与审计'};
const forms = {create: 'sandbox.create', delegate: 'iam.delegate', policy: 'iam.policy',
  issue: 'iam.credentials', revoke: 'iam.credentials', session: 'sandbox.execute', command: 'sandbox.execute',
  file: 'sandbox.read', query: 'sandbox.read'};
const uuid = () => crypto.randomUUID().replaceAll('-', '');
const values = (form) => Object.fromEntries(new FormData(form).entries());
const pretty = (value) => JSON.stringify(value, null, 2);
function notice(message, level = '') { $('notice').textContent = message; $('notice').dataset.level = level; }
async function confirmAction(message) {
  const dialog = $('confirm-dialog'); $('confirm-message').textContent = message;
  return new Promise(resolve => {
    const finish = (accepted) => {
      dialog.close(); dialog.removeEventListener('cancel', cancel);
      $('confirm-accept').onclick = null; $('confirm-cancel').onclick = null; resolve(accepted);
    };
    const cancel = (event) => { event.preventDefault(); finish(false); };
    dialog.addEventListener('cancel', cancel);
    $('confirm-accept').onclick = () => finish(true);
    $('confirm-cancel').onclick = () => finish(false);
    dialog.showModal(); $('confirm-cancel').focus();
  });
}
function element(tag, text, className) {
  const el = document.createElement(tag); if (text !== undefined) el.textContent = text;
  if (className) el.className = className; return el;
}
function badge(text) {
  const el = element('span', text, 'badge');
  el.dataset.tone = ['HEALTHY', 'RELEASED', 'RUNNING', 'DONE'].includes(text) ? 'good' :
    ['UNREACHABLE', 'UNKNOWN'].includes(text) ? 'bad' : 'warn'; return el;
}
function has(permission) { return state.identity?.permissions.includes(permission) || false; }
function controls() {
  $('refresh').disabled = state.busy || !state.token;
  $('parent').hidden = !state.identity?.parent;
  $('parent').disabled = state.busy || !state.identity?.parent;
  $('disconnect').disabled = state.busy || !state.token;
  for (const el of $('connect').elements) el.disabled = state.busy;
  for (const [form, permission] of Object.entries(forms)) {
    for (const button of $(form).querySelectorAll('button')) button.disabled = state.busy || !has(permission);
  }
  for (const button of document.querySelectorAll('[data-action]')) button.disabled = state.busy || !has(button.dataset.permission || 'sandbox.read');
  for (const id of ['sandbox', 'project', 'request', 'audit']) $(id + '-more').disabled = state.busy || !state.token;
}
function navigate(page) {
  for (const el of document.querySelectorAll('.page')) el.hidden = el.id !== page;
  for (const el of document.querySelectorAll('nav button')) {
    if (el.dataset.page === page) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  }
  $('page-title').textContent = names[page];
}
for (const button of document.querySelectorAll('nav button')) button.addEventListener('click', () => navigate(button.dataset.page));
$('endpoint').textContent = 'API · ' + location.origin + '/v1/call';
async function rpc(operation, args = {}, {sandbox = null, demand, mutation = false, id = uuid()} = {}) {
  const project = state.project, token = state.token;
  const payload = {project, request_id: id, operation, sandbox_id: sandbox, args};
  if (demand) payload.resource_demand = demand;
  if (mutation) {
    $('operation-panel').hidden = false;
    $('operation-id').textContent = `${project} / ${operation} / 请求 ID: ${id}`;
    $('operation-result').textContent = '已发送，等待结果。请保留此请求 ID。';
    $('query').elements.lookup_id.value = id;
    $('query').elements.sandbox_id.value = sandbox || '';
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), mutation ? 180000 : 15000);
  try {
    const response = await fetch('/v1/call', {method: 'POST', cache: 'no-store', credentials: 'omit',
      redirect: 'error', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + token},
      body: JSON.stringify(payload), signal: controller.signal});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const raw = await response.text();
    if (raw.length > 8 * 1024 ** 2) throw new Error('响应超出界限');
    const result = JSON.parse(raw);
    if (result.request_id !== id || typeof result.ok !== 'boolean') throw new Error('响应 ID 或格式不匹配');
    if (!result.ok) {
      const error = new Error(`${result.error.type}: ${result.error.message}`);
      error.type = result.error.type; error.authoritative = true; throw error;
    }
    if (mutation) $('operation-result').textContent = pretty(result.result);
    return result.result;
  } catch (error) {
    const unknown = mutation && (!error.authoritative || error.type === 'RequestOutcomeUnknown' || error.type === 'RequestPending');
    if (mutation) $('operation-result').textContent = unknown ?
      `结果尚未确定。请求 ID: ${id}\n${error.message}\n不要重新提交；使用「请求与审计」查询原请求。IAM 操作需重新读取项目配置或核对审计；首版没有通用 IAM 请求回执。` : error.message;
    throw error;
  } finally { clearTimeout(timer); }
}
async function guarded(work) {
  if (state.busy) return;
  state.busy = true; controls();
  try { await work(); }
  catch (error) { notice(error.message + '。若操作结果未知，请查询原请求；不要据此重复操作。', 'error'); }
  finally { state.busy = false; controls(); }
}
function table(id, columns, rows) {
  const box = $(id); box.replaceChildren();
  if (!rows.length) { box.append(element('div', '当前没有可展示的记录', 'empty')); return; }
  const tbl = element('table'), head = element('thead'), tr = element('tr');
  for (const col of columns) tr.append(element('th', col)); head.append(tr); tbl.append(head);
  const body = element('tbody');
  for (const row of rows) {
    const r = element('tr');
    for (const value of row) {
      const cell = element('td');
      if (value instanceof Node) cell.append(value); else cell.textContent = value ?? '—';
      r.append(cell);
    } body.append(r);
  } tbl.append(body); box.append(tbl);
}
function action(label, work, permission = 'sandbox.read') {
  const button = element('button', label); button.dataset.action = 'true'; button.dataset.permission = permission;
  button.addEventListener('click', () => guarded(work)); return button;
}
function actions(...buttons) { const el = element('div'); el.append(...buttons); return el; }
function renderNodes() {
  const elapsed = Math.max(0, (performance.now() - state.nodesFetched) / 1000);
  const nodes = state.nodes.map(n => ({...n,
    state: n.state === 'HEALTHY' && elapsed >= n.valid_for_seconds ? 'STALE' : n.state,
    schedulable: n.schedulable && elapsed < n.valid_for_seconds,
    observation_age_seconds: n.observation_age_seconds === null ? null : n.observation_age_seconds + elapsed}));
  table('node-table', ['节点', '健康 / 放置', '最后观测', '槽位 / 负载', '剩余资源（观测值）', '后端 / 环境'], nodes.map(n => {
    const view = n.last_view;
    const observed = n.observation_age_seconds === null ? '从未观测' : `${n.observation_age_seconds.toFixed(1)} 秒前${n.error ? ' · ' + n.error : ''}`;
    return [n.node_id, actions(badge(n.state), element('small', n.schedulable ? '可调度' : '不可调度')),
      observed, view ? `${view.active} active / ${view.load.toFixed(2)}` : null,
      view ? `${view.available.cpu} CPU · ${view.available.memory_mb} MiB RAM · ${view.available.disk_mb} MiB disk` : null,
      view ? `${view.backends.join(', ')} / ${view.environments.join(', ')}` : null];
  }));
  $('healthy').textContent = state.identity ? nodes.filter(n => n.schedulable).length : '—';
  $('node-count').textContent = state.identity ? `${state.nodes.length} 个配置节点 · ${nodes.filter(n => !n.schedulable).length} 个不可调度` : '等待观测';
}
function runtimeOperation(record, operation) { return record.backend === 'container' ? 'container_' + operation : operation; }
function selectSandbox(record) {
  if (state.selected?.sandbox_id !== record.sandbox_id) $('session').elements.session_id.value = '';
  state.selected = record;
  $('selected').textContent = `选中 ${record.sandbox_id} · ${record.backend}`;
  $('native-panel').open = true;
  if (record.backend !== 'microvm') notice('容器原生会话需要完整环境 spec，请通过 SDK 操作；此版界面仅支持 microVM 原生会话。');
}
function renderList(kind) {
  const items = state.results[kind] || [];
  if (kind === 'sandbox') table('sandbox-table', ['沙盒 / 请求', '后端 / Node', '配额状态', '运行态（单独查询）', '操作'], items.map(r => {
    const runtime = r.runtime || '未查询';
    return [actions(element('div', r.sandbox_id, 'mono'), element('small', r.request_id)), `${r.backend} / ${r.node}`,
      actions(badge(r.state), element('small', r.quota_held ? '配额占用中' : '已释放')),
      actions(badge(runtime), element('small', r.runtime_at ? '观测于 ' + r.runtime_at : ''),
        element('small', r.runtime_previous ? '上次状态 ' + r.runtime_previous : '')), actions(action('查询状态', async () => {
        let result;
        try { result = await rpc(runtimeOperation(r, 'status'), {}, {sandbox: r.sandbox_id}); }
        catch (error) { r.runtime_previous = r.runtime; r.runtime = 'UNKNOWN'; renderList('sandbox'); throw error; }
        r.runtime = result.state || 'UNKNOWN'; r.runtime_at = new Date().toLocaleTimeString('zh-CN', {hour12: false});
        r.runtime_previous = null; renderList('sandbox');
        $('operation-panel').hidden = false; $('operation-id').textContent = 'Node 状态 · ' + r.sandbox_id;
        $('operation-result').textContent = pretty(result); controls();
      }), action('选择', async () => selectSandbox(r)), action('停止', async () => {
        if (!await confirmAction(`停止此沙盒？\n${r.sandbox_id}`)) return;
        await rpc(runtimeOperation(r, 'stop'), {}, {sandbox: r.sandbox_id, mutation: true}); await refresh();
      }, 'sandbox.stop'))];
  }));
  if (kind === 'request') table('request-table', ['请求 ID', 'Node / 后端', '配额状态', '资源声明', '操作'], items.map(r => [
    element('span', r.request_id, 'mono'), `${r.node} / ${r.backend}`, actions(badge(r.state), element('small', r.quota_held ? '占用中' : '已释放')),
    `${r.demand.cpu} CPU · ${r.demand.memory_mb} MiB · ${r.demand.disk_mb} MiB disk`,
    action('查询创建结果', async () => { $('query').elements.lookup_id.value = r.request_id;
      $('query').elements.sandbox_id.value = ''; await queryResult(r.request_id, null); })
  ]));
  if (kind === 'project') table('project-table', ['项目', '上级', '槽位占用 / 预算', '已委派', '权限', '操作'], [...(state.identity ? [state.identity] : []), ...items].map(r => [
    r.project, r.parent, `${r.usage.concurrency} / ${r.quota.concurrency}`, r.delegated.concurrency, r.permissions.join(', '),
    r.project === state.project ? element('small', '当前项目') : action('进入', async () => { state.project = r.project; $('connect').elements.project.value = r.project;
      resetViews(); await refresh(); })
  ]));
  if (kind === 'audit') table('audit-table', ['时间', '动作', '身份', '记录 ID'], items.map(r => [r.timestamp, r.action, r.actor, element('span', r.id, 'mono')]));
  $(kind + '-more').hidden = !state.pages[kind];
}
const operations = {sandbox: 'sandbox.list', request: 'admission.list', project: 'project.list', audit: 'audit.list'};
async function load(kind, append = false) {
  if (kind === 'audit' && !has('iam.policy')) {
    state.results.audit = []; state.pages.audit = null;
    $('audit-table').replaceChildren(element('div', '当前身份没有审计读取权限', 'empty')); $('audit-more').hidden = true; return;
  }
  const result = await rpc(operations[kind], {limit: 50, ...(append ? {after: state.pages[kind]} : {})});
  state.results[kind] = append ? [...state.results[kind], ...result.items] : result.items;
  state.pages[kind] = result.next_after; renderList(kind);
}
async function refresh() {
  const info = await rpc('project.get'); state.identity = info;
  $('identity').textContent = `${info.principal} / ${state.project}`;
  $('used').textContent = info.usage.concurrency;
  $('quota').textContent = `总配额 ${info.quota.concurrency} · 资源预留槽位，非运行数`;
  $('delegated').textContent = info.delegated.concurrency;
  $('policy').elements.grants.value = info.grants ? pretty(info.grants) : '';
  $('resources').replaceChildren();
  const units = {cpu: 'CPU', memory_mb: 'MiB RAM', disk_mb: 'MiB disk', network_mbps: 'Mbps'};
  for (const [key, unit] of Object.entries(units)) {
    const row = element('div', undefined, 'resource');
    row.append(element('span', unit), element('strong', `使用 ${info.usage[key]} + 委派 ${info.delegated[key]} / ${info.quota[key]}`));
    $('resources').append(row);
  }
  state.nodesFetched = performance.now();
  state.nodes = (await rpc('node.list')).nodes; renderNodes();
  for (const kind of Object.keys(operations)) await load(kind);
  $('updated').textContent = new Date().toLocaleTimeString('zh-CN', {hour12: false});
  notice('已连接。视图来自当前控制面；点击刷新获取最新状态。');
}
function resetViews() {
  state.identity = null; state.selected = null; state.results = {}; state.pages = {}; state.nodes = [];
  $('identity').textContent = '未连接'; $('operation-panel').hidden = true;
  $('operation-result').textContent = ''; $('operation-id').textContent = '';
  $('query-result').textContent = '尚未查询'; $('native-result').textContent = '尚未执行'; $('selected').textContent = '先在列表选择沙盒。';
  $('query').reset(); $('session').reset(); $('policy').reset(); $('resources').replaceChildren();
  for (const id of ['healthy', 'used', 'delegated', 'updated']) $(id).textContent = '—';
  $('node-count').textContent = '等待观测'; $('quota').textContent = '等待观测';
  for (const kind of Object.keys(operations)) { state.results[kind] = []; renderList(kind); }
  renderNodes(); controls();
}
$('connect').addEventListener('submit', (event) => { event.preventDefault(); const data = values(event.target); guarded(async () => {
  state.token = data.token; state.project = data.project;
  event.target.elements.token.value = ''; resetViews(); await refresh();
}); });
$('parent').addEventListener('click', () => guarded(async () => {
  state.project = state.identity.parent; $('connect').elements.project.value = state.project;
  resetViews(); await refresh();
}));
$('disconnect').addEventListener('click', () => { state.token = ''; resetViews();
  for (const id of ['issue', 'revoke', 'command', 'file']) $(id).reset(); notice('已退出，页面内存凭据与操作结果已清空。'); });
$('refresh').addEventListener('click', () => guarded(refresh));
for (const kind of Object.keys(operations)) $(kind + '-more').addEventListener('click', () => guarded(() => load(kind, true)));
function submit(id, work) { $(id).addEventListener('submit', event => {
  event.preventDefault(); const data = values(event.target), submitter = event.submitter?.value;
  guarded(() => work(data, submitter));
}); }
submit('create', async (data) => {
  const args = JSON.parse(data.args), demand = {};
  for (const key of ['cpu', 'memory_mb', 'disk_mb', 'network_mbps']) demand[key] = Number(data[key]);
  await rpc(data.backend === 'container' ? 'container_create' : 'create', args, {demand, mutation: true}); await refresh();
});
submit('delegate', async data => {
  await rpc('project.create', {parent: state.project, project: data.project, quota: JSON.parse(data.quota), grants: JSON.parse(data.grants)}, {mutation: true}); await refresh();
});
submit('policy', async data => {
  if (!await confirmAction('更新当前项目权限？这可能撤销你的访问权限。')) return;
  await rpc('project.policy', {grants: JSON.parse(data.grants)}, {mutation: true}); await refresh();
});
submit('issue', async data => { try { await rpc('credential.issue', data, {mutation: true}); notice('凭据已签发；请在项目权限中授予该身份所需权限。'); }
  finally { $('issue').elements.new_token.value = ''; } });
submit('revoke', async data => { if (!await confirmAction('撤销此凭据？')) return;
  try { await rpc('credential.revoke', data, {mutation: true}); await refresh(); }
  finally { $('revoke').elements.token.value = ''; } });
async function queryResult(lookup_id, sandbox_id) {
  const operation = sandbox_id?.split('.')[3] === 'container' ? 'container_query_request' : 'query_request';
  const result = await rpc(operation, {lookup_id}, {sandbox: sandbox_id});
  $('query-result').textContent = pretty(result); return result;
}
submit('query', async data => queryResult(data.lookup_id, data.sandbox_id || null));
function nativeArgs(action, args = {}) {
  if (!state.selected || state.selected.backend !== 'microvm') throw new Error('先选择一个 microVM 沙盒');
  return {backend: 'microvm', action, ...args};
}
submit('session', async (data, action) => {
  const session_id = action === 'open' ? uuid() : data.session_id;
  const args = nativeArgs(action, {session_id});
  const result = await rpc('native', args, {sandbox: state.selected.sandbox_id, mutation: true,
    ...(action === 'open' ? {id: session_id} : {})});
  $('native-result').textContent = pretty(result);
  if (action === 'open') $('session').elements.session_id.value = result.session_id;
});
submit('command', async data => {
  const args = nativeArgs('run', {session_id: $('session').elements.session_id.value,
    command: data.command, timeout_ms: Number(data.timeout_ms), output_limit: 65536});
  const result = await rpc('native', args, {sandbox: state.selected.sandbox_id, mutation: true});
  $('native-result').textContent = pretty(result);
});
submit('file', async data => {
  const args = nativeArgs('read', {path: data.path, transfer_id: uuid(), offset: 0, length: 65536});
  const result = await rpc('native', args, {sandbox: state.selected.sandbox_id});
  const bytes = Uint8Array.from(atob(result.data), c => c.charCodeAt(0));
  $('native-result').textContent = `${result.eof ? '完整文件' : '前 64 KiB（未完整）'}\n` + new TextDecoder().decode(bytes);
});
controls();

// Expire the displayed observation locally; this never polls or changes placement.
setInterval(() => { if (state.identity && !state.busy) renderNodes(); }, 1000);

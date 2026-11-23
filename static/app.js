const $ = (id) => document.getElementById(id);
const providers = {demo: 'Pricing demo', codelens: 'CodeLens', graphify: 'Graphify'};
const indexStates = {
  queued: 'Đang chờ', indexing: 'Đang index', ready: 'Sẵn sàng', failed: 'Cần xử lý',
  manual: 'MCP thủ công', unconfigured: 'Chưa kết nối', not_indexed: 'Chưa index', expired: 'Cần làm mới token',
};
const state = {
  projects: [], available: {}, detail: null, selected: null, busy: false, mode: 'graphify',
  index: {}, displayedReviews: [], pending: null, comparison: [],
  session: null, scope: [], sessionReviews: [],
};

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
}

async function api(path, {method = 'GET', body} = {}) {
  const isForm = body instanceof FormData;
  const response = await fetch(path, {
    method,
    headers: body && !isForm ? {'Content-Type': 'application/json'} : {},
    body: body == null || isForm ? body : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload.detail;
    const message = Array.isArray(detail) ? detail.map((entry) => entry.msg || entry).join(' · ') : detail;
    throw new Error(message || 'Yêu cầu thất bại.');
  }
  return payload;
}

function showStatus(text, error = false, id = 'status') {
  $(id).textContent = text;
  $(id).classList.toggle('error', error);
}

function shortSha(value) {
  if (!value) return '—';
  return value.startsWith('zip-') ? value.slice(0, 12) : value.slice(0, 9);
}

function time(value) {
  return new Intl.DateTimeFormat('vi-VN', {dateStyle: 'short', timeStyle: 'short'}).format(new Date(value));
}

function option(label, value) {
  const choice = element('option', label);
  choice.value = value;
  return choice;
}

function snapshot(id) {
  return state.detail?.snapshots.find((item) => String(item.id) === $(id).value);
}

function snapshotPath(head = snapshot('head')) {
  return '/api/projects/' + state.selected + '/snapshots/' + head.id + '/graphify';
}

function sessionPath(id = state.session?.id) {
  return '/api/projects/' + state.selected + '/sessions' + (id ? '/' + id : '');
}

function currentReviews() {
  return state.session ? state.sessionReviews : state.detail.reviews;
}

function renderNavigation() {
  $('projects').replaceChildren(...state.projects.map((project) => {
    const button = element('button', null, 'repo-button');
    button.type = 'button';
    button.disabled = state.busy;
    button.setAttribute('aria-current', String(project.id === state.selected));
    button.append(element('span', project.kind === 'demo' ? '◇' : '▱', 'repo-icon'),
      element('span', project.name, 'repo-name'));
    button.addEventListener('click', choose.bind(null, project.id));
    return button;
  }));
  const statuses = [
    ['Model', state.available.model],
    ['Graphify', state.available.graphify_pilot || state.available.graphify],
    ['CodeLens', state.available.codelens],
  ];
  $('providers').replaceChildren(...statuses.map(([label, ready]) => {
    const row = element('div', null, 'provider-status');
    row.dataset.ready = Boolean(ready);
    row.append(element('span', label), element('small', ready ? 'Đã cấu hình' : 'Chưa cấu hình'));
    return row;
  }));
  $('chat-navigation').hidden = !state.detail;
  $('quick-review').setAttribute('aria-current', String(!state.session));
  $('quick-review').disabled = state.busy;
  $('sessions').replaceChildren(...(state.detail?.sessions || []).map((session) => {
    const button = element('button', null, 'repo-button');
    button.type = 'button';
    button.title = session.title;
    button.disabled = state.busy;
    button.setAttribute('aria-current', String(session.id === state.session?.id));
    button.append(element('span', '◇', 'session-icon'), element('span', session.title, 'repo-name'));
    button.onclick = () => openSession(session.id);
    return button;
  }));
}

function updateControls() {
  const project = state.detail?.project;
  const base = snapshot('base');
  const head = snapshot('head');
  const havePair = Boolean(base && head && base.id !== head.id);
  const graphify = ['ready', 'manual'].includes(state.index.status);
  const codelens = state.available.codelens && project?.codelens_project_id && project.codelens_branch;
  const available = {demo: true, graphify: Boolean(state.available.model && graphify),
    codelens: Boolean(state.available.model && codelens)};
  $('run').disabled = state.busy || !havePair || !available[state.mode];
  $('diff').disabled = state.busy || !havePair;
  $('compare').hidden = Boolean(state.session);
  $('compare').disabled = state.busy || !havePair || !available.graphify || !available.codelens;
  $('index').disabled = state.busy || !head || ['queued', 'indexing'].includes(state.index.status);
  for (const id of ['refresh', 'upload', 'config', 'add-project', 'connections', 'save-connections', 'focus']) {
    $(id).disabled = state.busy;
  }
  for (const id of ['base', 'head', 'mode-graphify', 'mode-codelens']) {
    $(id).disabled = state.busy || Boolean(state.session);
  }
  for (const id of ['new-session', 'attach-repository']) {
    $(id).disabled = state.busy || (!havePair && project?.kind !== 'github');
  }
  const relatedCount = state.scope.length - 1;
  const context = providers[state.mode] + ' · ' + shortSha(base?.revision) + ' → ' + shortSha(head?.revision);
  $('composer-context').textContent = relatedCount > 0 ? context + ' · ' + relatedCount + ' repo liên quan' : context;
  if (project) {
    const mode = $('mode-' + state.mode);
    for (const button of $('modes').querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button === mode));
    }
  }
}

function renderIndex() {
  const project = state.detail?.project;
  const graphify = state.mode === 'graphify';
  const status = state.index.status || 'not_indexed';
  $('index-panel').hidden = !project || state.mode === 'demo';
  $('index-heading').textContent = graphify ? 'Graphify index' : 'CodeLens index';
  $('index-state').dataset.status = graphify ? status : project?.codelens_project_id ? 'ready' : 'unconfigured';
  $('index-state').textContent = graphify ? indexStates[status] :
    project?.codelens_project_id ? 'Đã liên kết' : 'Chưa liên kết';
  if (graphify) {
    $('index').hidden = !state.available.graphify_pilot || status === 'ready' || status === 'manual';
    $('index').textContent = status === 'failed' ? 'Thử index lại' :
      status === 'expired' ? 'Làm mới kết nối' : 'Index head';
    const details = {
      queued: 'Snapshot đã được đưa vào hàng đợi.',
      indexing: 'Graphify Pilot đang phân tích source của head.',
      ready: 'MCP đã nối với snapshot head.',
      manual: 'Đang dùng MCP và SHA thiết lập trong Kết nối.',
      unconfigured: 'Thêm URL và tài khoản Pilot trong Kết nối để tự động index.',
      not_indexed: 'Chọn head để gửi bản mã nguồn của snapshot sang Pilot.',
      expired: 'Agent sẽ lấy token mới cho project hiện có.',
    };
    $('index-detail').textContent = [details[status], state.index.detail].filter(Boolean).join(' ');
    $('config').hidden = true;
  } else {
    $('index').hidden = true;
    $('index-detail').textContent = project?.codelens_project_id ?
      'Project ' + project.codelens_project_id + ' · ' + project.codelens_branch :
      'Chọn project và branch đã được index bên CodeLens.';
    $('config').hidden = false;
  }
  updateControls();
}

function applyScope(scope) {
  state.scope = scope;
  state.index = scope[0]?.graphify || {};
  renderIndex();
  renderRelated();
}

function renderRelated() {
  const demo = state.mode === 'demo';
  const related = state.scope.slice(1);
  $('related-panel').hidden = demo;
  $('attach-repository').textContent = state.session ? 'Gắn repo trong chat mới' : 'Gắn repo';
  $('related-repositories').replaceChildren(...related.map((repo) => {
    const row = element('div', null, 'related-row');
    const heading = element('div', null, 'section-heading');
    const status = repo.graphify?.status || (repo.codelens ? 'ready' : 'unconfigured');
    const badge = element('span', repo.graphify ? indexStates[status] :
      repo.codelens ? 'Đã liên kết' : 'Chỉ có source', 'index-state');
    badge.dataset.status = status;
    heading.append(element('strong', repo.name), badge);
    const revisions = repo.base ? shortSha(repo.base.revision) + ' → ' + shortSha(repo.head.revision) :
      shortSha(repo.head.revision) + ' · đọc ngữ cảnh';
    row.append(heading, element('p', revisions, 'muted'));
    if (repo.graphify?.detail) row.append(element('p', repo.graphify.detail, 'muted'));
    const actions = element('div', null, 'actions');
    if (repo.base) {
      const diff = element('button', 'Xem diff', 'button quiet');
      diff.type = 'button';
      diff.onclick = () => job(() => openDiff(repo.project_id, repo.base.id, repo.head.id), '');
      actions.append(diff);
    }
    if (repo.graphify?.status === 'failed') {
      const retry = element('button', 'Thử index lại', 'button quiet');
      retry.type = 'button';
      retry.onclick = () => job(async () => {
        await api('/api/projects/' + repo.project_id + '/snapshots/' + repo.head.id + '/graphify', {method: 'POST'});
        await prepareHead();
      }, '');
      actions.append(retry);
    }
    if (actions.childElementCount) row.append(actions);
    return row;
  }));
  if (!related.length) $('related-repositories').append(element('p', 'Phiên này dùng repo chính.', 'muted'));
}

function renderFiles() {
  const inventory = state.detail.inventory;
  const search = $('file-filter').value.trim().toLowerCase();
  $('inventory-summary').textContent = 'File nguồn · ' + inventory.file_count + ' file · ' +
    inventory.line_count.toLocaleString('vi-VN') + ' dòng';
  const files = inventory.paths.filter((file) => file.path.toLowerCase().includes(search));
  $('files').replaceChildren(...files.map((file) => {
    const row = element('div', null, 'file-row');
    row.append(element('span', file.path), element('span', String(file.lines)));
    return row;
  }));
  if (!files.length) $('files').append(element('p', 'Không có file phù hợp.', 'muted'));
  if (inventory.file_count > inventory.paths.length) {
    $('files').append(element('p', 'Danh sách hiển thị 100 file đầu.', 'muted'));
  }
}

function renderProject({pair} = {}) {
  const {project, snapshots} = state.detail;
  const demo = project.kind === 'demo';
  state.mode = state.session?.provider || (demo ? 'demo' : project.review_provider);
  $('page').hidden = false;
  $('boot-message').hidden = true;
  $('breadcrumb').textContent = project.name;
  $('repo-name').textContent = state.session?.title || project.name;
  $('repo-ref').textContent = state.session ? 'Repo chính · ' + project.name :
    project.kind === 'github' ? project.github_repo + ' · ' + project.ref :
    project.kind === 'zip' ? 'Snapshot từ ZIP' : 'Dữ liệu mẫu để thử luồng review';
  $('refresh').hidden = project.kind !== 'github';
  $('upload').hidden = project.kind !== 'zip';
  $('modes').hidden = demo;
  $('demo-mode').hidden = !demo;
  $('snapshot-count').textContent = snapshots.length;
  for (const id of ['base', 'head']) {
    $(id).replaceChildren(...snapshots.map((item) => option(shortSha(item.revision) + ' · ' + time(item.created_at), item.id)));
  }
  if (snapshots.length > 1) $('base').value = snapshots[1].id;
  if (state.session) pair = {base: String(state.session.base_id), head: String(state.session.head_id)};
  if (pair) {
    for (const id of ['base', 'head']) {
      if (snapshots.some((item) => String(item.id) === pair[id])) $(id).value = pair[id];
    }
  }
  state.index = state.scope[0]?.graphify || snapshot('head')?.graphify || {};
  renderFiles();
  renderIndex();
  renderRelated();
  renderHistory();
  renderThread();
}

function addDetails(block, title, items) {
  if (!items.length) return;
  const details = element('details');
  const list = element('ul');
  list.append(...items.map((item) => element('li', item)));
  details.append(element('summary', title), list);
  block.append(details);
}

function renderFinding(finding, scope) {
  const card = element('div', null, 'finding');
  const repo = scope?.find((entry) => entry.key === finding.repository);
  if (repo) card.append(element('span', repo.name + ' · ' + shortSha(finding.sha), 'message-byline'));
  card.append(element('code', finding.path + ':' + finding.line), element('p', finding.claim),
    element('pre', finding.quote));
  return card;
}

function renderReview(record) {
  const result = record.result;
  const block = element('article', null, 'result-block');
  block.id = 'review-' + record.id;
  const heading = element('div', null, 'section-heading');
  const label = result.status === 'complete' ? 'Hoàn tất' : 'Còn thiếu bằng chứng';
  heading.append(element('h3', providers[record.provider]), element('span', label, 'result-state ' + result.status));
  block.append(heading);
  const metrics = [result.findings.length + ' findings', result.model_calls + ' lượt model', result.tool_calls + ' tool calls'];
  if (result.elapsed_ms != null) metrics.push((result.elapsed_ms / 1000).toFixed(1) + ' s');
  if (result.tokens != null) metrics.push(result.tokens + ' tokens');
  if (result.graph_calls != null) metrics.push(result.revision_matched_calls + '/' + result.graph_calls + ' truy vấn khớp SHA');
  const metricRow = element('div', null, 'metrics');
  metricRow.append(...metrics.map((value) => element('span', value)));
  block.append(metricRow);
  if (!result.findings.length) block.append(element('p', 'Lượt review chưa ghi nhận finding.', 'muted'));
  block.append(...result.findings.map((finding) => renderFinding(finding, result.scope)));
  const relatedFiles = (result.scope || []).slice(1).flatMap((repo) =>
    (repo.changed_files || []).map((path) => repo.name + ' · ' + path));
  addDetails(block, 'File thay đổi', [...(result.changed_files || []), ...relatedFiles]);
  addDetails(block, 'Giới hạn và dữ liệu còn thiếu', result.gaps);
  addDetails(block, 'Trace gọi model và tool', result.trace.map((step) => {
    if (!step.tool) return 'reviewer · ' + step.finish_reason;
    const repo = result.scope?.find((entry) => entry.key === step.repository);
    return [step.tool, repo?.name || step.repository, step.path || step.query,
      step.sha ? shortSha(step.sha) : null, step.status, step.reason,
      step.revision_evidence].filter(Boolean).join(' · ');
  }));
  return block;
}

function renderTurn(records, pending) {
  const record = records[0];
  const turn = element('section', null, 'turn');
  const request = element('div', null, 'chat-request');
  const prompt = pending?.focus || record?.result.focus || 'Review các thay đổi giữa hai snapshot.';
  const base = pending?.base || record?.result.base_sha;
  const head = pending?.head || record?.result.head_sha;
  request.append(element('span', 'Bạn', 'message-byline'), element('p', prompt),
    element('code', shortSha(base) + ' → ' + shortSha(head)));
  const response = element('div', null, 'chat-response');
  response.append(element('span', 'Code Review Agent', 'message-byline'));
  if (pending) {
    response.append(element('p', 'Đang chạy với ' + pending.providers.map((key) => providers[key]).join(' và ') + '…', 'muted loading'));
  } else {
    const results = element('div', null, records.length > 1 ? 'comparison-grid' : '');
    results.append(...records.map(renderReview));
    response.append(results);
  }
  turn.append(request, response);
  return turn;
}

function renderThread() {
  const reviews = currentReviews().filter((record) => record.provider === state.mode).reverse();
  const compared = new Set(state.comparison.map((review) => review.id));
  const turns = reviews.filter((record) => !compared.has(record.id)).map((record) => renderTurn([record]));
  if (state.comparison.length) turns.push(renderTurn(state.comparison));
  if (state.pending) turns.push(renderTurn([], state.pending));
  $('result').replaceChildren(...turns);
  $('suggestions').hidden = Boolean(turns.length);
  if (!turns.length) {
    const intro = element('section', null, 'welcome');
    const copy = state.detail.snapshots.length < 2 ? 'Thêm một snapshot nữa để tạo diff và bắt đầu review.' :
      state.mode === 'demo' ? 'Chạy ca mẫu để xem agent tìm lỗi ở một file nằm ngoài diff.' :
        state.session ? 'Gửi yêu cầu review cho các snapshot đã ghim.' :
          'Chọn base và head, rồi gửi yêu cầu để bắt đầu phiên chat.';
    intro.append(element('div', '⌘', 'welcome-icon'), element('h2', 'Bạn muốn review gì?'), element('p', copy, 'muted'));
    $('result').append(intro);
  }
  state.displayedReviews = state.comparison.length ? state.comparison : reviews.slice(-1);
  $('export').hidden = !state.displayedReviews.length;
}

function renderHistory() {
  const reviews = currentReviews();
  $('history').replaceChildren(...reviews.map((record) => {
    const button = element('button', null, 'history-button');
    button.type = 'button';
    button.append(element('strong', providers[record.provider] + ' · ' + record.result.findings.length + ' finding'),
      element('span', shortSha(record.result.head_sha) + ' · ' + time(record.created_at), 'muted'));
    button.addEventListener('click', () => {
      const existing = $('review-' + record.id);
      if (existing) {
        existing.scrollIntoView({block: 'nearest', behavior: 'smooth'});
        existing.classList.add('highlighted');
        setTimeout(() => existing.classList.remove('highlighted'), 900);
      } else {
        $('result').append(renderTurn([record]));
        $('review-' + record.id).scrollIntoView({block: 'nearest', behavior: 'smooth'});
      }
      state.displayedReviews = [record];
      $('export').hidden = false;
    });
    return button;
  }));
  if (!reviews.length) $('history').append(element('p', 'Chưa có lượt review.', 'muted'));
}

async function loadProject(projectId, {pair, sessionId = projectId === state.selected ? state.session?.id : null} = {}) {
  const detail = await api('/api/projects/' + projectId);
  const chat = detail.sessions.some((session) => session.id === sessionId) ?
    await api('/api/projects/' + projectId + '/sessions/' + sessionId) : null;
  state.selected = projectId;
  state.detail = {...detail, inventory: chat?.inventory || detail.inventory};
  state.session = chat?.session || null;
  state.scope = chat?.scope || [];
  state.sessionReviews = chat?.reviews || [];
  renderNavigation();
  renderProject({pair});
  if (state.session) $('thread').scrollTop = $('thread').scrollHeight;
  const url = new URL(window.location.href);
  url.searchParams.set('repo', projectId);
  if (state.session) url.searchParams.set('chat', state.session.id);
  else url.searchParams.delete('chat');
  history.replaceState(null, '', url);
}

function selectedPair() {
  return {base: $('base').value, head: $('head').value};
}

async function loadProjects(projectId = state.selected, options) {
  const data = await api('/api/projects');
  state.projects = data.projects;
  state.available = data.available;
  $('connections').hidden = !data.can_configure_providers;
  const selected = state.projects.find((project) => project.id === projectId)?.id || state.projects[0]?.id;
  if (selected) await loadProject(selected, options);
}

async function choose(projectId) {
  if (state.busy || projectId === state.selected) return;
  await job(async () => {
    state.comparison = [];
    $('focus').value = '';
    $('file-filter').value = '';
    await loadProject(projectId);
    await prepareHead();
    showStatus('');
  }, 'Đang mở repository…');
}

async function openSession(sessionId) {
  if (state.session?.id === sessionId || (!state.session && sessionId === null)) return;
  await job(async () => {
    state.comparison = [];
    $('focus').value = '';
    await loadProject(state.selected, {sessionId, pair: selectedPair()});
    await prepareHead();
    $('thread').scrollTop = $('thread').scrollHeight;
  }, 'Đang mở phiên chat…');
}

async function job(operation, pending, errorId = 'status') {
  if (state.busy) return;
  state.busy = true;
  updateControls();
  renderNavigation();
  showStatus(pending, false, errorId);
  try {
    await operation();
    if (errorId !== 'status') showStatus('', false, errorId);
  } catch (error) {
    showStatus(error.message, true, errorId);
  } finally {
    state.busy = false;
    updateControls();
    renderNavigation();
  }
}

async function prepareHead({retry = false} = {}) {
  const head = snapshot('head');
  if (!head || state.mode === 'demo') return;
  if (state.session) {
    if (state.mode !== 'graphify') return;
    if (retry) await api(snapshotPath(head), {method: 'POST'});
    const path = sessionPath();
    const data = await api(path + '/prepare', {method: 'POST'});
    if (path === sessionPath()) applyScope(data.scope);
    return;
  }
  const path = snapshotPath(head);
  const status = await api(path);
  if (path !== (snapshot('head') && snapshotPath())) return;
  state.index = status;
  if (state.mode === 'graphify' && state.available.graphify_pilot &&
      (retry || ['not_indexed', 'expired'].includes(status.status))) {
    const update = await api(path, {method: 'POST'});
    if (path !== (snapshot('head') && snapshotPath())) return;
    state.index = update;
  }
  renderIndex();
}

async function pollIndex() {
  try {
    const pending = (status) => ['queued', 'indexing'].includes(status);
    if (state.session && state.scope.some((repo) => pending(repo.graphify?.status))) {
      const path = sessionPath();
      const data = await api(path + '/scope');
      if (path === sessionPath()) applyScope(data.scope);
    } else if (!state.session && state.detail?.project.kind !== 'demo' && snapshot('head') && pending(state.index.status)) {
      const path = snapshotPath();
      const status = await api(path);
      if (path === (snapshot('head') && snapshotPath())) {
        state.index = status;
        renderIndex();
      }
    }
  } catch {
    // The next poll can resume after a temporary dashboard connection failure.
  }
  setTimeout(pollIndex, 2000);
}

async function uploadZip(id, file, index) {
  const form = new FormData();
  form.append('file', file);
  return api('/api/projects/' + id + '/snapshots?index=' + index, {method: 'POST', body: form});
}

function runReview(keys) {
  job(async () => {
    const pair = selectedPair();
    const focus = $('focus').value.trim();
    const base = snapshot('base').revision;
    const head = snapshot('head').revision;
    if (keys.length === 1 && !state.session) {
      const title = focus ? focus.split('\n', 1)[0].slice(0, 100) : '';
      const data = await api(sessionPath(), {method: 'POST', body: {
        title, provider: keys[0], base_id: Number(pair.base), head_id: Number(pair.head),
      }});
      await loadProject(state.selected, {sessionId: data.session.id});
    }
    const previous = focus && keys.length === 1 && keys[0] !== 'demo' && currentReviews()[0];
    state.pending = {focus, base, head, providers: keys};
    renderThread();
    $('thread').scrollTop = $('thread').scrollHeight;
    try {
      const path = state.session ? sessionPath() + '/turns' : '/api/projects/' + state.selected + '/review';
      const message = {focus, follow_up_to: previous ? previous.id : null};
      const body = state.session ? message : {
        ...message, base_id: Number(pair.base), head_id: Number(pair.head), providers: keys,
      };
      const data = await api(path, {method: 'POST', body});
      state.comparison = keys.length > 1 ? data.reviews : [];
      state.pending = null;
      await loadProject(state.selected, {pair});
      $('focus').value = '';
      $('review-' + data.reviews[0].id)?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      showStatus('');
    } finally {
      if (state.pending) {
        state.pending = null;
        renderThread();
      }
    }
  }, 'Đang chạy review…');
}

for (const button of document.querySelectorAll('dialog .close')) {
  button.onclick = () => button.closest('dialog').close();
}
$('add-project').onclick = () => $('project-dialog').showModal();
$('quick-review').onclick = () => openSession(null);
$('new-session').onclick = () => openSessionDialog();
$('attach-repository').onclick = () => openSessionDialog({copyScope: true});
for (const button of $('suggestions').querySelectorAll('button')) {
  button.onclick = () => {
    $('focus').value = button.dataset.focus;
    $('focus').focus();
  };
}
$('focus').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('run').disabled) $('composer').requestSubmit();
  }
});
$('composer').onsubmit = (event) => {
  event.preventDefault();
  if (!$('run').disabled) runReview([state.mode]);
};
$('compare').onclick = () => runReview(['graphify', 'codelens']);
$('export').onclick = () => {
  const filename = 'review-' + (state.displayedReviews[0]?.id || 'result') + '.json';
  const url = URL.createObjectURL(new Blob([JSON.stringify(state.displayedReviews, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 500);
};

let draftScope = [];

function snapshotOptions(snapshots) {
  return snapshots.map((item) => option(shortSha(item.revision) + ' · ' + time(item.created_at), item.id));
}

function renderDraftScope() {
  const selected = new Set(draftScope.map((repo) => repo.project_id));
  const choices = state.projects.filter((project) => project.kind !== 'demo' &&
    project.id !== state.selected && !selected.has(project.id));
  $('related-project').replaceChildren(option('Chọn repository', ''), ...choices.map((repo) => option(repo.name, repo.id)));
  $('related-project').disabled = draftScope.length >= 4 || !choices.length;
  $('related-snapshots').hidden = true;
  $('draft-repositories').replaceChildren(...draftScope.map((repo) => {
    const row = element('div', null, 'related-row');
    const heading = element('div', null, 'section-heading');
    const remove = element('button', 'Bỏ gắn', 'button quiet');
    remove.type = 'button';
    remove.onclick = () => {
      draftScope = draftScope.filter((entry) => entry.project_id !== repo.project_id);
      renderDraftScope();
    };
    heading.append(element('strong', repo.name), remove);
    row.append(heading, element('p', repo.base ? shortSha(repo.base.revision) + ' → ' + shortSha(repo.head.revision) :
      shortSha(repo.head.revision) + ' · đọc ngữ cảnh', 'muted'));
    return row;
  }));
}

function openSessionDialog({copyScope = false} = {}) {
  const {project, snapshots} = state.detail;
  const pair = selectedPair();
  const demo = project.kind === 'demo';
  $('session-form').reset();
  $('session-provider').replaceChildren(...(demo ? ['demo'] : ['graphify', 'codelens']).map((key) => option(providers[key], key)));
  $('session-provider').value = state.mode;
  $('session-base').replaceChildren(...snapshotOptions(snapshots));
  $('session-head').replaceChildren(...snapshotOptions(snapshots));
  $('session-base').value = pair.base;
  $('session-head').value = pair.head;
  const github = project.kind === 'github';
  $('session-source-field').hidden = !github;
  $('session-source').value = github && pair.base === pair.head ? 'github' : 'saved';
  $('session-head-ref').value = github ? project.ref || '' : '';
  updateSessionSource();
  $('session-related').hidden = demo;
  draftScope = copyScope ? state.scope.slice(1) : [];
  renderDraftScope();
  showStatus('', false, 'session-error');
  $('session-dialog').showModal();
  if (copyScope && !$('related-project').disabled) $('related-project').focus();
}

function updateSessionSource() {
  const github = $('session-source').value === 'github';
  $('session-saved-pair').hidden = github;
  $('session-github-pair').hidden = !github;
  for (const id of ['session-base', 'session-head']) $(id).disabled = github;
  for (const id of ['session-base-ref', 'session-head-ref']) {
    $(id).disabled = !github;
    $(id).required = github;
  }
}

$('session-source').onchange = updateSessionSource;

let relatedChoices = [];
$('related-project').onchange = async () => {
  const selected = $('related-project').value;
  $('related-snapshots').hidden = true;
  $('add-related').disabled = true;
  if (!selected) return;
  try {
    const data = await api('/api/projects/' + selected);
    if ($('related-project').value !== selected) return;
    relatedChoices = data.snapshots;
    $('related-base').replaceChildren(option('Chỉ đọc ngữ cảnh', ''), ...snapshotOptions(relatedChoices));
    $('related-head').replaceChildren(...snapshotOptions(relatedChoices));
    $('related-snapshots').hidden = false;
    $('add-related').disabled = !relatedChoices.length;
    showStatus(relatedChoices.length ? '' : 'Repository này chưa có snapshot.', false, 'session-error');
  } catch (error) {
    showStatus(error.message, true, 'session-error');
  }
};

$('add-related').onclick = () => {
  const repo = state.projects.find((project) => String(project.id) === $('related-project').value);
  const base = relatedChoices.find((item) => String(item.id) === $('related-base').value);
  const head = relatedChoices.find((item) => String(item.id) === $('related-head').value);
  if (!repo || !head || base?.id === head.id) {
    showStatus('Chọn head và, nếu có, một base khác head.', true, 'session-error');
    return;
  }
  draftScope.push({project_id: repo.id, name: repo.name, base, head});
  renderDraftScope();
  showStatus('', false, 'session-error');
};

$('session-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    if ($('related-project').value) throw new Error('Bấm Gắn vào phiên để lưu repo đang chọn hoặc bỏ lựa chọn.');
    const pair = $('session-source').value === 'github' ? {
      base_ref: $('session-base-ref').value.trim(), head_ref: $('session-head-ref').value.trim(),
    } : {
      base_id: Number($('session-base').value), head_id: Number($('session-head').value),
    };
    const data = await api(sessionPath(null), {method: 'POST', body: {
      title: $('session-title').value.trim(),
      provider: $('session-provider').value,
      ...pair,
      related: draftScope.map((repo) => ({project_id: repo.project_id, base_id: repo.base?.id || null, head_id: repo.head.id})),
    }});
    state.comparison = [];
    $('focus').value = '';
    await loadProject(state.selected, {sessionId: data.session.id});
    $('session-dialog').close();
    $('focus').focus();
  }, $('session-source').value === 'github' ? 'Đang tải base và head từ GitHub…' : 'Đang tạo phiên…', 'session-error');
};

$('base').onchange = updateControls;
$('head').onchange = () => job(prepareHead, '');
$('file-filter').oninput = renderFiles;
$('index').onclick = () => job(() => prepareHead({retry: true}), '');

for (const provider of ['graphify', 'codelens']) {
  $('mode-' + provider).onclick = () => {
    if (state.mode === provider) return;
    job(async () => {
      const pair = selectedPair();
      await api('/api/projects/' + state.selected + '/mode', {method: 'PATCH', body: {provider}});
      state.comparison = [];
      await loadProject(state.selected, {pair});
      await prepareHead();
    }, '');
  };
}

async function openDiff(projectId, baseId, headId) {
  const query = new URLSearchParams({base_id: baseId, head_id: headId});
  const data = await api('/api/projects/' + projectId + '/diff?' + query);
  $('diff-files').replaceChildren(...data.changed_files.map((file) => element('code', file)));
  const lines = data.diff.split('\n').map((line) => {
    const kind = line.startsWith('+') ? 'addition' : line.startsWith('-') ? 'deletion' :
      line.startsWith('@@') ? 'hunk' : '';
    return element('span', line, kind);
  });
  $('diff-content').replaceChildren(...lines);
  $('diff-dialog').showModal();
}

$('diff').onclick = () => job(() => {
  const pair = selectedPair();
  return openDiff(state.selected, pair.base, pair.head);
}, '');

const connectionFields = $('connections-form').elements;
let currentConnections;
$('connections').onclick = () => job(async () => {
  currentConnections = await api('/api/provider-settings');
  for (const [name, value] of Object.entries(currentConnections.fields)) {
    connectionFields[name].value = value;
  }
  for (const [name, configured] of Object.entries(currentConnections.has_key)) {
    const remove = connectionFields['remove_' + name];
    connectionFields[name].value = '';
    connectionFields[name].disabled = false;
    connectionFields[name].placeholder = configured ? 'Đã cấu hình · để trống để giữ' : '';
    remove.checked = false;
    remove.closest('label').hidden = !configured;
    remove.onchange = () => connectionFields[name].disabled = remove.checked;
  }
  $('connections-transport').hidden = window.isSecureContext;
  showStatus('', false, 'connections-status');
  $('connections-dialog').showModal();
}, '');
$('connections-dialog').addEventListener('close', () => $('connections-form').reset());
$('connections-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    const changes = {};
    for (const [name, current] of Object.entries(currentConnections.fields)) {
      const value = connectionFields[name].value.trim();
      if (value !== current) changes[name] = value;
    }
    for (const name of Object.keys(currentConnections.has_key)) {
      const value = name === 'graphify_pilot_password' ? connectionFields[name].value : connectionFields[name].value.trim();
      if (connectionFields['remove_' + name].checked) changes[name] = '';
      else if (value) changes[name] = value;
    }
    const data = await api('/api/provider-settings', {method: 'PATCH', body: changes});
    state.available = data.available;
    $('connections-dialog').close();
    await prepareHead();
    showStatus('Đã lưu cấu hình kết nối.');
  }, 'Đang lưu kết nối…', 'connections-status');
};

$('source-kind').onchange = () => {
  const zip = $('source-kind').value === 'zip';
  $('github-fields').hidden = zip;
  $('zip-fields').hidden = !zip;
};
$('project-mode').onchange = () => {
  $('project-mode-note').textContent = $('project-mode').value === 'graphify' ?
    'Khi đã kết nối Pilot, agent tự gửi source của head sang Graphify để index.' :
    'Sau khi nhập source, liên kết với một branch đã index bên CodeLens.';
};

$('project-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    const kind = $('source-kind').value;
    const file = $('zip-file').files[0];
    const baseZip = $('zip-base').files[0];
    if (kind === 'zip' && !file) throw new Error('Chọn ZIP head để nhập repository.');
    const data = await api('/api/projects', {method: 'POST', body: {
      kind, name: $('project-name').value || (kind === 'zip' ? file.name.replace(/\.zip$/i, '') : ''),
      repo: $('github-repo').value, ref: $('git-ref').value || 'main', base_ref: $('git-base').value,
      review_provider: $('project-mode').value,
    }});
    state.comparison = [];
    $('focus').value = '';
    $('file-filter').value = '';
    try {
      if (kind === 'zip') {
        if (baseZip) await uploadZip(data.id, baseZip, false);
        await uploadZip(data.id, file, true);
      }
    } catch (error) {
      await loadProjects(data.id);
      $('project-dialog').close();
      showStatus('Đã tạo repository. Dùng Thêm ZIP để tiếp tục. ' + error.message, true);
      return;
    }
    await loadProjects(data.id);
    await prepareHead();
    $('project-form').reset();
    $('source-kind').onchange();
    $('project-mode').onchange();
    $('project-dialog').close();
  }, 'Đang nhập snapshot…', 'project-error');
};

$('config').onclick = () => {
  const project = state.detail.project;
  $('codelens-id').value = project.codelens_project_id || '';
  $('codelens-branch').value = project.codelens_branch || '';
  $('branch-id').value = project.codelens_branch_id || '';
  showStatus('', false, 'config-error');
  $('config-dialog').showModal();
};
$('config-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    await api('/api/projects/' + state.selected + '/codelens', {method: 'PATCH', body: {
      project_id: Number($('codelens-id').value) || null,
      branch: $('codelens-branch').value,
      branch_id: Number($('branch-id').value) || null,
    }});
    await loadProject(state.selected, {pair: selectedPair()});
    $('config-dialog').close();
  }, 'Đang lưu cấu hình…', 'config-error');
};

$('refresh').onclick = () => job(async () => {
  const data = await api('/api/projects/' + state.selected + '/refresh', {method: 'POST'});
  await loadProject(state.selected, data.changed ? {} : {pair: selectedPair()});
  await prepareHead();
  showStatus(data.changed ? 'Đã thêm snapshot mới.' : 'Branch vẫn ở revision đã lưu.');
}, 'Đang kiểm tra thay đổi…');

$('upload').onclick = () => {
  $('upload-form').reset();
  $('upload-role').value = state.detail.snapshots.length ? 'head' : 'base';
  showStatus('', false, 'upload-error');
  $('upload-dialog').showModal();
};
$('upload-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    const head = $('upload-role').value === 'head';
    const pair = selectedPair();
    const data = await uploadZip(state.selected, $('extra-zip').files[0], head);
    await loadProject(state.selected, {pair});
    const added = state.detail.snapshots.find((item) => item.revision === data.revision);
    if (!state.session) $(head ? 'head' : 'base').value = added.id;
    if (head) await prepareHead();
    else {
      state.index = snapshot('head')?.graphify || {};
      renderIndex();
    }
    $('upload-dialog').close();
    showStatus(data.changed ? 'Đã thêm snapshot.' : 'Snapshot này đã có trong repository.');
  }, 'Đang đọc ZIP…', 'upload-error');
};

$('project-mode').onchange();
const openParams = new URLSearchParams(window.location.search);
loadProjects(Number(openParams.get('repo')) || null, {sessionId: Number(openParams.get('chat')) || null})
  .then(prepareHead).catch((error) => {
  $('boot-message').hidden = false;
  $('boot-message').textContent = error.message;
});
setTimeout(pollIndex, 2000);

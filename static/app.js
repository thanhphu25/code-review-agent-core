const $ = (id) => document.getElementById(id);
const providers = {demo: 'Demo fixture', codelens: 'CodeLens', graphify: 'Graphify'};
const state = {projects: [], available: {}, detail: null, selected: null, busy: false, displayedReviews: []};

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
    throw new Error(Array.isArray(detail) ? detail.map((entry) => entry.msg).join(' · ') : detail || 'Yêu cầu thất bại.');
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

function renderNavigation() {
  $('projects').replaceChildren(...state.projects.map((project) => {
    const button = element('button', project.name, 'repo-button');
    button.type = 'button';
    button.disabled = state.busy;
    button.setAttribute('aria-current', String(project.id === state.selected));
    button.addEventListener('click', () => choose(project.id));
    return button;
  }));
  $('providers').replaceChildren(...[['Model', 'model'], ['CodeLens', 'codelens'], ['Graphify', 'graphify']].map(([label, key]) => {
    const row = element('div', null, 'provider-status');
    row.dataset.ready = Boolean(state.available[key]);
    row.append(element('span', label), element('small', state.available[key] ? 'Đã cấu hình' : 'Chưa cấu hình'));
    return row;
  }));
}

function updateControls() {
  const detail = state.detail;
  const ready = detail?.snapshots.length >= 2 && $('base').value !== $('head').value;
  const liveReady = state.available.model && state.available.codelens && state.available.graphify;
  $('run').disabled = state.busy || !ready || !$('provider').value;
  $('compare').disabled = state.busy || !ready || !liveReady || detail?.project.kind === 'demo';
  for (const id of ['base', 'head', 'provider', 'refresh', 'upload', 'config', 'add-project', 'top-add']) {
    $(id).disabled = state.busy;
  }
}

function renderProject() {
  const {project, snapshots, inventory, reviews} = state.detail;
  $('page').hidden = false;
  $('boot-message').hidden = true;
  $('repo-name').textContent = project.name;
  $('repo-type').textContent = {github: 'GitHub', zip: 'ZIP snapshots', demo: 'Fixture demo'}[project.kind];
  $('repo-ref').textContent = project.github_repo ? `${project.github_repo} · ${project.ref}` : '';
  $('refresh').hidden = project.kind !== 'github';
  $('upload').hidden = project.kind !== 'zip';
  $('config').hidden = project.kind === 'demo';
  $('compare').hidden = project.kind === 'demo';
  $('snapshot-count').textContent = snapshots.length;
  $('file-count').textContent = inventory.file_count;
  $('line-count').textContent = inventory.line_count.toLocaleString('vi-VN');
  $('latest-sha').textContent = shortSha(snapshots[0]?.revision);
  $('latest-sha').title = snapshots[0]?.revision || '';
  for (const id of ['base', 'head']) {
    $(id).replaceChildren(...snapshots.map((item) => option(`${shortSha(item.revision)} · ${time(item.created_at)}`, item.id)));
  }
  if (snapshots.length > 1) $('base').value = snapshots[1].id;
  const choices = project.kind === 'demo' ? ['demo'] : ['codelens', 'graphify'];
  $('provider').replaceChildren(...choices.map((key) => {
    const available = key === 'demo' || (state.available.model && state.available[key]);
    const item = option(available ? providers[key] : `${providers[key]} · chưa cấu hình`, key);
    item.disabled = !available;
    return item;
  }));
  const next = [...$('provider').options].find((item) => !item.disabled);
  $('provider').value = next?.value || '';
  $('files').replaceChildren(...inventory.paths.map((file) => {
    const row = element('div', null, 'file-row');
    row.append(element('span', file.path), element('span', `${file.lines} dòng`));
    return row;
  }));
  if (inventory.file_count > inventory.paths.length) {
    $('files').append(element('p', 'Hiển thị 100 file đầu.', 'muted'));
  }
  $('history').replaceChildren(...reviews.map((record) => {
    const button = element('button', null, 'history-button');
    button.type = 'button';
    button.append(element('span', `${providers[record.provider]} · ${record.result.findings.length} finding`),
      element('span', `${shortSha(record.result.head_sha)} · ${time(record.created_at)}`, 'muted'));
    button.addEventListener('click', () => renderResults([record]));
    return button;
  }));
  if (!reviews.length) $('history').append(element('p', 'Chưa có lượt review.', 'muted'));
  renderResults(reviews.slice(0, 1));
  updateControls();
}

function renderResults(records) {
  state.displayedReviews = records;
  $('export').hidden = !records.length;
  $('result').replaceChildren();
  if (!records.length) {
    const count = state.detail?.snapshots.length || 0;
    $('result').append(element('p', count < 2 ? 'Thêm snapshot tiếp theo để review thay đổi.' : 'Chọn base, head và chạy review.', 'empty'));
    return;
  }
  for (const record of records) {
    const result = record.result;
    const block = element('article', null, 'result-block');
    const heading = element('div', null, 'section-heading');
    heading.append(element('h3', providers[record.provider]), element('span', result.status, `result-state ${result.status}`));
    block.append(heading);
    const metrics = [`${result.findings.length} findings`, `${result.model_calls} model`, `${result.tool_calls} tools`];
    if (result.elapsed_ms != null) metrics.push(`${(result.elapsed_ms / 1000).toFixed(1)} s`);
    if (result.tokens != null) metrics.push(`${result.tokens} tokens`);
    if (result.graph_calls != null) metrics.push(`${result.revision_matched_calls}/${result.graph_calls} graph calls khớp SHA`);
    const metricRow = element('div', null, 'metrics');
    metricRow.append(...metrics.map((value) => element('span', value)));
    block.append(metricRow, element('p', `${shortSha(result.base_sha)} → ${shortSha(result.head_sha)}`, 'muted'));
    if (!result.findings.length) block.append(element('p', 'Lượt review không trả finding.', 'muted'));
    for (const finding of result.findings) {
      const card = element('div', null, 'finding');
      card.append(element('code', `${finding.path}:${finding.line}`), element('p', finding.claim), element('pre', finding.quote));
      block.append(card);
    }
    addDetails(block, 'Giới hạn và dữ liệu còn thiếu', result.gaps);
    addDetails(block, 'Trace gọi model và tool', result.trace.map((step) => step.tool ?
      [step.tool, step.path, step.status, step.reason, step.revision_evidence].filter(Boolean).join(' · ') :
      `reviewer · ${step.finish_reason}`));
    $('result').append(block);
  }
}

function addDetails(block, title, items) {
  if (!items.length) return;
  const details = element('details');
  const list = element('ul');
  list.append(...items.map((item) => element('li', item)));
  details.append(element('summary', title), list);
  block.append(details);
}

async function choose(projectId) {
  try {
    const detail = await api(`/api/projects/${projectId}`);
    state.selected = projectId;
    state.detail = detail;
    renderNavigation();
    renderProject();
    showStatus('');
  } catch (error) {
    if (state.detail) showStatus(error.message, true);
    else $('boot-message').textContent = error.message;
  }
}

async function loadProjects(selected) {
  const data = await api('/api/projects');
  state.projects = data.projects;
  state.available = data.available;
  if (data.projects.length) await choose(selected || data.projects[0].id);
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

async function uploadZip(id, file) {
  const form = new FormData();
  form.append('file', file);
  return api(`/api/projects/${id}/snapshots`, {method: 'POST', body: form});
}

function runReview(keys) {
  job(async () => {
    const data = await api(`/api/projects/${state.selected}/review`, {method: 'POST', body: {
      base_id: Number($('base').value), head_id: Number($('head').value), providers: keys,
    }});
    const base = $('base').value;
    const head = $('head').value;
    const provider = $('provider').value;
    await choose(state.selected);
    $('base').value = base;
    $('head').value = head;
    $('provider').value = provider;
    renderResults(data.reviews);
    showStatus('Kết quả đã lưu trong lịch sử.');
  }, keys.length > 1 ? 'Đang review lần lượt với hai provider…' : 'Đang chạy review…');
}

for (const id of ['add-project', 'top-add']) $(id).onclick = () => $('project-dialog').showModal();
for (const button of document.querySelectorAll('dialog .close')) button.onclick = () => button.closest('dialog').close();
$('source-kind').onchange = () => {
  const zip = $('source-kind').value === 'zip';
  $('github-fields').hidden = zip;
  $('zip-fields').hidden = !zip;
};
$('project-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    const kind = $('source-kind').value;
    const file = $('zip-file').files[0];
    if (kind === 'zip' && !file) throw new Error('Chọn ZIP để tạo snapshot đầu tiên.');
    const data = await api('/api/projects', {method: 'POST', body: {
      kind, name: $('project-name').value || (kind === 'zip' ? file.name.replace(/\.zip$/i, '') : ''),
      repo: $('github-repo').value, ref: $('git-ref').value || 'main', base_ref: $('git-base').value,
    }});
    try {
      if (kind === 'zip') await uploadZip(data.id, file);
    } catch (error) {
      await loadProjects(data.id);
      $('project-dialog').close();
      showStatus(`Repository đã tạo. Tải lại ZIP bằng nút Thêm ZIP. ${error.message}`, true);
      return;
    }
    await loadProjects(data.id);
    $('project-form').reset();
    $('source-kind').onchange();
    $('project-dialog').close();
  }, 'Đang nhập snapshot…', 'project-error');
};
$('config').onclick = () => {
  const project = state.detail.project;
  $('codelens-id').value = project.codelens_project_id || '';
  $('codelens-branch').value = project.codelens_branch || project.ref || '';
  $('branch-id').value = project.codelens_branch_id || '';
  $('config-dialog').showModal();
};
$('config-form').onsubmit = (event) => {
  event.preventDefault();
  job(async () => {
    await api(`/api/projects/${state.selected}/codelens`, {method: 'PATCH', body: {
      project_id: Number($('codelens-id').value) || null, branch: $('codelens-branch').value,
      branch_id: Number($('branch-id').value) || null,
    }});
    await choose(state.selected);
    $('config-dialog').close();
  }, 'Đang lưu…', 'config-error');
};
$('refresh').onclick = () => job(async () => {
  const data = await api(`/api/projects/${state.selected}/refresh`, {method: 'POST'});
  await choose(state.selected);
  showStatus(data.changed ? 'Đã lưu snapshot mới. Có thể chạy review thay đổi.' : 'Branch chưa có commit mới.');
}, 'Đang kiểm tra GitHub…');
$('upload').onclick = () => $('extra-zip').click();
$('extra-zip').onchange = () => {
  const file = $('extra-zip').files[0];
  $('extra-zip').value = '';
  if (!file) return;
  job(async () => {
    const data = await uploadZip(state.selected, file);
    await choose(state.selected);
    showStatus(data.changed ? 'Đã lưu snapshot mới. Có thể chạy review thay đổi.' : 'Snapshot này đã có trong repository.');
  }, 'Đang đọc ZIP…');
};
$('base').onchange = $('head').onchange = $('provider').onchange = updateControls;
$('run').onclick = () => runReview([$('provider').value]);
$('compare').onclick = () => runReview(['codelens', 'graphify']);
$('export').onclick = () => {
  const blob = new Blob([JSON.stringify(state.displayedReviews, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const link = element('a');
  link.href = url;
  link.download = 'review-results.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
};

loadProjects().catch((error) => $('boot-message').textContent = error.message);

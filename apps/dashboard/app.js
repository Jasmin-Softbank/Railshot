const views = {
  deploy: document.querySelector('#deploy-view'),
  history: document.querySelector('#history-view'),
  monitor: document.querySelector('#monitor-view'),
  connections: document.querySelector('#connections-view'),
};

function showView(name) {
  for (const [key, view] of Object.entries(views)) view.hidden = key !== name;
  for (const button of document.querySelectorAll('[data-view]')) {
    const active = button.dataset.view === name;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  document.title = `RailShot · ${views[name].querySelector('h1').textContent}`;
  savePreferences({ view: name });
  if (sessionReady && name === 'history') loadHistory();
  if (sessionReady && name === 'monitor') loadEnvironments();
  else stopEnvironmentPolling();
  if (sessionReady && name === 'monitor' && consoleTab === 'app') refreshLogs();
}

document.querySelectorAll('[data-view]').forEach((button) => {
  button.addEventListener('click', () => showView(button.dataset.view));
});
document.querySelector('.brand').addEventListener('click', (event) => {
  event.preventDefault();
  showView('deploy');
});

const archive = document.querySelector('#archive');
const folder = document.querySelector('#folder');
const repositoryUrl = document.querySelector('#repository-url');
const selection = document.querySelector('#source-selection');
const sourceBox = document.querySelector('#source-box');
const error = document.querySelector('#form-error');
const provider = document.querySelector('#provider');
const providerField = document.querySelector('#provider-field');
const cloudProvider = document.querySelector('#cloud-provider');
const deploymentDatabase = document.querySelector('#deployment-database');
const deployButton = document.querySelector('#deploy-button');
const requestError = document.querySelector('#request-error');
let selectedSource = null;
let reviewed = null;
let deploymentOptions = [];
let profiles = [];
let reviewing = false;
let reviewGeneration = 0;
let connectionError = null;
let submitting = false;
let current = null;
let sessionReady = false, preferenceTimer;
let history = [], connections = [], editingConnection = null;
let historyMarkers = [null], historyNext = null, historyTotal = null, historyController, historyError = null;
const savedHistory = window.history.state?.railshotHistory;
let historyKind = savedHistory?.kind === 'builds' ? 'builds' : 'deployments';
if (Array.isArray(savedHistory?.markers) && savedHistory.markers.length <= 100 && savedHistory.markers[0] === null
    && savedHistory.markers.slice(1).every((value) => typeof value === 'string' && value.length <= 1024)) historyMarkers = savedHistory.markers;
document.querySelector('#history-kind').value = historyKind;
let targets = [], observations = new Map(), environmentController, environmentTimer;

let preferences = { view: 'deploy', environment: 'cloud', provider: '' };
// The cookie is HttpOnly; no session token, credential, or execution locator enters localStorage.
try { localStorage.removeItem('railshot.lastExecution'); } catch { /* Storage may be disabled. */ }
let timer;
let pollController;
let lastReadAt = null;
let observationError = false;
let logSnapshot = null, logController;
const requests = new Set();

function invalidateReview() {
  reviewGeneration += 1;
  reviewed = null;
  document.querySelector('#review-panel').hidden = true;
  deployButton.disabled = true;
  error.hidden = true;
}

function setSource(source) {
  selectedSource = source;
  selection.textContent = source ? source.label : '';
  selection.hidden = !source;
  invalidateReview();
}

document.querySelector('#choose-file').addEventListener('click', () => archive.click());
document.querySelector('#choose-folder').addEventListener('click', () => folder.click());
archive.addEventListener('change', () => {
  const file = archive.files[0];
  if (!file) return;
  folder.value = '';
  repositoryUrl.value = '';
  setSource({ kind: 'archive', label: file.name });
});
folder.addEventListener('change', () => {
  if (!folder.files.length) return;
  archive.value = '';
  repositoryUrl.value = '';
  const name = (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0];
  setSource({ kind: 'folder', label: `${name} · ${folder.files.length}개 파일` });
});
repositoryUrl.addEventListener('input', () => {
  archive.value = '';
  folder.value = '';
  const url = repositoryUrl.value.trim();
  setSource(url ? { kind: 'repository', label: url } : null);
});
sourceBox.addEventListener('dragover', (event) => {
  event.preventDefault();
  sourceBox.classList.add('dragging');
});
sourceBox.addEventListener('dragleave', () => sourceBox.classList.remove('dragging'));
sourceBox.addEventListener('drop', (event) => {
  event.preventDefault();
  sourceBox.classList.remove('dragging');
  if (submitting) return;
  const files = [...event.dataTransfer.files];
  if (files.length !== 1 || !files[0].name.toLowerCase().endsWith('.zip')) {
    error.textContent = 'ZIP 파일 하나를 놓아주세요. 폴더는 폴더 선택을 이용하세요.';
    error.hidden = false;
    return;
  }
  const transfer = new DataTransfer();
  transfer.items.add(files[0]);
  archive.files = transfer.files;
  folder.value = '';
  repositoryUrl.value = '';
  setSource({ kind: 'archive', label: files[0].name });
});

const terminal = new Set(['succeeded', 'failed', 'blocked', 'unknown', 'published', 'publication_unverified']);
const labels = { queued: '실행 대기 중', running: '실행 중', succeeded: '앱 배포 완료', published: '이미지 게시 완료',
  failed: '실행 실패', blocked: '실행 조건 확인 필요', unknown: '실행 결과 확인 필요', publication_unverified: '게시 결과 확인 필요' };
function executionLabel(row) { return row.kind === 'builds' && row.status === 'succeeded' ? '이미지 게시 완료' : labels[row.status] || row.status || '실행 상태 확인 중'; }
function activeRun() { return current && (current.status === 'unknown' || !terminal.has(current.status)); }
function deploymentSelection() {
  const environment = document.querySelector('[name="environment"]:checked').value;
  return { environment, provider: environment === 'cloud' ? cloudProvider.value : provider.value };
}
function selectedOption() {
  const selected = deploymentSelection();
  return deploymentOptions.find((item) => item.environment === selected.environment && item.provider === selected.provider);
}
function selectedProfiles() {
  return profiles.filter((item) => item.provider === deploymentSelection().provider && item.deployment_supported);
}
function selectedProfile() { const matches = selectedProfiles(); return matches.length === 1 ? matches[0] : null; }
function planCost(plan) {
  return plan.cost ? ` 추가 비용 예상 ${plan.cost.incremental_estimate} ${plan.cost.currency} · 기존 사용·예약을 포함한 예상 ${plan.cost.projected_total} / 한도 ${plan.cost.limit} ${plan.cost.currency}.` : '';
}

function databaseSummary(profile, mode) {
  const database = profile?.database;
  return mode === 'patroni' && database ? `PostgreSQL ${database.database_nodes}대 · DCS 투표 노드 ${database.dcs_voters}대 · 프록시 ${database.proxy_nodes}대` : 'DB 없음';
}
function databaseChoice(select, profile, reset = false) {
  const available = profile?.database?.mode === 'patroni';
  const required = available && profile.database.required === true;
  select.querySelector('[value="none"]').disabled = required;
  const option = select.querySelector('[value="patroni"]');
  option.disabled = !available;
  option.textContent = required ? 'PostgreSQL HA 포함 (필수)' : 'PostgreSQL HA 포함';
  if (!available) select.value = 'none';
  else if (reset || required) select.value = 'patroni';
}
function updateSelection() {
  const selected = deploymentSelection();
  providerField.hidden = selected.environment !== 'onprem';
  document.querySelector('#cloud-provider-field').hidden = selected.environment !== 'cloud';
  const profile = selectedProfile();
  document.querySelector('#deployment-database-field').hidden = !profile?.database;
  databaseChoice(deploymentDatabase, profile);
  document.querySelector('#deployment-database-note').textContent = databaseSummary(profile, deploymentDatabase.value);
  document.querySelector('#connection-status').textContent = connectionError || (selectedProfiles().length > 1
    ? '사용할 배포 사양을 운영자가 하나로 지정해야 합니다.'
    : profile ? (profile.supported ? '새 실행 환경과 앱을 함께 준비합니다. 선택 내용을 확인하면 비용과 실행 계획을 표시합니다.' : '환경 생성 사양을 아직 실행할 수 없습니다.')
    : selectedOption()?.message || (selected.environment === 'onprem' && !selected.provider ? '온프레미스 인프라 종류를 선택하세요.' : '실행 가능한 인프라 연결을 준비 중입니다.'));
  invalidateReview();
  savePreferences(selected);
}
document.querySelectorAll('[name="environment"]').forEach((input) => input.addEventListener('change', updateSelection));
provider.addEventListener('change', updateSelection);
cloudProvider.addEventListener('change', updateSelection);
deploymentDatabase.addEventListener('change', updateSelection);

async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.method === 'POST' ? (path === '/api/v1/plans' ? 600000 : 120000) : path.endsWith('/logs') ? 60000 : 15000);
  try {
    const response = await fetch(path, { credentials: 'same-origin', ...options, signal: controller.signal, redirect: 'error' });
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) throw new Error(data.error?.message || (typeof data.error === 'string' ? data.error : `요청 실패 (HTTP ${response.status})`));
    return { data, location: response.headers.get('location') };
  } finally { clearTimeout(timeout); requests.delete(controller); }
}

async function checkConnection() {
  try {
    const [{ data }, { data: catalog }] = await Promise.all([request('/api/v1/options'), request('/api/v1/profiles?limit=100')]);
    if (!Array.isArray(catalog.items) || catalog.next_marker) throw new Error('배포 사양 목록을 확인하지 못했습니다.');
    profiles = catalog.items;
    if (!Array.isArray(data.items)) throw new Error('인프라 연결 상태를 확인하지 못했습니다.');
    deploymentOptions = data.items;
    connectionError = null;
  } catch (cause) {
    deploymentOptions = []; profiles = [];
    connectionError = cause.message;
  }
  updateSelection();
}

async function createPlan(name, profile, mode) {
  const database = mode === 'patroni' ? { mode, placements: [{ profile_id: profile.id,
    database_nodes: profile.database.database_nodes, dcs_voters: profile.database.dcs_voters, proxy_nodes: profile.database.proxy_nodes }] } : { mode: 'none' };
  const { data } = await request('/api/v1/plans', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, runtime: { profile_id: profile.id, node_count: 1 }, database }) });
  if (typeof data.id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(data.id) || data.name !== name || data.runtime?.profile_id !== profile.id
      || data.database?.mode !== mode || !Number.isFinite(Date.parse(data.expires_at))
      || (data.runtime_target_id !== undefined && (typeof data.runtime_target_id !== 'string' || !/^[a-z][a-z0-9-]{2,39}$/.test(data.runtime_target_id)))) throw new Error('환경 계획 응답이 선택 내용과 일치하지 않습니다.');
  return data;
}

async function sourceApplication(profile, source) {
  if (!profile.create_per_request && profile.application_name) return profile.application_name;
  const name = source.kind === 'repository' ? source.label.split('/').filter(Boolean).at(-1).replace(/\.git$/, '')
    : source.kind === 'archive' ? archive.files[0].name : (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0];
  const app = name.normalize('NFKD').toLowerCase().replace(/\.zip$/i, '').replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '').replace(/^[^a-z]+/, '').slice(0, 30).replace(/-+$/g, '');
  if (/^[a-z][a-z0-9-]{1,28}[a-z0-9]$/.test(app)) return app;
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(name))));
  return `app-${[...hash].map((value) => value.toString(16).padStart(2, '0')).join('').slice(0, 10)}`;
}

document.querySelector('#deploy-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (submitting || reviewing) return;
  const selected = deploymentSelection();
  const profile = selectedProfile();
  const option = selectedOption();
  if (!selectedSource) error.textContent = '배포할 소스를 선택하세요.';
  else if (selectedSource.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(selectedSource.label)) error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  else if (selectedSource.kind === 'archive' && !archive.files[0].name.toLowerCase().endsWith('.zip')) error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  else if (selected.environment === 'onprem' && !selected.provider) error.textContent = '온프레미스 인프라 종류를 선택하세요.';
  else if (connectionError || selectedProfiles().length > 1 || (profile ? !profile.supported : !option?.available)) error.textContent = connectionError || (profile || selectedProfiles().length > 1 ? document.querySelector('#connection-status').textContent : option?.message) || '실행 가능한 인프라가 아직 연결되지 않았습니다.';
  else if (activeRun()) error.textContent = '진행 중인 실행을 먼저 확인하세요.';
  else {
    invalidateReview();
    const generation = reviewGeneration, source = selectedSource;
    let plan;
    reviewing = true;
    const reviewButton = document.querySelector('#deploy-form button[type="submit"]');
    reviewButton.disabled = true;
    try {
      if (profile) {
        const app = await sourceApplication(profile, source);
        if (generation !== reviewGeneration) return;
        plan = await createPlan(app, profile, deploymentDatabase.value);
        if (generation !== reviewGeneration) return;
        if (!plan.executable) throw new Error(`현재 실행할 수 없는 계획입니다: ${(plan.blockers || []).join(', ')}${planCost(plan)}`);
        if (Date.parse(plan.expires_at) <= Date.now()) throw new Error('환경 계획이 만료됐습니다. 선택 내용을 다시 확인하세요.');
      }
    } catch (cause) {
      if (generation === reviewGeneration) { error.textContent = cause.message; error.hidden = false; }
      return;
    } finally { reviewing = false; reviewButton.disabled = false; }
    reviewed = { ...selected, source, kind: 'deployments', key: crypto.randomUUID(), plan,
      targetId: plan?.runtime_target_id || profile?.target_id };
    document.querySelector('#review-source').textContent = source.label;
    document.querySelector('#review-target').textContent = profile ? `클라우드 · ${profile.label || profile.id} · ${databaseSummary(profile, plan.database.mode)}` : option.label;
    document.querySelector('#review-note').textContent = plan
      ? `앱 ${plan.name}: 새 자원을 생성하고 ${plan.database.mode === 'patroni' ? 'DB 준비, ' : ''}소스 검사, 이미지 게시, 앱 적용과 공개 URL 확인을 시작합니다. 계획 유효 시각: ${new Date(plan.expires_at).toLocaleTimeString('ko-KR')}.${planCost(plan)}`
      : option.message;
    deployButton.disabled = false;
    requestError.hidden = true;
    error.hidden = true;
    document.querySelector('#review-panel').hidden = false;
    document.querySelector('#review-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }
  error.hidden = false;
});
document.querySelector('#edit-selection').addEventListener('click', () => { if (!submitting) invalidateReview(); });

function remember() {
  history = history.map((row) => row.id === current.id && row.kind === current.kind ? { ...row, ...current } : row);
  renderHistory();
}
function safeLink(selector, value, enabled, github = false) {
  const link = document.querySelector(selector);
  link.hidden = true;
  link.removeAttribute('href');
  try {
    const url = new URL(value);
    if (enabled && url.protocol === 'https:' && !url.username && !url.password && (!github || url.origin === 'https://github.com')) {
      link.href = url.href; link.hidden = false;
    }
  } catch { /* Only a verified HTTPS link is shown. */ }
}
function taskList(tasks = []) {
  const list = document.createElement('ul');
  for (const task of tasks) list.append(element('li', `${task.number}. ${task.name}: ${task.conclusion || task.status}`));
  return list;
}
function renderRun() {
  document.querySelector('#run-panel').hidden = false;
  const label = executionLabel(current);
  document.querySelector('#run-freshness').textContent = lastReadAt ? `마지막 상태 조회: ${new Date(lastReadAt).toLocaleString()}${observationError ? ' · 조회 실패, 마지막 기록입니다.' : ''}` : '서버 상태 조회 전';
  document.querySelector('#run-meta').textContent = `${current.app || '앱'} · ${current.id} · ${current.target_id || ''}`;
  document.querySelector('#run-state').textContent = label;
  document.querySelector('#run-message').textContent = current.error?.message || current.message || (activeRun()
    ? '15초마다 상태를 확인합니다. 페이지를 닫아도 서버의 실행은 계속됩니다.'
    : current.status === 'published' ? '검증된 이미지가 게시됐습니다. 앱 배포 완료와는 별개입니다.'
    : current.status === 'unknown' ? '결과를 확인하기 전에는 같은 작업을 새로 실행하지 않습니다.' : '서버가 확인한 최종 실행 결과입니다.');
  const steps = current.steps || current.ci?.steps || [];
  document.querySelector('#run-steps').replaceChildren(...steps.map((step) => {
    const item = document.createElement('li');
    item.textContent = `${({ loop: '앱 검사 및 수정', release: '검증 이미지 게시' })[step.key] || step.key}: ${step.conclusion || step.status}`;
    if (step.tasks?.length) item.append(taskList(step.tasks));
    return item;
  }));
  if (current.environment) {
    const item = document.createElement('li'); item.textContent = `환경 준비: ${current.environment.stage || ''} · ${current.environment.status}`;
    document.querySelector('#run-steps').prepend(item);
  }
  for (const [name, value] of [['DB migration', current.cd?.migration?.state], ['앱 적용', current.cd?.state], ['공개 URL', current.public_http?.state]]) {
    if (value) { const item = document.createElement('li'); item.textContent = `${name}: ${value}`; document.querySelector('#run-steps').append(item); }
  }
  for (const selector of ['#actions-link', '#monitor-actions-link']) safeLink(selector, current.actions_url || current.ci?.actions_url, true, true);
  document.querySelector('#monitor-message').textContent = document.querySelector('#run-message').textContent;
  const canOpen = current.kind === 'deployments' && current.status === 'succeeded' && current.public_http?.state === 'succeeded' && Boolean(current.public_http?.verified_at);
  for (const selector of ['#application-link', '#monitor-application-link']) safeLink(selector, current.public_http?.site_url || current.url || current.public_http?.url, canOpen);
  const binding = [`앱 / 대상: ${current.app || '—'} / ${current.target_id || '—'}`, `배포: ${current.id}`,
    `현재 단계: ${current.stage || 'ci'}`, `CI run: ${current.ci?.run_id || (current.kind === 'builds' ? current.id : '대기')}`,
    `소스 commit: ${current.source_commit || '대기'}`, `입력 SHA-256: ${current.source_digest || '미제공'}`,
    `이미지: ${Object.values(current.ci?.images || current.publication?.images || {}).join(', ') || '게시 대기'}`,
    `배포 revision: ${current.cd?.revision || '대기'}`].join('\n');
  document.querySelector('#run-binding').textContent = binding;
  const stageStates = ['접수 완료', steps.find((step) => step.key === 'loop')?.conclusion || steps.find((step) => step.key === 'loop')?.status,
    steps.find((step) => step.key === 'release')?.conclusion || steps.find((step) => step.key === 'release')?.status, current.cd?.state, current.public_http?.state];
  document.querySelector('#monitor-steps').replaceChildren(...['소스 접수', '앱 검사 및 수정', '검증 이미지 게시', 'GitOps 반영', 'URL 및 앱 상태 확인'].map((label, index) => {
    const item = document.createElement('li'); item.textContent = `${label} · ${stageStates[index] || '대기'}`;
    const step = steps.find((step) => step.key === ({ 1: 'loop', 2: 'release' })[index]);
    if (step?.tasks?.length) item.append(taskList(step.tasks));
    return item;
  }));
  renderHistory();
  document.querySelector('#monitor-state').textContent = observationError ? `${label} · 상태 조회 실패` : label;
  renderMetrics();
  renderConsole();
}

deployButton.addEventListener('click', async () => {
  if (submitting || !reviewed || deployButton.disabled || activeRun()) return;
  submitting = true;
  deployButton.disabled = true;
  requestError.hidden = true;
  const draft = reviewed;
  const controls = document.querySelectorAll('#deploy-form input, #deploy-form button, #deploy-form select, #edit-selection');
  controls.forEach((control) => { control.disabled = true; });
  try {
    if (draft.plan && !draft.attempted && Date.parse(draft.plan.expires_at) <= Date.now()) {
      reviewed = null; throw new Error('환경 계획이 만료됐습니다. 선택 내용을 다시 확인하세요.');
    }
    const payload = new FormData();
    if (draft.plan) {
      payload.set('app', draft.plan.name); payload.set('target_id', draft.targetId); payload.set('plan_id', draft.plan.id);
    } else { payload.set('environment', draft.environment); payload.set('provider', draft.provider); }
    if (!draft.plan && draft.source.kind === 'folder') payload.set('source_name', (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0]);
    if (draft.source.kind === 'repository') payload.set('repository_url', draft.source.label);
    else if (draft.source.kind === 'archive') payload.set('archive', archive.files[0]);
    else {
      const paths = [];
      for (const file of folder.files) {
        const parts = (file.webkitRelativePath || file.name).split('/');
        const path = parts.length > 1 ? parts.slice(1).join('/') : file.name;
        if (path.split('/').some((part) => ['.git', 'node_modules', '__MACOSX', '.DS_Store'].includes(part))) continue;
        paths.push(path); payload.append('files', file, file.name);
      }
      payload.set('paths', JSON.stringify(paths));
    }
    draft.attempted = true;
    const { data, location } = await request(`/api/v1/${draft.kind}`, {
      method: 'POST', headers: { 'Idempotency-Key': draft.key }, body: payload,
    });
    const id = data.resource_id || data.id;
    if (typeof id !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(id) || location !== `/api/v1/${draft.kind}/${encodeURIComponent(id)}`) throw new Error('실행 조회 주소를 확인하지 못했습니다.');
    current = { ...data, id, kind: draft.kind, status: data.status === 'accepted' ? 'queued' : data.status };
    lastReadAt = null; observationError = false; remember(); renderRun();
    reviewed = null;
    historyKind = draft.kind; document.querySelector('#history-kind').value = historyKind;
    loadHistory([null]);
    document.querySelector('#run-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    refreshRun();
  } catch (cause) {
    requestError.textContent = `${cause.name === 'AbortError' ? '요청 시간이 초과되었습니다.' : cause.message} 서버에서 이미 처리했을 수 있습니다. 배포 재요청은 같은 요청 키를 사용합니다.`;
    requestError.hidden = false;
    // Only deployments promise a safe retry with the same key and unchanged source.
    deployButton.disabled = draft.kind !== 'deployments';
  } finally {
    submitting = false;
    controls.forEach((control) => { control.disabled = false; });
    if (!reviewed) deployButton.disabled = true;
  }
});
function stopPolling() {
  clearTimeout(timer); timer = null; pollController?.abort(); pollController = null;
  document.querySelector('#stop-polling').hidden = true;
  document.querySelector('#refresh-run').hidden = !current;
}
async function refreshRun() {
  stopPolling();
  if (!current) return;
  const controller = new AbortController(); pollController = controller;
  document.querySelector('#stop-polling').hidden = false;
  document.querySelector('#refresh-run').hidden = true;
  try {
    const { data } = await request(`/api/v1/${current.kind}/${encodeURIComponent(current.id)}`, {}, controller);
    if (pollController !== controller) return;
    if (data.id !== current.id || (current.target_id && data.target_id !== current.target_id)) throw new Error('실행 또는 대상이 요청과 일치하지 않습니다.');
    current = { ...current, ...data }; lastReadAt = Date.now(); observationError = false; remember(); renderRun();
    if (consoleTab === 'app' && !views.monitor.hidden) refreshLogs();
    if (!terminal.has(current.status) || current.kind === 'deployments') timer = setTimeout(refreshRun, 15000);
    else stopPolling();
  } catch (cause) {
    if (pollController !== controller) return;
    stopPolling(); observationError = true;
    timer = setTimeout(refreshRun, 15000); renderRun();
    document.querySelector('#stop-polling').hidden = false;
    document.querySelector('#run-message').textContent = `${cause.name === 'AbortError' ? '상태 조회 시간이 초과되었습니다.' : cause.message} 15초 후 다시 조회합니다.`;
  }
}
document.querySelector('#stop-polling').addEventListener('click', () => { stopPolling(); renderMetrics(); document.querySelector('#run-message').textContent = '상태 조회를 중지했습니다. 서버의 실행은 계속됩니다.'; });
document.querySelector('#refresh-run').addEventListener('click', refreshRun);
window.addEventListener('pagehide', () => { stopPolling(); stopEnvironmentPolling(); for (const controller of requests) controller.abort(); });
let consoleTab = 'work';
const logLabels = { loading: '앱 로그를 조회하고 있습니다.', not_deployed: '앱 적용 전입니다. 작업 로그에서 CI 진행과 실패 원인을 확인하세요.',
  not_configured: '이 배포 대상의 로그 조회 설정이 없습니다.', unavailable: '앱 로그 조회에 실패했습니다. 다음 조회에서 다시 확인합니다.',
  no_data: '현재 컨테이너에서 출력한 로그가 없습니다.', superseded: '다른 배포 버전이 적용되어 이 기록의 앱 로그를 표시하지 않습니다.' };
async function refreshLogs() {
  if (logController && logSnapshot?.deployment_id === current?.id) return;
  logController?.abort();
  if (!current || current.kind !== 'deployments') return;
  const id = current.id, controller = new AbortController(); logController = controller;
  logSnapshot = { deployment_id: id, state: 'loading' }; renderConsole();
  try {
    const { data } = await request(`/api/v1/deployments/${encodeURIComponent(id)}/logs`, {}, controller);
    if (logController !== controller || current?.id !== id) return;
    if (data.deployment_id !== id || data.app !== current.app || data.target_id !== current.target_id) throw new Error('로그 대상 불일치');
    logSnapshot = data;
  } catch {
    if (logController !== controller || current?.id !== id) return;
    logSnapshot = { deployment_id: id, state: 'unavailable' };
  } finally { if (logController === controller) { logController = null; renderConsole(); } }
}
function renderConsole() {
  const logs = logSnapshot?.deployment_id === current?.id ? logSnapshot : null;
  const data = !current ? '실행을 시작하면 확인된 상태가 여기에 표시됩니다.'
    : consoleTab === 'app' ? (logs?.state === 'ready'
      ? [`조회 ${new Date(logs.checked_at).toLocaleString()}`, ...logs.entries.map((entry) => `[${entry.pod} / ${entry.container}]\n${entry.text}`)].join('\n\n')
      : logLabels[logs?.state] || '앱 로그 탭을 선택하면 현재 배포의 로그를 조회합니다.')
    : consoleTab === 'environment' ? { target_id: current.target_id, environment: current.environment || null, cd: current.cd || null, public_http: current.public_http || null, observation: current.observation || null }
    : { status: current.status, stage: current.stage || 'ci', diagnostics: current.ci?.diagnostics || current.diagnostics || null,
      steps: current.steps || current.ci?.steps || [], error: current.error || null };
  document.querySelector('#console-output').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
}
const metricLabels = { not_configured: '연결 전', unsupported: '대상 미지원', unavailable: '수집 연결 실패', collection_failed: '수집 실패', no_data: '데이터 없음', stale: '오래된 값' };
function renderMetrics() {
  const observation = current?.observation;
  const bound = observation && observation.deployment_id === current.id && observation.target_id === current.target_id && observation.app === current.app;
  for (const name of ['pods', 'cpu_percent', 'memory_percent', 'http']) {
    const metric = bound ? observation.metrics?.[name] : null;
    let state = observationError ? 'unavailable' : metric ? metricState(metric, observation) : current?.kind === 'builds' ? 'unsupported' : 'not_configured';
    const time = Date.parse(metric?.observed_at);
    if (state === 'ready' && (!Number.isFinite(time) || Date.now() - time > Math.min(observation.stale_after_seconds || 90, 90) * 1000 || time > Date.now() + 5000)) state = 'stale';
    const field = document.querySelector(`#metric-${name}`);
    field.dataset.state = state === 'ready' && name === 'http' && metric.value === 0 ? 'collection_failed' : state;
    field.textContent = state !== 'ready' ? metricLabels[state] || '확인 불가'
      : name === 'http' ? (metric.value === 1 ? '2xx 응답' : '검사 실패')
      : name === 'pods' ? `${metric.value}개` : `${Number(metric.value).toFixed(1)}%`;
    document.querySelector(`#metric-${name}-time`).textContent = Number.isFinite(time) ? `수집 ${new Date(time).toLocaleString()}` : '수집 시각 없음';
  }
  document.querySelector('#observation-status').textContent = observationError && (pollController || timer) ? '관측 조회 실패 · 재시도 중'
    : bound ? `${pollController || timer ? '조회' : '조회 중지 · 마지막 조회'} ${new Date(observation.checked_at).toLocaleTimeString()}` : '관측 연결 대기';
  const collector = bound && observation.collector;
  document.querySelector('#collector-note').textContent = collector
    ? `공유 관측 서버 · ${collector.lifecycle === 'acceptance' ? '임시 인수용' : '운영용'} · 만료 ${new Date(collector.expires_at).toLocaleString()}`
    : '수집기 수명 정보 미제공';
}
// Even after polling is stopped, expire old samples on screen.
const freshnessTimer = setInterval(() => { renderMetrics(); renderEnvironments(); }, 15000);
window.addEventListener('pagehide', () => clearInterval(freshnessTimer));
document.querySelectorAll('[data-console]').forEach((button) => button.addEventListener('click', () => {
  consoleTab = button.dataset.console;
  document.querySelectorAll('[data-console]').forEach((tab) => tab.setAttribute('aria-selected', String(tab === button)));
  renderConsole();
  if (consoleTab === 'app') refreshLogs();
}));
function savePreferences(patch) {
  if (!sessionReady) return;
  Object.assign(preferences, patch);
  clearTimeout(preferenceTimer);
  preferenceTimer = setTimeout(() => request('/api/v1/preferences', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(preferences) }).catch(() => { document.querySelector('#session-note').textContent = '화면 설정을 저장하지 못했습니다. 연결을 확인하세요.'; }), 200);
}
function formatTime(value) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR') : '관측 시각 없음';
}
function element(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
function showHistoryError(cause) { historyError = cause.message; document.querySelector('#history-detail').textContent = `내역 조회 실패: ${cause.message} 최신 내역을 눌러 다시 확인하세요.`; }
function openExecution(row, tab = 'work') {
  stopPolling(); logController?.abort(); logSnapshot = null;
  current = row; lastReadAt = null; observationError = false; consoleTab = tab;
  document.querySelectorAll('[data-console]').forEach((button) => button.setAttribute('aria-selected', String(button.dataset.console === tab)));
  showView('monitor'); renderRun(); refreshRun();
  document.querySelector('.execution-heading').scrollIntoView({ block: 'start' });
}
function renderHistory() {
  const kindLabel = historyKind === 'builds' ? '빌드' : '배포';
  document.querySelector('#history-summary').textContent = historyError ? '실행 내역을 확인하지 못했습니다' : history.length
    ? `이 세션의 ${kindLabel}${Number.isInteger(historyTotal) ? ` · 전체 ${historyTotal}건` : ''}` : `아직 ${kindLabel} 내역이 없습니다`;
  document.querySelector('#history-page').textContent = `${historyMarkers.length}페이지 · ${history.length}건`;
  document.querySelector('#history-prev').disabled = Boolean(historyController) || historyMarkers.length < 2;
  document.querySelector('#history-next').disabled = Boolean(historyController) || !historyNext;
  document.querySelector('#history-list').replaceChildren(...history.map((row) => {
    const item = document.createElement('li'), header = element('div', '', 'history-row'), content = document.createElement('div');
    const badge = element('span', executionLabel(row), 'state-badge');
    badge.dataset.state = ['failed', 'blocked'].includes(row.status) ? 'failed' : row.status === 'succeeded' ? 'ready' : 'unknown';
    content.append(element('strong', row.app || '앱 이름 미제공'), element('small', `환경 ${row.target_id || '미지정'} · ${formatTime(row.created_at)}`, 'history-meta'));
    header.append(content, badge);
    const actions = element('div', '', 'history-actions');
    for (const [label, tab] of [['실행 상세·작업 로그', 'work'], ...(row.kind === 'deployments' ? [['앱 로그', 'app']] : [])]) {
      const button = element('button', label, 'text-button'); button.type = 'button';
      button.setAttribute('aria-label', `${row.app || '앱'} ${label}`);
      button.addEventListener('click', () => openExecution(row, tab)); actions.append(button);
    }
    item.append(header, element('small', `실행 ${row.id}`, 'history-meta'), actions); return item;
  }));
}
async function loadHistory(markers = historyMarkers) {
  historyController?.abort();
  const controller = new AbortController(), kind = historyKind; historyController = controller;
  history = []; historyNext = null; historyError = null; renderHistory();
  document.querySelector('#history-summary').textContent = '실행 내역을 불러오는 중입니다';
  document.querySelector('#history-detail').textContent = '이 브라우저 세션의 기록을 서버에서 조회합니다.';
  document.querySelector('#history-list').setAttribute('aria-busy', 'true');
  const query = new URLSearchParams({ limit: '10' });
  if (markers.at(-1)) query.set('marker', markers.at(-1));
  try {
    const { data } = await request(`/api/v1/${kind}?${query}`, {}, controller);
    if (historyController !== controller) return;
    if (!Array.isArray(data.items) || data.items.some((row) => typeof row.id !== 'string')
        || (data.next_marker != null && typeof data.next_marker !== 'string')) throw new Error('내역 응답을 확인하지 못했습니다.');
    history = data.items.map((row) => ({ ...row, kind })); historyNext = data.next_marker || null;
    historyTotal = Number.isInteger(data.total) ? data.total : null; historyMarkers = markers;
    window.history.replaceState({ ...window.history.state, railshotHistory: { kind, markers } }, '');
    document.querySelector('#history-detail').textContent = '최신 접수 순 · 페이지 이동 중 새 실행이 생기면 최신 내역에서 확인하세요.';
  } catch (cause) {
    if (historyController !== controller) return;
    showHistoryError(cause);
  } finally {
    if (historyController === controller) {
      historyController = null; renderHistory();
      document.querySelector('#history-list').setAttribute('aria-busy', 'false');
    }
  }
}
document.querySelector('#history-kind').addEventListener('change', (event) => { historyKind = event.target.value; loadHistory([null]); });
document.querySelector('#history-refresh').addEventListener('click', () => loadHistory([null]));
document.querySelector('#history-prev').addEventListener('click', () => loadHistory(historyMarkers.slice(0, -1)));
document.querySelector('#history-next').addEventListener('click', () => { if (historyNext) loadHistory([...historyMarkers, historyNext]); });

const metricNames = { node_up: '노드 수집', cpu_percent: 'CPU', memory_percent: '메모리', disk_percent: '디스크',
  network_receive_bytes_per_second: '네트워크 수신', network_transmit_bytes_per_second: '네트워크 송신', pods: '실행 중 Pod', http: '앱 HTTP' };
const providerNames = { aws: 'AWS', gcp: 'GCP', openstack: 'OpenStack' };
function metricState(metric, observation) {
  if (metric?.state !== 'ready') return metric?.state || 'not_configured';
  const time = Date.parse(metric.observed_at), value = metric.value;
  if (!Number.isFinite(time) || Date.now() - time > Math.min(observation?.stale_after_seconds || 90, 90) * 1000 || time > Date.now() + 5000) return 'stale';
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? 'ready' : 'no_data';
}
function metricText(name, observation) {
  const metric = observation?.metrics?.[name], state = metricState(metric, observation);
  if (state !== 'ready') return metricLabels[state] || '확인 불가';
  if (name === 'http') return metric.value === 1 ? '2xx 응답' : '검사 실패';
  if (name === 'node_up') return metric.value === 1 ? '수집 중' : '수집 실패';
  if (name.endsWith('_percent')) return `${metric.value.toFixed(1)}%`;
  if (name.endsWith('_per_second')) return `${(metric.value / 1024).toFixed(1)} KiB/s`;
  return `${metric.value}개`;
}
function environmentState(observation) {
  if (!observation) return 'missing';
  if (observation.failed) return 'failed';
  const metrics = Object.entries(observation.metrics || {}), states = metrics.map(([, metric]) => metricState(metric, observation));
  if (states.some((state) => ['collection_failed', 'unavailable'].includes(state))
      || metrics.some(([name, metric]) => ['node_up', 'http'].includes(name) && metricState(metric, observation) === 'ready' && metric.value === 0)) return 'failed';
  if (states.includes('stale')) return 'stale';
  if (metricState(observation.metrics?.node_up, observation) === 'ready') return 'ready';
  return 'missing';
}
function visibleTargets() {
  const provider = document.querySelector('#monitor-provider').value, target = document.querySelector('#monitor-target').value;
  return targets.filter((row) => (!provider || row.provider === provider) && (!target || row.id === target));
}
function renderEnvironments() {
  const opened = new Set([...document.querySelectorAll('#environment-detail details[open]')].map((item) => item.dataset.target));
  const focused = document.activeElement?.closest('#environment-detail details')?.dataset.target;
  const rows = visibleTargets(), stateNames = { ready: '노드 수집 정상', failed: '수집·응답 실패', missing: '미수집', stale: '오래된 관측' };
  document.querySelector('#environment-summary').replaceChildren(...Object.entries(stateNames).map(([state, label]) => {
    const count = rows.filter((row) => environmentState(observations.get(row.id)) === state).length;
    const node = element('span', label); node.append(element('strong', String(count))); return node;
  }));
  document.querySelector('#environment-list').replaceChildren(...rows.map((row) => {
    const observation = observations.get(row.id), state = environmentState(observation), tr = document.createElement('tr');
    const name = element('td', row.label || row.id);
    name.append(element('small', `${providerNames[row.provider] || row.provider || '종류 미제공'} · ${row.application_name || '앱 미지정'}`));
    const status = document.createElement('td'), badge = element('span', stateNames[state], 'state-badge'); badge.dataset.state = state; status.append(badge);
    tr.append(name, status, ...['cpu_percent', 'memory_percent', 'disk_percent', 'http'].map((metric) => element('td', observation?.failed ? '조회 실패' : metricText(metric, observation))));
    const times = Object.values(observation?.metrics || {}).map((metric) => Date.parse(metric.observed_at)).filter(Number.isFinite);
    tr.append(element('td', times.length ? formatTime(new Date(Math.min(...times)).toISOString()) : '수집 시각 없음'));
    return tr;
  }));
  document.querySelector('#environment-detail').replaceChildren(...rows.map((row) => {
    const observation = observations.get(row.id), details = element('details', '', 'environment-detail');
    details.dataset.target = row.id; details.open = opened.has(row.id);
    details.append(element('summary', `${row.label || row.id} · 전체 지표와 수집 시각`));
    const list = element('dl', '', 'metric-details');
    for (const [key, label] of Object.entries(metricNames)) {
      const cell = document.createElement('div'), value = element('dd', observation?.failed ? '조회 실패' : metricText(key, observation));
      value.append(element('small', formatTime(observation?.metrics?.[key]?.observed_at))); cell.append(element('dt', label), value); list.append(cell);
    }
    details.append(list); return details;
  }));
  if (focused) [...document.querySelectorAll('#environment-detail details')].find((item) => item.dataset.target === focused)?.querySelector('summary').focus();
}
function stopEnvironmentPolling() {
  clearTimeout(environmentTimer);
  const controller = environmentController; environmentController = null; controller?.abort();
}
async function loadEnvironments() {
  stopEnvironmentPolling();
  renderEnvironments();
  const controller = new AbortController(); environmentController = controller;
  document.querySelector('#environment-message').textContent = '실행 환경과 관측 지표를 조회하고 있습니다.';
  try {
    const { data } = await request('/api/v1/targets?limit=100', {}, controller);
    if (environmentController !== controller) return;
    if (!Array.isArray(data.items) || data.items.some((row) => typeof row.id !== 'string') || data.next_marker) throw new Error('환경 목록을 모두 확인하지 못했습니다.');
    targets = data.items;
    const select = document.querySelector('#monitor-target'), selected = select.value, provider = document.querySelector('#monitor-provider').value;
    const options = targets.filter((row) => !provider || row.provider === provider);
    select.replaceChildren(new Option('전체 환경', ''), ...options.map((row) => new Option(row.label || row.id, row.id)));
    if (options.some((row) => row.id === selected)) select.value = selected;
    const rows = visibleTargets();
    // Bound observer fan-out to four requests; targets are capped at 100 by the API.
    for (let index = 0; index < rows.length; index += 4) {
      await Promise.all(rows.slice(index, index + 4).map(async (row) => {
        try {
          const { data: observation } = await request(`/api/v1/targets/${encodeURIComponent(row.id)}/observations`, {}, controller);
          if (observation.target_id !== row.id || (row.application_name && observation.app !== row.application_name)) throw new Error('관측 대상 불일치');
          if (environmentController === controller) observations.set(row.id, observation);
        } catch {
          if (environmentController === controller) observations.set(row.id, { failed: true });
        }
      }));
      if (environmentController !== controller) return;
      if (controller.signal.aborted) throw new Error('지표 조회 시간이 초과되었습니다.');
    }
    renderEnvironments();
    document.querySelector('#environment-message').textContent = rows.length
      ? `마지막 조회 ${formatTime(new Date().toISOString())} · 30초마다 갱신 · 수집 실패는 0으로 표시하지 않습니다.`
      : '선택한 조건에 등록된 환경이 없습니다. 다른 클라우드를 선택하거나 앱을 배포하세요.';
  } catch (cause) {
    if (environmentController !== controller) return;
    observations = new Map(targets.map((row) => [row.id, { failed: true }])); renderEnvironments();
    document.querySelector('#environment-message').textContent = `환경 조회 실패: ${cause.name === 'AbortError' ? '조회 시간이 초과되었습니다.' : cause.message} 30초 후 다시 확인합니다.`;
  } finally {
    if (environmentController === controller) {
      environmentController = null;
      if (!views.monitor.hidden) environmentTimer = setTimeout(loadEnvironments, 30000);
    }
  }
}
document.querySelector('#monitor-provider').addEventListener('change', () => { document.querySelector('#monitor-target').value = ''; loadEnvironments(); });
document.querySelector('#monitor-target').addEventListener('change', loadEnvironments);
document.querySelector('#monitor-refresh').addEventListener('click', loadEnvironments);
function resetConnectionForm() {
  editingConnection = null; document.querySelector('#connection-form').reset();
  document.querySelector('#connection-cancel').hidden = true;
  document.querySelector('#connection-save').textContent = '저장';
}
async function loadConnections() {
  const { data } = await request('/api/v1/connections?limit=100'); connections = data.items;
  document.querySelector('#connection-list').replaceChildren(...connections.map((row) => {
    const item = document.createElement('li'), link = document.createElement('a'), detail = document.createElement('p');
    link.textContent = row.label; link.href = row.console_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    detail.textContent = `${row.username || '접속 ID 없음'} · 비밀번호 ${row.has_password ? '저장됨' : '없음'}`;
    const edit = document.createElement('button'), remove = document.createElement('button');
    for (const button of [edit, remove]) { button.type = 'button'; button.className = 'text-button'; }
    edit.textContent = '수정'; remove.textContent = '삭제';
    edit.addEventListener('click', () => {
      editingConnection = row.id;
      for (const [field, value] of [['label', row.label], ['url', row.console_url], ['username', row.username], ['password', '']]) document.querySelector(`#connection-${field}`).value = value;
      document.querySelector('#connection-clear-password').checked = false;
      document.querySelector('#connection-cancel').hidden = false; document.querySelector('#connection-save').textContent = '수정 저장';
      document.querySelector('#connection-label').focus();
    });
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      try {
        await request(`/api/v1/connections/${row.id}`, { method: 'DELETE' });
        if (editingConnection === row.id) resetConnectionForm();
        await loadConnections(); document.querySelector('#connection-message').textContent = '연결 정보를 삭제했습니다.';
      } catch (cause) { document.querySelector('#connection-message').textContent = cause.message; remove.disabled = false; }
    });
    item.append(link, detail, edit, remove); return item;
  }));
}
document.querySelector('#connection-cancel').addEventListener('click', resetConnectionForm);
document.querySelector('#connection-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!sessionReady) return;
  const button = document.querySelector('#connection-save'), password = document.querySelector('#connection-password');
  const body = { label: document.querySelector('#connection-label').value, console_url: document.querySelector('#connection-url').value,
    username: document.querySelector('#connection-username').value };
  if (document.querySelector('#connection-clear-password').checked) body.password = null;
  else if (password.value) body.password = password.value;
  button.disabled = true;
  try {
    await request(`/api/v1/connections${editingConnection ? '/' + editingConnection : ''}`, {
      method: editingConnection ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    resetConnectionForm(); await loadConnections();
    document.querySelector('#connection-message').textContent = '이 세션에 저장했습니다.';
  } catch (cause) { document.querySelector('#connection-message').textContent = cause.message; }
  finally { password.value = ''; delete body.password; button.disabled = false; }
});
async function initializeDashboard() {
  try {
    const { data: session } = await request('/api/v1/sessions', { method: 'POST' });
    const { data: saved } = await request('/api/v1/preferences');
    preferences = saved;
    document.querySelector(`[name="environment"][value="${saved.environment}"]`).checked = true;
    cloudProvider.value = ['aws', 'gcp'].includes(saved.provider) ? saved.provider : 'aws';
    provider.value = ['openstack', 'proxmox'].includes(saved.provider) ? saved.provider : '';
    showView(saved.view);
    await checkConnection();
    document.querySelector('#session-note').textContent = `이 브라우저 세션 · ${new Date(session.expires_at).toLocaleDateString()}까지 유지`;
    sessionReady = true;
    await Promise.allSettled([loadHistory().catch(showHistoryError), loadConnections().catch((cause) => {
      document.querySelector('#connection-message').textContent = cause.message;
    })]);
    if (history.length) { current = history[0]; renderRun(); refreshRun(); }
    if (!views.monitor.hidden) loadEnvironments();
  } catch (cause) {
    connectionError = cause.message; updateSelection();
    document.querySelector('#session-note').textContent = '세션을 불러오지 못했습니다. 새로고침하세요.';
    showHistoryError(cause);
  }
}
initializeDashboard();

const views = {
  deploy: document.querySelector('#deploy-view'),
  history: document.querySelector('#history-view'),
  monitor: document.querySelector('#monitor-view'),
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
const appName = document.querySelector('#app-name');
const targetSelect = document.querySelector('#target');
const operation = document.querySelector('#operation');
const deployButton = document.querySelector('#deploy-button');
const requestError = document.querySelector('#request-error');
let selectedSource = null;
let reviewed = null;
let targets = [];
let submitting = false;
// ponytail: remember one resource locator per browser; server owns durable execution records.
let current = null;
try { current = JSON.parse(localStorage.getItem('railshot.lastExecution')); } catch { /* Storage may be unavailable. */ }
if (!current || !/^(builds|deployments)$/.test(current.kind) || !/^[a-zA-Z0-9._-]{1,128}$/.test(current.id || '')) current = null;
let timer;
let pollController;
const requests = new Set();

function invalidateReview() {
  reviewed = null;
  document.querySelector('#review-panel').hidden = true;
  deployButton.disabled = true;
  error.hidden = true;
}

function setSource(source) {
  selectedSource = source;
  selection.textContent = source ? source.label : '';
  selection.hidden = !source;
  const name = source?.kind === 'repository' ? source.label.split('/').filter(Boolean).at(-1)?.replace(/\.git$/, '')
    : source?.kind === 'archive' ? archive.files[0].name.replace(/\.zip$/i, '')
    : source ? (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0] : '';
  appName.value = (name || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '').slice(0, 30).replace(/-+$/g, '');
  updateTarget();
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
function activeRun() { return current && !terminal.has(current.status); }
function selectedTarget() { return targets.find((item) => item.id === targetSelect.value); }
function supported(target, kind) { return Boolean(target?.capabilities?.[kind === 'builds' ? 'ci_submission' : 'application_deployment']); }
function updateTarget() {
  const target = selectedTarget();
  if (operation.value === 'deployments' && target?.application_name) appName.value = target.application_name;
  appName.readOnly = operation.value === 'deployments' && Boolean(target?.application_name);
  for (const option of operation.options) option.disabled = !supported(target, option.value);
  if (!supported(target, operation.value)) operation.value = [...operation.options].find((item) => !item.disabled)?.value || '';
  document.querySelector('#target-note').textContent = !target ? '사용할 수 있는 실행 대상이 없습니다.'
    : target.capabilities.application_deployment ? '이미지 게시 후 앱 적용과 공개 URL까지 확인합니다.'
    : '이 대상은 검사와 이미지 게시를 지원합니다. 앱 적용 연결은 아직 준비되지 않았습니다.';
  if (operation.value === 'deployments' && target?.application_name) {
    appName.value = target.application_name; appName.readOnly = true;
    document.querySelector('#target-note').textContent += ` 이 대상의 앱 이름은 ${target.application_name}입니다. 소스는 새로 올릴 수 있습니다.`;
  } else appName.readOnly = false;
  invalidateReview();
}
targetSelect.addEventListener('change', updateTarget);
operation.addEventListener('change', updateTarget);
appName.addEventListener('input', invalidateReview);

async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.method === 'POST' ? 120000 : 15000);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, redirect: 'error' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || (typeof data.error === 'string' ? data.error : `요청 실패 (HTTP ${response.status})`));
    return { data, location: response.headers.get('location') };
  } finally { clearTimeout(timeout); requests.delete(controller); }
}

async function checkConnection() {
  try {
    const { data } = await request('/api/v1/targets');
    if (!Array.isArray(data.items)) throw new Error('실행 대상 응답을 확인하지 못했습니다.');
    const before = targetSelect.value;
    targets = data.items;
    targetSelect.replaceChildren(...targets.map((target) => new Option(target.label || target.id, target.id)));
    if (!targets.length) targetSelect.append(new Option('실행 대상 준비 중', ''));
    else if (targets.some((target) => target.id === before)) targetSelect.value = before;
    document.querySelector('#connection-status').textContent = targets.length ? 'API 연결됨 · 실행 대상을 선택하세요.' : 'API 연결됨 · 실행 대상 준비 중';
    updateTarget();
  } catch (cause) {
    targets = [];
    targetSelect.replaceChildren(new Option('실행 대상 연결 확인 필요', ''));
    document.querySelector('#connection-status').textContent = cause.message;
    updateTarget();
  }
}

document.querySelector('#deploy-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (submitting) return;
  const target = selectedTarget();
  const app = appName.value.trim();
  if (!selectedSource) error.textContent = '배포할 소스를 선택하세요.';
  else if (selectedSource.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(selectedSource.label)) error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  else if (selectedSource.kind === 'archive' && !archive.files[0].name.toLowerCase().endsWith('.zip')) error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  else if (!/^[a-z][a-z0-9-]{1,28}[a-z0-9]$/.test(app)) error.textContent = '앱 이름은 영문 소문자로 시작하고 소문자나 숫자로 끝나는 3~30자여야 합니다.';
  else if (!supported(target, operation.value)) error.textContent = '실행 가능한 대상을 선택하세요.';
  else if (activeRun()) error.textContent = '진행 중인 실행을 먼저 확인하세요.';
  else {
    reviewed = { app, source: selectedSource, targetId: target.id, kind: operation.value, key: crypto.randomUUID() };
    document.querySelector('#review-source').textContent = selectedSource.label;
    document.querySelector('#review-app').textContent = app;
    document.querySelector('#review-target').textContent = target.label || target.id;
    document.querySelector('#review-note').textContent = operation.value === 'deployments'
      ? '소스 검사, 이미지 게시, 앱 적용과 공개 URL 확인을 시작합니다.' : '소스 검사와 이미지 게시를 시작합니다. 앱 배포는 수행하지 않습니다.';
    deployButton.textContent = operation.selectedOptions[0].textContent;
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
  try { localStorage.setItem('railshot.lastExecution', JSON.stringify({ kind: current.kind, id: current.id, app: current.app })); }
  catch { /* Execution records remain on the server when browser storage is disabled. */ }
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
function renderRun() {
  document.querySelector('#run-panel').hidden = false;
  const label = labels[current.status] || '실행 상태 확인 중';
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
    return item;
  }));
  for (const [name, value] of [['앱 적용', current.cd?.state], ['공개 URL', current.public_http?.state]]) {
    if (value) { const item = document.createElement('li'); item.textContent = `${name}: ${value}`; document.querySelector('#run-steps').append(item); }
  }
  safeLink('#actions-link', current.actions_url || current.ci?.actions_url, true, true);
  safeLink('#application-link', current.url || current.public_http?.url, current.kind === 'deployments' && current.status === 'succeeded' && Boolean(current.public_http?.verified_at));
  document.querySelector('#history-summary').textContent = `${current.app || '앱'} · ${current.id}`;
  document.querySelector('#history-detail').textContent = label;
  document.querySelector('#monitor-state').textContent = label;
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
    const payload = new FormData();
    payload.set('app', draft.app); payload.set('target_id', draft.targetId);
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
    const { data, location } = await request(`/api/v1/${draft.kind}`, {
      method: 'POST', headers: { 'Idempotency-Key': draft.key }, body: payload,
    });
    const id = data.resource_id || data.id;
    if (typeof id !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(id) || location !== `/api/v1/${draft.kind}/${encodeURIComponent(id)}`) throw new Error('실행 조회 주소를 확인하지 못했습니다.');
    current = { ...data, id, kind: draft.kind, app: draft.app, target_id: draft.targetId, status: data.status === 'accepted' ? 'queued' : data.status };
    remember(); renderRun();
    reviewed = null;
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
  clearTimeout(timer); pollController?.abort(); pollController = null;
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
    current = { ...current, ...data }; remember(); renderRun();
    if (!terminal.has(current.status)) timer = setTimeout(refreshRun, 15000);
    else stopPolling();
  } catch (cause) {
    if (pollController !== controller) return;
    stopPolling();
    document.querySelector('#run-message').textContent = `${cause.name === 'AbortError' ? '상태 조회 시간이 초과되었습니다.' : cause.message} 상태 다시 조회를 누르세요.`;
  }
}
document.querySelector('#stop-polling').addEventListener('click', () => { stopPolling(); document.querySelector('#run-message').textContent = '상태 조회를 중지했습니다. 서버의 실행은 계속됩니다.'; });
document.querySelector('#refresh-run').addEventListener('click', refreshRun);
window.addEventListener('pagehide', () => { stopPolling(); for (const controller of requests) controller.abort(); });
let consoleTab = 'work';
function renderConsole() {
  const data = !current ? '실행을 시작하면 확인된 상태가 여기에 표시됩니다.'
    : consoleTab === 'app' ? (current.public_http || '앱 로그 스트리밍은 제공하지 않습니다. 공개 HTTP 확인 결과가 여기에 표시됩니다.')
    : consoleTab === 'environment' ? { target_id: current.target_id, cd: current.cd || null, public_http: current.public_http || null }
    : { status: current.status, stage: current.stage || 'ci', steps: current.steps || current.ci?.steps || [], error: current.error || null };
  document.querySelector('#console-output').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
}
document.querySelectorAll('[data-console]').forEach((button) => button.addEventListener('click', () => {
  consoleTab = button.dataset.console;
  document.querySelectorAll('[data-console]').forEach((tab) => tab.setAttribute('aria-selected', String(tab === button)));
  renderConsole();
}));
checkConnection();
if (current) { renderRun(); refreshRun(); }

const profileSelect = document.querySelector('#profile');
const environmentMessage = document.querySelector('#environment-message');
const environmentButton = document.querySelector('#environment-button');
const environmentRefresh = document.querySelector('#environment-refresh');
let environmentPlan = null, environmentKey = null, environmentId = null, environmentTimer;
try { environmentId = localStorage.getItem('railshot.lastEnvironment'); } catch { /* Optional browser locator. */ }
if (environmentId && !/^[a-zA-Z0-9._-]{1,128}$/.test(environmentId)) environmentId = null;
let environmentBusy = false;
async function loadProfiles() {
  try {
    const { data } = await request('/api/v1/profiles');
    if (!Array.isArray(data.items)) throw new Error('환경 사양 응답을 확인하지 못했습니다.');
    profileSelect.replaceChildren(...data.items.map((profile) => {
      const option = new Option(`${profile.label || profile.id} · ${profile.provider}`, profile.id);
      option.disabled = !profile.supported;
      return option;
    }));
    if (data.items.some((profile) => profile.supported)) profileSelect.value = data.items.find((profile) => profile.supported).id;
    else {
      profileSelect.replaceChildren(new Option('준비된 환경 사양이 없습니다', ''));
      environmentMessage.textContent = '새 환경을 생성할 수 있는 사양이 아직 등록되지 않았습니다. 준비된 대상에는 바로 배포할 수 있습니다.';
    }
    document.querySelector('#plan-button').disabled = !profileSelect.value;
  } catch (cause) { environmentMessage.textContent = cause.message; }
}
function clearPlan() {
  environmentPlan = null; environmentKey = null;
  environmentButton.disabled = true; environmentButton.hidden = true;
}
profileSelect.addEventListener('change', clearPlan);
document.querySelector('#environment-name').addEventListener('input', clearPlan);
document.querySelector('#environment-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (environmentBusy || !profileSelect.value) return;
  environmentBusy = true; clearPlan();
  try {
    const { data } = await request('/api/v1/plans', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: document.querySelector('#environment-name').value.trim(), runtime: { profile_id: profileSelect.value, node_count: 1 }, database: { mode: 'none' } }) });
    environmentPlan = data; environmentKey = crypto.randomUUID();
    environmentMessage.textContent = data.executable
      ? `${data.name} · runtime 노드 1대 · DB 없음. 계획을 실행하면 클라우드 자원이 생성됩니다.`
      : `현재 실행할 수 없는 계획입니다: ${(data.blockers || []).join(', ')}`;
    environmentButton.hidden = false; environmentButton.disabled = !data.executable;
  } catch (cause) { environmentMessage.textContent = cause.message; }
  finally { environmentBusy = false; }
});
environmentButton.addEventListener('click', async () => {
  if (environmentBusy || !environmentPlan?.executable) return;
  environmentBusy = true; environmentButton.disabled = true;
  try {
    const { data, location } = await request('/api/v1/environments', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': environmentKey }, body: JSON.stringify({ plan_id: environmentPlan.id }) });
    const id = data.resource_id || data.id;
    if (typeof id !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(id) || location !== `/api/v1/environments/${encodeURIComponent(id)}`) throw new Error('환경 조회 주소를 확인하지 못했습니다.');
    environmentId = id;
    try { localStorage.setItem('railshot.lastEnvironment', id); } catch { /* Optional browser locator. */ }
    clearPlan(); refreshEnvironment();
  } catch (cause) {
    environmentMessage.textContent = `${cause.message} 재요청은 같은 계획과 요청 키를 사용합니다.`;
    environmentButton.disabled = false;
  } finally { environmentBusy = false; }
});
async function refreshEnvironment() {
  clearTimeout(environmentTimer);
  if (!environmentId) return;
  environmentRefresh.hidden = false;
  try {
    const { data } = await request(`/api/v1/environments/${encodeURIComponent(environmentId)}`);
    if (data.id !== environmentId) throw new Error('환경 조회 결과가 요청과 일치하지 않습니다.');
    environmentMessage.textContent = data.status === 'succeeded'
      ? `${data.runtime_target_id || data.id}: runtime 준비 완료. ${data.deployment_supported ? '앱 배포 대상으로 연결됐습니다.' : '앱 배포에는 CI·Argo·공개 경로 연결이 추가로 필요합니다.'}`
      : `${data.stage || '환경 준비'}: ${labels[data.status] || data.status}${data.error?.message ? ` · ${data.error.message}` : ''}`;
    if (!terminal.has(data.status)) environmentTimer = setTimeout(refreshEnvironment, 15000);
    if (data.status === 'succeeded' && data.deployment_supported) checkConnection();
  } catch (cause) { environmentMessage.textContent = `${cause.message} 환경 상태를 다시 조회하세요.`; }
}
environmentRefresh.addEventListener('click', refreshEnvironment);
window.addEventListener('pagehide', () => clearTimeout(environmentTimer));
loadProfiles();
if (environmentId) refreshEnvironment();

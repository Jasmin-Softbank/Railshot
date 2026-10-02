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
const provider = document.querySelector('#provider');
const providerField = document.querySelector('#provider-field');
const deployButton = document.querySelector('#deploy-button');
const requestError = document.querySelector('#request-error');
let selectedSource = null;
let reviewed = null;
let deploymentOptions = [];
let connectionError = null;
let submitting = false;
// ponytail: remember one resource locator per browser; server owns durable execution records.
let current = null;
try { current = JSON.parse(localStorage.getItem('railshot.lastExecution')); } catch { /* Storage may be unavailable. */ }
if (!current || !/^(builds|deployments)$/.test(current.kind) || !/^[a-zA-Z0-9._-]{1,128}$/.test(current.id || '')) current = null;
let timer;
let pollController;
let lastReadAt = null;
let observationError = false;
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
function activeRun() { return current && !terminal.has(current.status); }
function deploymentSelection() {
  const environment = document.querySelector('[name="environment"]:checked').value;
  return { environment, provider: environment === 'cloud' ? 'aws' : provider.value };
}
function selectedOption() {
  const selected = deploymentSelection();
  return deploymentOptions.find((item) => item.environment === selected.environment && item.provider === selected.provider);
}
function updateSelection() {
  const selected = deploymentSelection();
  providerField.hidden = selected.environment !== 'onprem';
  document.querySelector('#connection-status').textContent = connectionError || selectedOption()?.message
    || (selected.environment === 'onprem' && !selected.provider ? '온프레미스 인프라 종류를 선택하세요.' : '실행 가능한 인프라 연결을 준비 중입니다.');
  invalidateReview();
}
document.querySelectorAll('[name="environment"]').forEach((input) => input.addEventListener('change', updateSelection));
provider.addEventListener('change', updateSelection);

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
    const { data } = await request('/api/v1/deployment-options');
    if (!Array.isArray(data.items)) throw new Error('인프라 연결 상태를 확인하지 못했습니다.');
    deploymentOptions = data.items;
    connectionError = null;
  } catch (cause) {
    deploymentOptions = [];
    connectionError = cause.message;
  }
  updateSelection();
}

document.querySelector('#deploy-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (submitting) return;
  const selected = deploymentSelection();
  const option = selectedOption();
  if (!selectedSource) error.textContent = '배포할 소스를 선택하세요.';
  else if (selectedSource.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(selectedSource.label)) error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  else if (selectedSource.kind === 'archive' && !archive.files[0].name.toLowerCase().endsWith('.zip')) error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  else if (selected.environment === 'onprem' && !selected.provider) error.textContent = '온프레미스 인프라 종류를 선택하세요.';
  else if (!option?.available) error.textContent = connectionError || option?.message || '실행 가능한 인프라가 아직 연결되지 않았습니다.';
  else if (activeRun()) error.textContent = '진행 중인 실행을 먼저 확인하세요.';
  else {
    reviewed = { ...selected, source: selectedSource, kind: 'deployments', key: crypto.randomUUID() };
    document.querySelector('#review-source').textContent = selectedSource.label;
    document.querySelector('#review-target').textContent = option.label;
    document.querySelector('#review-note').textContent = option.message;
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
    return item;
  }));
  if (current.environment) {
    const item = document.createElement('li'); item.textContent = `환경 준비: ${current.environment.stage || ''} · ${current.environment.status}`;
    document.querySelector('#run-steps').prepend(item);
  }
  for (const [name, value] of [['DB migration', current.cd?.migration?.state], ['앱 적용', current.cd?.state], ['공개 URL', current.public_http?.state]]) {
    if (value) { const item = document.createElement('li'); item.textContent = `${name}: ${value}`; document.querySelector('#run-steps').append(item); }
  }
  safeLink('#actions-link', current.actions_url || current.ci?.actions_url, true, true);
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
  document.querySelector('#monitor-steps').replaceChildren(...['소스 접수', '앱 검사 및 수정', '이미지 빌드', 'GitOps 반영', 'URL 및 앱 상태 확인'].map((label, index) => {
    const item = document.createElement('li'); item.textContent = `${label} · ${stageStates[index] || '대기'}`; return item;
  }));
  document.querySelector('#history-summary').textContent = `${current.app || '앱'} · ${current.id}`;
  document.querySelector('#history-detail').textContent = label;
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
    const payload = new FormData();
    payload.set('environment', draft.environment); payload.set('provider', draft.provider);
    if (draft.source.kind === 'folder') payload.set('source_name', (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0]);
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
    current = { ...data, id, kind: draft.kind, status: data.status === 'accepted' ? 'queued' : data.status };
    lastReadAt = null; observationError = false; remember(); renderRun();
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
    current = { ...current, ...data }; lastReadAt = Date.now(); observationError = false; remember(); renderRun();
    if (!terminal.has(current.status) || current.kind === 'deployments') timer = setTimeout(refreshRun, 15000);
    else stopPolling();
  } catch (cause) {
    if (pollController !== controller) return;
    stopPolling(); observationError = true; renderRun();
    timer = setTimeout(refreshRun, 15000);
    document.querySelector('#run-message').textContent = `${cause.name === 'AbortError' ? '상태 조회 시간이 초과되었습니다.' : cause.message} 15초 후 다시 조회합니다.`;
  }
}
document.querySelector('#stop-polling').addEventListener('click', () => { stopPolling(); document.querySelector('#run-message').textContent = '상태 조회를 중지했습니다. 서버의 실행은 계속됩니다.'; });
document.querySelector('#refresh-run').addEventListener('click', refreshRun);
window.addEventListener('pagehide', () => { stopPolling(); for (const controller of requests) controller.abort(); });
let consoleTab = 'work';
function renderConsole() {
  const data = !current ? '실행을 시작하면 확인된 상태가 여기에 표시됩니다.'
    : consoleTab === 'app' ? '앱 로그 수집은 아직 연결되지 않았습니다. 배포 작업 기록은 작업 로그에서, 앱 응답 확인 결과는 환경 상태에서 확인하세요.'
    : consoleTab === 'environment' ? { target_id: current.target_id, environment: current.environment || null, cd: current.cd || null, public_http: current.public_http || null, observation: current.observation || null }
    : { status: current.status, stage: current.stage || 'ci', steps: current.steps || current.ci?.steps || [], error: current.error || null };
  document.querySelector('#console-output').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
}
const metricLabels = { not_configured: '연결 전', unsupported: '대상 미지원', unavailable: '수집 연결 실패', collection_failed: '수집 실패', no_data: '데이터 없음', stale: '오래된 값' };
function renderMetrics() {
  const observation = current?.observation;
  const bound = observation && observation.deployment_id === current.id && observation.target_id === current.target_id && observation.app === current.app;
  for (const name of ['pods', 'cpu_percent', 'memory_percent', 'http']) {
    const metric = bound ? observation.metrics?.[name] : null;
    let state = observationError ? 'unavailable' : metric?.state || (current?.kind === 'builds' ? 'unsupported' : 'not_configured');
    const time = Date.parse(metric?.observed_at);
    if (state === 'ready' && (!Number.isFinite(time) || Date.now() - time > Math.min(observation.stale_after_seconds || 90, 90) * 1000 || time > Date.now() + 5000)) state = 'stale';
    const field = document.querySelector(`#metric-${name}`);
    field.dataset.state = state === 'ready' && name === 'http' && metric.value === 0 ? 'collection_failed' : state;
    field.textContent = state !== 'ready' ? metricLabels[state] || '확인 불가'
      : name === 'http' ? (metric.value === 1 ? '2xx 응답' : '검사 실패')
      : name === 'pods' ? `${metric.value}개` : `${Number(metric.value).toFixed(1)}%`;
    document.querySelector(`#metric-${name}-time`).textContent = Number.isFinite(time) ? `수집 ${new Date(time).toLocaleString()}` : '수집 시각 없음';
  }
  document.querySelector('#observation-status').textContent = observationError ? '관측 조회 실패 · 재시도 중'
    : bound ? `조회 ${new Date(observation.checked_at).toLocaleTimeString()}` : '관측 연결 대기';
}
// Even after polling is stopped, expire old samples on screen.
const freshnessTimer = setInterval(renderMetrics, 15000);
window.addEventListener('pagehide', () => clearInterval(freshnessTimer));
document.querySelectorAll('[data-console]').forEach((button) => button.addEventListener('click', () => {
  consoleTab = button.dataset.console;
  document.querySelectorAll('[data-console]').forEach((tab) => tab.setAttribute('aria-selected', String(tab === button)));
  renderConsole();
}));
checkConnection();
if (current) { renderRun(); refreshRun(); }

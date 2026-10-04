import { sourceAppName } from '../../contracts/application.mjs';

const views = {
  deploy: document.querySelector('#deploy-view'),
  history: document.querySelector('#history-view'),
  monitor: document.querySelector('#monitor-view'),
  personal: document.querySelector('#personal-view'),
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
  if (sessionReady && name === 'history') { loadHistory(); loadApplications(); }
  if (sessionReady && ['monitor', 'deploy'].includes(name)) loadEnvironments();
  else stopEnvironmentPolling();
  if (sessionReady && name === 'personal') { loadOwnerInfo(); loadOwnedTargets(); }
  else stopOwnedTargetPolling();
  if (sessionReady && name === 'monitor' && consoleTab === 'app') refreshLogs();
  if (sessionReady && name === 'monitor' && consoleTab === 'work') refreshEvents();
}

document.querySelectorAll('[data-view]').forEach((button) => {
  button.addEventListener('click', () => { if (button.dataset.view === 'deploy' && updateApplication && !submitting) setUpdateMode(null); showView(button.dataset.view); });
});
document.querySelector('.brand').addEventListener('click', (event) => {
  event.preventDefault();
  if (updateApplication && !submitting) setUpdateMode(null);
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
let updateApplication = null, updatePreviewRequest = null, applicationDetail = null;
let applications = [], applicationPageEnds = [], applicationPage = 0, applicationsController, applicationController;
let reviewed = null;
let deploymentOptions = [];
let profiles = [];
let reviewing = false;
let reviewGeneration = 0;
let connectionError = null;
let submitting = false;
let resuming = null;
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
let ownedTargets = [], ownedTargetError = null, ownedTargetController, ownedTargetTimer, selectedOwnedTarget = null;
let selectedOwnedDetail = null, ownedObservation = null, ownedApplications = [], enrollmentTargetId = null, environmentDeleteDraft = null;
let environmentDeleteOperation = null, environmentDeletePlanController = null, environmentDeleteReconciliationTimer;
let enrollmentRequestGeneration = 0, ownedTargetSelectionGeneration = 0;
let environmentRegistrationTargetId = null;
let ownerRecoveryConfigured = false, ownerInfoGeneration = 0;

let preferences = { view: 'deploy', environment: 'cloud', provider: '' };
// The cookie is HttpOnly; no session token, credential, or execution locator enters localStorage.
try { localStorage.removeItem('railshot.lastExecution'); } catch { /* Storage may be disabled. */ }
let timer;
let pollController;
let lastReadAt = null;
let observationError = false;
let logSnapshot = null, logController;
let eventSnapshot = null, eventController;
const requests = new Set();

function invalidateReview() {
  reviewGeneration += 1;
  reviewed = null;
  document.querySelector('#review-panel').hidden = true;
  deployButton.disabled = true;
  document.querySelector('#update-review').hidden = true;
  deployButton.textContent = updateApplication ? '업데이트 시작' : '배포 시작';
  error.hidden = true;
}

function setSource(source) {
  selectedSource = source && { ...source, files: source.kind === 'archive' ? [...archive.files] : source.kind === 'folder' ? [...folder.files] : [] };
  updatePreviewRequest = null;
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

const terminal = new Set(['succeeded', 'failed', 'blocked', 'unknown', 'published', 'publication_unverified', 'preview', 'unchanged', 'expired']);
const labels = { queued: '실행 대기 중', running: '실행 중', succeeded: '앱 배포 완료', published: '이미지 게시 완료',
  failed: '실행 실패', blocked: '실행 조건 확인 필요', unknown: '실행 결과 확인 필요', publication_unverified: '게시 결과 확인 필요',
  preview: '변경 검토 대기', unchanged: '변경 없음 · 실행 생략', expired: '변경 검토 만료' };
function executionLabel(row) { return row.kind === 'builds' && row.status === 'succeeded' ? '이미지 게시 완료' : labels[row.status] || row.status || '실행 상태 확인 중'; }
function environmentLabel(row) {
  return row.environment_target_id ? `환경 ${row.environment_target_id}` : `배포 대상 ${row.target_id || '미지정'}`;
}
function activeRun() { return current && (current.status === 'unknown' || !terminal.has(current.status)); }
function deploymentSelection() {
  const environment = document.querySelector('[name="environment"]:checked').value;
  const targetId = environment === 'onprem' && provider.value && provider.value !== '__new_openstack__' ? provider.value : null;
  return { environment, provider: environment === 'cloud' ? cloudProvider.value : 'openstack', targetId,
    registerNew: environment === 'onprem' && provider.value === '__new_openstack__' };
}
function selectedOption() {
  const selected = deploymentSelection();
  if (selected.environment === 'onprem') return selected.targetId ? ownedTargets.find((item) => item.id === selected.targetId) : null;
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
  document.querySelector('#onprem-registration-guide').hidden = !selected.registerNew;
  const profile = selectedProfile();
  document.querySelector('#deployment-database-field').hidden = !profile?.database;
  databaseChoice(deploymentDatabase, profile);
  document.querySelector('#deployment-database-note').textContent = databaseSummary(profile, deploymentDatabase.value);
  const target = selectedOption();
  const targetReady = target?.status === 'ready' && target.deployable === true;
  let connectionStatus;
  if (selected.registerNew) connectionStatus = '새 OpenStack 환경을 등록한 뒤 배포할 수 있습니다.';
  else if (ownedTargetError && selected.environment === 'onprem') connectionStatus = `등록 환경 조회 실패: ${ownedTargetError}`;
  else if (selected.environment === 'onprem' && selected.targetId) connectionStatus = targetReady
    ? `${target.label || target.id} 환경에 배포합니다.`
    : `${target?.label || selected.targetId} 환경은 ${targetStatusLabel(target?.status)} 상태라 아직 배포할 수 없습니다.`;
  else if (connectionError) connectionStatus = connectionError;
  else if (selectedProfiles().length > 1) connectionStatus = '사용할 배포 사양을 운영자가 하나로 지정해야 합니다.';
  else if (profile) connectionStatus = profile.supported
    ? '새 실행 환경과 앱을 함께 준비합니다. 선택 내용을 확인하면 비용과 실행 계획을 표시합니다.'
    : '환경 생성 사양을 아직 실행할 수 없습니다.';
  else connectionStatus = selectedOption()?.message || (selected.environment === 'onprem' && !selected.targetId ? '등록할 OpenStack 환경을 선택하세요.' : '앱 배포 설정을 확인하고 있습니다.');
  document.querySelector('#connection-status').textContent = connectionStatus;
  renderRuntimeConnection();
  invalidateReview();
  savePreferences({ environment: selected.environment, provider: selected.environment === 'cloud' ? selected.provider : '' });
}
document.querySelectorAll('[name="environment"]').forEach((input) => input.addEventListener('change', updateSelection));
provider.addEventListener('change', updateSelection);
cloudProvider.addEventListener('change', updateSelection);
deploymentDatabase.addEventListener('change', updateSelection);

async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.method === 'POST' ? (path === '/api/v1/plans' ? 600000 : 120000) : path.endsWith('/logs') ? 60000 : 15000);
  try {
    const personalApi = /^\/api\/v1\/(?:owners(?:[/?]|$)|recoveries(?:[/?]|$)|operations(?:[/?]|$)|targets(?:[/?]|$))/.test(path);
    const headers = new Headers(options.headers || {});
    if (personalApi) headers.set('X-Railshot-Request', 'dashboard');
    const response = await fetch(path, { credentials: 'same-origin', ...options, headers, signal: controller.signal, redirect: 'error' });
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const failure = new Error(data.error?.message || (typeof data.error === 'string' ? data.error : `요청 실패 (HTTP ${response.status})`));
      Object.assign(failure, { status: response.status, code: data.error?.code,
        outcomeUnknown: data.error?.outcome_unknown, admission: data.error?.admission });
      throw failure;
    }
    return { data, location: response.headers.get('location'), status: response.status };
  } finally { clearTimeout(timeout); requests.delete(controller); }
}

async function checkConnection() {
  try {
    const [{ data }, { data: catalog }] = await Promise.all([request('/api/v1/options'), request('/api/v1/profiles?limit=100')]);
    if (!Array.isArray(catalog.items) || catalog.next_marker) throw new Error('배포 사양 목록을 확인하지 못했습니다.');
    profiles = catalog.items;
    if (!Array.isArray(data.items)) throw new Error('앱 배포 설정을 확인하지 못했습니다.');
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

function sourceApplication(source) {
  const name = source.kind === 'repository' ? source.label.split('/').filter(Boolean).at(-1).replace(/\.git$/i, '')
    : source.kind === 'archive' ? source.files[0].name : (source.files[0].webkitRelativePath || source.files[0].name).split('/')[0];
  return sourceAppName(name);
}

function appendSource(payload, source) {
  if (source.kind === 'repository') payload.set('repository_url', source.label);
  else if (source.kind === 'archive') payload.set('archive', source.files[0]);
  else {
    const paths = [];
    for (const file of source.files) {
      const parts = (file.webkitRelativePath || file.name).split('/');
      const path = parts.length > 1 ? parts.slice(1).join('/') : file.name;
      if (path.split('/').some((part) => ['.git', 'node_modules', '__MACOSX', '.DS_Store'].includes(part))) continue;
      paths.push(path); payload.append('files', file, file.name);
    }
    payload.set('paths', JSON.stringify(paths));
  }
  return payload;
}

const resourceId = (value) => typeof value === 'string' && /^[A-Za-z0-9._-]{1,128}$/.test(value);
function setUpdateMode(application) {
  updateApplication = application; updatePreviewRequest = null;
  archive.value = ''; folder.value = ''; repositoryUrl.value = ''; setSource(null);
  document.querySelector('#update-context').hidden = !application;
  document.querySelector('#target-section').hidden = Boolean(application);
  document.querySelector('#deploy-title').textContent = application ? '앱 업데이트하기' : '앱 배포하기';
  document.querySelector('#deploy-view .page-header .eyebrow').textContent = application ? 'APPLICATION UPDATE' : 'NEW DEPLOYMENT';
  document.querySelector('#review-title').textContent = application ? '변경 내용' : '선택 내용';
  document.querySelector('#update-identity').textContent = application ? `앱 ${application.app} · ${environmentLabel(application)}` : '';
  document.querySelector('#deploy-form button[type="submit"]').textContent = application ? '변경 내용 확인 →' : '선택 내용 확인 →';
  requestError.hidden = true;
}
document.querySelector('#cancel-update').addEventListener('click', () => { if (!submitting) { setUpdateMode(null); showView('deploy'); } });
function beginUpdate(application) {
  if (!resourceId(application?.id)) return false;
  application = { ...application, ...applications.find((row) => row.id === application.id) };
  if (updateBlocked(application)) return false;
  setUpdateMode(application); showView('deploy');
  document.querySelector('#update-context').scrollIntoView({ block: 'start' });
  document.querySelector('#choose-file').focus({ preventScroll: true });
  return true;
}

function validatePreview(data, application) {
  if (!resourceId(data?.id) || data.status !== 'preview' || data.app !== application.app || data.target_id !== application.target_id
      || data.application_id !== undefined && data.application_id !== application.id
      || !resourceId(data.base_deployment_id) || !Number.isFinite(Date.parse(data.expires_at))
      || !['submitted', 'deployed'].includes(data.baseline_kind) || typeof data.no_changes !== 'boolean'
      || !data.changes || !['added', 'modified', 'deleted'].every((key) => Array.isArray(data.changes[key])
        && data.changes[key].every((path) => typeof path === 'string')) || !Number.isSafeInteger(data.changes.unchanged) || data.changes.unchanged < 0
      || data.no_changes && (data.baseline_kind !== 'deployed' || ['added', 'modified', 'deleted'].some((key) => data.changes[key].length))) {
    throw new Error('변경 검토 응답이 선택한 앱과 일치하지 않습니다.');
  }
}
function renderUpdatePreview(data, application, sourceLabel) {
  document.querySelector('#review-source').textContent = sourceLabel || '서버에 보관된 소스';
  document.querySelector('#review-app').textContent = application.app;
  document.querySelector('#review-target').textContent = environmentLabel(application);
  document.querySelector('#review-note').textContent = `소스가 서버에 고정되었습니다. 아래 변경을 확인한 뒤 업데이트를 시작하세요. 유효 시각: ${formatTime(data.expires_at)}`;
  document.querySelector('#update-baseline').textContent = `비교 기준 배포: ${data.base_deployment_id} · ${data.baseline_kind === 'deployed'
    ? '검증된 최종 소스와 비교합니다.' : '제출 원본과 비교합니다. 이 배포의 AI 수정 후 최종 소스는 보관되어 있지 않습니다. 실제 배포 소스와 동일한지는 확인할 수 없어 검사·빌드를 생략하지 않습니다.'}`;
  document.querySelector('#update-origin').textContent = data.source_origin?.repository
    ? `가져온 저장소: ${data.source_origin.repository} · 고정 commit: ${data.source_origin.sha || '미제공'}` : '업로드한 소스를 서버에 보관했습니다.';
  const changes = data.changes;
  document.querySelector('#update-diff-summary').textContent = `${data.no_changes ? '변경된 파일이 없습니다. ' : ''}추가 ${changes.added.length} · 수정 ${changes.modified.length} · 삭제 ${changes.deleted.length} · 동일 ${changes.unchanged}`;
  document.querySelector('#update-changes').replaceChildren(...[['added', '추가'], ['modified', '수정'], ['deleted', '삭제']].map(([key, label]) => {
    const group = document.createElement('details'); group.open = changes[key].length > 0;
    const files = document.createElement('ul'); files.append(...changes[key].map((path) => element('li', path)));
    group.append(element('summary', `${label} ${changes[key].length}개`), files); return group;
  }));
  document.querySelector('#update-review').hidden = false;
  document.querySelector('#rebuild-field').hidden = !data.no_changes;
  const rebuild = document.querySelector('#update-rebuild'); rebuild.checked = false; rebuild.disabled = false;
  deployButton.textContent = data.no_changes ? '변경 없음으로 완료' : '업데이트 시작';
  deployButton.disabled = Date.parse(data.expires_at) <= Date.now();
  requestError.hidden = true; error.hidden = true;
  document.querySelector('#review-panel').hidden = false;
  document.querySelector('#review-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
document.querySelector('#update-rebuild').addEventListener('change', (event) => {
  if (reviewed?.update && !reviewed.attempted) deployButton.textContent = event.target.checked ? '다시 빌드·배포' : '변경 없음으로 완료';
});
async function reviewUpdate() {
  if (applicationBusy(updateApplication?.id)) error.textContent = '진행 중인 앱 관리 작업을 먼저 확인하세요.';
  else if (!selectedSource) error.textContent = '업데이트할 소스를 선택하세요.';
  else if (selectedSource.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(selectedSource.label)) error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  else if (selectedSource.kind === 'archive' && !selectedSource.files[0]?.name.toLowerCase().endsWith('.zip')) error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  else if (activeRun()) error.textContent = '진행 중인 실행을 먼저 확인하세요.';
  else {
    invalidateReview();
    const generation = reviewGeneration, application = updateApplication;
    const draft = updatePreviewRequest ||= { key: crypto.randomUUID(), source: selectedSource };
    const button = document.querySelector('#deploy-form button[type="submit"]');
    reviewing = true; button.disabled = true;
    try {
      const { data, location } = await request(`/api/v1/applications/${encodeURIComponent(application.id)}/updates`, {
        method: 'POST', headers: { 'Idempotency-Key': draft.key }, body: appendSource(new FormData(), draft.source),
      });
      if (generation !== reviewGeneration || updateApplication?.id !== application.id) return;
      validatePreview(data, application);
      if (location !== `/api/v1/deployments/${encodeURIComponent(data.id)}`) throw new Error('변경 검토 조회 주소를 확인하지 못했습니다.');
      if (Date.parse(data.expires_at) <= Date.now()) {
        updatePreviewRequest = null; throw new Error('변경 검토가 만료됐습니다. 같은 소스로 새 변경 검토를 요청하세요.');
      }
      reviewed = { update: true, kind: 'deployments', preview: data, application, key: draft.key };
      renderUpdatePreview(data, application, draft.source.label);
    } catch (cause) {
      if (generation === reviewGeneration && updateApplication?.id === application.id) {
        error.textContent = `${cause.name === 'AbortError' ? '변경 검토 요청 시간이 초과되었습니다.' : cause.message}${updatePreviewRequest === draft ? ' 다시 확인하면 같은 요청 키를 사용합니다.' : ''}`;
        error.hidden = false;
      }
    } finally { reviewing = false; button.disabled = false; }
    return;
  }
  error.hidden = false;
}
async function startUpdate(draft) {
  if (applicationBusy(draft.application.id, true)) throw new Error('진행 중인 앱 관리 작업을 먼저 확인하세요.');
  if (!draft.attempted && Date.parse(draft.preview.expires_at) <= Date.now()) {
    reviewed = null; updatePreviewRequest = null; throw new Error('변경 검토가 만료됐습니다. 소스 선택에서 다시 확인하세요.');
  }
  draft.startRebuild ??= document.querySelector('#update-rebuild').checked;
  draft.attempted = true;
  document.querySelector('#update-rebuild').disabled = true;
  let response;
  try {
    response = await request(`/api/v1/deployments/${encodeURIComponent(draft.preview.id)}/start`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rebuild: draft.startRebuild }),
    });
  } catch (cause) {
    if (['UPDATE_EXPIRED', 'UPDATE_BASE_CHANGED'].includes(cause.code)) { reviewed = null; updatePreviewRequest = null; }
    throw cause;
  }
  const { data, location } = response;
  const id = data.resource_id || data.id;
  if (id !== draft.preview.id || location !== `/api/v1/deployments/${encodeURIComponent(id)}`
      || !['accepted', 'queued', 'running', ...terminal].includes(data.status) || data.status === 'preview') throw new Error('업데이트 실행 응답을 확인하지 못했습니다.');
  current = { ...draft.preview, ...data, id, kind: 'deployments', status: data.status === 'accepted' ? 'queued' : data.status };
  lastReadAt = null; observationError = false; remember(); renderRun();
  reviewed = null; updatePreviewRequest = null; deployButton.disabled = true;
  document.querySelector('#review-panel').hidden = true;
  historyKind = 'deployments'; document.querySelector('#history-kind').value = historyKind;
  loadHistory([null]); loadApplications(); refreshRun();
  document.querySelector('#run-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

document.querySelector('#deploy-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (submitting || reviewing) return;
  if (updateApplication) { await reviewUpdate(); return; }
  const selected = deploymentSelection();
  const profile = selectedProfile();
  const option = selectedOption();
  if (!selectedSource) error.textContent = '배포할 소스를 선택하세요.';
  else if (selectedSource.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(selectedSource.label)) error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  else if (selectedSource.kind === 'archive' && !archive.files[0].name.toLowerCase().endsWith('.zip')) error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  else if (selected.environment === 'onprem' && selected.registerNew) error.textContent = '새 OpenStack 환경을 등록한 뒤 배포할 수 있습니다.';
  else if (selected.environment === 'onprem' && !selected.targetId) error.textContent = '배포할 등록 OpenStack 환경을 선택하세요.';
  else if (selected.environment === 'onprem' && (ownedTargetError || option?.status !== 'ready' || option?.deployable !== true)) error.textContent = document.querySelector('#connection-status').textContent;
  else if (selected.environment === 'cloud' && (connectionError || selectedProfiles().length > 1 || (profile ? !profile.supported : !option?.available))) error.textContent = connectionError || (profile || selectedProfiles().length > 1 ? document.querySelector('#connection-status').textContent : option?.message) || '실행 가능한 인프라가 아직 연결되지 않았습니다.';
  else if (activeRun()) error.textContent = '진행 중인 실행을 먼저 확인하세요.';
  else {
    invalidateReview();
    const generation = reviewGeneration, source = selectedSource;
    let plan, app;
    reviewing = true;
    const reviewButton = document.querySelector('#deploy-form button[type="submit"]');
    reviewButton.disabled = true;
    try {
      app = sourceApplication(source);
      if (profile) {
        if (!profile.create_per_request && profile.application_name && profile.application_name !== app) {
          throw new Error(`선택한 환경은 ${profile.application_name} 앱 전용입니다. ${app} 배포에는 새 앱용 환경 또는 같은 이름의 앱 등록이 필요합니다.`);
        }
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
      targetId: selected.targetId || plan?.runtime_target_id || profile?.target_id };
    document.querySelector('#review-source').textContent = source.label;
    document.querySelector('#review-app').textContent = app;
    document.querySelector('#review-target').textContent = selected.environment === 'onprem' ? `온프레미스 · ${option.label || option.id}` : profile ? `클라우드 · ${profile.label || profile.id} · ${databaseSummary(profile, plan.database.mode)}` : option.label;
    document.querySelector('#review-note').textContent = plan
      ? `앱 ${plan.name}: 환경을 준비하고 ${plan.database.mode === 'patroni' ? 'DB 준비, ' : ''}소스 검사, 이미지 게시, 앱 적용과 공개 URL 확인을 시작합니다. 계획 유효 시각: ${new Date(plan.expires_at).toLocaleTimeString('ko-KR')}.${planCost(plan)}`
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
  renderApplicationActions();
  document.querySelector('#run-panel').hidden = false;
  const resume = document.querySelector('#resume-run');
  resume.hidden = !(current.kind === 'deployments' && current.application_id && current.status === 'unknown'
    && ['cd', 'http'].includes(current.stage) && current.ci?.state === 'published');
  resume.disabled = Boolean(resuming) || observationError || applicationBusy(current.application_id);
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
    if (draft.update) { await startUpdate(draft); return; }
    const payload = new FormData();
    if (draft.plan) {
      payload.set('app', draft.plan.name); payload.set('target_id', draft.targetId); payload.set('plan_id', draft.plan.id);
    } else if (draft.environment === 'onprem') {
      payload.set('target_id', draft.targetId);
      payload.set('environment', 'onprem'); payload.set('provider', 'openstack');
    } else { payload.set('environment', draft.environment); payload.set('provider', draft.provider); }
    if (!draft.plan && draft.source.kind === 'folder') payload.set('source_name', (draft.source.files[0].webkitRelativePath || draft.source.files[0].name).split('/')[0]);
    appendSource(payload, draft.source);
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
    const uncertain = draft.attempted && (cause.outcomeUnknown === true || !cause.status);
    const next = cause.code === 'EXECUTOR_BUSY'
      ? cause.admission?.reason === 'reconciliation_required'
        ? '운영자가 기존 작업의 결과를 확인한 뒤 다시 요청하세요. 자동으로 시작되지 않습니다.'
        : '기존 작업이 끝난 뒤 다시 요청하세요. 자동으로 시작되지 않습니다.'
      : draft.update ? reviewed ? '다시 누르면 서버에 보관한 같은 변경 검토의 실행 상태를 확인합니다.' : '소스 선택에서 변경 내용을 다시 확인하세요.'
      : uncertain ? '서버에서 이미 처리했을 수 있습니다. 배포 재요청은 같은 요청 키를 사용합니다.'
      : '요청 조건을 확인한 뒤 다시 시도하세요.';
    requestError.textContent = `${cause.name === 'AbortError' ? '요청 시간이 초과되었습니다.' : cause.message} ${next}`;
    requestError.hidden = false;
    // Only deployments promise a safe retry with the same key and unchanged source.
    deployButton.disabled = draft.kind !== 'deployments';
  } finally {
    submitting = false;
    controls.forEach((control) => { control.disabled = false; });
    document.querySelector('#update-rebuild').disabled = Boolean(draft.update && draft.attempted);
    if (!reviewed) deployButton.disabled = true;
    renderApplications();
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
    if (consoleTab === 'work' && !views.monitor.hidden) refreshEvents();
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
document.querySelector('#resume-run').addEventListener('click', async () => {
  if (resuming || document.querySelector('#resume-run').disabled || document.querySelector('#resume-run').hidden) return;
  const id = current.id;
  resuming = id;
  const message = document.querySelector('#resume-error');
  message.hidden = true;
  stopPolling(); renderRun();
  try {
    const { data, location } = await request(`/api/v1/deployments/${encodeURIComponent(id)}/actions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'resume' }),
    });
    if (data.resource_id !== id || location !== `/api/v1/deployments/${encodeURIComponent(id)}`) throw new Error('실행 조회 주소를 확인하지 못했습니다.');
    if (current?.id === id) await refreshRun();
  } catch (cause) {
    if (current?.id === id) {
      await refreshRun();
      message.textContent = `${cause.name === 'AbortError' ? '요청 시간이 초과되었습니다.' : cause.message} ${observationError ? '상태 조회도 실패했습니다. 마지막 기록을 표시합니다.' : '실행 상태를 다시 확인했습니다.'}`;
      message.hidden = false;
    }
  } finally {
    resuming = null;
    if (current) renderRun();
  }
});
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
function eventsMatch(events) {
  return events && current?.kind === 'deployments' && events.deployment_id === current.id
    && events.app === current.app && events.target_id === current.target_id
    && (events.source_commit ?? null) === (current.source_commit ?? null)
    && String(events.run_id ?? '') === String(current.ci?.run_id ?? '');
}
async function refreshEvents() {
  if (eventController && eventsMatch(eventSnapshot)) return;
  eventController?.abort();
  if (!current || current.kind !== 'deployments') return;
  const id = current.id, controller = new AbortController(); eventController = controller;
  if (!eventsMatch(eventSnapshot)) eventSnapshot = { deployment_id: id, app: current.app, target_id: current.target_id,
    source_commit: current.source_commit ?? null, run_id: current.ci?.run_id ?? null, state: 'loading', items: [] };
  renderConsole();
  try {
    const { data } = await request(`/api/v1/deployments/${encodeURIComponent(id)}/events`, {}, controller);
    if (eventController !== controller || current?.id !== id) return;
    if (!eventsMatch(data) || !Array.isArray(data.items)) throw new Error('이벤트 대상 불일치');
    eventSnapshot = data;
  } catch {
    if (eventController !== controller || current?.id !== id) return;
    if (eventsMatch(eventSnapshot)) eventSnapshot = { ...eventSnapshot, read_error: true };
  } finally { if (eventController === controller) { eventController = null; renderConsole(); } }
}
function agentEvents() {
  const events = eventsMatch(eventSnapshot) ? eventSnapshot : null;
  const age = events?.updated_at ? Date.now() - Date.parse(events.updated_at) : NaN;
  const stale = events?.stale || events?.state === 'live' && (!Number.isFinite(age) || age > 90000);
  const messages = { loading: '에이전트 이벤트를 조회하고 있습니다.', live: '에이전트 실행 관측 중',
    complete: '에이전트 관측이 종료되었습니다. CI와 앱 배포 결과는 위 상태에서 확인하세요.',
    not_started: '아직 에이전트 이벤트가 기록되지 않았습니다.', no_data: '이 실행에 기록된 에이전트 이벤트가 없습니다.',
    unavailable: '에이전트 이벤트를 확인할 수 없습니다. 다음 조회에서 다시 확인합니다.' };
  return { message: events?.read_error ? '이벤트 조회 실패 · 마지막으로 확인한 기록입니다.'
    : stale ? '이벤트 갱신이 지연되고 있습니다. 마지막 기록만으로 실행 정지를 판단할 수 없습니다.'
    : messages[events?.state] || '작업 로그 탭에서 현재 실행의 에이전트 이벤트를 조회합니다.',
    ...(events || { items: [] }),
    ...(events?.truncated ? { history_note: '최근 이벤트만 표시합니다. 전체 기록은 CI 실행 로그에서 확인하세요.' } : {}) };
}
function renderConsole() {
  const logs = logSnapshot?.deployment_id === current?.id ? logSnapshot : null;
  const data = !current ? '실행을 시작하면 확인된 상태가 여기에 표시됩니다.'
    : consoleTab === 'app' ? (logs?.state === 'ready'
      ? [`조회 ${new Date(logs.checked_at).toLocaleString()}`, ...logs.entries.map((entry) => `[${entry.pod} / ${entry.container}]\n${entry.text}`)].join('\n\n')
      : logLabels[logs?.state] || '앱 로그 탭을 선택하면 현재 배포의 로그를 조회합니다.')
    : consoleTab === 'environment' ? { target_id: current.target_id, environment: current.environment || null, cd: current.cd || null, public_http: current.public_http || null, observation: current.observation || null }
    : { status: current.status, stage: current.stage || 'ci', diagnostics: current.ci?.diagnostics || current.diagnostics || null,
      steps: current.steps || current.ci?.steps || [], error: current.error || null, agent_events: agentEvents() };
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
const freshnessTimer = setInterval(() => { renderMetrics(); renderConsole(); renderEnvironments(); }, 15000);
window.addEventListener('pagehide', () => clearInterval(freshnessTimer));
document.querySelectorAll('[data-console]').forEach((button) => button.addEventListener('click', () => {
  consoleTab = button.dataset.console;
  document.querySelectorAll('[data-console]').forEach((tab) => tab.setAttribute('aria-selected', String(tab === button)));
  renderConsole();
  if (consoleTab === 'app') refreshLogs();
  if (consoleTab === 'work') refreshEvents();
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
function sourceDownloads(deployment) {
  const actions = element('div', '', 'history-actions');
  const message = element('p', '', 'field-note source-download-message'); message.setAttribute('role', 'status');
  for (const [variant, label] of [['submitted', '제출 원본 다운로드'], ['deployed', '최종 소스 다운로드']]) {
    const button = element('button', label, 'text-button'); button.type = 'button';
    button.setAttribute('aria-label', `${deployment.app || '앱'} ${deployment.id} ${label}`);
    button.addEventListener('click', async () => {
      if (!resourceId(deployment.id)) return;
      button.disabled = true; message.textContent = '소스를 준비하고 있습니다.';
      const controller = new AbortController(); requests.add(controller);
      const timeout = setTimeout(() => controller.abort(), 120000);
      try {
        const response = await fetch(`/api/v1/deployments/${encodeURIComponent(deployment.id)}/source?variant=${variant}`,
          { credentials: 'same-origin', redirect: 'error', signal: controller.signal });
        if (!response.ok) {
          const data = await response.json(); throw new Error(data.error?.message || '이 배포의 소스를 내려받을 수 없습니다.');
        }
        if (!/application\/(?:zip|octet-stream)/i.test(response.headers.get('content-type') || '')) throw new Error('소스 다운로드 응답을 확인하지 못했습니다.');
        const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
        link.href = url; link.download = `${deployment.id}-${variant}.zip`; document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        message.textContent = `${label}를 준비했습니다.`;
      } catch (cause) {
        message.textContent = cause.name === 'AbortError' ? '소스 다운로드 시간이 초과되었습니다. 다시 시도하세요.' : cause.message;
      } finally { clearTimeout(timeout); requests.delete(controller); button.disabled = false; }
    });
    actions.append(button);
  }
  actions.append(message); return actions;
}
function applicationVersion(label, deployment, uncertain = false) {
  const group = element('div', '', 'application-version');
  group.append(element('h4', label));
  if (!deployment) { group.append(element('p', '검증된 배포 기록이 없습니다.')); return group; }
  group.append(element('p', `${executionLabel(deployment)} · ${deployment.id}`),
    element('p', `소스 commit: ${deployment.source_commit || '미제공'} · ${formatTime(deployment.updated_at || deployment.created_at)}`, 'field-note'));
  if (uncertain) group.append(element('p', '이후 실행의 적용 결과가 불확실합니다. 이 기록을 현재 서비스 버전으로 확정할 수 없습니다.', 'field-note'));
  const button = element('button', deployment.status === 'preview' ? '변경 검토 열기' : '이 배포 상세·로그', 'text-button'); button.type = 'button';
  button.addEventListener('click', () => deployment.status === 'preview' ? openUpdatePreview(deployment) : openExecution({ ...deployment, kind: 'deployments' }));
  group.append(button, sourceDownloads(deployment)); return group;
}
function renderApplications() {
  document.querySelector('#applications-list').replaceChildren(...applications.slice(0, applicationPageEnds[applicationPage] || 0).map((application) => {
    const item = document.createElement('li'), header = element('div', '', 'history-row'), content = document.createElement('div');
    content.append(element('strong', application.app), element('span', applicationStates[application.status] || '상태 확인 필요', 'state-badge'),
      element('small', `${environmentLabel(application)} · 앱 ID ${application.id}`, 'history-meta'),
      element('small', `${application.current_deployment_state === 'unverified' ? '마지막 검증 성공 (현재 상태 확인 필요)' : '현재 서비스'}: ${application.current_deployment ? application.current_deployment.id : '검증된 배포 없음'}`, 'history-meta'),
      element('small', `최근 시도: ${application.latest_deployment ? `${executionLabel(application.latest_deployment)} · ${application.latest_deployment.id}` : '없음'}`, 'history-meta'));
    const detail = element('button', '앱 상세·업데이트', 'secondary-button'); detail.type = 'button';
    detail.setAttribute('aria-label', `${application.app} 앱 상세·업데이트`);
    detail.addEventListener('click', () => loadApplication(application.id));
    header.append(content, detail); item.append(header, applicationButtons(application)); return item;
  }));
  document.querySelector('#applications-more').hidden = applicationPage >= applicationPageEnds.length - 1;
  document.querySelector('#applications-more').disabled = Boolean(applicationsController);
  renderApplicationActions(); renderHistory();
}
async function loadApplications(more = false) {
  if (more) {
    if (applicationsController || applicationPage >= applicationPageEnds.length - 1) return false;
    applicationPage += 1; renderApplications(); return true;
  }
  applicationsController?.abort();
  const controller = new AbortController(); applicationsController = controller;
  applications = []; applicationPageEnds = []; applicationPage = 0; renderApplications();
  document.querySelector('#applications-list').setAttribute('aria-busy', 'true');
  const message = document.querySelector('#applications-message'); message.textContent = '이 세션의 최신 앱 목록을 확인하고 있습니다.';
  try {
    // Fetch the full owned inventory for history/detail actions; reveal its API pages progressively.
    let marker = null; const rows = [], markers = new Set(), pageEnds = [];
    do {
      const query = new URLSearchParams({ limit: '20' }); if (marker) query.set('marker', marker);
      const { data } = await request(`/api/v1/applications?${query}`, {}, controller);
      if (!Array.isArray(data.items) || data.items.some((item) => !resourceId(item.id) || typeof item.app !== 'string')
          || data.next_marker != null && typeof data.next_marker !== 'string') throw new Error('앱 목록을 확인하지 못했습니다.');
      rows.push(...data.items); pageEnds.push(rows.length); marker = data.next_marker;
      if (rows.length > 1000 || marker && markers.has(marker)) throw new Error('앱 목록 범위를 확인하지 못했습니다.');
      markers.add(marker);
    } while (marker);
    if (applicationsController !== controller) return false;
    if (new Set(rows.map((row) => row.id)).size !== rows.length) throw new Error('앱 ID가 중복된 응답입니다.');
    applications = rows; applicationPageEnds = pageEnds;
    message.textContent = applications.length ? `${rows.length}개 앱 · 이 세션의 현재 서비스와 최근 배포 시도를 구분해 표시합니다.` : '아직 이 세션에 등록된 앱이 없습니다.';
    return true;
  } catch (cause) {
    if (applicationsController === controller) message.textContent = `앱 목록 조회 실패: ${cause.message} 앱 새로고침을 눌러 다시 확인하세요.`;
    return false;
  } finally {
    if (applicationsController === controller) {
      applicationsController = null; renderApplications(); document.querySelector('#applications-list').setAttribute('aria-busy', 'false');
    }
  }
}
async function loadApplication(id) {
  applicationController?.abort();
  const controller = new AbortController(); applicationController = controller; applicationDetail = null;
  const panel = document.querySelector('#application-detail'), message = document.querySelector('#application-detail-message');
  panel.hidden = false; document.querySelector('#application-detail-title').textContent = '앱 상세';
  document.querySelector('#application-versions').replaceChildren(); document.querySelector('#application-update').disabled = true;
  message.textContent = '앱 상세를 조회하고 있습니다.';
  try {
    const { data } = await request(`/api/v1/applications/${encodeURIComponent(id)}`, {}, controller);
    if (applicationController !== controller) return;
    if (data.id !== id || typeof data.app !== 'string' || typeof data.target_id !== 'string') throw new Error('앱 상세가 요청한 앱과 일치하지 않습니다.');
    applicationDetail = data;
    document.querySelector('#application-detail-title').textContent = data.app;
    const blocked = updateBlocked(data);
    message.textContent = `${environmentLabel(data)} · 앱 ID ${data.id}${blocked ? ` · ${blocked}` : ''}`;
    document.querySelector('#application-versions').replaceChildren(
      applicationVersion(data.current_deployment_state === 'unverified' ? '마지막 검증 성공 · 현재 상태 확인 필요' : '현재 서비스 버전', data.current_deployment, data.current_deployment_state === 'unverified'),
      applicationVersion('최근 배포 시도', data.latest_deployment));
    document.querySelector('#application-update').disabled = Boolean(blocked);
    document.querySelector('#application-detail-title').focus({ preventScroll: true }); panel.scrollIntoView({ block: 'nearest' });
  } catch (cause) {
    if (applicationController === controller) message.textContent = `앱 상세 조회 실패: ${cause.message}`;
  } finally { if (applicationController === controller) { applicationController = null; renderApplicationActions(); } }
}
async function openUpdatePreview(record) {
  if (submitting) return;
  const generation = ++reviewGeneration;
  try {
    const { data } = await request(`/api/v1/deployments/${encodeURIComponent(record.id)}`);
    if (generation !== reviewGeneration) return;
    if (!resourceId(data.application_id)) throw new Error('이 변경 검토의 앱 연결을 확인하지 못했습니다.');
    const { data: application } = await request(`/api/v1/applications/${encodeURIComponent(data.application_id)}`);
    if (generation !== reviewGeneration) return;
    validatePreview(data, application);
    if (!beginUpdate(application)) throw new Error('현재 앱 상태에서는 변경 검토를 열 수 없습니다. 앱 관리 작업과 배포 상태를 확인하세요.');
    reviewed = { update: true, kind: 'deployments', preview: data, application };
    renderUpdatePreview(data, application);
  } catch (cause) {
    if (generation === reviewGeneration) document.querySelector('#applications-message').textContent = `변경 검토 조회 실패: ${cause.message}`;
  }
}
document.querySelector('#applications-refresh').addEventListener('click', () => loadApplications());
document.querySelector('#applications-more').addEventListener('click', () => loadApplications(true));
document.querySelector('#application-update').addEventListener('click', () => beginUpdate(applicationDetail));
function showHistoryError(cause) { historyError = cause.message; document.querySelector('#history-detail').textContent = `내역 조회 실패: ${cause.message} 최신 내역을 눌러 다시 확인하세요.`; }
function openExecution(row, tab = 'work') {
  if (row.status === 'preview') { openUpdatePreview(row); return; }
  stopPolling(); logController?.abort(); logSnapshot = null; eventController?.abort(); eventSnapshot = null;
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
    content.append(element('strong', row.app || '앱 이름 미제공'), element('small', `${environmentLabel(row)} · ${formatTime(row.created_at)}`, 'history-meta'));
    header.append(content, badge);
    const actions = element('div', '', 'history-actions');
    for (const [label, tab] of [['실행 상세·작업 로그', 'work'], ...(row.kind === 'deployments' ? [['앱 로그', 'app']] : [])]) {
      const button = element('button', label, 'text-button'); button.type = 'button';
      button.setAttribute('aria-label', `${row.app || '앱'} ${label}`);
      button.addEventListener('click', () => openExecution(row, tab)); actions.append(button);
    }
    if (row.kind === 'deployments') {
      const app = applications.find((item) => item.id === row.application_id);
      if (app) actions.append(applicationButtons(app));
      else actions.append(element('span', '앱 관리 ID를 확인할 수 없어 자동 삭제를 지원하지 않습니다.', 'field-note'));
    }
    item.append(header, element('small', `실행 ${row.id}`, 'history-meta'), actions);
    if (row.kind === 'deployments') item.append(sourceDownloads(row));
    return item;
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

const metricNames = { runtime_healthz: '런타임 /healthz', node_up: '노드 지표 수집', cpu_percent: 'CPU', memory_percent: '메모리', disk_percent: '디스크',
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
  if (name === 'runtime_healthz') return metric.value === 1 ? '연결 정상' : '응답 실패';
  if (name === 'node_up') return metric.value === 1 ? '수집 중' : '수집 실패';
  if (name.endsWith('_percent')) return `${metric.value.toFixed(1)}%`;
  if (name.endsWith('_per_second')) return `${(metric.value / 1024).toFixed(1)} KiB/s`;
  return `${metric.value}개`;
}
function environmentState(observation) {
  if (!observation) return 'missing';
  const health = observation.metrics?.runtime_healthz, state = metricState(health, observation);
  if (state === 'stale') return 'stale';
  if (state === 'ready' && [0, 1].includes(health.value)) return health.value === 1 ? 'ready' : 'failed';
  return 'missing';
}
function renderRuntimeConnection() {
  const rows = targets.filter((item) => item.provider === deploymentSelection().provider);
  document.querySelector('#runtime-connection-status').textContent = rows.length ? rows.map((row) => {
    const observation = observations.get(row.id), health = observation?.metrics?.runtime_healthz, state = environmentState(observation);
    const label = { ready: '런타임 연결 정상', failed: '런타임 /healthz 응답 실패', stale: '런타임 연결 확인 필요 · 오래된 관측', missing: '런타임 연결 확인 불가' }[state];
    return `${rows.length > 1 ? `${row.label || row.id}: ` : ''}${label}${state === 'missing' ? ` · ${health ? metricText('runtime_healthz', observation) : '관측 대기'}` : ''}${health?.observed_at ? ` · ${formatTime(health.observed_at)}` : ''}`;
  }).join(' / ') : '등록된 런타임이 없습니다.';
}
function visibleTargets() {
  const provider = document.querySelector('#monitor-provider').value, target = document.querySelector('#monitor-target').value;
  return targets.filter((row) => (!provider || row.provider === provider) && (!target || row.id === target));
}
function renderEnvironments() {
  const opened = new Set([...document.querySelectorAll('#environment-detail details[open]')].map((item) => item.dataset.target));
  const focused = document.activeElement?.closest('#environment-detail details')?.dataset.target;
  const rows = visibleTargets(), stateNames = { ready: '런타임 연결 정상', failed: '런타임 응답 실패', missing: '연결 확인 불가', stale: '오래된 관측' };
  renderRuntimeConnection();
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
    tr.append(element('td', observation?.metrics?.runtime_healthz?.observed_at ? formatTime(observation.metrics.runtime_healthz.observed_at) : '확인 시각 없음'));
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
    const rows = views.deploy.hidden ? visibleTargets() : targets;
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
      : '선택한 조건에 등록된 런타임이 없습니다.';
  } catch (cause) {
    if (environmentController !== controller) return;
    observations = new Map(targets.map((row) => [row.id, { failed: true }])); renderEnvironments();
    document.querySelector('#environment-message').textContent = `환경 조회 실패: ${cause.name === 'AbortError' ? '조회 시간이 초과되었습니다.' : cause.message} 30초 후 다시 확인합니다.`;
  } finally {
    if (environmentController === controller) {
      environmentController = null;
      if (!views.monitor.hidden || !views.deploy.hidden) environmentTimer = setTimeout(loadEnvironments, 30000);
    }
  }
}
document.querySelector('#monitor-provider').addEventListener('change', () => { document.querySelector('#monitor-target').value = ''; loadEnvironments(); });
document.querySelector('#monitor-target').addEventListener('change', loadEnvironments);
document.querySelector('#monitor-refresh').addEventListener('click', loadEnvironments);

const targetStatuses = {
  pending: '등록 대기', installing: '설치 중', connecting: '연결 확인 중', preparing: '서버 배포 준비 중', ready: '배포 준비 완료', offline: '연결 끊김',
  queued: '접수됨', running: '진행 중', succeeded: '완료', blocked: '진행 차단', deleting: '삭제 중', attention: '확인 필요', deleted: '삭제 완료', failed: '등록 실패', unknown: '상태 확인 필요',
};
const connectionStatuses = { connecting: '확인 중', ready: '연결됨', offline: '연결 끊김' };
const runtimePreparationStatuses = { not_started: '시작 전', queued: '접수됨', running: '진행 중', succeeded: '완료', blocked: '진행 차단', failed: '실패', unknown: '결과 확인 필요', revoked: '연결 해제됨' };
const runtimePreparationStages = { client: '서버 배포 준비 시작 전', client_selection: '배포 서버 가상 머신 선택 또는 생성', client_installation: 'K3s 확인 또는 Ansible 자동 설치', client_verification: '배포 서버 검증', registration: '중앙 서버 등록', configuration: '중앙 배포 설정 확인', permission_verification: '배포 권한 확인', verification: '최종 검증', reconciliation: '결과 확인 필요', complete: '준비 완료' };
const runtimePreparationBlockers = {
  RUNTIME_OPERATOR_NOT_CONFIGURED: '중앙 서버에 OpenStack 배포 준비 설정이 없습니다. 운영자 설정이 필요합니다.',
  APPLICATION_CI_NOT_CONFIGURED: '중앙 서버에 앱 빌드·배포 설정이 없습니다. 운영자 설정이 필요합니다.',
  RUNTIME_APPLICATION_BINDING_INCOMPLETE: '중앙 서버의 앱 배포 연결 설정이 완료되지 않았습니다.',
  RUNTIME_DEPLOYMENT_PERMISSION_MISMATCH: '배포 서버 권한이 중앙 서버 설정과 일치하지 않습니다.',
  RUNTIME_RESOURCE_UNVERIFIED: '선택한 OpenStack 가상 머신을 확인하지 못했습니다.',
  RUNTIME_RESOURCE_MISMATCH: '확인한 OpenStack 가상 머신이 선택한 대상과 일치하지 않습니다.',
  RUNTIME_HOST_IDENTITY_CHANGED: '배포 서버의 신원이 이전 확인 결과와 달라 운영자 확인이 필요합니다.',
  RUNTIME_READBACK_FAILED: '중앙 서버가 배포 서버 상태를 다시 확인하지 못했습니다.',
  RUNTIME_PREPARATION_INTERRUPTED: '서버 배포 준비가 중단되어 결과 확인이 필요합니다.',
  RUNTIME_PREPARATION_UNVERIFIED: '서버 배포 준비 결과를 확인하지 못했습니다. 자동으로 다시 실행하지 않습니다.',
  RUNTIME_VERIFICATION_FAILED: '서버 배포 준비의 최종 검증에 실패했습니다.',
  RUNTIME_CLIENT_PREPARATION_FAILED: '관리 호스트에서 배포 서버 준비에 실패했습니다.',
  RUNTIME_PRIOR_OUTCOME_UNKNOWN: '이전 준비 작업의 결과가 불명확하여 다시 실행할 수 없습니다.',
  RUNTIME_EXISTING_CLUSTER_UNHEALTHY: '선택한 가상 머신의 기존 K3s 상태가 정상이 아닙니다.',
  RUNTIME_ANSIBLE_NOT_READY: 'Ansible 자동 설치 후 K3s 준비 상태를 확인하지 못했습니다.',
};
const registrationStages = { gateway: '보안 연결 구성 중', openstack: 'OpenStack 제어 연결 확인 중', checks: '연결 점검 중', runtime: '서버 배포 준비 중', created: '등록 요청 생성', enrollment: '설치 명령 실행 대기', installing: '클라이언트 설치 중', connecting: '연결 확인 중', failed: '등록 확인 필요' };
const deleteBlockers = { ACTIVE_DEPLOYMENT: '진행 중인 배포가 있어 먼저 상태 확인이 필요합니다.', DATA_CONSENT_REQUIRED: '앱 전용 데이터 삭제 동의가 필요합니다.', TARGET_UNAVAILABLE: '환경 연결 상태를 확인한 뒤 다시 시도하세요.' };
function targetStatusLabel(status) { return targetStatuses[status] || '확인 필요'; }
function connectionStatusLabel(status) { return connectionStatuses[status] || '확인 필요'; }
function runtimePreparationStatusLabel(status) { return runtimePreparationStatuses[status] || '확인 필요'; }
function runtimePreparationStageLabel(stage) { return runtimePreparationStages[stage] || `기타 단계 (${stage || '정보 없음'})`; }
function runtimePreparationBlockerLabel(blocker) { return runtimePreparationBlockers[blocker] || `확인 필요 (${blocker || '알 수 없는 사유'})`; }
function registrationStageLabel(stage, target) {
  if (['ready', 'complete'].includes(stage)) return target?.deployable === true && target.runtime_preparation?.status === 'succeeded'
    ? '등록·배포 준비 완료' : 'OpenStack 등록 완료';
  return registrationStages[stage] || '확인 필요';
}
function deletionBlockerLabel(blocker) { return deleteBlockers[blocker] || '삭제 전 확인이 필요합니다.'; }
function appendListItems(list, items, fallback) {
  list.replaceChildren(...(items.length ? items.map((item) => element('li', typeof item === 'string' ? item : item.label || item.name || item.id || '세부 정보 없음')) : [element('li', fallback, 'field-note')]));
}
const environmentDeleteStages = { queued: '접수', services: 'RailShot 서비스 삭제', runtime: '서버 배포 권한 회수', client: '관리 클라이언트 제거', gateway: '보안 연결 제거', reconciliation: '남은 항목 확인 필요', complete: '완료' };
const environmentDeleteSteps = { 'runtime:revoke': '서버 배포 권한 회수', client: '관리 클라이언트 제거', gateway: '보안 연결 제거' };
const environmentDeleteResiduals = { RemovalVerification: '클라이언트·보안 연결 제거 확인', RailShotClient: 'RailShot 관리 클라이언트', GatewayRegistration: 'RailShot 보안 연결 등록', CustomerServices: '고객 별도 서비스', Infrastructure: '기반 인프라' };
const environmentReconciliationBlockers = {
  RECONCILIATION_IN_PROGRESS: '기존 삭제 결과를 확인하고 있습니다.', CLIENT_STILL_ACTIVE: '관리 클라이언트가 아직 연결되어 있습니다.',
  RESIDUALS_REMAIN: '삭제되지 않은 RailShot 관리 항목이 남아 있습니다.', OPERATION_NOT_RESUMABLE: '현재 작업은 안전하게 재개할 수 없습니다.',
  REMOVAL_STILL_RUNNING: '이전 관리 클라이언트 제거 작업이 아직 실행 중입니다.',
  CLIENT_INSTALLATION_NOT_INTACT: '관리 클라이언트 설치 상태가 안전 재개 조건과 일치하지 않습니다.',
};
function environmentDeleteStepLabel(name) {
  if (environmentDeleteSteps[name]) return environmentDeleteSteps[name];
  if (name?.startsWith('application:')) return `RailShot 서비스 ${name.slice('application:'.length)} 삭제`;
  return `기타 단계 (${name || '이름 없음'})`;
}
function renderEnvironmentDeleteOperation() {
  const operation = environmentDeleteOperation;
  const panel = document.querySelector('#environment-delete-operation'); panel.hidden = !operation;
  if (!operation) return;
  const state = ({ queued: '접수됨', running: '진행 중', succeeded: '삭제 완료', failed: '실패', blocked: '진행 차단', unknown: '결과 확인 필요' })[operation.status] || '결과 확인 필요';
  const stage = operation.stage ? environmentDeleteStages[operation.stage] || `기타 단계 (${operation.stage})` : '';
  document.querySelector('#environment-delete-operation-state').textContent = `작업 ${operation.id} · ${state}${stage ? ` · ${stage}` : ''}`;
  const reconciliation = operation.reconciliation;
  const reconciliationUsable = reconciliation?.status === 'ready' && reconciliation.resumable === true && Date.parse(reconciliation.expires_at) > Date.now();
  const reconciliationMessage = operation.reconciliationError ? operation.reconciliationError
    : reconciliation?.status === 'pending' ? '기존 삭제 결과와 남은 관리 항목을 확인하고 있습니다.'
    : reconciliationUsable ? '안전 확인을 마쳤습니다. 환경 이름과 데이터 삭제 동의를 다시 확인한 뒤 기존 작업을 재개할 수 있습니다.'
    : reconciliation?.status === 'ready' && reconciliation.resumable === true ? '삭제 재개 안전 확인의 유효 시간이 지났습니다. 가능 여부를 다시 확인하세요.'
    : reconciliation?.status === 'blocked' ? (reconciliation.blockers || []).map((code) => environmentReconciliationBlockers[code] || `재개 확인 필요 (${code})`).join(' ') : '';
  document.querySelector('#environment-delete-operation-message').textContent = operation.readError || reconciliationMessage || operation.error?.message
    || (operation.status === 'succeeded' ? '서버가 환경 삭제 완료를 확인했습니다.' : ['queued', 'running'].includes(operation.status)
      ? '삭제 작업이 진행 중입니다. 상태를 다시 조회하면 최신 결과를 확인할 수 있습니다.' : '삭제 완료를 확인하지 못했습니다. 자동으로 다시 실행하지 않습니다. 남은 항목을 확인하세요.');
  const residuals = Array.isArray(operation.residuals) ? operation.residuals : [];
  const steps = Array.isArray(operation.steps) ? operation.steps : [];
  document.querySelector('#environment-delete-operation-steps').replaceChildren(...(steps.length
    ? steps.map((item) => element('li', `${environmentDeleteStepLabel(item.name)} · ${targetStatusLabel(item.status)}`))
    : [element('li', '서버가 단계별 결과를 제공하지 않았습니다.', 'field-note')]));
  document.querySelector('#environment-delete-operation-residuals').replaceChildren(...(residuals.length
    ? residuals.map((item) => element('li', `${environmentDeleteResiduals[item.kind] || `기타 확인 항목 (${item.kind || '종류 없음'})`} · ${item.name || item.id || '세부 정보 없음'}`))
    : [element('li', operation.status === 'succeeded' ? '남은 항목 없음' : '서버가 남은 항목 목록을 제공하지 않았습니다.', 'field-note')]));
  const reconcile = document.querySelector('#environment-delete-reconcile'), resume = document.querySelector('#environment-delete-resume');
  const uncertain = ['unknown', 'failed', 'blocked'].includes(operation.status);
  reconcile.hidden = !uncertain || reconciliationUsable;
  reconcile.disabled = reconciliation?.status === 'pending';
  reconcile.textContent = reconciliation?.status === 'pending' ? '삭제 재개 가능 여부 확인 중' : reconciliation?.status === 'blocked' ? '삭제 재개 가능 여부 다시 확인' : '삭제 재개 가능 여부 확인';
  resume.hidden = !(uncertain && reconciliationUsable);
}
function stopEnvironmentDeleteReconciliationPolling() { clearTimeout(environmentDeleteReconciliationTimer); environmentDeleteReconciliationTimer = undefined; }
function scheduleEnvironmentDeleteReconciliation(target, operation) {
  stopEnvironmentDeleteReconciliationPolling();
  if (operation?.reconciliation?.status === 'pending' && selectedOwnedDetail?.id === target.id) {
    environmentDeleteReconciliationTimer = setTimeout(() => loadEnvironmentDeleteOperation(target), 2000);
  }
}
async function loadEnvironmentDeleteOperation(target = selectedOwnedDetail) {
  const id = target?.deletion_operation_id;
  if (!id) { environmentDeleteOperation = null; renderEnvironmentDeleteOperation(); return null; }
  document.querySelector('#environment-delete-operation-refresh').disabled = true;
  try {
    const { data } = await request(`/api/v1/operations/${encodeURIComponent(id)}`);
    if (!data?.id || data.id !== id || data.target_id !== target.id || data.kind !== 'target-lifecycle') throw new Error('환경 삭제 작업 상태 응답을 확인하지 못했습니다.');
    if (selectedOwnedDetail?.id !== target.id) return null;
    environmentDeleteOperation = data; renderEnvironmentDeleteOperation(); scheduleEnvironmentDeleteReconciliation(target, data);
    if (data.status === 'succeeded') {
      clearEnvironmentRegistrationFeedback(target.id);
      await loadOwnedTargets({ preserveDetail: false, removedMessage: '환경 삭제를 완료했습니다. 등록 목록과 배포 대상에서 제거했습니다.' });
    }
    return data;
  } catch (cause) {
    if (selectedOwnedDetail?.id !== target.id) return null;
    environmentDeleteOperation = { id, status: 'unknown', readError: `삭제 작업 상태 조회 실패: ${cause.message}` };
    renderEnvironmentDeleteOperation(); return null;
  } finally { document.querySelector('#environment-delete-operation-refresh').disabled = false; }
}
function renderOwnedTargetOptions() {
  const selected = provider.value || provider.dataset.restoreTarget || '';
  const deployable = ownedTargets.filter((target) => target.status !== 'deleting' && target.status !== 'deleted');
  provider.replaceChildren(new Option(ownedTargetError ? '등록 환경을 다시 불러오세요' : '등록 환경을 선택하세요', ''),
    ...deployable.map((target) => new Option(`${target.label || target.id} · ${targetStatusLabel(target.status)}`, target.id)),
    new Option('신규 OpenStack환경 추가', '__new_openstack__'));
  if ([...provider.options].some((option) => option.value === selected)) { provider.value = selected; delete provider.dataset.restoreTarget; }
}
function renderOwnedTargetList() {
  const list = document.querySelector('#environment-owned-items');
  const message = document.querySelector('#environment-owned-message');
  if (ownedTargetError) {
    message.textContent = `목록 조회 실패: ${ownedTargetError} 등록된 환경이 없다는 뜻이 아닙니다. 현재 화면에는 마지막으로 확인했거나 방금 등록한 환경을 표시합니다.`;
  }
  if (!ownedTargets.length) {
    if (!ownedTargetError) message.textContent = '아직 등록한 OpenStack 환경이 없습니다.';
    list.replaceChildren();
    return;
  }
  if (!ownedTargetError) message.textContent = `${ownedTargets.length}개 환경 · 상태와 마지막 응답 시각은 새로고침할 때 갱신됩니다.`;
  list.replaceChildren(...ownedTargets.map((target) => {
    const item = document.createElement('li'), button = element('button', target.label || target.id, 'text-button');
    button.type = 'button'; button.classList.toggle('active', target.id === selectedOwnedTarget);
    button.addEventListener('click', () => selectOwnedTarget(target.id));
    const project = target.project_id ? ` · 프로젝트 ${target.project_id}` : '';
    const runtime = target.runtime_preparation || { status: 'not_started' };
    item.append(button, element('small', `OpenStack${project} · 전체 ${targetStatusLabel(target.status)} · 제어 연결 ${connectionStatusLabel(target.connection_status)} · 서버 준비 ${runtimePreparationStatusLabel(runtime.status)} · 마지막 응답 ${formatTime(target.last_seen_at)} · RailShot 서비스 ${Number.isFinite(target.application_count) ? target.application_count : '확인 중'}개`));
    return item;
  }));
}
function renderOwnedTargetDetail() {
  const message = document.querySelector('#environment-detail-message'), content = document.querySelector('#environment-detail-content');
  if (!selectedOwnedDetail) { content.hidden = true; message.textContent = selectedOwnedTarget ? '환경 상세를 불러오지 못했습니다.' : '목록에서 환경을 선택하세요.'; return; }
  content.hidden = false;
  const target = selectedOwnedDetail;
  message.textContent = `${target.label || target.id} · ${targetStatusLabel(target.status)}`;
  const runtime = target.runtime_preparation || { status: 'not_started', stage: 'client', blockers: [], verified_at: null };
  const facts = [['전체 상태', targetStatusLabel(target.status)], ['OpenStack 제어 연결', connectionStatusLabel(target.connection_status)], ['서버 배포 준비', runtimePreparationStatusLabel(runtime.status)], ['등록 단계', registrationStageLabel(target.registration_stage, target)],
    ['배포 가능', target.deployable === true ? '가능' : '아직 불가'], ['클라이언트', target.client_version || '상태 보고 대기'],
    ['마지막 응답', formatTime(target.last_seen_at)], ['OpenStack 프로젝트', target.project_id || '정보 없음']];
  document.querySelector('#environment-detail-facts').replaceChildren(...facts.map(([name, value]) => {
    const row = document.createElement('div'); row.append(element('dt', name), element('dd', value)); return row;
  }));
  document.querySelector('#environment-runtime-preparation-state').textContent = `${runtimePreparationStatusLabel(runtime.status)} · ${runtimePreparationStageLabel(runtime.stage)}${runtime.verified_at ? ` · 최종 확인 ${formatTime(runtime.verified_at)}` : ''}`;
  const runtimeBlockers = Array.isArray(runtime.blockers) ? runtime.blockers : [];
  const runtimeNote = runtime.status === 'succeeded' ? '중앙 서버가 앱 배포 연결까지 확인했습니다.'
    : runtime.status === 'not_started' ? '관리 호스트 설치 명령에서 별도 OpenStack 가상 머신을 선택하거나 새로 만들어 준비합니다.'
    : ['queued', 'running'].includes(runtime.status) ? '기존 K3s를 확인해 재사용하거나, 없으면 Ansible 자동 설치 후 중앙 서버가 검증합니다.'
    : runtime.status === 'unknown' ? '준비 결과가 불명확합니다. 자동으로 다시 실행하지 않으며 운영자 확인이 필요합니다.'
    : runtime.status === 'blocked' || runtime.status === 'failed' ? '아래 사유를 해결한 뒤 서버 배포 준비를 다시 확인해야 합니다.' : '서버 배포 준비 상태를 확인하고 있습니다.';
  document.querySelector('#environment-runtime-preparation-blockers').replaceChildren(...(runtimeBlockers.length
    ? runtimeBlockers.map((blocker) => element('li', runtimePreparationBlockerLabel(blocker))) : [element('li', runtimeNote, 'field-note')]));
  const appMessage = document.querySelector('#environment-applications-message');
  appMessage.textContent = Array.isArray(ownedApplications) ? (ownedApplications.length ? `RailShot으로 배포한 서비스 ${ownedApplications.length}개` : 'RailShot으로 배포한 서비스가 없습니다.') : '서비스 목록을 불러오지 못했습니다.';
  const appList = document.querySelector('#environment-applications-list');
  appList.replaceChildren(...(Array.isArray(ownedApplications) && ownedApplications.length ? ownedApplications.map((app) => element('li', `${app.name || app.app || app.id || '이름 미제공'} · ${app.status || '상태 정보 없음'} · 마지막 배포 ${formatTime(app.deployed_at || app.updated_at)}${app.public_url || app.url ? ` · ${app.public_url || app.url}` : ''}`)) : []));
  const observationList = document.querySelector('#environment-observations');
  const metrics = ownedObservation?.metrics;
  if (!metrics || ownedObservation.failed) observationList.replaceChildren(element('div', ownedObservation?.failed ? '지표 조회 실패' : '아직 수집된 지표가 없습니다.'));
  else observationList.replaceChildren(...['cpu_percent', 'memory_percent', 'disk_percent', 'network_receive_bytes_per_second', 'network_transmit_bytes_per_second'].map((name) => {
    const cell = document.createElement('div'), metric = metrics[name];
    cell.append(element('dt', metricNames[name] || name), element('dd', `${metricText(name, ownedObservation)} · ${formatTime(metric?.observed_at)}`)); return cell;
  }));
  document.querySelector('#environment-continue-deploy').disabled = !(target.status === 'ready' && target.deployable === true);
  const deleteButton = document.querySelector('#environment-delete');
  deleteButton.textContent = target.deletion_operation_id ? '삭제 상태 확인' : '환경 삭제';
  deleteButton.disabled = target.status === 'deleted';
}
function clearEnrollment() {
  enrollmentRequestGeneration += 1;
  enrollmentTargetId = null;
  const panel = document.querySelector('#environment-enrollment');
  panel.hidden = true;
  document.querySelector('#environment-enrollment-expiry').textContent = '';
  document.querySelector('#environment-enrollment-command-state').textContent = '';
  const command = document.querySelector('#environment-install-command');
  command.textContent = ''; command.hidden = true;
  const copy = document.querySelector('#environment-copy-command');
  copy.disabled = true; copy.onclick = null;
  document.querySelector('#environment-regenerate-command').disabled = true;
  document.querySelector('#environment-enrollment-message').textContent = '';
}
function clearEnvironmentRegistrationFeedback(targetId) {
  if (enrollmentTargetId === targetId) clearEnrollment();
  if (environmentRegistrationTargetId === targetId) {
    environmentRegistrationTargetId = null;
    document.querySelector('#environment-register-message').textContent = '';
  }
}
function showEnrollmentState(id, commandState, message, { retry = true } = {}) {
  enrollmentTargetId = id;
  document.querySelector('#environment-enrollment').hidden = false;
  document.querySelector('#environment-enrollment-expiry').textContent = '';
  document.querySelector('#environment-enrollment-command-state').textContent = commandState;
  const command = document.querySelector('#environment-install-command');
  command.textContent = ''; command.hidden = true;
  const copy = document.querySelector('#environment-copy-command');
  copy.disabled = true; copy.onclick = null;
  document.querySelector('#environment-regenerate-command').disabled = !retry;
  document.querySelector('#environment-enrollment-message').textContent = message;
}
function canIssueEnrollment(target) {
  return target?.status === 'pending' || target?.registration_stage === 'enrollment';
}
function resetOwnedEnvironmentSelection() {
  stopEnvironmentDeleteReconciliationPolling();
  clearEnrollment();
  ownedTargetSelectionGeneration += 1;
  selectedOwnedTarget = null; selectedOwnedDetail = null; ownedObservation = null; ownedApplications = [];
  ownedTargets = []; ownedTargetError = null;
  provider.value = ''; delete provider.dataset.restoreTarget;
  renderOwnedTargetOptions(); renderOwnedTargetList(); renderOwnedTargetDetail(); updateSelection();
}
function emphasizeOwnedTargets() {
  const panel = document.querySelector('#environment-owned-list-panel');
  panel.classList.add('recovery-highlight');
  panel.focus({ preventScroll: true });
  panel.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest' });
}
function stopOwnedTargetPolling() { clearTimeout(ownedTargetTimer); ownedTargetTimer = undefined; ownedTargetController?.abort(); ownedTargetController = null; }
async function loadOwnedTargets({ preserveDetail = true, removedMessage = '' } = {}) {
  stopOwnedTargetPolling();
  const controller = new AbortController(); ownedTargetController = controller;
  try {
    const { data } = await request('/api/v1/targets?provider=openstack&scope=owned', {}, controller);
    if (!Array.isArray(data.items) || data.next_marker) throw new Error('개인 환경 목록을 모두 확인하지 못했습니다.');
    ownedTargets = data.items.filter((target) => target.provider === 'openstack' && target.status !== 'deleted'); ownedTargetError = null;
    const selectedRemoved = selectedOwnedTarget && !ownedTargets.some((target) => target.id === selectedOwnedTarget);
    if (selectedRemoved) {
      clearEnvironmentRegistrationFeedback(selectedOwnedTarget);
      stopEnvironmentDeleteReconciliationPolling(); selectedOwnedTarget = null; selectedOwnedDetail = null; ownedObservation = null; ownedApplications = []; environmentDeleteOperation = null;
    }
    renderOwnedTargetOptions(); renderOwnedTargetList(); updateSelection();
    if (selectedRemoved) { renderOwnedTargetDetail(); renderEnvironmentDeleteOperation(); if (removedMessage) document.querySelector('#environment-detail-message').textContent = removedMessage; }
    if (selectedOwnedTarget && (!preserveDetail || !views.personal.hidden)) await selectOwnedTarget(selectedOwnedTarget, false);
    return ownedTargets;
  } catch (cause) {
    if (controller.signal.aborted) return;
    ownedTargetError = cause.name === 'AbortError' ? '조회 시간이 초과되었습니다.' : cause.message;
    renderOwnedTargetOptions(); renderOwnedTargetList(); updateSelection();
    return null;
  } finally {
    if (ownedTargetController === controller) {
      ownedTargetController = null;
      if (!views.personal.hidden) ownedTargetTimer = setTimeout(() => loadOwnedTargets(), 30000);
    }
  }
}
async function selectOwnedTarget(id, refreshList = true) {
  const target = ownedTargets.find((item) => item.id === id); if (!target) return;
  if (selectedOwnedTarget !== id) clearEnrollment();
  const selectionGeneration = ++ownedTargetSelectionGeneration;
  selectedOwnedTarget = id; selectedOwnedDetail = target; ownedObservation = null; ownedApplications = [];
  stopEnvironmentDeleteReconciliationPolling(); environmentDeleteOperation = null; renderEnvironmentDeleteOperation();
  renderOwnedTargetList(); renderOwnedTargetDetail();
  if (canIssueEnrollment(target) && enrollmentTargetId !== id) showEnrollmentState(id, '설치 명령이 아직 발급되지 않았습니다.', '등록 대기 환경입니다. 설치 명령을 다시 발급할 수 있습니다.');
  document.querySelector('#environment-detail-message').textContent = '환경 상세와 서비스·지표를 조회하고 있습니다.';
  try {
    const [detail, observationsResponse, applicationsResponse] = await Promise.allSettled([
      request(`/api/v1/targets/${encodeURIComponent(id)}`), request(`/api/v1/targets/${encodeURIComponent(id)}/observations`), request(`/api/v1/targets/${encodeURIComponent(id)}/applications`),
    ]);
    if (selectedOwnedTarget !== id || ownedTargetSelectionGeneration !== selectionGeneration) return;
    if (detail.status === 'fulfilled') selectedOwnedDetail = detail.value.data;
    else throw detail.reason;
    ownedObservation = observationsResponse.status === 'fulfilled' ? observationsResponse.value.data : { failed: true };
    ownedApplications = applicationsResponse.status === 'fulfilled' && Array.isArray(applicationsResponse.value.data.items) ? applicationsResponse.value.data.items : null;
    renderOwnedTargetDetail();
    if (selectedOwnedDetail.deletion_operation_id) await loadEnvironmentDeleteOperation(selectedOwnedDetail);
  } catch (cause) {
    if (selectedOwnedTarget !== id || ownedTargetSelectionGeneration !== selectionGeneration) return;
    selectedOwnedDetail = null; renderOwnedTargetDetail();
    document.querySelector('#environment-detail-message').textContent = `환경 상세 조회 실패: ${cause.message}`;
  }
  if (refreshList) loadOwnedTargets();
}
async function copyText(value, message) {
  try { await navigator.clipboard.writeText(value); message.textContent = '복사했습니다. 안전한 곳에 보관하세요.'; }
  catch { message.textContent = '자동 복사에 실패했습니다. 내용을 직접 복사하세요.'; }
}
function showRecoveryKey(key) {
  if (typeof key !== 'string' || !key) return;
  document.querySelector('#environment-recovery-key').hidden = false;
  document.querySelector('#environment-recovery-key-value').textContent = key;
  document.querySelector('#environment-copy-recovery-key').onclick = () => copyText(key, document.querySelector('#environment-recovery-key-message'));
}
async function ensureEnvironmentOwner() {
  if (ownerRecoveryConfigured) { renderOwnerNote(); return; }
  const { data } = await request('/api/v1/owners', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  if (!data?.id) throw new Error('개인 환경 소유권을 확인하지 못했습니다.');
  const issuedRecoveryKey = typeof data.recovery_key === 'string' && data.recovery_key.length > 0;
  if (issuedRecoveryKey && data.recovery_key_once !== true) throw new Error('개인 환경 소유권 복구 키를 발급하지 못했습니다.');
  if (!issuedRecoveryKey && data.recovery_configured !== true) throw new Error('개인 환경 소유권을 확인하지 못했습니다.');
  ownerRecoveryConfigured = true; ownerInfoGeneration += 1; renderOwnerNote();
  if (issuedRecoveryKey) showRecoveryKey(data.recovery_key);
}
async function checkPersonalReadiness() {
  const { data } = await request('/api/v1/readiness?scope=personal');
  if (data?.scope !== 'personal' || typeof data.ready !== 'boolean' || !Array.isArray(data.blockers))
    throw new Error('개인 환경 설치 선행 조건 응답을 확인하지 못했습니다.');
  if (!data.ready) {
    const reason = data.blockers.map((row) => row?.message).filter(Boolean).join(' ');
    throw new Error(reason || '개인 환경 운영 설정이 아직 준비되지 않았습니다.');
  }
}
async function issueEnrollment(id, target = ownedTargets.find((item) => item.id === id)) {
  if (!canIssueEnrollment(target)) {
    showEnrollmentState(id, '설치 명령을 발급할 수 없습니다.', '현재 환경 상태에서는 설치 명령을 발급할 수 없습니다.', { retry: false });
    return;
  }
  const requestGeneration = ++enrollmentRequestGeneration;
  showEnrollmentState(id, '설치 명령을 발급하고 있습니다.', '설치 명령을 발급하고 있습니다.', { retry: false });
  const message = document.querySelector('#environment-enrollment-message');
  try {
    const { data } = await request(`/api/v1/targets/${encodeURIComponent(id)}/enrollments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (typeof data.install_command !== 'string' || !data.install_command || data.target_id !== id) throw new Error('설치 명령 응답을 확인하지 못했습니다.');
    if (enrollmentTargetId !== id || enrollmentRequestGeneration !== requestGeneration) return;
    document.querySelector('#environment-enrollment-command-state').textContent = '한 번만 실행할 설치 명령입니다.';
    const command = document.querySelector('#environment-install-command');
    command.textContent = data.install_command; command.hidden = false;
    document.querySelector('#environment-enrollment-expiry').textContent = `한 번만 실행할 설치 명령 · 만료 ${formatTime(data.expires_at)}`;
    const copy = document.querySelector('#environment-copy-command');
    copy.disabled = false; copy.onclick = () => copyText(data.install_command, message);
    document.querySelector('#environment-regenerate-command').disabled = false;
    message.textContent = '명령을 복사해 OpenStack 관리망에 접근 가능한 관리 호스트에서 실행하세요. 이어서 앱 배포용 가상 머신을 선택하거나 새로 만들고, K3s 확인 또는 Ansible 자동 설치를 진행합니다.';
  } catch (cause) {
    if (enrollmentTargetId !== id || enrollmentRequestGeneration !== requestGeneration) return;
    showEnrollmentState(id, '설치 명령을 발급하지 못했습니다.', `설치 명령 발급 실패: ${cause.message} 환경 등록 상태를 확인한 뒤 다시 시도하세요.`);
  }
}
document.querySelector('#environment-register-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const label = document.querySelector('#environment-label').value.trim(), message = document.querySelector('#environment-register-message');
  if (!label) { message.textContent = '환경 이름을 입력하세요.'; return; }
  const submit = document.querySelector('#environment-register-submit'); submit.disabled = true; environmentRegistrationTargetId = null; message.textContent = '개인 환경을 만들고 있습니다.';
  clearEnrollment();
  showEnrollmentState(null, '환경을 만들고 있습니다.', '환경 생성 후 설치 명령을 발급합니다.', { retry: false });
  try {
    await checkPersonalReadiness();
    await ensureEnvironmentOwner();
    const { data } = await request('/api/v1/targets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ label, provider: 'openstack' }) });
    if (typeof data.id !== 'string' || data.provider !== 'openstack') throw new Error('환경 등록 응답을 확인하지 못했습니다.');
    ownedTargetError = null;
    const index = ownedTargets.findIndex((target) => target.id === data.id);
    if (index >= 0) ownedTargets.splice(index, 1, data); else ownedTargets.push(data);
    selectedOwnedTarget = data.id; selectedOwnedDetail = data; ownedObservation = null; ownedApplications = [];
    renderOwnedTargetOptions(); renderOwnedTargetList(); renderOwnedTargetDetail(); emphasizeOwnedTargets();
    environmentRegistrationTargetId = data.id;
    message.textContent = `${data.label || label} 환경을 만들었습니다. 현재 브라우저에 관리 권한이 연결되었습니다. 복구 키를 다시 입력할 필요가 없습니다. 설치 명령을 발급합니다.`;
    const enrollment = issueEnrollment(data.id, data);
    await loadOwnedTargets({ preserveDetail: false }); await selectOwnedTarget(data.id, false); await enrollment;
  } catch (cause) {
    message.textContent = `환경 등록 실패: ${cause.message}`;
    showEnrollmentState(null, '환경 등록을 완료하지 못했습니다.', `설치 명령을 발급하지 못했습니다: ${cause.message}`, { retry: false });
  }
  finally { submit.disabled = false; }
});
document.querySelector('#environment-owned-refresh').addEventListener('click', () => loadOwnedTargets({ preserveDetail: false }));
document.querySelector('#environment-regenerate-command').addEventListener('click', () => { if (enrollmentTargetId) issueEnrollment(enrollmentTargetId); });
document.querySelector('#environment-continue-deploy').addEventListener('click', () => {
  if (!selectedOwnedDetail || !(selectedOwnedDetail.status === 'ready' && selectedOwnedDetail.deployable === true)) return;
  document.querySelector('[name="environment"][value="onprem"]').checked = true; renderOwnedTargetOptions(); provider.value = selectedOwnedDetail.id; updateSelection(); showView('deploy');
});
document.querySelector('#open-environment-management').addEventListener('click', () => showView('personal'));
const environmentDeleteDialog = document.querySelector('#environment-delete-dialog');
function renderEnvironmentDeleteLoading(target) {
  environmentDeleteDraft = null;
  document.querySelector('#environment-delete-title').textContent = '환경 삭제 계획';
  document.querySelector('#environment-delete-description').textContent = `${target.label || target.id} 환경의 최신 삭제 계획을 확인하고 있습니다.`;
  document.querySelector('#environment-delete-expiry').textContent = '';
  appendListItems(document.querySelector('#environment-delete-resources'), [], '삭제할 RailShot 리소스를 확인하고 있습니다.');
  appendListItems(document.querySelector('#environment-delete-retained'), [], '보존할 리소스를 확인하고 있습니다.');
  document.querySelector('#environment-delete-confirmation-label').textContent = '계획 확인이 끝나면 환경 이름을 입력할 수 있습니다.';
  const confirmation = document.querySelector('#environment-delete-confirmation'); confirmation.value = ''; confirmation.disabled = true;
  const consent = document.querySelector('#environment-delete-data-consent'); consent.checked = false; consent.disabled = true;
  document.querySelector('#environment-delete-confirm').textContent = '삭제 시작'; document.querySelector('#environment-delete-confirm').disabled = true;
  const failure = document.querySelector('#environment-delete-error'); failure.textContent = ''; failure.hidden = true;
}
function renderEnvironmentDeleteResumeDraft(target, operation) {
  const reconciliation = operation.reconciliation;
  environmentDeleteDraft = { mode: 'resume', target, operation, key: crypto.randomUUID() };
  document.querySelector('#environment-delete-title').textContent = '환경 삭제 재개';
  document.querySelector('#environment-delete-description').textContent = `${target.label || target.id} 환경의 기존 삭제 작업을 안전 확인 결과에 따라 재개합니다.`;
  document.querySelector('#environment-delete-expiry').textContent = `재개 확인 유효 시각: ${formatTime(reconciliation.expires_at)}`;
  appendListItems(document.querySelector('#environment-delete-resources'), operation.residuals || [], '서버가 남은 RailShot 관리 항목을 다시 확인했습니다.');
  appendListItems(document.querySelector('#environment-delete-retained'), ['고객 별도 서비스', '기반 가상 머신·네트워크·기존 K3s', '공유 데이터'], '고객 자원은 보존합니다.');
  document.querySelector('#environment-delete-confirmation-label').textContent = `재개하려면 환경 이름 “${target.label || target.id}”을 입력하세요.`;
  const confirmation = document.querySelector('#environment-delete-confirmation'); confirmation.value = ''; confirmation.disabled = false;
  const consent = document.querySelector('#environment-delete-data-consent'); consent.checked = false; consent.disabled = false;
  const submit = document.querySelector('#environment-delete-confirm'); submit.textContent = '삭제 재개 실행'; submit.disabled = true;
  const failure = document.querySelector('#environment-delete-error'); failure.textContent = ''; failure.hidden = true;
}
function renderEnvironmentDeleteDraft() {
  const draft = environmentDeleteDraft; if (!draft) return;
  document.querySelector('#environment-delete-description').textContent = `${draft.target.label || draft.target.id} 환경에서 RailShot 서비스와 앱 전용 데이터를 삭제합니다.`;
  document.querySelector('#environment-delete-expiry').textContent = `계획 유효 시각: ${formatTime(draft.plan.expires_at)}`;
  appendListItems(document.querySelector('#environment-delete-resources'), draft.plan.resources || [], '삭제할 RailShot 리소스가 없습니다.');
  appendListItems(document.querySelector('#environment-delete-retained'), draft.plan.retained || [], '보존 항목 정보가 없습니다.');
  document.querySelector('#environment-delete-confirmation-label').textContent = `삭제를 진행하려면 환경 이름 “${draft.target.label || draft.target.id}”을 입력하세요.`;
  document.querySelector('#environment-delete-confirmation').value = '';
  document.querySelector('#environment-delete-confirmation').disabled = false;
  document.querySelector('#environment-delete-data-consent').checked = false;
  document.querySelector('#environment-delete-data-consent').disabled = false;
  document.querySelector('#environment-delete-confirm').disabled = true;
  document.querySelector('#environment-delete-confirm').textContent = '삭제 시작';
  document.querySelector('#environment-delete-error').hidden = true;
}
function updateEnvironmentDeleteConfirmation() {
  const draft = environmentDeleteDraft;
  const typed = document.querySelector('#environment-delete-confirmation').value.trim();
  const consent = document.querySelector('#environment-delete-data-consent').checked;
  document.querySelector('#environment-delete-confirm').disabled = !draft || typed !== (draft.target.label || draft.target.id) || !consent;
}
document.querySelector('#environment-delete-confirmation').addEventListener('input', updateEnvironmentDeleteConfirmation);
document.querySelector('#environment-delete-data-consent').addEventListener('change', updateEnvironmentDeleteConfirmation);
document.querySelector('#environment-delete-cancel').addEventListener('click', () => {
  environmentDeletePlanController?.abort(); environmentDeletePlanController = null; environmentDeleteDraft = null; environmentDeleteDialog.close();
});
document.querySelector('#environment-delete').addEventListener('click', async () => {
  const target = selectedOwnedDetail; if (!target) return;
  if (target.deletion_operation_id) {
    document.querySelector('#environment-detail-message').textContent = '기존 환경 삭제 작업 상태를 조회하고 있습니다.';
    const operation = await loadEnvironmentDeleteOperation(target);
    document.querySelector('#environment-detail-message').textContent = operation
      ? `${target.label || target.id} · ${targetStatusLabel(target.status)} · 삭제 작업 ${targetStatusLabel(operation.status)}`
      : `${target.label || target.id} · ${targetStatusLabel(target.status)} · 삭제 작업 상태 조회 실패`;
    document.querySelector('#environment-delete-operation').scrollIntoView({ block: 'nearest' }); return;
  }
  const message = document.querySelector('#environment-detail-message'); message.textContent = '삭제할 RailShot 리소스 계획을 계산하고 있습니다.';
  document.querySelector('#environment-delete').disabled = true;
  renderEnvironmentDeleteLoading(target); environmentDeleteDialog.showModal();
  const controller = new AbortController(); environmentDeletePlanController = controller;
  try {
    const { data: plan } = await request(`/api/v1/targets/${encodeURIComponent(target.id)}/plans`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'delete', delete_data: true }) }, controller);
    if (!environmentDeleteDialog.open || environmentDeletePlanController !== controller) return;
    if (!plan?.id || plan.target_id !== target.id || plan.action !== 'delete' || typeof plan.plan_hash !== 'string') throw new Error('환경 삭제 계획 응답을 확인하지 못했습니다.');
    if (Array.isArray(plan.blockers) && plan.blockers.length) throw new Error(`삭제 전 확인이 필요합니다: ${plan.blockers.map(deletionBlockerLabel).join(' ')}`);
    environmentDeleteDraft = { target, plan }; renderEnvironmentDeleteDraft();
  } catch (cause) {
    if (cause.name === 'AbortError' && !environmentDeleteDialog.open) return;
    const failure = document.querySelector('#environment-delete-error'); failure.textContent = `환경 삭제 계획 조회 실패: ${cause.name === 'AbortError' ? '요청 시간이 초과되었습니다.' : cause.message}`; failure.hidden = false;
    message.textContent = failure.textContent;
  } finally {
    if (environmentDeletePlanController === controller) environmentDeletePlanController = null;
    document.querySelector('#environment-delete').disabled = target.status === 'deleted';
  }
});
async function refreshEnvironmentDeleteOperation(id, targetId) {
  const { data } = await request(`/api/v1/operations/${encodeURIComponent(id)}`);
  if (!data?.id || data.id !== id || data.target_id !== targetId) throw new Error('환경 삭제 작업 상태 응답을 확인하지 못했습니다.');
  if (selectedOwnedDetail?.id !== targetId) return;
  environmentDeleteOperation = data; renderEnvironmentDeleteOperation();
  document.querySelector('#environment-detail-message').textContent = `환경 삭제 ${targetStatusLabel(data.status)}${data.message ? ` · ${data.message}` : ''}`;
  if (!['succeeded', 'failed', 'blocked', 'unknown', 'deleted'].includes(data.status)) setTimeout(() => refreshEnvironmentDeleteOperation(id, targetId).catch((cause) => {
    document.querySelector('#environment-detail-message').textContent = `환경 삭제 상태 조회 실패: ${cause.message}`;
  }), 5000);
  else {
    if (data.status === 'succeeded') clearEnvironmentRegistrationFeedback(targetId);
    loadOwnedTargets({ preserveDetail: false, removedMessage: data.status === 'succeeded' ? '환경 삭제를 완료했습니다. 등록 목록과 배포 대상에서 제거했습니다.' : '' });
  }
}
document.querySelector('#environment-delete-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const draft = environmentDeleteDraft, failure = document.querySelector('#environment-delete-error');
  if (!draft) return;
  const expected = draft.target.label || draft.target.id;
  if (document.querySelector('#environment-delete-confirmation').value.trim() !== expected || !document.querySelector('#environment-delete-data-consent').checked) return;
  const button = document.querySelector('#environment-delete-confirm'); button.disabled = true;
  try {
    draft.key ||= crypto.randomUUID();
    const body = draft.mode === 'resume'
      ? { action: 'resume', operation_id: draft.operation.id, reconciliation_id: draft.operation.reconciliation.id, confirmation: expected, delete_data: true }
      : { action: 'delete', plan_id: draft.plan.id, plan_hash: draft.plan.plan_hash, confirmation: expected, delete_data: true };
    const { data, location, status } = await request(`/api/v1/targets/${encodeURIComponent(draft.target.id)}/operations`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': draft.key }, body: JSON.stringify(body) });
    if (status !== 202 || !data?.id || location !== `/api/v1/operations/${encodeURIComponent(data.id)}`) throw new Error('환경 삭제 작업 접수 응답을 확인하지 못했습니다.');
    if (draft.mode === 'resume' && data.id !== draft.operation.id) throw new Error('기존 환경 삭제 작업과 다른 응답을 받았습니다.');
    environmentDeleteDialog.close(); environmentDeleteDraft = null;
    document.querySelector('#environment-detail-message').textContent = draft.mode === 'resume' ? '기존 환경 삭제 작업을 재개했습니다. 완료 상태를 확인합니다.' : '환경 삭제 작업을 접수했습니다. RailShot 서비스와 앱 전용 데이터를 삭제하고 관리 클라이언트를 제거합니다.';
    await refreshEnvironmentDeleteOperation(data.id, draft.target.id);
  } catch (cause) { failure.textContent = `환경 삭제를 ${draft.mode === 'resume' ? '재개하지' : '시작하지'} 못했습니다: ${cause.message}`; failure.hidden = false; button.disabled = false; }
});
document.querySelector('#environment-delete-operation-refresh').addEventListener('click', () => loadEnvironmentDeleteOperation());
document.querySelector('#environment-delete-reconcile').addEventListener('click', async () => {
  const target = selectedOwnedDetail, operation = environmentDeleteOperation;
  if (!target || !operation || !['unknown', 'failed', 'blocked'].includes(operation.status)) return;
  const button = document.querySelector('#environment-delete-reconcile'); button.disabled = true;
  try {
    const { data, location, status } = await request(`/api/v1/targets/${encodeURIComponent(target.id)}/reconciliations`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operation_id: operation.id }),
    });
    if (status !== 202 || data?.id !== operation.id || data.target_id !== target.id || location !== `/api/v1/operations/${encodeURIComponent(operation.id)}`) throw new Error('삭제 재개 확인 응답을 확인하지 못했습니다.');
    if (selectedOwnedDetail?.id !== target.id) return;
    environmentDeleteOperation = data; renderEnvironmentDeleteOperation(); scheduleEnvironmentDeleteReconciliation(target, data);
  } catch (cause) {
    if (selectedOwnedDetail?.id !== target.id) return;
    environmentDeleteOperation = { ...operation, reconciliationError: `삭제 재개 가능 여부 확인 실패: ${cause.message}` }; renderEnvironmentDeleteOperation();
  } finally { if (selectedOwnedDetail?.id === target.id) renderEnvironmentDeleteOperation(); }
});
document.querySelector('#environment-delete-resume').addEventListener('click', () => {
  const target = selectedOwnedDetail, operation = environmentDeleteOperation;
  if (!target || operation?.reconciliation?.status !== 'ready' || operation.reconciliation.resumable !== true || Date.parse(operation.reconciliation.expires_at) <= Date.now()) return;
  renderEnvironmentDeleteResumeDraft(target, operation); environmentDeleteDialog.showModal();
});
async function loadOwnerInfo() {
  const generation = ++ownerInfoGeneration;
  try {
    const { data } = await request('/api/v1/owners');
    if (generation !== ownerInfoGeneration) return;
    ownerRecoveryConfigured = data?.recovery_configured === true;
    renderOwnerNote();
  } catch (cause) { if (generation === ownerInfoGeneration) document.querySelector('#environment-owner-note').textContent = `소유권 상태 조회 실패: ${cause.message} 새 환경 등록 전 다시 확인합니다.`; }
}
function renderOwnerNote() {
  document.querySelector('#environment-owner-note').textContent = ownerRecoveryConfigured
    ? '현재 브라우저에 개인 환경 관리 권한이 연결되어 있습니다. 복구 키를 다시 입력할 필요가 없습니다.'
    : '첫 환경 등록 시 한 번만 표시되는 복구 키를 발급합니다.';
}
document.querySelector('#environment-recover-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const input = document.querySelector('#environment-recovery-input'), message = document.querySelector('#environment-recover-message');
  const recoveryKey = input.value.trim(); if (!recoveryKey) { message.textContent = '복구 키를 입력하세요.'; return; }
  const button = document.querySelector('#environment-recover-submit'); button.disabled = true; message.textContent = '관리권을 복구하고 있습니다.';
  try {
    const { data } = await request('/api/v1/recoveries', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ recovery_key: recoveryKey }) });
    if (!data?.id || typeof data.recovery_key !== 'string' || !data.recovery_key) throw new Error('복구 응답을 확인하지 못했습니다.');
    input.value = ''; ownerRecoveryConfigured = true; ownerInfoGeneration += 1; renderOwnerNote(); showRecoveryKey(data.recovery_key);
    stopOwnedTargetPolling(); resetOwnedEnvironmentSelection();
    const restoredTargets = await loadOwnedTargets({ preserveDetail: false });
    if (restoredTargets === null) {
      message.textContent = '관리권은 복구했으나 환경 목록을 불러오지 못했습니다. 목록 새로고침으로 다시 확인하세요.';
    } else {
      if (restoredTargets.length) await selectOwnedTarget(restoredTargets[0].id, false);
      message.textContent = `이 브라우저에서 관리권을 복구했습니다. 등록한 환경 ${restoredTargets.length}개를 불러왔습니다. 새 복구 키를 안전한 곳에 저장하세요.`;
    }
    emphasizeOwnedTargets();
    loadOwnerInfo();
  } catch (cause) { message.textContent = `관리권 복구 실패: ${cause.message}`; }
  finally { button.disabled = false; }
});
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
// Lifecycle actions bind only to the latest session-owned application inventory.
const lifecycleDialog = document.querySelector('#lifecycle-dialog');
const lifecycleNames = { stop: '중지', start: '재개', delete: '삭제' };
const applicationStates = { ready: '실행 중', stopped: '중지됨', deleted: '삭제됨', unknown: '확인 필요',
  stopping: '중지 중', starting: '재개 중', deleting: '삭제 중', queued: '배포 대기', registering: '배포 준비 중' };
let lifecycleDraft, lifecycleBusy = false, lifecycleTimer, lifecycleReadBusy = false;
let lifecycleOperation = null;
try { lifecycleOperation = JSON.parse(sessionStorage.getItem('railshot.application-operation') || 'null'); } catch { /* No writes are replayed on reload. */ }
function storeLifecycleOperation() {
  try { sessionStorage.setItem('railshot.application-operation', JSON.stringify(lifecycleOperation)); } catch { /* The server remains the authority. */ }
}
function applicationBusy(id, ownSubmission = false) {
  return (!ownSubmission && submitting) || Boolean(resuming) || lifecycleBusy || lifecycleReadBusy
    || ['queued', 'running'].includes(lifecycleOperation?.status)
    || Boolean(lifecycleOperation && lifecycleOperation.application_id === id && lifecycleOperation.status !== 'succeeded');
}
function updateBlocked(app) {
  return applicationBusy(app?.id) ? '진행 중이거나 결과를 확인해야 하는 앱 관리 작업이 있습니다.'
    : app?.current_deployment_state === 'unverified' ? '현재 적용 결과가 불확실하여 운영자 확인 후 업데이트할 수 있습니다.'
    : app?.status !== 'ready' ? '앱이 실행 가능한 준비 상태일 때 업데이트할 수 있습니다.'
    : !app.current_deployment ? '업데이트 기준으로 사용할 성공한 배포가 없습니다.' : '';
}
function applicationAllowed(app, action) {
  return !applicationBusy(app.id)
    && (action === 'start' ? app.status === 'stopped' : action === 'stop' ? app.status === 'ready' : ['ready', 'stopped', 'queued', 'running', 'registering'].includes(app.status));
}
function applicationButtons(app) {
  const actions = element('div', '', 'history-actions');
  for (const action of ['stop', 'start', 'delete']) {
    const button = element('button', action === 'delete' ? '🗑 삭제' : lifecycleNames[action], action === 'delete' ? 'text-button danger-button' : 'text-button');
    button.type = 'button'; button.disabled = !applicationAllowed(app, action);
    button.setAttribute('aria-label', `${app.app} ${lifecycleNames[action]}${action === 'delete' ? ' · 데이터도 영구 삭제' : ''}`);
    button.addEventListener('click', () => reviewApplication(app.id, action)); actions.append(button);
  }
  return actions;
}
function renderApplicationActions() {
  const app = applications.find((row) => row.id === current?.application_id);
  for (const id of ['run-application-actions', 'monitor-application-actions']) {
    const holder = document.getElementById(id); holder.replaceChildren();
    if (app) holder.append(element('strong', `${app.app} · ${applicationStates[app.status] || '상태 확인 필요'}`), applicationButtons(app));
    else if (current?.kind === 'deployments') holder.append(element('span', '이 배포의 앱 관리 ID를 최신 목록에서 확인하지 못했습니다. 자동 삭제를 지원하지 않습니다.', 'field-note'));
  }
  const detail = applications.find((row) => row.id === applicationDetail?.id);
  document.querySelector('#detail-application-actions').replaceChildren(...(detail ? [applicationButtons(detail)] : []));
  document.querySelector('#application-update').disabled = !detail || Boolean(updateBlocked({ ...applicationDetail, ...detail }));
  if (current) document.querySelector('#resume-run').disabled = observationError || applicationBusy(current.application_id);
}
function lifecycleResources(selector, rows, empty) {
  document.querySelector(selector).replaceChildren(...(rows?.length ? rows.map((row) => element('li', `${row.kind} · ${row.namespace ? row.namespace + '/' : ''}${row.name}`)) : [element('li', empty)]));
}
function resourceList(rows) { return Array.isArray(rows) && rows.every((row) => row && typeof row.kind === 'string' && typeof row.name === 'string' && (row.namespace == null || typeof row.namespace === 'string')); }
function lifecycleError(message) { const error = document.querySelector('#lifecycle-error'); error.textContent = message; error.hidden = !message; }
function lifecycleConfirmState() {
  const draft = lifecycleDraft;
  const valid = draft?.plan && Date.parse(draft.plan.expires_at) > Date.now();
  document.querySelector('#lifecycle-confirm').disabled = lifecycleBusy || !valid;
  if (draft?.plan && !valid) lifecycleError('계획이 만료됐습니다. 취소한 뒤 새 계획을 확인하세요.');
}
async function reviewApplication(id, action) {
  if (lifecycleBusy || lifecycleDialog.open) return;
  const opener = document.activeElement;
  lifecycleDraft = { id, action, opener, openerLabel: opener?.getAttribute('aria-label') }; lifecycleError('');
  document.querySelector('#lifecycle-title').textContent = `앱 ${lifecycleNames[action]} 계획`;
  document.querySelector('#lifecycle-description').textContent = '최신 앱 상태와 실행 계획을 확인하고 있습니다.';
  document.querySelector('#lifecycle-expiry').textContent = '';
  document.querySelector('#lifecycle-resources').replaceChildren(); document.querySelector('#lifecycle-retained').replaceChildren();
  document.querySelector('#lifecycle-confirm').textContent = action === 'delete' ? '영구 삭제' : `앱 ${lifecycleNames[action]}`;
  document.querySelector('#lifecycle-confirm').classList.toggle('danger-button', action === 'delete');
  lifecycleConfirmState(); lifecycleDialog.showModal();
  const draft = lifecycleDraft; lifecycleDialog.setAttribute('aria-busy', 'true');
  try {
    if (!await loadApplications()) throw new Error('최신 앱 목록을 확인한 뒤 다시 시도하세요.');
    const app = applications.find((row) => row.id === id);
    if (!app || !applicationAllowed(app, action)) throw new Error('현재 앱 상태에서는 이 작업을 실행할 수 없습니다. 진행 중인 작업이나 배포 상태를 확인하세요.');
    const { data: plan } = await request(`/api/v1/applications/${encodeURIComponent(id)}/plans`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
    if (lifecycleDraft !== draft || !lifecycleDialog.open) return;
    if (plan.application_id !== id || plan.action !== action || !/^[a-f0-9-]{36}$/.test(plan.id || '')
        || !/^[a-f0-9]{64}$/.test(plan.plan_hash || '') || !Number.isFinite(Date.parse(plan.expires_at))
        || !resourceList(plan.resources) || !resourceList(plan.retained)) throw new Error('앱과 일치하는 실행 계획을 확인하지 못했습니다.');
    Object.assign(draft, { app, plan });
    document.querySelector('#lifecycle-title').textContent = `${app.app} ${lifecycleNames[action]}`;
    document.querySelector('#lifecycle-description').textContent = action === 'delete'
      ? '데이터도 영구 삭제됩니다. 진행 중인 배포는 중단을 확인한 뒤 정리합니다. 앱 전용 Pod·네트워크·스토리지와 데이터를 영구 삭제합니다. 삭제한 데이터는 복구할 수 없습니다. 공용 노드와 공용 로드밸런서는 보존합니다.'
      : action === 'stop' ? '앱 실행을 중지합니다. 데이터와 스토리지는 보존하며, 다시 사용하려면 재개를 선택하세요.' : '보존된 앱 설정과 데이터로 실행을 재개합니다.';
    document.querySelector('#lifecycle-expiry').textContent = `계획 유효 기한: ${formatTime(plan.expires_at)}`;
    lifecycleResources('#lifecycle-resources', plan.resources, '변경할 리소스 없음');
    lifecycleResources('#lifecycle-retained', plan.retained, '서버가 별도로 표시한 보존 리소스 없음');
  } catch (cause) { if (lifecycleDraft === draft) lifecycleError(cause.message); }
  finally { if (lifecycleDraft === draft) { lifecycleDialog.setAttribute('aria-busy', 'false'); lifecycleConfirmState(); } }
}
function renderLifecycleOperation() {
  const operation = lifecycleOperation;
  document.querySelector('#lifecycle-operation').hidden = !operation;
  if (!operation) return;
  const title = `${operation.app || '앱'} ${lifecycleNames[operation.action] || '관리'}`;
  document.querySelector('#lifecycle-operation-title').textContent = title;
  document.querySelector('#lifecycle-operation-state').textContent = `${({ queued: '접수됨', running: '실행 중', succeeded: '완료', blocked: '실행 차단', failed: '실행 실패', unknown: '결과 확인 필요' })[operation.status] || '결과 확인 필요'}${operation.stage ? ' · ' + operation.stage : ''}`;
  document.querySelector('#lifecycle-operation-message').textContent = operation.readError || operation.error?.message ||
    (operation.status === 'succeeded' ? '서버가 실행 완료를 확인했습니다.' : ['queued', 'running'].includes(operation.status)
      ? '15초마다 상태를 확인합니다. 페이지를 닫아도 서버 작업은 계속됩니다.' : '완료로 확인되지 않았습니다. 자동으로 다시 실행하지 않습니다. 남은 자원과 서버의 실행 상태를 확인하세요.');
  document.querySelector('#lifecycle-operation-steps').replaceChildren(...(Array.isArray(operation.steps) ? operation.steps : []).map((step) => element('li', `${step.name}: ${step.status}`)));
  document.querySelector('#lifecycle-residuals').hidden = !operation.residuals?.length;
  lifecycleResources('#lifecycle-residual-list', operation.residuals, '');
  document.querySelector('#lifecycle-operation-refresh').disabled = lifecycleReadBusy || !operation.id;
  document.querySelector('#lifecycle-operation').setAttribute('aria-busy', String(lifecycleReadBusy || ['queued', 'running'].includes(operation.status)));
  renderApplications();
}
async function refreshLifecycleOperation() {
  clearTimeout(lifecycleTimer);
  const previous = lifecycleOperation;
  if (!previous?.id || lifecycleReadBusy) return;
  lifecycleReadBusy = true; renderLifecycleOperation();
  try {
    const { data } = await request(`/api/v1/operations/${encodeURIComponent(previous.id)}`);
    if (lifecycleOperation !== previous) return;
    if (data.id !== previous.id || data.application_id !== previous.application_id || data.action !== previous.action
        || !['queued', 'running', 'succeeded', 'blocked', 'failed', 'unknown'].includes(data.status)
        || !resourceList(data.residuals || [])) throw new Error('실행 결과의 앱 정보가 일치하지 않습니다.');
    lifecycleOperation = { ...previous, ...data, readError: null }; storeLifecycleOperation();
    if (['queued', 'running'].includes(data.status)) lifecycleTimer = setTimeout(refreshLifecycleOperation, 15000);
    else await loadApplications();
  } catch (cause) {
    if (lifecycleOperation === previous) { lifecycleOperation = { ...previous, status: 'unknown', readError: `상태 조회 실패: ${cause.message} 실행을 다시 보내지 않습니다. 상태 다시 조회를 눌러 확인하세요.` }; storeLifecycleOperation(); }
  } finally { lifecycleReadBusy = false; renderLifecycleOperation(); }
}
document.querySelector('#lifecycle-form').addEventListener('submit', async (event) => {
  event.preventDefault(); lifecycleConfirmState();
  const draft = lifecycleDraft;
  if (document.querySelector('#lifecycle-confirm').disabled || !draft?.plan) return;
  lifecycleBusy = true; lifecycleConfirmState(); renderApplications(); lifecycleDialog.setAttribute('aria-busy', 'true');
  document.querySelector('#lifecycle-cancel').disabled = true;
  let submitted = false;
  try {
    if (!await loadApplications()) throw new Error('최신 앱 상태를 확인하지 못했습니다. 요청을 보내지 않았습니다.');
    const app = applications.find((row) => row.id === draft.id);
    if (!app || app.app !== draft.app.app || app.status !== draft.app.status || Date.parse(draft.plan.expires_at) <= Date.now()) throw new Error('앱 상태가 바뀌었거나 계획이 만료됐습니다. 취소 후 새 계획을 확인하세요.');
    lifecycleOperation = { application_id: app.id, app: app.app, action: draft.action, status: 'unknown', id: null };
    storeLifecycleOperation(); submitted = true;
    const { data, location, status } = await request(`/api/v1/applications/${encodeURIComponent(app.id)}/operations`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ action: draft.action, plan_id: draft.plan.id, plan_hash: draft.plan.plan_hash,
        confirmation: app.app, ...(draft.action === 'delete' ? { delete_data: true } : {}) }) });
    if (status !== 202 || !/^[A-Za-z0-9._-]+$/.test(data.id || '') || location !== `/api/v1/operations/${data.id}`
        || data.application_id !== app.id || data.action !== draft.action || !['queued', 'running', 'succeeded', 'blocked', 'failed', 'unknown'].includes(data.status)) throw new Error('접수 결과를 확인하지 못했습니다.');
    lifecycleOperation = { ...lifecycleOperation, ...data, application_id: app.id, app: app.app, action: draft.action };
    storeLifecycleOperation(); lifecycleDialog.close(); refreshLifecycleOperation();
  } catch (cause) {
    if (submitted) {
      lifecycleOperation.readError = `요청 결과를 확인하지 못했습니다: ${cause.message} 중복 실행을 막기 위해 다시 보내지 않습니다. 운영자에게 실행 확인을 요청하세요.`;
      storeLifecycleOperation(); lifecycleDialog.close();
    } else lifecycleError(cause.message);
  } finally {
    lifecycleBusy = false; document.querySelector('#lifecycle-cancel').disabled = false;
    lifecycleDialog.setAttribute('aria-busy', 'false'); lifecycleConfirmState(); renderLifecycleOperation();
  }
});
document.querySelector('#lifecycle-cancel').addEventListener('click', () => lifecycleDialog.close());
lifecycleDialog.addEventListener('cancel', (event) => { if (lifecycleBusy) event.preventDefault(); });
lifecycleDialog.addEventListener('keydown', (event) => {
  if (event.key !== 'Tab') return;
  const controls = [...lifecycleDialog.querySelectorAll('button:not(:disabled)')];
  const first = controls[0], last = controls.at(-1);
  if (!first) { event.preventDefault(); return; }
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
lifecycleDialog.addEventListener('close', () => {
  const draft = lifecycleDraft; lifecycleDraft = null;
  const opener = draft?.opener?.isConnected ? draft.opener : [...document.querySelectorAll('button[aria-label]')].find((button) => button.getAttribute('aria-label') === draft?.openerLabel && !button.disabled && button.getClientRects().length);
  (opener || (!views.history.hidden ? document.querySelector('#applications-refresh') : document.querySelector('#lifecycle-operation-refresh'))).focus();
});
document.querySelector('#lifecycle-operation-refresh').addEventListener('click', refreshLifecycleOperation);
window.addEventListener('pagehide', () => clearTimeout(lifecycleTimer));

async function initializeDashboard() {
  try {
    const { data: session } = await request('/api/v1/sessions', { method: 'POST' });
    const { data: saved } = await request('/api/v1/preferences');
    preferences = saved;
    document.querySelector(`[name="environment"][value="${saved.environment}"]`).checked = true;
    cloudProvider.value = ['aws', 'gcp'].includes(saved.provider) ? saved.provider : 'aws';
    provider.dataset.restoreTarget = typeof saved.target_id === 'string' ? saved.target_id : '';
    showView(saved.view);
    await checkConnection();
    document.querySelector('#session-note').textContent = `이 브라우저 세션 · ${new Date(session.expires_at).toLocaleDateString()}까지 유지`;
    sessionReady = true;
    await Promise.allSettled([loadApplications(), loadHistory().catch(showHistoryError), loadOwnedTargets(), loadConnections().catch((cause) => {
      document.querySelector('#connection-message').textContent = cause.message;
    })]);
    renderLifecycleOperation();
    if (!views.personal.hidden) loadOwnerInfo();
    if (lifecycleOperation?.id) refreshLifecycleOperation();
    if (history.length) { current = history[0]; renderRun(); refreshRun(); }
    if (!views.monitor.hidden || !views.deploy.hidden) loadEnvironments();
  } catch (cause) {
    connectionError = cause.message; updateSelection();
    document.querySelector('#session-note').textContent = '세션을 불러오지 못했습니다. 새로고침하세요.';
    showHistoryError(cause);
  }
}
initializeDashboard();

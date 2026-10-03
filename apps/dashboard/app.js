import { createHistoryDetail, filterHistory } from './src/deployment-history.js';
import { APP_NAME, APP_NAME_MESSAGE, sourceAppName } from '../../contracts/application.mjs';
import { request, requests } from './src/api.js';
import { initializeOpenStackInstaller } from './src/openstack-installer.js';
import { applicationLabel, createLifecycleController } from './src/lifecycle.js';

const views = {
  deploy: document.querySelector('#deploy-view'),
  history: document.querySelector('#history-view'),
  monitor: document.querySelector('#monitor-view'),
};

// Navigation and source selection.
function showView(name) {
  if (!Object.hasOwn(views, name)) name = 'deploy';
  if (name !== 'history') historyDetail.close();
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
const applicationName = document.querySelector('#application-name');
const selection = document.querySelector('#source-selection');
const sourceBox = document.querySelector('#source-box');
const error = document.querySelector('#form-error');
const provider = document.querySelector('#provider');
const providerField = document.querySelector('#provider-field');
const cloudProvider = document.querySelector('#cloud-provider');
const openstackInstallField = document.querySelector('#openstack-install-field');
const deploymentDatabase = document.querySelector('#deployment-database');
const deployButton = document.querySelector('#deploy-button');
const requestError = document.querySelector('#request-error');
let selectedSource = null;
let updateApplication = null, updatePreviewRequest = null, applicationDetail = null;
let updateStage = 'source', updateRunId = null, previewExpiryTimer;
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
let history = [];
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
let ciSnapshot = null, ciReadError = false;
let logSnapshot = null, logController;
let eventSnapshot = null, eventController;
let diagnosticSnapshot = null, diagnosticController;

function invalidateReview() {
  clearTimeout(previewExpiryTimer);
  reviewGeneration += 1;
  reviewed = null;
  document.querySelector('#review-panel').hidden = true;
  deployButton.disabled = true;
  document.querySelector('#update-review').hidden = true;
  deployButton.textContent = updateApplication ? '업데이트 시작' : '배포 시작';
  error.hidden = true;
  document.querySelector('#renew-update').hidden = true;
  if (updateApplication) setUpdateStage('source');
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
applicationName.addEventListener('input', invalidateReview);
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
  openstackInstallField.hidden = selected.environment !== 'onprem' || selected.provider !== 'openstack';
  const existingOpenStack = selectedOption()?.available === true;
  // Keep registration separate from deploying to the existing operator-bound runtime.
  // Repeated observations must not undo a visitor's disclosure choice.
  if (!openstackInstallField.hidden && openstackInstallField.dataset.available !== String(existingOpenStack)) {
    openstackInstallField.open = !existingOpenStack;
    openstackInstallField.dataset.available = String(existingOpenStack);
    document.querySelector('#openstack-install-heading').textContent = existingOpenStack
      ? '다른 OpenStack 환경 등록' : 'OpenStack 환경 등록';
  }
  document.querySelector('#cloud-provider-field').hidden = selected.environment !== 'cloud';
  const profile = selectedProfile();
  document.querySelector('#deployment-database-field').hidden = !profile?.database;
  databaseChoice(deploymentDatabase, profile);
  document.querySelector('#deployment-database-note').textContent = databaseSummary(profile, deploymentDatabase.value);
  document.querySelector('#connection-status').textContent = connectionError || (selectedProfiles().length > 1
    ? '사용할 배포 사양을 운영자가 하나로 지정해야 합니다.'
    : profile ? (profile.supported ? '새 실행 환경과 앱을 함께 준비합니다. 선택 내용을 확인하면 비용과 실행 계획을 표시합니다.' : '환경 생성 사양을 아직 실행할 수 없습니다.')
    : selectedOption()?.message || (selected.environment === 'onprem' && !selected.provider ? '온프레미스 인프라 종류를 선택하세요.' : '앱 배포 설정을 확인하고 있습니다.'));
  renderRuntimeConnection();
  invalidateReview();
  savePreferences(selected);
}
document.querySelectorAll('[name="environment"]').forEach((input) => input.addEventListener('change', updateSelection));
provider.addEventListener('change', updateSelection);
cloudProvider.addEventListener('change', updateSelection);
deploymentDatabase.addEventListener('change', updateSelection);

initializeOpenStackInstaller();

// Deployment input, review, and submission.
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
function renderUpdateContext() {
  if (!updateApplication) return;
  const application = applications.find((row) => row.id === updateApplication.id) || updateApplication;
  const url = applicationSiteUrl(application);
  document.querySelector('#update-identity').textContent = application.app;
  document.querySelector('#update-environment').textContent = `${({ aws: 'AWS', gcp: 'Google Cloud', openstack: 'OpenStack', proxmox: 'Proxmox' })[application.provider] || '배포 환경'} / ${application.environment_target_id}`;
  document.querySelector('#update-service-state').textContent = url ? '현재 서비스 · 마지막 접속 확인 ' + formatTime(application.current_deployment.public_http.verified_at)
    : application.current_deployment_state === 'unverified' ? '현재 서비스 상태 확인 필요' : '마지막 성공 배포 기준으로 업데이트합니다.';
  safeLink('#update-site', url, Boolean(url));
  const latest = application.latest_deployment;
  const note = document.querySelector('#update-latest-note');
  note.hidden = !latest || !['failed', 'blocked', 'unknown'].includes(latest.status);
  note.textContent = latest?.status === 'unknown' ? '최근 시도의 적용 결과를 확인해야 합니다.'
    : '최근 배포 시도가 완료되지 않았습니다. 위 서비스 정보는 마지막으로 검증된 배포의 기록입니다.';
}
function setUpdateStage(stage) {
  updateStage = stage;
  const active = Boolean(updateApplication);
  document.querySelector('#deploy-form').hidden = active && stage !== 'source';
  document.querySelector('#review-panel').hidden = active ? stage !== 'review' : !reviewed;
  document.querySelector('#run-panel').hidden = active ? stage !== 'run' || current?.id !== updateRunId : !current;
  const steps = ['source', 'review', 'run'];
  document.querySelectorAll('[data-update-step]').forEach((item) => {
    const selected = item.dataset.updateStep === stage;
    if (selected) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
    item.dataset.complete = String(steps.indexOf(item.dataset.updateStep) < steps.indexOf(stage));
  });
}
function expireUpdateReview() {
  if (!reviewed?.update || reviewed.attempted) return;
  const remaining = Date.parse(reviewed.preview.expires_at) - Date.now();
  clearTimeout(previewExpiryTimer);
  if (remaining > 0) { previewExpiryTimer = setTimeout(expireUpdateReview, Math.min(remaining, 2147483647)); return; }
  deployButton.disabled = true;
  document.querySelector('#update-ready-badge').textContent = '검토 만료';
  document.querySelector('#update-ready-badge').dataset.tone = 'attention';
  document.querySelector('#update-rebuild').disabled = true;
  document.querySelector('#review-note').textContent = '검토가 만료됐습니다. 최신 상태로 변경 내용을 다시 확인하세요.';
  document.querySelector('#review-note').dataset.tone = 'attention';
  document.querySelector('#renew-update').hidden = false;
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) expireUpdateReview(); });
document.querySelector('#renew-update').addEventListener('click', () => {
  if (submitting) return;
  updatePreviewRequest = null; invalidateReview();
  if (selectedSource) reviewUpdate(); else document.querySelector('#repository-url').focus();
});
document.querySelector('#update-again').addEventListener('click', () => beginUpdate(updateApplication));
function setUpdateMode(application, preserveSource = false) {
  updateApplication = application; updatePreviewRequest = null; updateRunId = null;
  invalidateReview();
  document.querySelector('#deploy-view').classList.toggle('update-mode', Boolean(application));
  for (const button of document.querySelectorAll('#deploy-view .primary-button, #deploy-view .secondary-button, #deploy-view .text-button, #deploy-view [data-update-button]')) {
    const original = button.dataset.updateButton || ['primary-button', 'secondary-button', 'text-button'].find(name => button.classList.contains(name));
    if (application) {
      button.dataset.updateButton = original; button.classList.remove(original); button.classList.add('btn');
      button.dataset.variant = original === 'primary-button' ? 'primary' : original === 'secondary-button' ? 'outline' : 'ghost';
    } else {
      button.classList.remove('btn'); button.classList.add(original); delete button.dataset.variant; delete button.dataset.updateButton;
    }
  }
  document.querySelector('.update-delivery-summary').hidden = !application;
  applicationName.value = '';
  document.querySelector('#application-name-field').hidden = Boolean(application);
  if (!preserveSource) { archive.value = ''; folder.value = ''; repositoryUrl.value = ''; setSource(null); }
  document.querySelector('#update-context').hidden = !application;
  document.querySelector('#target-section').hidden = Boolean(application);
  document.querySelector('#deploy-title').textContent = application ? '앱 업데이트하기' : '앱 배포하기';
  document.querySelector('#deploy-view .page-header .eyebrow').textContent = application ? 'APPLICATION UPDATE' : 'NEW DEPLOYMENT';
  document.querySelector('#review-title').textContent = application ? '변경 내용' : '선택 내용';
  document.querySelector('#source-title').textContent = application ? '새 버전의 소스' : '소스 선택';
  document.querySelector('#edit-selection').textContent = application ? '소스 수정' : '수정';
  document.querySelector('#deploy-form button[type="submit"]').textContent = application ? '변경 내용 확인' : '선택 내용 확인 →';
  document.querySelector('#run-details').open = !application;
  renderUpdateContext(); setUpdateStage('source');
  requestError.hidden = true;
}
document.querySelector('#cancel-update').addEventListener('click', () => {
  if (submitting) return;
  const id = updateApplication?.id;
  setUpdateMode(null); showView('history'); if (id) loadApplication(id);
});
function beginUpdate(application) {
  if (!resourceId(application?.id)) return false;
  application = { ...application, ...applications.find((row) => row.id === application.id) };
  if (updateBlocked(application)) return false;
  setUpdateMode(application); showView('deploy');
  document.querySelector('#update-context').scrollIntoView({ block: 'start' });
  document.querySelector('#repository-url').focus({ preventScroll: true });
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
  let sourceName = sourceLabel || '보관된 소스';
  try { const url = new URL(sourceName); if (url.hostname === 'github.com') sourceName = url.pathname.replace(/^\//, '').replace(/\/$/, ''); } catch { /* Upload labels are plain text. */ }
  document.querySelector('#review-source').textContent = sourceName;
  document.querySelector('#review-app').textContent = application.app;
  document.querySelector('#review-target').textContent = environmentLabel(application);
  document.querySelector('#review-note').textContent = '확인한 소스로 기존 앱을 업데이트합니다.';
  document.querySelector('#review-note').dataset.tone = '';
  document.querySelector('#update-baseline').textContent = `비교 기준 배포: ${data.base_deployment_id} · ${data.baseline_kind === 'deployed'
    ? '검증된 최종 소스와 비교합니다.' : '제출 원본과 비교합니다. 이 배포의 AI 수정 후 최종 소스는 보관되어 있지 않습니다. 실제 배포 소스와 동일한지는 확인할 수 없어 검사·빌드를 생략하지 않습니다.'}`;
  document.querySelector('#update-origin').textContent = data.source_origin?.repository
    ? `가져온 저장소: ${data.source_origin.repository} · 고정 commit: ${data.source_origin.sha || '미제공'}` : '업로드한 소스를 서버에 보관했습니다.';
  document.querySelector('#update-expiry').textContent = `검토 유효 시각: ${formatTime(data.expires_at)}`;
  document.querySelector('#update-target-summary').textContent = environmentLabel(application);
  const changes = data.changes;
  const total = changes.added.length + changes.modified.length + changes.deleted.length;
  document.querySelector('#review-title').textContent = data.no_changes ? '변경된 파일이 없습니다' : total ? `${total}개 파일이 변경됩니다` : '변경 내용 확인';
  const summary = document.querySelector('#update-diff-summary'); summary.replaceChildren();
  for (const [key, label] of [['added', '추가'], ['modified', '수정'], ['deleted', '삭제']]) if (changes[key].length) {
    const badge = element('span', `${label} ${changes[key].length}`, 'badge'); badge.dataset.variant = 'secondary'; badge.dataset.change = key; summary.append(badge);
  }
  summary.append(element('span', `${data.no_changes ? '변경된 파일이 없습니다. ' : ''}동일 ${changes.unchanged}개`, 'diff-unchanged'));
  const impact = document.querySelector('#update-impact');
  impact.dataset.tone = changes.deleted.length ? 'attention' : 'neutral';
  impact.textContent = data.no_changes ? '현재 배포된 소스와 같습니다. 빌드와 배포를 생략하고 완료할 수 있습니다.'
    : changes.deleted.length ? `삭제 목록의 파일 ${changes.deleted.length}개는 새 버전에서 제외됩니다.`
    : data.baseline_kind === 'submitted' ? '이전 제출 원본과 비교한 결과입니다. 실제 배포된 최종 소스와 동일한지 확인할 수 없어 검사와 빌드를 진행합니다.'
    : '변경된 파일을 확인하고 업데이트를 시작하세요.';
  document.querySelector('#update-changes').replaceChildren(...[['added', '추가'], ['modified', '수정'], ['deleted', '삭제']].filter(([key]) => changes[key].length).map(([key, label]) => {
    const group = document.createElement('details'); group.dataset.change = key;
    const files = document.createElement('ul'); files.append(...changes[key].map((path) => element('li', path)));
    group.append(element('summary', `${label} ${changes[key].length}개`), files); return group;
  }));
  document.querySelector('#update-review').hidden = false;
  document.querySelector('#update-source-details').open = false;
  document.querySelector('#update-ready-badge').textContent = data.no_changes ? '변경 없음' : '검토 준비됨';
  document.querySelector('#update-ready-badge').dataset.tone = '';
  document.querySelector('#rebuild-field').hidden = !data.no_changes;
  const rebuild = document.querySelector('#update-rebuild'); rebuild.checked = false; rebuild.disabled = false;
  deployButton.textContent = data.no_changes ? '변경 없음으로 완료' : '업데이트 시작';
  deployButton.disabled = Date.parse(data.expires_at) <= Date.now();
  requestError.hidden = true; error.hidden = true;
  setUpdateStage('review'); expireUpdateReview();
  document.querySelector('#review-title').focus({ preventScroll: true });
  document.querySelector('#update-context').scrollIntoView({ block: 'start' });
}
document.querySelector('#update-rebuild').addEventListener('change', (event) => {
  if (reviewed?.update && !reviewed.attempted) deployButton.textContent = event.target.checked ? '다시 빌드·배포' : '변경 없음으로 완료';
});
async function reviewUpdate() {
  if (applicationBusy(updateApplication?.id)) error.textContent = '진행 중인 앱 관리 작업을 먼저 확인하세요.';
  else if (!selectedSource) error.textContent = '업데이트할 소스를 선택하세요.';
  else if (selectedSource.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(selectedSource.label)) error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  else if (selectedSource.kind === 'archive' && !selectedSource.files[0]?.name.toLowerCase().endsWith('.zip')) error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  else {
    invalidateReview();
    const generation = reviewGeneration, application = updateApplication;
    const draft = updatePreviewRequest ||= { key: crypto.randomUUID(), source: selectedSource };
    const button = document.querySelector('#deploy-form button[type="submit"]');
    reviewing = true; button.disabled = true; button.textContent = '변경 내용 확인 중…';
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
        error.textContent = `${cause.name === 'AbortError' ? '변경 검토 요청 시간이 초과되었습니다.' : cause.message} 변경 내용 확인을 눌러 다시 시도하세요.`;
        error.hidden = false;
      }
    } finally { reviewing = false; button.disabled = false; button.textContent = updateApplication ? '변경 내용 확인' : '선택 내용 확인 →'; }
    return;
  }
  error.hidden = false;
}
async function startUpdate(draft) {
  if (applicationBusy(draft.application.id, true)) throw new Error('진행 중인 앱 관리 작업을 먼저 확인하세요.');
  if (!draft.attempted && Date.parse(draft.preview.expires_at) <= Date.now()) {
    expireUpdateReview(); throw new Error('변경 검토가 만료됐습니다. 변경 내용을 다시 확인하세요.');
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
    if (['UPDATE_EXPIRED', 'UPDATE_BASE_CHANGED'].includes(cause.code)) {
      reviewed = null; updatePreviewRequest = null;
      document.querySelector('#renew-update').hidden = false;
    }
    throw cause;
  }
  const { data, location } = response;
  const id = data.resource_id || data.id;
  if (id !== draft.preview.id || location !== `/api/v1/deployments/${encodeURIComponent(id)}`
      || !['accepted', 'queued', 'running', ...terminal].includes(data.status) || data.status === 'preview') throw new Error('업데이트 실행 응답을 확인하지 못했습니다.');
  current = { ...draft.preview, ...data, id, kind: 'deployments', status: data.status === 'accepted' ? 'queued' : data.status };
  updateRunId = id; clearTimeout(previewExpiryTimer); setUpdateStage('run');
  ciSnapshot = null; ciReadError = false;
  lastReadAt = null; observationError = false; remember(); renderRun();
  reviewed = null; updatePreviewRequest = null; deployButton.disabled = true;
  document.querySelector('#review-panel').hidden = true;
  historyKind = 'deployments'; document.querySelector('#history-kind').value = historyKind;
  loadHistory([null]); loadApplications(); refreshRun();
  document.querySelector('#run-title').focus({ preventScroll: true });
  document.querySelector('#update-context').scrollIntoView({ block: 'start' });
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
  else if (selected.environment === 'onprem' && !selected.provider) error.textContent = '온프레미스 인프라 종류를 선택하세요.';
  else if (connectionError || selectedProfiles().length > 1 || (profile ? !profile.supported : !option?.available)) error.textContent = connectionError || (profile || selectedProfiles().length > 1 ? document.querySelector('#connection-status').textContent : option?.message) || '실행 가능한 인프라가 아직 연결되지 않았습니다.';
  else {
    invalidateReview();
    const generation = reviewGeneration, source = selectedSource;
    let plan, app;
    reviewing = true;
    const reviewButton = document.querySelector('#deploy-form button[type="submit"]');
    reviewButton.disabled = true;
    try {
      app = applicationName.value.trim() || sourceApplication(source);
      if (!APP_NAME.test(app)) throw new Error(APP_NAME_MESSAGE);
      if (!profile) {
        const { data } = await request(`/api/v1/applications/resolve?${new URLSearchParams({ ...selected, app })}`);
        if (generation !== reviewGeneration) return;
        if (data.app !== app || !resourceId(data.environment_target_id) || !Object.hasOwn(data, 'application'))
          throw new Error('기존 앱 조회 결과가 선택 내용과 일치하지 않습니다.');
        const existing = data.application;
        if (existing) {
          if (!resourceId(existing.id) || !resourceId(existing.target_id) || existing.app !== app
              || existing.environment_target_id !== data.environment_target_id || existing.provider !== selected.provider)
            throw new Error('기존 앱의 이름과 배포 환경을 확인하지 못했습니다.');
          if (existing.current_deployment) {
            const blocked = updateBlocked(existing);
            if (blocked) throw new Error(blocked);
            setUpdateMode(existing, true);
            await reviewUpdate();
            return;
          }
          if (existing.status !== 'ready') throw new Error('이 이름의 앱은 준비 중이거나 중지·삭제된 상태입니다. 배포 내역에서 현재 작업을 먼저 확인하세요.');
        }
      }
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
    reviewed = { ...selected, source, app, kind: 'deployments', key: crypto.randomUUID(), plan,
      targetId: plan?.runtime_target_id || profile?.target_id };
    document.querySelector('#review-source').textContent = source.label;
    document.querySelector('#review-app').textContent = app;
    document.querySelector('#review-target').textContent = profile ? `클라우드 · ${profile.label || profile.id} · ${databaseSummary(profile, plan.database.mode)}` : option.label;
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
document.querySelector('#edit-selection').addEventListener('click', () => {
  if (submitting) return;
  invalidateReview(); requestError.hidden = true;
  if (updateApplication) document.querySelector('#repository-url').focus();
});

// Current execution and live observations.
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
const stageStatus = { queued: '대기', pending: '대기', waiting: '대기', in_progress: '진행 중', running: '진행 중',
  completed: '결과 확인 중', success: '성공', succeeded: '성공', failure: '실패', failed: '실패',
  cancelled: '취소', timed_out: '시간 초과', skipped: '건너뜀', blocked: '차단', unknown: '결과 확인 필요' };
function stageResult(value) { return stageStatus[value] || (value ? '결과 확인 필요' : '대기'); }
function stageTone(value) {
  if (['success', 'succeeded', 'published'].includes(value)) return 'success';
  if (['failure', 'failed', 'cancelled', 'timed_out', 'blocked', 'publication_unverified'].includes(value)) return 'failed';
  if (['in_progress', 'running'].includes(value)) return 'running';
  return 'pending';
}
function renderMonitorSteps() {
  const runId = current.ci?.run_id || (current.kind === 'builds' ? current.id : null);
  const direct = current.kind === 'builds' ? current
    : ciSnapshot?.deployment_id === current.id && String(ciSnapshot.run_id) === String(runId) ? ciSnapshot : null;
  const steps = direct?.steps || current.ci?.steps || [];
  const loop = steps.find((step) => step.key === 'loop');
  const release = steps.find((step) => step.key === 'release');
  const ciState = direct?.status || current.ci?.state;
  const checkedAt = current.kind === 'builds' ? lastReadAt : direct?.read_at;
  document.querySelector('#monitor-ci-status').textContent = runId
    ? `GitHub Actions #${runId} · ${ciReadError || observationError ? '조회 실패, 마지막 기록 표시' : checkedAt ? `확인 ${new Date(checkedAt).toLocaleTimeString('ko-KR')}` : '서버 기록 표시'}`
    : 'GitHub Actions 실행 대기';
  const releaseResult = ciState === 'published' ? '게시 확인' : ciState === 'publication_unverified' ? '게시 검증 실패'
    : release?.conclusion === 'success' ? 'Actions 성공 · 게시 확인 대기' : stageResult(release?.conclusion || release?.status);
  const entries = [
    ['소스 접수', '접수 완료', 'success'],
    ['앱 검사 및 수정', stageResult(loop?.conclusion || loop?.status), stageTone(loop?.conclusion || loop?.status), loop],
    ['검증 이미지 게시', releaseResult, stageTone(ciState === 'published' ? 'published' : ciState === 'publication_unverified' ? ciState : release?.conclusion || release?.status), release],
    ['GitOps 반영', current.cd?.deployed === true ? '적용 확인' : stageResult(current.cd?.state), stageTone(current.cd?.deployed === true ? 'succeeded' : current.cd?.state)],
    ['URL 및 앱 상태 확인', current.public_http?.state === 'succeeded' && current.public_http.verified_at ? '외부 접속 확인' : stageResult(current.public_http?.state),
      stageTone(current.public_http?.state === 'succeeded' && current.public_http.verified_at ? 'succeeded' : current.public_http?.state)],
  ];
  document.querySelector('#monitor-steps').replaceChildren(...entries.map(([name, result, tone, job]) => {
    const item = document.createElement('li');
    item.dataset.state = tone;
    item.append(element('span', name, 'stage-name'), element('strong', result, 'stage-result'));
    const tasks = job?.tasks || [];
    const active = tasks.find((task) => ['failure', 'cancelled', 'timed_out'].includes(task.conclusion))
      || tasks.find((task) => task.status === 'in_progress') || tasks.filter((task) => task.status === 'completed').at(-1);
    if (active) item.append(element('small', `Actions 단계 ${active.number}: ${active.name} · ${stageResult(active.conclusion || active.status)}`, 'stage-detail'));
    return item;
  }));
}
function renderRun() {
  renderApplicationActions();
  document.querySelector('#run-panel').hidden = Boolean(updateApplication && (updateStage !== 'run' || current.id !== updateRunId));
  const resume = document.querySelector('#resume-run');
  resume.hidden = !(current.kind === 'deployments' && current.application_id && ['unknown', 'blocked'].includes(current.status)
    && ['cd', 'http'].includes(current.stage) && current.ci?.state === 'published');
  resume.disabled = Boolean(resuming) || observationError || applicationBusy(current.application_id);
  const label = executionLabel(current);
  document.querySelector('#run-freshness').textContent = lastReadAt ? `마지막 상태 조회: ${new Date(lastReadAt).toLocaleString()}${observationError ? ' · 조회 실패, 마지막 기록입니다.' : ''}` : '서버 상태 조회 전';
  const ciObservation = current.ci?.observation;
  if (ciObservation) {
    const checked = ciObservation.last_success_at ? new Date(ciObservation.last_success_at).toLocaleString() : '아직 확인 전';
    document.querySelector('#run-freshness').textContent = `CI 원본 마지막 확인: ${checked}${ciObservation.error ? ' · 원본 조회 지연' : ''}${observationError ? ' · 대시보드 API 조회 실패' : ''}`;
  }
  const cdObservation = current.cd?.observation;
  if (cdObservation) document.querySelector('#run-freshness').textContent += ` · 클러스터 원본 마지막 확인: ${cdObservation.last_success_at ? new Date(cdObservation.last_success_at).toLocaleString() : '아직 확인 전'}${cdObservation.error ? ' · 원본 조회 지연' : ''}`;
  document.querySelector('#run-meta').textContent = `${current.app || '앱'} · ${current.id} · ${current.target_id || ''}`;
  document.querySelector('#run-state').textContent = label;
  document.querySelector('#run-message').textContent = current.error?.message || current.message || (activeRun()
    ? '15초마다 상태를 확인합니다. 페이지를 닫아도 서버의 실행은 계속됩니다.'
    : current.status === 'published' ? '검증된 이미지가 게시됐습니다. 앱 배포 완료와는 별개입니다.'
    : current.status === 'unknown' ? '결과를 확인하기 전에는 같은 작업을 새로 실행하지 않습니다.' : '서버가 확인한 최종 실행 결과입니다.');
  if (current.status === 'queued' && current.queue?.enqueued_at)
    document.querySelector('#run-message').textContent = '소스를 저장하고 대기열에 접수했습니다. 앞선 작업이 끝나면 자동으로 실행됩니다.';
  if (current.status === 'unknown' && current.queue?.released_at)
    document.querySelector('#run-message').textContent = `${current.error?.message || '기존 실행 결과는 확인이 필요합니다.'} 대기 제한 시간이 지나 다른 앱의 실행을 허용했습니다. 이 작업을 자동으로 재실행하지 않습니다.`;
  if (current.status === 'running' && ciObservation?.error) {
    document.querySelector('#run-state').textContent = current.ci.run_id ? '마지막 CI 상태 유지 · 재조회 중' : 'CI 접수 확인 중';
    document.querySelector('#run-message').textContent = `${ciObservation.error.message}${ciObservation.next_retry_at ? ` 다음 조회: ${new Date(ciObservation.next_retry_at).toLocaleTimeString()}.` : ''}${current.ci.run_id ? ` 기존 실행 #${current.ci.run_id}을 조회하며 새 실행을 만들지 않습니다.` : ''}`;
  }
  if (current.status === 'running' && cdObservation?.error) {
    document.querySelector('#run-state').textContent = '마지막 배포 상태 유지 · 클러스터 재조회 중';
    document.querySelector('#run-message').textContent = `${cdObservation.error.message} 기존 배포 revision을 조회하며 CI나 앱 적용을 다시 실행하지 않습니다.`;
  }
  const isUpdateRun = Boolean(updateApplication && current.id === updateRunId);
  document.querySelector('#run-title').textContent = isUpdateRun ? '이번 업데이트' : '실행 상태';
  document.querySelector('#run-state').dataset.tone = ['succeeded', 'unchanged'].includes(current.status) ? 'success'
    : ['failed', 'blocked'].includes(current.status) ? 'danger' : current.status === 'unknown' ? 'attention' : 'neutral';
  document.querySelector('#update-again').hidden = !isUpdateRun || !['succeeded', 'unchanged', 'failed', 'blocked'].includes(current.status);
  document.querySelector('#update-run-progress').hidden = !isUpdateRun || current.status === 'unchanged';
  if (isUpdateRun) {
    if (current.status === 'unchanged') document.querySelector('#run-message').textContent = '변경된 소스가 없어 빌드와 배포를 생략했습니다. 현재 서비스는 그대로 유지됩니다.';
    if (current.status === 'unknown') document.querySelector('#run-message').textContent = '이번 업데이트의 적용 결과를 아직 확인하지 못했습니다. 새 업데이트를 만들기 전에 상태를 다시 조회하세요.';
    document.querySelector('#update-run-progress').replaceChildren(...[
      ['이미지 준비', current.ci?.state === 'published' ? '완료' : current.stage === 'ci' ? '진행 중' : '대기'],
      ['앱 적용', current.cd?.deployed ? '완료' : current.stage === 'cd' ? '진행 중' : '대기'],
      ['서비스 주소 확인', current.public_http?.state === 'succeeded' && current.public_http?.verified_at ? '완료' : current.stage === 'http' ? '진행 중' : '대기'],
    ].map(([name, state]) => {
      const item = element('li', '');
      if (state === '진행 중' && terminal.has(current.status)) state = current.status === 'unknown' ? '확인 필요' : '중단됨';
      item.dataset.state = state; item.append(element('span', name), element('strong', state)); return item;
    }));
  }
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
  renderRunSite();
  const binding = [`앱 / 대상: ${current.app || '—'} / ${current.target_id || '—'}`, `배포: ${current.id}`,
    `현재 단계: ${current.stage || 'ci'}`, `CI run: ${current.ci?.run_id || (current.kind === 'builds' ? current.id : '대기')}`,
    `소스 commit: ${current.source_commit || '대기'}`, `입력 SHA-256: ${current.source_digest || '미제공'}`,
    `이미지: ${Object.values(current.ci?.images || current.publication?.images || {}).join(', ') || '게시 대기'}`,
    `배포 revision: ${current.cd?.revision || '대기'}`].join('\n');
  document.querySelector('#run-binding').textContent = binding;
  renderMonitorSteps();
  renderHistory();
  document.querySelector('#monitor-state').textContent = `${document.querySelector('#run-state').textContent}${observationError ? ' · 상태 조회 실패' : ''}`;
  renderMetrics();
  renderConsole();
}

deployButton.addEventListener('click', async () => {
  if (submitting || !reviewed || deployButton.disabled) return;
  submitting = true;
  deployButton.disabled = true;
  requestError.hidden = true;
  const draft = reviewed;
  const controls = document.querySelectorAll('#deploy-form input, #deploy-form button, #deploy-form select, #edit-selection, #cancel-update, #renew-update');
  controls.forEach((control) => { control.disabled = true; });
  try {
    if (draft.plan && !draft.attempted && Date.parse(draft.plan.expires_at) <= Date.now()) {
      reviewed = null; throw new Error('환경 계획이 만료됐습니다. 선택 내용을 다시 확인하세요.');
    }
    if (draft.update) { await startUpdate(draft); return; }
    const payload = new FormData();
    if (draft.plan) {
      payload.set('app', draft.plan.name); payload.set('target_id', draft.targetId); payload.set('plan_id', draft.plan.id);
    } else { payload.set('environment', draft.environment); payload.set('provider', draft.provider); }
    if (!draft.plan) payload.set('source_name', draft.app);
    appendSource(payload, draft.source);
    draft.attempted = true;
    const { data, location } = await request(`/api/v1/${draft.kind}`, {
      method: 'POST', headers: { 'Idempotency-Key': draft.key }, body: payload,
    });
    const id = data.resource_id || data.id;
    if (typeof id !== 'string' || !/^[a-zA-Z0-9._-]{1,128}$/.test(id) || location !== `/api/v1/${draft.kind}/${encodeURIComponent(id)}`) throw new Error('실행 조회 주소를 확인하지 못했습니다.');
    current = { ...data, id, kind: draft.kind, status: data.status === 'accepted' ? 'queued' : data.status };
    ciSnapshot = null; ciReadError = false;
    lastReadAt = null; observationError = false; remember(); renderRun();
    reviewed = null;
    historyKind = draft.kind; document.querySelector('#history-kind').value = historyKind;
    loadHistory([null]);
    document.querySelector('#run-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    refreshRun();
  } catch (cause) {
    const uncertain = draft.attempted && (cause.outcomeUnknown === true || !cause.status);
    if (draft.update) {
      draft.uncertain = uncertain;
      if (!uncertain) { draft.attempted = false; delete draft.startRebuild; }
      deployButton.textContent = uncertain ? '실행 상태 다시 확인' : '업데이트 다시 시도';
    }
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
    if (draft.update && draft.uncertain) document.querySelector('#edit-selection').disabled = true;
    if (draft.update) expireUpdateReview();
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
    current = { ...current, ...data }; lastReadAt = Date.now(); observationError = false; remember();
    if (current.kind === 'deployments' && current.status === 'succeeded' && !applicationsController) await loadApplications();
    if (pollController !== controller) return;
    renderRun();
    if (current.kind === 'deployments' && current.ci?.run_id && !views.monitor.hidden) {
      const id = current.id, runId = String(current.ci.run_id);
      try {
        const { data: ci } = await request(`/api/v1/builds/${encodeURIComponent(runId)}`, {}, controller);
        if (pollController !== controller || current.id !== id) return;
        if (ci.id !== runId || ci.app !== current.app || ci.target_id !== current.target_id
            || (ci.source_commit ?? null) !== (current.source_commit ?? null) || !Array.isArray(ci.steps)) throw new Error('CI 실행 대상 불일치');
        ciSnapshot = { ...ci, deployment_id: id, run_id: runId, read_at: Date.now() };
        ciReadError = false;
        for (const selector of ['#actions-link', '#monitor-actions-link']) safeLink(selector, ci.actions_url, true, true);
      } catch {
        if (pollController !== controller || current.id !== id) return;
        ciReadError = true;
      }
      renderMonitorSteps();
    }
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
  refreshDiagnostics();
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
  const messages = { loading: 'CI 진행 이벤트를 조회하고 있습니다.', live: 'CI 단계 관측 중',
    complete: 'CI 관측이 종료되었습니다. 앱 적용과 외부 응답은 별도 기록에서 확인하세요.',
    not_started: '아직 CI 진행 이벤트가 기록되지 않았습니다.', no_data: '이 실행에 기록된 CI 진행 이벤트가 없습니다.',
    unavailable: 'CI 진행 이벤트를 확인할 수 없습니다. 마지막 기록과 조회 시각을 확인하세요.' };
  return { message: events?.read_error ? '이벤트 조회 실패 · 마지막으로 확인한 기록입니다.'
    : stale ? '이벤트 갱신이 지연되고 있습니다. 마지막 기록만으로 실행 정지를 판단할 수 없습니다.'
    : messages[events?.state] || '작업 로그 탭에서 현재 실행의 CI 진행 이벤트를 조회합니다.',
    ...(events || { items: [] }),
    ...(events?.truncated ? { history_note: '최근 이벤트만 표시합니다. 전체 기록은 CI 실행 로그에서 확인하세요.' } : {}) };
}
function renderConsole() {
  renderDiagnostics();
  const logs = logSnapshot?.deployment_id === current?.id ? logSnapshot : null;
  const data = !current ? '실행을 시작하면 확인된 상태가 여기에 표시됩니다.'
    : consoleTab === 'app' ? (logs?.state === 'ready'
      ? [`조회 ${new Date(logs.checked_at).toLocaleString()}`, ...logs.entries.map((entry) => `[${entry.pod} / ${entry.container}]\n${entry.text}`)].join('\n\n')
      : logLabels[logs?.state] || '앱 로그 탭을 선택하면 현재 배포의 로그를 조회합니다.')
    : consoleTab === 'environment' ? { target_id: current.target_id, environment: current.environment || null, cd: current.cd || null, public_http: current.public_http || null, observation: current.observation || null }
    : { status: current.status, stage: current.stage || 'ci', diagnostics: current.ci?.diagnostics || current.diagnostics || null,
      steps: current.steps || current.ci?.steps || [], error: current.error || null,
      pipeline_timeline: eventsMatch(eventSnapshot) ? eventSnapshot.timeline : current.telemetry || null,
      ci_events: agentEvents(), classification: current.classification || null };
  document.querySelector('#console-output').textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
}

async function refreshDiagnostics() {
  if (!current || current.kind !== 'deployments' || !['failed', 'blocked', 'unknown'].includes(current.status)) { renderDiagnostics(); return; }
  if (diagnosticController) return;
  const id = current.id, source = current.source_commit, run = current.ci?.run_id;
  const controller = new AbortController(); diagnosticController = controller;
  try {
    const { data } = await request(`/api/v1/deployments/${encodeURIComponent(id)}/diagnostics`, {}, controller);
    if (current?.id !== id || current.source_commit !== source || current.ci?.run_id !== run) return;
    if (data.deployment_id !== id || data.binding && (data.binding.app !== current.app || data.binding.target_id !== current.target_id
      || data.binding.source_commit !== source || String(data.binding.run_id) !== String(run))) throw new Error('진단 대상 불일치');
    diagnosticSnapshot = { ...data, source_commit: source, run_id: run, read_at: Date.now() };
  } catch {
    if (current?.id === id) diagnosticSnapshot = { deployment_id: id, source_commit: source, run_id: run, state: 'unavailable', read_error: true };
  } finally { diagnosticController = null; renderDiagnostics(); }
}
function renderDiagnostics() {
  const panel = document.querySelector('#diagnostic-panel');
  panel.hidden = !current || current.kind !== 'deployments' || !['failed', 'blocked', 'unknown'].includes(current.status);
  if (panel.hidden) return;
  const d = diagnosticSnapshot?.deployment_id === current.id && diagnosticSnapshot.source_commit === current.source_commit
    && diagnosticSnapshot.run_id === current.ci?.run_id ? diagnosticSnapshot : null;
  document.querySelector('#diagnostic-state').textContent = d?.state === 'ready' ? '실행과 소스에 연결된 진단입니다.'
    : d?.read_error ? '진단 조회에 실패했습니다. 실행 결과와 별도로 다음 조회에서 다시 확인합니다.' : '검증 가능한 진단 자료를 확인하고 있습니다.';
  const f = d?.failure;
  document.querySelector('#diagnostic-failure').textContent = d?.state === 'ready'
    ? [f?.layer || d.error?.phase, f?.code || d.error?.code || '원인 미확정'].filter(Boolean).join(' · ') : current.error?.code || '';
  const list = document.querySelector('#diagnostic-checks'); list.replaceChildren();
  const outcomeLabels = { PASS: '통과', FAIL: '실패', BLOCKED: '실행 차단', UNKNOWN: '확인 필요', NOT_RUN: '실행하지 않음', INCOMPLETE: '미완료', RUNNING: '진행 중' };
  for (const check of d?.checks || []) {
    const li = document.createElement('li'); li.dataset.outcome = check.outcome;
    li.textContent = `${check.check_id} · ${outcomeLabels[check.outcome] || check.outcome}${check.required ? '' : ' · 선택 검사'}`; list.append(li);
  }
  document.querySelector('#diagnostic-excerpt').textContent = f?.excerpt || '';
  const c = d?.classification || current.classification;
  const categoryLabels = { source: '소스 코드', dependency: '의존성', packaging: '앱 패키징', configuration: '설정', platform: '실행 환경', resource: '자원', database: '데이터베이스', unknown: '근거 부족' };
  document.querySelector('#diagnostic-classification').textContent = c?.state === 'succeeded'
    ? `Jev 원인 추정: ${categoryLabels[c.category?.choice] || '근거 부족'}. 이 분류는 관측 사실을 보완하며 복구 완료를 뜻하지 않습니다.`
    : c?.state === 'running' ? 'Jev가 진단 근거를 분류하고 있습니다.'
    : c?.state === 'failed' ? `원인 분류를 완료하지 못했습니다 (${c.code}). 기존 검사 결과는 유지됩니다.`
    : c?.state === 'not_configured' ? '자동 분류를 사용할 수 없습니다. 관측된 오류와 검사 근거를 확인하세요.' : '원인 분류는 아직 요청되지 않았습니다.';
  document.querySelector('#diagnostic-action').textContent = c?.action || (d?.missing_evidence?.length ? `누락된 근거: ${d.missing_evidence.join(', ')}` : '');
  const classify = document.querySelector('#classify-failure');
  classify.hidden = d?.state !== 'ready' || ['running', 'succeeded', 'failed', 'not_configured'].includes(c?.state);
  const source = document.querySelector('#diagnostic-source');
  source.hidden = d?.state !== 'ready' || !d.source?.snapshot || d.source.tested_sha256 !== d.source.after_sha256;
  if (!source.hidden) source.href = `/api/v1/deployments/${encodeURIComponent(current.id)}/source?variant=failed`;
  document.querySelector('#diagnostic-freshness').textContent = d?.read_at
    ? `마지막 조회 ${new Date(d.read_at).toLocaleTimeString()}${d.checked_at ? ` · 근거 수집 ${new Date(d.checked_at).toLocaleString()}` : ''}` : '진단 조회 대기';
}
document.querySelector('#classify-failure').addEventListener('click', async () => {
  if (!current) return;
  const id = current.id, button = document.querySelector('#classify-failure'); button.disabled = true;
  try {
    await request(`/api/v1/deployments/${encodeURIComponent(id)}/classifications`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (current?.id === id) await refreshDiagnostics();
  } catch { if (current?.id === id) document.querySelector('#diagnostic-classification').textContent = '분류 요청 결과를 확인하지 못했습니다. 진단을 다시 조회하세요.'; }
  finally { button.disabled = false; }
});
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
// Application inventory and history.
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
function applicationSiteUrl(application) {
  const deployment = application?.current_deployment;
  if (application?.status !== 'ready' || application.current_deployment_state !== 'verified'
      || deployment?.status !== 'succeeded' || deployment.cd?.deployed !== true || !deployment.cd?.revision
      || deployment.public_http?.state !== 'succeeded' || !deployment.public_http.verified_at) return null;
  try {
    const url = new URL(deployment.public_http.site_url || deployment.url || deployment.public_http.url);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
function renderRunSite() {
  const application = applications.find((app) => app.id === current?.application_id);
  const url = !observationError && current?.kind === 'deployments' && current.status === 'succeeded'
    && application?.current_deployment?.id === current.id ? applicationSiteUrl(application) : null;
  for (const selector of ['#application-link', '#monitor-application-link']) safeLink(selector, url, Boolean(url));
}
function applicationSite(application) {
  const group = element('div', '', 'application-site'), deployment = application?.current_deployment;
  const url = applicationSiteUrl(application);
  if (url) {
    const link = element('a', '배포한 앱 열기 ↗', 'secondary-button');
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.setAttribute('aria-label', `${application.app} 배포한 앱 열기`);
    group.append(element('strong', '서비스 주소'), element('span', url, 'application-site-url'), link,
      element('small', `접속 확인: ${formatTime(deployment.public_http.verified_at)}`, 'field-note'));
  }
  return group;
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
    const title = element('div', '', 'application-title');
    title.append(element('strong', application.app), element('span', applicationLabel(application), 'state-badge'));
    content.append(title,
      element('small', `${environmentLabel(application)} · 앱 ID ${application.id}`, 'history-meta'),
      element('small', `${application.current_deployment_state === 'unverified' ? '마지막 검증 성공 (현재 상태 확인 필요)' : '현재 서비스'}: ${application.current_deployment ? application.current_deployment.id : '검증된 배포 없음'}`, 'history-meta'),
      element('small', `최근 시도: ${application.latest_deployment ? `${executionLabel(application.latest_deployment)} · ${application.latest_deployment.id}` : '없음'}`, 'history-meta'));
    const detail = element('button', '앱 상세·업데이트', 'secondary-button'); detail.type = 'button';
    detail.setAttribute('aria-label', `${application.app} 앱 상세·업데이트`);
    detail.addEventListener('click', () => loadApplication(application.id));
    header.append(content, detail); item.append(header, applicationSite(application), applicationButtons(application)); return item;
  }));
  document.querySelector('#applications-more').hidden = applicationPage >= applicationPageEnds.length - 1;
  document.querySelector('#applications-more').disabled = Boolean(applicationsController);
  renderApplicationActions(); renderHistory(); renderRunSite(); renderUpdateContext();
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
      rows.push(...data.items.filter((app) => app.status !== 'deleted')); pageEnds.push(rows.length); marker = data.next_marker;
      if (rows.length > 1000 || marker && markers.has(marker)) throw new Error('앱 목록 범위를 확인하지 못했습니다.');
      markers.add(marker);
    } while (marker);
    if (applicationsController !== controller) return false;
    if (new Set(rows.map((row) => row.id)).size !== rows.length) throw new Error('앱 ID가 중복된 응답입니다.');
    applications = rows; applicationPageEnds = pageEnds;
    message.textContent = applications.length ? `${rows.length}개 앱 · 최근 등록순 · 이 세션의 현재 서비스와 최근 배포 시도를 구분해 표시합니다.` : '아직 이 세션에 등록된 앱이 없습니다.';
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
  current = row; ciSnapshot = null; ciReadError = false; lastReadAt = null; observationError = false; consoleTab = tab;
  document.querySelectorAll('[data-console]').forEach((button) => button.setAttribute('aria-selected', String(button.dataset.console === tab)));
  showView('monitor'); renderRun(); refreshRun();
  document.querySelector('.execution-heading').scrollIntoView({ block: 'start' });
}
function renderHistory() {
  const expanded = new Set([...document.querySelectorAll('#history-list .dh-card:has(details[open])')].map((card) => card.dataset.executionId));
  const kindLabel = historyKind === 'builds' ? '빌드' : '배포';
  document.querySelector('#history-summary').textContent = historyError ? '실행 내역을 확인하지 못했습니다' : history.length
    ? `이 세션의 ${kindLabel}${Number.isInteger(historyTotal) ? ` · 전체 ${historyTotal}건` : ''}` : `아직 ${kindLabel} 내역이 없습니다`;
  document.querySelector('#history-page').textContent = `${historyMarkers.length}페이지 · ${history.length}건`;
  document.querySelector('#history-prev').disabled = Boolean(historyController) || historyMarkers.length < 2;
  document.querySelector('#history-next').disabled = Boolean(historyController) || !historyNext;
  const visible = filterHistory(history, { search: document.querySelector('#history-search').value,
    status: document.querySelector('#history-status').value, days: document.querySelector('#history-period').value });
  const empty = document.querySelector('#history-empty');
  empty.hidden = Boolean(visible.length) || Boolean(historyController) || Boolean(historyError);
  empty.textContent = history.length ? '선택한 조건에 해당하는 내역이 없습니다. 검색어나 필터를 변경해 주세요.' : '아직 실행 내역이 없습니다. 새 배포에서 첫 배포를 시작해 보세요.';
  document.querySelector('#history-list').replaceChildren(...visible.map((row) => {
    const item = element('li', '', 'dh-card'); item.dataset.executionId = `${row.kind}:${row.id}`;
    const main = element('button', '', 'dh-card-open'); main.type = 'button';
    main.setAttribute('aria-label', `${row.app || '앱'} 실행 상세·작업 로그`);
    const badge = element('span', executionLabel(row), 'dh-status');
    badge.classList.add(['failed'].includes(row.status) ? 'fail' : ['succeeded', 'published', 'unchanged'].includes(row.status) ? 'ok' : ['queued', 'running'].includes(row.status) ? 'run' : 'wait');
    const foot = element('span', '', 'dh-card-foot'); foot.append(badge, element('span', '›', 'dh-arrow'));
    main.append(element('strong', row.app || '앱 이름 미제공'), element('small', formatTime(row.created_at)), foot);
    main.addEventListener('click', () => row.status === 'preview' || row.kind === 'builds' ? openExecution(row) : historyDetail.open(row));
    item.append(main);
    if (row.kind === 'deployments') {
      const extra = element('details', '', 'dh-card-extra'); extra.open = expanded.has(item.dataset.executionId); extra.append(element('summary', '배포 관리'));
      const actions = element('div', '', 'history-actions');
      const logs = element('button', '앱 로그', 'text-button'); logs.type = 'button'; logs.addEventListener('click', () => openExecution(row, 'app')); actions.append(logs);
      const app = applications.find((entry) => entry.id === row.application_id);
      if (app) actions.append(applicationButtons(app));
      else actions.append(element('span', '앱 관리 ID를 확인할 수 없어 자동 삭제를 지원하지 않습니다.', 'field-note'));
      extra.append(actions, sourceDownloads(row)); item.append(extra);
    }
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
const historyDetail = createHistoryDetail({ host: document.querySelector('#deployment-history-detail'), request,
  getRecords: () => history, getApplications: () => applications, serviceUrl: applicationSiteUrl,
  onLogs: (record) => openExecution(record), onMonitor: (record) => openExecution(record), downloads: sourceDownloads,
  onVisibility: (visible) => { document.querySelector('#history-overview').hidden = visible;
    document.querySelector('#history-view > .page-header').hidden = visible;
    if (!visible && !views.history.hidden) document.querySelector('#history-title').focus(); },
});
for (const id of ['history-search', 'history-status', 'history-period']) document.querySelector(`#${id}`).addEventListener('input', renderHistory);
window.addEventListener('pagehide', () => historyDetail.dispose());
document.querySelector('#history-kind').addEventListener('change', (event) => { historyKind = event.target.value; loadHistory([null]); });
document.querySelector('#history-refresh').addEventListener('click', () => loadHistory([null]));
document.querySelector('#history-prev').addEventListener('click', () => loadHistory(historyMarkers.slice(0, -1)));
document.querySelector('#history-next').addEventListener('click', () => { if (historyNext) loadHistory([...historyMarkers, historyNext]); });

const metricNames = { runtime_healthz: '런타임 /healthz', node_up: '노드 지표 수집', cpu_percent: 'CPU', memory_percent: '메모리', disk_percent: '디스크',
  network_receive_bytes_per_second: '네트워크 수신', network_transmit_bytes_per_second: '네트워크 송신', pods: '실행 중 Pod', http: '앱 HTTP' };
const providerNames = { aws: 'AWS', gcp: 'GCP', openstack: 'OpenStack' };
// Environment status and target observations.
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
// Session-owned controls live in focused modules and read current view state through callbacks.
const lifecycle = createLifecycleController({
  getApplications: () => applications, getCurrent: () => current, getApplicationDetail: () => applicationDetail,
  clearApplicationDetail: () => { applicationDetail = null; document.querySelector('#application-detail').hidden = true; },
  isApplicationsLoading: () => Boolean(applicationsController), applicationSite,
  isSubmitting: () => submitting, isResuming: () => resuming, hasObservationError: () => observationError,
  isHistoryHidden: () => views.history.hidden, loadApplications, renderApplications, element, formatTime,
});
const { applicationBusy, updateBlocked, applicationButtons, renderApplicationActions,
  renderLifecycleOperation, refreshLifecycleOperation } = lifecycle;

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
    if (!Object.hasOwn(views, saved.view)) savePreferences({ view: 'deploy' });
    await Promise.allSettled([loadApplications(), loadHistory().catch(showHistoryError)]);
    renderLifecycleOperation();
    if (lifecycle.operation?.id || lifecycle.operation?.plan_id) refreshLifecycleOperation();
    if (history.length) { current = history[0]; ciSnapshot = null; ciReadError = false; renderRun(); refreshRun(); }
    if (!views.monitor.hidden || !views.deploy.hidden) loadEnvironments();
  } catch (cause) {
    connectionError = cause.message; updateSelection();
    document.querySelector('#session-note').textContent = '세션을 불러오지 못했습니다. 새로고침하세요.';
    showHistoryError(cause);
  }
}
initializeDashboard();

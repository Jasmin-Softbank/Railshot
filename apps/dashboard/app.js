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
const token = document.querySelector('#api-token');
const deployButton = document.querySelector('#deploy-button');
const requestError = document.querySelector('#request-error');
let selectedSource = null;
let reviewed = null;
let health = null;
let submitting = false;
// ponytail: retain only this page's last run; add server-backed history when runs must survive navigation.
let current = null;
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
  if (source?.kind !== 'registered') {
    const name = source?.kind === 'repository' ? source.label.split('/').filter(Boolean).at(-1)?.replace(/\.git$/, '')
      : source?.kind === 'archive' ? archive.files[0].name.replace(/\.zip$/i, '')
      : source ? (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0] : '';
    appName.value = (name || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9-]+/g, '-')
      .replace(/^-+|-+$/g, '').slice(0, 30).replace(/-+$/g, '');
  }
  invalidateReview();
}

document.querySelector('#choose-file').addEventListener('click', () => archive.click());
document.querySelector('#choose-folder').addEventListener('click', () => folder.click());
document.querySelector('#choose-registered').addEventListener('click', () => {
  archive.value = '';
  folder.value = '';
  repositoryUrl.value = '';
  setSource({ kind: 'registered', label: '등록된 앱' });
  appName.focus();
});
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

const providerField = document.querySelector('#provider-field');
const provider = document.querySelector('#provider');
function getDeploymentDraft() {
  const environment = document.querySelector('[name="environment"]:checked').value;
  return {
    source: selectedSource,
    app: appName.value.trim(),
    target: { environment, provider: environment === 'cloud' ? 'aws' : environment === 'onprem' ? provider.value : null },
  };
}

document.querySelectorAll('[name="environment"]').forEach((input) => {
  input.addEventListener('change', () => {
    providerField.hidden = input.value !== 'onprem';
    invalidateReview();
  });
});
provider.addEventListener('change', invalidateReview);
appName.addEventListener('input', invalidateReview);

document.querySelector('#deploy-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (submitting) return;
  const draft = getDeploymentDraft();
  if (!draft.source) {
    error.textContent = '배포할 소스를 선택하세요.';
  } else if (draft.source.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(draft.source.label)) {
    error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  } else if (draft.source.kind === 'archive' && !archive.files[0].name.toLowerCase().endsWith('.zip')) {
    error.textContent = 'ZIP 파일만 업로드할 수 있습니다.';
  } else if (!/^[a-z][a-z0-9-]{1,28}[a-z0-9]$/.test(draft.app)) {
    error.textContent = '앱 이름은 영문 소문자로 시작하고 소문자나 숫자로 끝나는 3~30자여야 합니다. 중간에 하이픈을 사용할 수 있습니다.';
  } else if (draft.target.environment === 'onprem' && !draft.target.provider) {
    error.textContent = '온프레미스 인프라 종류를 선택하세요.';
  } else {
    error.hidden = true;
    const registered = draft.target.environment === 'registered';
    const target = registered ? `운영자 등록 대상 · ${health?.target_id || '서버에 등록된 대상'}` : draft.target.environment === 'cloud'
      ? '클라우드 · RailShot AWS'
      : `온프레미스 · ${provider.selectedOptions[0].textContent}`;
    document.querySelector('#review-source').textContent = draft.source.label;
    document.querySelector('#review-app').textContent = draft.app;
    document.querySelector('#review-target').textContent = target;
    reviewed = { ...draft, targetId: health?.target_id || null };
    const allowed = registered && health?.configured && !activeRun();
    deployButton.disabled = !allowed;
    requestError.hidden = true;
    document.querySelector('#review-note').textContent = !registered
      ? '선택한 인프라를 등록 대상에 연결하는 기능은 아직 없습니다. 운영자 등록 대상을 선택하세요.'
      : !health?.configured ? 'API 또는 운영자 대상 설정을 확인한 뒤 페이지를 새로 여세요.'
      : activeRun() ? '진행 중인 CI 실행을 먼저 확인하세요.'
      : draft.source.kind === 'registered' ? '기존 앱 소스 변경 없이 CI 검사와 이미지 게시를 다시 시작합니다. 실제 앱 배포는 별도입니다.'
      : '소스를 등록하고 CI 검사 및 이미지 게시를 요청합니다. 대상 자원 생성과 앱 배포·외부 URL 확인은 포함하지 않습니다.';
    document.querySelector('#review-panel').hidden = false;
    document.querySelector('#review-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }
  error.hidden = false;
});
document.querySelector('#edit-selection').addEventListener('click', () => {
  if (submitting) return;
  invalidateReview();
  document.querySelector('#source-title').scrollIntoView({ behavior: 'smooth' });
});

function activeRun() {
  return current && !['published', 'publication_unverified', 'failed'].includes(current.state);
}

async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.method === 'POST' ? 120000 : 15000);
  try {
    const secret = path.startsWith('/api/') ? token.value.trim() : '';
    if (secret && !/^[\x21-\x7e]{32,4096}$/.test(secret)) throw new Error('API 접근 토큰은 공백 없는 ASCII 문자 32~4096자여야 합니다.');
    const response = await fetch(path, { ...options, signal: controller.signal, redirect: 'error',
      headers: { ...options.headers, ...(secret ? { Authorization: `Bearer ${secret}` } : {}) } });
    const result = await response.json();
    if (!response.ok) throw new Error(response.status === 401 ? 'API 접근 토큰을 확인하세요.'
      : result.error || `요청 실패 (HTTP ${response.status})`);
    return result;
  } finally {
    clearTimeout(timeout);
    requests.delete(controller);
  }
}

async function checkConnection() {
  try {
    health = await request('/healthz');
    document.querySelector('#registered-target').textContent = health.target_id || '서버에 등록된 대상';
    document.querySelector('#connection-status').textContent = health.configured
      ? 'API 연결됨 · 운영자 등록 대상 사용 가능' : 'GitHub 및 운영자 대상 설정 필요';
  } catch {
    health = null;
    document.querySelector('#registered-target').textContent = 'API 연결 확인 필요';
    document.querySelector('#connection-status').textContent = 'API 연결 실패 · API가 제공하는 주소로 접속하세요.';
  }
}

deployButton.addEventListener('click', async () => {
  if (submitting || !reviewed || deployButton.disabled || activeRun()) return;
  submitting = true;
  deployButton.disabled = true;
  deployButton.textContent = '요청 전송 중…';
  requestError.hidden = true;
  const controls = document.querySelectorAll('#deploy-form input, #deploy-form button, #deploy-form select, #edit-selection');
  controls.forEach((control) => { control.disabled = true; });
  try {
    await checkConnection();
    if (!health?.configured || (health.target_id || null) !== reviewed.targetId) {
      throw new Error('등록 대상 설정이 변경되었거나 연결되지 않았습니다. 선택 내용을 다시 확인하세요.');
    }
    const payload = new FormData();
    payload.set('app', reviewed.app);
    if (reviewed.targetId) payload.set('target_id', reviewed.targetId);
    if (reviewed.source.kind === 'registered') payload.set('source_type', 'registered');
    else if (reviewed.source.kind === 'repository') payload.set('repository_url', reviewed.source.label);
    else if (reviewed.source.kind === 'archive') payload.set('archive', archive.files[0]);
    else {
      const paths = [];
      for (const file of folder.files) {
        const parts = (file.webkitRelativePath || file.name).split('/');
        const path = parts.length > 1 ? parts.slice(1).join('/') : file.name;
        if (path.split('/').some((part) => ['.git', 'node_modules', '__MACOSX', '.DS_Store'].includes(part))) continue;
        paths.push(path);
        payload.append('files', file, file.name);
      }
      payload.set('paths', JSON.stringify(paths));
    }
    const result = await request('/api/deploy', {
      method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: payload,
    });
    if (!Number.isSafeInteger(result.run_id) || result.run_id < 1) throw new Error('CI 실행 ID를 확인하지 못했습니다.');
    current = { ...result, app: reviewed.app };
    renderRun();
    document.querySelector('#run-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    refreshRun();
  } catch (cause) {
    requestError.textContent = `${cause.name === 'AbortError' ? '요청 시간이 초과되었습니다.' : cause.message} 전송 오류가 발생한 경우 서버에서 이미 처리했을 수 있으므로 재요청 전에 GitHub Actions를 확인하세요.`;
    requestError.hidden = false;
  } finally {
    submitting = false;
    controls.forEach((control) => { control.disabled = false; });
    deployButton.textContent = '검사 및 이미지 게시';
    deployButton.disabled = true;
    // Every subsequent submission requires a fresh review, including a failed POST.
    reviewed = null;
  }
});

const runLabels = { queued: 'CI 대기 중', running: 'CI 진행 중', published: '이미지 게시 완료 · 앱 배포 미확인',
  publication_unverified: '게시 증거 확인 필요', failed: 'CI 실패 또는 취소' };

function renderRun() {
  document.querySelector('#run-panel').hidden = false;
  document.querySelector('#run-meta').textContent = `${current.app} · RUN #${current.run_id} · ${current.target_id || '서버에 등록된 대상'}`;
  const label = runLabels[current.state] || 'CI 상태 확인 필요';
  document.querySelector('#run-state').textContent = label;
  document.querySelector('#run-message').textContent = current.message || (activeRun()
    ? '15초마다 상태를 확인합니다. 조회를 중지해도 서버의 CI 실행은 계속됩니다.'
    : 'CI 실행이 종료되었습니다. 실제 앱 배포와 외부 URL은 별도로 확인해야 합니다.');
  document.querySelector('#run-steps').replaceChildren(...(current.steps || []).map((step) => {
    const item = document.createElement('li');
    item.textContent = `${({ loop: '앱 검사 및 수정', release: '검증 이미지 게시' })[step.key] || step.key}: ${step.conclusion || step.status}`;
    if (step.actions_steps?.length) {
      const details = document.createElement('ul');
      for (const action of step.actions_steps) {
        const row = document.createElement('li');
        row.textContent = `${action.name} · ${action.conclusion || action.status || '대기'}`;
        details.append(row);
      }
      item.append(details);
    }
    return item;
  }));
  const actions = document.querySelector('#actions-link');
  actions.removeAttribute('href');
  actions.hidden = true;
  try {
    const url = new URL(current.actions_url);
    if (url.origin === 'https://github.com' && !url.username && !url.password) {
      actions.href = url.href;
      actions.hidden = false;
    }
  } catch { /* No verified Actions link is available. */ }
  document.querySelector('#history-summary').textContent = `${current.app} · RUN #${current.run_id}`;
  document.querySelector('#history-detail').textContent = label;
  document.querySelector('#monitor-state').textContent = label;
}

function stopPolling() {
  clearTimeout(timer);
  pollController?.abort();
  pollController = null;
  document.querySelector('#stop-polling').hidden = true;
  document.querySelector('#refresh-run').hidden = !current;
}

async function refreshRun() {
  stopPolling();
  if (!current) return;
  const controller = new AbortController();
  pollController = controller;
  document.querySelector('#stop-polling').hidden = false;
  document.querySelector('#refresh-run').hidden = true;
  try {
    const result = await request(`/api/runs/${current.run_id}`, {}, controller);
    if (pollController !== controller) return;
    if (result.run_id !== current.run_id || (result.target_id && current.target_id && result.target_id !== current.target_id)) {
      throw new Error('조회한 CI 실행 또는 대상이 요청과 일치하지 않습니다.');
    }
    current = { ...current, ...result, app: current.app };
    renderRun();
    if (['queued', 'running'].includes(current.state)) timer = setTimeout(refreshRun, 15000);
    else stopPolling();
  } catch (cause) {
    if (pollController !== controller) return;
    stopPolling();
    document.querySelector('#run-message').textContent = `${cause.name === 'AbortError' ? '상태 조회 시간이 초과되었습니다.' : cause.message} 자동 조회를 중지했습니다. 상태 다시 조회를 누르세요.`;
  }
}

document.querySelector('#stop-polling').addEventListener('click', () => {
  stopPolling();
  document.querySelector('#run-message').textContent = '상태 조회를 중지했습니다. 서버의 CI 실행은 계속됩니다.';
});
document.querySelector('#refresh-run').addEventListener('click', refreshRun);
window.addEventListener('pagehide', () => {
  stopPolling();
  for (const controller of requests) controller.abort();
  token.value = '';
});
checkConnection();

const consoleMessages = {
  work: '배포가 연결되면 작업 로그가 여기에 표시됩니다.',
  environment: '배포가 연결되면 인프라와 앱 상태가 여기에 표시됩니다.',
  app: '배포가 연결되면 앱 로그가 여기에 표시됩니다.',
};
document.querySelectorAll('[data-console]').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-console]').forEach((tab) => {
      tab.setAttribute('aria-selected', String(tab === button));
    });
    document.querySelector('#console-output').textContent = consoleMessages[button.dataset.console];
  });
});

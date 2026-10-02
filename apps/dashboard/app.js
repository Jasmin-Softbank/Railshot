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
const registeredApp = document.querySelector('#registered-app');
const selection = document.querySelector('#source-selection');
const sourceBox = document.querySelector('#source-box');
const error = document.querySelector('#form-error');
let selectedSource = null;

function setSource(source) {
  selectedSource = source;
  selection.textContent = source ? source.label : '';
  selection.hidden = !source;
  error.hidden = true;
  document.querySelector('#review-panel').hidden = true;
  document.querySelector('#start-deployment').disabled = true;
}

document.querySelector('#choose-file').addEventListener('click', () => archive.click());
document.querySelector('#choose-folder').addEventListener('click', () => folder.click());
archive.addEventListener('change', () => {
  const file = archive.files[0];
  if (!file) return;
  folder.value = '';
  repositoryUrl.value = '';
  registeredApp.value = '';
  setSource({ kind: 'archive', label: file.name });
});
folder.addEventListener('change', () => {
  if (!folder.files.length) return;
  archive.value = '';
  repositoryUrl.value = '';
  registeredApp.value = '';
  const name = (folder.files[0].webkitRelativePath || folder.files[0].name).split('/')[0];
  setSource({ kind: 'folder', label: `${name} · ${folder.files.length}개 파일` });
});
repositoryUrl.addEventListener('input', () => {
  archive.value = '';
  folder.value = '';
  registeredApp.value = '';
  const url = repositoryUrl.value.trim();
  setSource(url ? { kind: 'repository', label: url } : null);
});
registeredApp.addEventListener('input', () => {
  archive.value = '';
  folder.value = '';
  repositoryUrl.value = '';
  const app = registeredApp.value.trim();
  setSource(app ? { kind: 'registered', label: `등록된 앱 · ${app}`, app } : null);
});
sourceBox.addEventListener('dragover', (event) => {
  event.preventDefault();
  sourceBox.classList.add('dragging');
});
sourceBox.addEventListener('dragleave', () => sourceBox.classList.remove('dragging'));
sourceBox.addEventListener('drop', (event) => {
  event.preventDefault();
  sourceBox.classList.remove('dragging');
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
  registeredApp.value = '';
  setSource({ kind: 'archive', label: files[0].name });
});

const providerField = document.querySelector('#provider-field');
const provider = document.querySelector('#provider');
const reviewNote = document.querySelector('#review-note');
const deployButton = document.querySelector('#start-deployment');
let deploymentDraft = null;
// Future planning and deployment steps can consume this same draft.
function getDeploymentDraft() {
  const environment = document.querySelector('[name="environment"]:checked').value;
  return {
    source: selectedSource,
    target: { environment, provider: environment === 'cloud' ? 'aws' : provider.value },
  };
}

document.querySelectorAll('[name="environment"]').forEach((input) => {
  input.addEventListener('change', () => {
    providerField.hidden = input.value !== 'onprem';
    document.querySelector('#review-panel').hidden = true;
    error.hidden = true;
  });
});
provider.addEventListener('change', () => {
  document.querySelector('#review-panel').hidden = true;
  error.hidden = true;
});

document.querySelector('#deploy-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const draft = getDeploymentDraft();
  if (!draft.source) {
    error.textContent = '배포할 소스를 선택하세요.';
  } else if (draft.source.kind === 'registered' && !/^[a-z0-9-]{1,30}$/.test(draft.source.app)) {
    error.textContent = '등록된 앱 이름은 소문자·숫자·하이픈 1~30자여야 합니다.';
  } else if (draft.source.kind === 'repository' && !/^https:\/\/github\.com\/[^/\s]+\/[^/\s?#]+\/?$/.test(draft.source.label)) {
    error.textContent = '공개 GitHub 저장소 URL을 입력하세요.';
  } else if (draft.target.environment === 'onprem' && !draft.target.provider) {
    error.textContent = '온프레미스 인프라 종류를 선택하세요.';
  } else {
    error.hidden = true;
    const target = draft.target.environment === 'cloud'
      ? '클라우드 · RailShot AWS'
      : `온프레미스 · ${provider.selectedOptions[0].textContent}`;
    document.querySelector('#review-source').textContent = draft.source.label;
    document.querySelector('#review-target').textContent = target;
    deploymentDraft = draft;
    const ready = draft.source.kind === 'registered' && draft.target.provider === 'aws';
    deployButton.disabled = !ready;
    reviewNote.textContent = ready
      ? '등록된 앱의 GitHub Actions를 실행합니다. 새 파일 업로드는 아직 연결되지 않았습니다.'
      : '이 소스 또는 배포 환경은 아직 실행 API에 연결되지 않았습니다.';
    document.querySelector('#review-panel').hidden = false;
    document.querySelector('#review-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }
  error.hidden = false;
});
document.querySelector('#edit-selection').addEventListener('click', () => {
  document.querySelector('#review-panel').hidden = true;
  document.querySelector('#source-title').scrollIntoView({ behavior: 'smooth' });
});

const consoleMessages = {
  work: '실행을 시작하면 Actions 단계별 상태가 표시됩니다.',
  environment: '대상 환경의 Ready 상태 조회는 아직 연결되지 않았습니다.',
  app: '앱 로그 조회는 아직 연결되지 않았습니다.',
};
function renderConsole() {
  const selected = document.querySelector('[data-console][aria-selected="true"]').dataset.console;
  document.querySelector('#console-output').textContent = consoleMessages[selected];
}
document.querySelectorAll('[data-console]').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-console]').forEach((tab) => {
      tab.setAttribute('aria-selected', String(tab === button));
    });
    renderConsole();
  });
});

const runTitle = document.querySelector('#run-title');
const runStatus = document.querySelector('#run-status');
const runDot = document.querySelector('#run-dot');
const actionsLink = document.querySelector('#actions-link');
const workflowJobs = document.querySelector('#workflow-jobs');
let refreshTimer;
let currentRunId;

function showActionsLink(url) {
  actionsLink.hidden = !url?.startsWith('https://github.com/');
  if (!actionsLink.hidden) actionsLink.href = url;
}

function resultLabel(item) {
  const value = item.conclusion || item.status || 'queued';
  return ({ queued: '대기', in_progress: '진행 중', completed: '완료', success: '성공', failure: '실패', skipped: '건너뜀', cancelled: '취소' })[value] || value;
}

function renderRun(run) {
  runTitle.textContent = `Actions 실행 #${run.runId}`;
  const completed = run.status === 'completed';
  runDot.classList.toggle('muted', !completed || run.conclusion !== 'success');
  runStatus.textContent = completed && run.conclusion === 'success'
    ? run.requiredJobsSucceeded && run.publicHttpCheck === 'passed'
      ? 'Actions URL HTTP 검사 통과 · 대상 Ready/버전은 별도 확인 필요'
      : 'Actions 종료 · 필수 작업 또는 공개 URL 검사가 확인되지 않음'
    : completed ? `Actions ${resultLabel(run)} · 배포 완료 아님` : `Actions ${resultLabel(run)} · 진행 상황 조회 중`;
  showActionsLink(run.actionsUrl);
  workflowJobs.replaceChildren();
  for (const job of run.jobs || []) {
    const item = document.createElement('li');
    const title = document.createElement('strong');
    title.textContent = job.name;
    const status = document.createElement('small');
    status.textContent = resultLabel(job);
    item.append(title, status);
    const steps = document.createElement('ol');
    for (const step of job.steps || []) {
      const line = document.createElement('li');
      line.textContent = `${step.name} · ${resultLabel(step)}`;
      steps.append(line);
    }
    item.append(steps);
    workflowJobs.append(item);
  }
  if (!run.jobs?.length) {
    const item = document.createElement('li');
    item.textContent = 'Actions 작업 대기';
    workflowJobs.append(item);
  }
  const lines = (run.jobs || []).flatMap((job) => [
    `${job.name} · ${resultLabel(job)}`,
    ...(job.steps || []).map((step) => `  ${step.name} · ${resultLabel(step)}`),
  ]);
  consoleMessages.work = lines.join('\n') || 'Actions 작업 대기';
  if (run.jobsTruncated) consoleMessages.work += '\n일부 작업은 표시되지 않았습니다. GitHub Actions에서 확인하세요.';
  renderConsole();
}

async function refreshRun(runId) {
  currentRunId = String(runId);
  clearTimeout(refreshTimer);
  try {
    const response = await fetch(`/api/deployments/${runId}`, { headers: { 'x-railshot-request': '1' } });
    const run = await response.json();
    if (!response.ok) throw new Error(run.error || 'Actions 상태를 읽지 못했습니다.');
    if (currentRunId !== String(runId)) return;
    renderRun(run);
    if (run.status !== 'completed') refreshTimer = setTimeout(() => refreshRun(runId), 5000);
  } catch (cause) {
    if (currentRunId !== String(runId)) return;
    runStatus.textContent = `${cause.message} · 잠시 후 다시 조회합니다.`;
    refreshTimer = setTimeout(() => refreshRun(runId), 10000);
  }
}

deployButton.addEventListener('click', async () => {
  if (!deploymentDraft || deploymentDraft.source.kind !== 'registered' || deploymentDraft.target.provider !== 'aws') return;
  deployButton.disabled = true;
  reviewNote.textContent = 'GitHub Actions 실행을 요청하는 중입니다.';
  try {
    const response = await fetch('/api/deployments', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-railshot-request': '1' },
      body: JSON.stringify({ app: deploymentDraft.source.app, target: deploymentDraft.target }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '실행을 시작하지 못했습니다.');
    showView('monitor');
    showActionsLink(result.actionsUrl);
    if (!result.runId) {
      currentRunId = null;
      clearTimeout(refreshTimer);
      try { sessionStorage.removeItem('railshot-run-id'); } catch { /* 로컬 실행 정보 없이 계속한다. */ }
      runTitle.textContent = 'Actions 요청 접수';
      runStatus.textContent = '실행 ID를 받지 못했습니다. Actions에서 실행을 확인한 뒤 재요청하세요.';
      return;
    }
    try { sessionStorage.setItem('railshot-run-id', String(result.runId)); } catch { /* 실행 조회는 현재 탭에서 계속된다. */ }
    runTitle.textContent = `Actions 실행 #${result.runId}`;
    runStatus.textContent = 'Actions 실행 상태를 조회하는 중입니다.';
    await refreshRun(result.runId);
  } catch (cause) {
    reviewNote.textContent = cause.message;
    deployButton.disabled = false;
  }
});

let savedRunId;
try { savedRunId = sessionStorage.getItem('railshot-run-id'); } catch { /* 저장소 사용이 제한된 브라우저 */ }
if (/^\d+$/.test(savedRunId || '')) refreshRun(savedRunId);

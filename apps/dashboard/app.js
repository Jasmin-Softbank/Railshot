const form = document.querySelector('#deploy-form');
const fileInput = document.querySelector('#archive');
const folderInput = document.querySelector('#folder');
const dropzone = document.querySelector('#dropzone');
const repositoryUrl = document.querySelector('#repository-url');
const sourceSelection = document.querySelector('#source-selection');
const message = document.querySelector('#form-message');
const deployButton = document.querySelector('#deploy-button');
const createView = document.querySelector('#create-view');
const runView = document.querySelector('#run-view');
const navCreate = document.querySelector('#nav-create');
const navRuns = document.querySelector('#nav-runs');
let current = JSON.parse(sessionStorage.getItem('jasmin-run') || 'null');
let timer;

function appName(name) {
  const slug = name.normalize('NFKD').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 30).replace(/-+$/g, '');
  if (!/^[a-z][a-z0-9-]{1,28}[a-z0-9]$/.test(slug)) throw new Error('앱 이름을 영문 소문자로 시작하는 3~30자로 지정하세요. 마지막 글자는 영문 소문자나 숫자여야 합니다.');
  return slug;
}

function selectFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.zip')) throw new Error('현재 파일 업로드는 ZIP만 지원합니다.');
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  folderInput.value = '';
  repositoryUrl.value = '';
  sourceSelection.textContent = `선택한 파일: ${file.name}`;
  report('');
}

function showRun(show) {
  createView.hidden = show;
  runView.hidden = !show;
  navCreate.classList.toggle('active', !show);
  navRuns.classList.toggle('active', show);
  if (show && current) refresh();
  else clearTimeout(timer);
}

function report(text) {
  message.textContent = text;
  message.hidden = !text;
}

document.querySelector('#choose-file').addEventListener('click', () => fileInput.click());
document.querySelector('#choose-folder').addEventListener('click', () => folderInput.click());
fileInput.addEventListener('change', () => {
  try { selectFile(fileInput.files[0]); } catch (error) { fileInput.value = ''; report(error.message); }
});
folderInput.addEventListener('change', () => {
  const files = Array.from(folderInput.files);
  if (!files.length) return;
  fileInput.value = '';
  repositoryUrl.value = '';
  sourceSelection.textContent = `선택한 폴더: ${(files[0].webkitRelativePath || files[0].name).split('/')[0]} · ${files.length}개 파일`;
  report('');
});
repositoryUrl.addEventListener('input', () => {
  if (repositoryUrl.value.trim()) { fileInput.value = ''; folderInput.value = ''; }
  sourceSelection.textContent = repositoryUrl.value.trim() ? '입력한 GitHub URL을 사용합니다.' : '선택한 소스가 없습니다.';
  report('');
});
dropzone.addEventListener('dragover', (event) => { event.preventDefault(); dropzone.classList.add('dragging'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragging'));
dropzone.addEventListener('drop', (event) => {
  event.preventDefault();
  dropzone.classList.remove('dragging');
  try {
    if (event.dataTransfer.files.length !== 1) throw new Error('ZIP 파일 하나를 놓으세요. 폴더는 폴더 선택 버튼을 사용하세요.');
    selectFile(event.dataTransfer.files[0]);
  } catch (error) { report(error.message); }
});
navCreate.addEventListener('click', () => showRun(false));
navRuns.addEventListener('click', () => showRun(Boolean(current)));

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  report('');
  deployButton.disabled = true;
  deployButton.textContent = '소스 확인 중…';
  try {
    const payload = new FormData();
    const override = document.querySelector('#app-name').value.trim();
    const url = repositoryUrl.value.trim();
    if (url) {
      let repository;
      try { repository = new URL(url); } catch { throw new Error('올바른 공개 GitHub 저장소 URL을 입력하세요.'); }
      payload.set('app', override || appName(repository.pathname.split('/')[2]?.replace(/\.git$/, '') || ''));
      payload.set('repository_url', url);
    } else if (fileInput.files[0]) {
      const file = fileInput.files[0];
      payload.set('app', override || appName(file.name.replace(/\.zip$/i, '')));
      payload.set('archive', file);
    } else if (folderInput.files.length) {
      const files = Array.from(folderInput.files);
      payload.set('app', override || appName((files[0].webkitRelativePath || files[0].name).split('/')[0]));
      const paths = [];
      for (const file of files) {
        const parts = (file.webkitRelativePath || file.name).split('/');
        const path = parts.length > 1 ? parts.slice(1).join('/') : file.name;
        if (path.split('/').some((part) => ['.git', 'node_modules', '__MACOSX'].includes(part))) continue;
        paths.push(path);
        payload.append('files', file, file.name);
      }
      payload.set('paths', JSON.stringify(paths));
    } else {
      throw new Error('ZIP 파일, 폴더 또는 공개 GitHub URL 하나를 입력하세요.');
    }
    const response = await fetch('/api/deploy', {
      method: 'POST', headers: { 'x-jasmin-request': 'deploy' }, body: payload,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '배포를 시작하지 못했습니다.');
    current = result;
    sessionStorage.setItem('jasmin-run', JSON.stringify(result));
    showRun(true);
  } catch (error) { report(error.message); }
  finally { deployButton.disabled = false; deployButton.textContent = '검사 및 이미지 게시 →'; }
});

const names = {
  loop: ['앱 검사 및 수정', '소스를 분석하고 배포 가능 여부를 확인합니다.'],
  release: ['검증 이미지 게시', '검사를 통과한 동일 이미지를 레지스트리에 게시하고 CD 인계 자료를 만듭니다.'],
};

async function refresh() {
  if (!current) return;
  clearTimeout(timer);
  try {
    const response = await fetch(`/api/runs/${current.run_id}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '상태 조회에 실패했습니다.');
    document.querySelector('#run-app').textContent = current.app;
    const changes = current.changes;
    const delta = changes && (changes.added || changes.updated || changes.deleted)
      ? ` · 파일 추가 ${changes.added}, 수정 ${changes.updated}, 삭제 ${changes.deleted}`
      : changes ? ' · 소스 변경 없음, 재실행' : '';
    document.querySelector('#run-meta').textContent = `RUN #${current.run_id} · ${current.target_id || result.target_id || "대상 미설정"} · ${current.tenant}${current.source_commit ? ` · ${current.source_commit.slice(0, 7)}` : ""}${delta}`;
    const badge = document.querySelector('#run-badge');
    badge.textContent = ({ published: '이미지 게시 · CD 인계 대기', publication_unverified: '게시 증거 확인 필요', failed: 'CI 실패', running: '진행 중', queued: '대기 중' })[result.state] || '상태 확인 중';
    badge.classList.toggle('failed', result.status === 'completed' && result.conclusion !== 'success');
    document.querySelector('#run-steps').replaceChildren(...result.steps.map((step, index) => {
      const li = document.createElement('li');
      const num = document.createElement('span');
      num.className = 'step-num'; num.textContent = String(index + 1).padStart(2, '0');
      const copy = document.createElement('span'); copy.className = 'step-copy';
      const strong = document.createElement('strong'); strong.textContent = names[step.key][0];
      const small = document.createElement('small'); small.textContent = names[step.key][1];
      copy.append(strong, small);
      const state = document.createElement('span'); state.className = 'step-state';
      state.textContent = step.conclusion === 'success' ? '완료' : step.conclusion === 'skipped' ? '건너뜀' : step.conclusion ? '실패' : step.status === 'in_progress' ? '진행 중' : '대기';
      state.classList.toggle('complete', step.conclusion === 'success');
      state.classList.toggle('active', step.status === 'in_progress');
      li.append(num, copy, state);
      return li;
    }));
    document.querySelector('#run-message').textContent = result.message || (result.status === 'completed' ? 'GitHub Actions 실행이 끝났습니다.' : '15초마다 상태를 확인합니다.');
    const actions = document.querySelector('#actions-link');
    actions.href = result.actions_url; actions.hidden = !result.actions_url;
    const link = document.querySelector('#result-link');
    link.href = result.url || '#'; link.hidden = !result.url;
    document.querySelector('#recent-row').textContent = `${current.app} · RUN #${current.run_id} · ${badge.textContent}`;
    if (result.status !== 'completed') timer = setTimeout(refresh, 15000);
  } catch (error) {
    document.querySelector('#run-message').textContent = error.message;
    timer = setTimeout(refresh, 15000);
  }
}

fetch('/healthz').then((response) => response.json()).then((health) => {
  document.querySelector('#target-id').value = health.target_id || '운영자 대상 설정 필요';
  const connection = document.querySelector('#connection-status');
  connection.textContent = health.configured ? '● API 연결됨' : '● GitHub 및 대상 설정 필요';
  connection.classList.toggle('offline', !health.configured);
}).catch(() => {
  document.querySelector('#target-id').value = health.target_id || '운영자 대상 설정 필요';
  const connection = document.querySelector('#connection-status');
  connection.textContent = '● API 연결 실패'; connection.classList.add('offline');
});
if (current) {
  document.querySelector('#recent-row').textContent = `${current.app} · RUN #${current.run_id}`;
  showRun(true);
}

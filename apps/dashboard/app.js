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
let selectedSource = null;

function setSource(source) {
  selectedSource = source;
  selection.textContent = source ? source.label : '';
  selection.hidden = !source;
  error.hidden = true;
  document.querySelector('#review-panel').hidden = true;
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

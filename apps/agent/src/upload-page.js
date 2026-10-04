const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[character]);

export function uploadPage({ app, target_id }, nonce) {
  return String.raw`<!doctype html>
<html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Railshot 파일 배포</title>
<style>body{font:16px system-ui;max-width:38rem;margin:3rem auto;padding:1rem;line-height:1.6}label{display:block;margin:1rem 0}button{padding:.7rem 1rem}output{display:block;white-space:pre-wrap;margin-top:1rem}</style>
<h1>Railshot 파일 배포</h1>
<p>앱 <strong>${escapeHtml(app)}</strong>을 대상 <strong>${escapeHtml(target_id)}</strong>에 배포합니다. ZIP, 개별 파일 또는 로컬 폴더 중 하나를 선택하세요.</p>
<p>선택한 소스는 Railshot API로 전송되며, 배포 요청이 접수되면 아래에 배포 ID가 표시됩니다. 이 링크는 10분 동안 유효합니다.</p>
<form id="upload-form">
  <label>ZIP 파일 <input id="archive" type="file" accept=".zip,application/zip"></label>
  <label>개별 파일 <input id="files" type="file" multiple></label>
  <label>폴더 <input id="folder" type="file" webkitdirectory multiple></label>
  <button type="submit">파일 배포 요청</button>
</form>
<output id="result" aria-live="polite"></output>
<script nonce="${nonce}">
const form = document.querySelector('#upload-form');
const archive = document.querySelector('#archive');
const files = document.querySelector('#files');
const folder = document.querySelector('#folder');
const result = document.querySelector('#result');
archive.addEventListener('change', () => { if (archive.files.length) { files.value = ''; folder.value = ''; } });
files.addEventListener('change', () => { if (files.files.length) { archive.value = ''; folder.value = ''; } });
folder.addEventListener('change', () => { if (folder.files.length) { archive.value = ''; files.value = ''; } });
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (archive.files.length !== 1 && !files.files.length && !folder.files.length) { result.textContent = 'ZIP, 개별 파일 또는 폴더를 선택하세요.'; return; }
  const body = new FormData();
  if (archive.files.length) {
    if (!archive.files[0].name.toLowerCase().endsWith('.zip')) { result.textContent = 'ZIP 파일만 업로드할 수 있습니다.'; return; }
    body.set('archive', archive.files[0]);
  } else {
    const paths = [];
    for (const file of folder.files.length ? folder.files : files.files) {
      const parts = (file.webkitRelativePath || file.name).split('/');
      const path = parts.length > 1 ? parts.slice(1).join('/') : file.name;
      if (path.split('/').some((part) => ['.git', 'node_modules', '__MACOSX', '.DS_Store'].includes(part))) continue;
      paths.push(path); body.append('files', file, file.name);
    }
    if (!paths.length) { result.textContent = '배포할 파일이 없습니다.'; return; }
    body.set('paths', JSON.stringify(paths));
  }
  form.querySelector('button').disabled = true;
  result.textContent = '파일을 전송하고 배포 요청을 접수하는 중입니다…';
  try {
    const response = await fetch(location.pathname, { method: 'POST', body, credentials: 'omit' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || '배포 요청에 실패했습니다.');
    result.textContent = '배포 요청 접수: ' + data.resource_id + '\nAI에게 파일 제출을 완료했다고 알려주세요. AI가 get_file_upload와 get_deployment로 상태를 확인할 수 있습니다.';
  } catch (error) {
    result.textContent = error.message + '\n결과가 불확실하면 같은 페이지에서 다시 시도하세요. 요청 키는 유지됩니다.';
  } finally { form.querySelector('button').disabled = false; }
});
</script></html>`;
}

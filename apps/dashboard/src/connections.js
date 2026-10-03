import { request } from './api.js';

export function createConnectionsController(isSessionReady) {
  let connections = [], editingConnection = null;
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
    if (!isSessionReady()) return;
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
  return { loadConnections };
}

import { createHash, randomBytes, randomUUID, createCipheriv, createDecipheriv } from 'node:crypto';
import { lstat, open, readFile } from 'node:fs/promises';
import { join } from 'node:path';

// OAuth MCP bearer tokens are bound to product sessions. Keep both durable so
// an established AI connection is not silently revoked by session expiry.
export const SESSION_SECONDS = 2_147_483_647;
export const SESSION_EXPIRES_AT = 8_640_000_000_000_000;
export const SESSION_COOKIE = 'railshot_session';
export const OWNER_COOKIE = 'railshot_owner';
export function ownerCookie(token, secure) { return `${OWNER_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000${secure ? '; Secure' : ''}`; }
export function ownerToken(header) {
  const values = (header || '').split(';').map((s) => s.trim()).filter((s) => s.startsWith(`${OWNER_COOKIE}=`));
  return values.length === 1 && /^[A-Za-z0-9_-]{43}$/.test(values[0].slice(OWNER_COOKIE.length + 1)) ? values[0].slice(OWNER_COOKIE.length + 1) : null;
}
export class DashboardError extends Error {
  constructor(message, status = 422, code = 'INVALID_INPUT') { super(message); Object.assign(this, { status, code }); }
}
const missing = () => new DashboardError('이 세션에서 자원을 찾을 수 없습니다.', 404, 'NOT_FOUND');
const defaults = { view: 'deploy', environment: 'cloud', provider: '' };
const hash = (value) => createHash('sha256').update(value).digest('hex');
export function sessionCookie(token, secure) {
  return `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${token ? SESSION_SECONDS : 0}${secure ? '; Secure' : ''}`;
}
export function cookieToken(header) {
  const matches = (header || '').split(';').map((item) => item.trim()).filter((item) => item.startsWith(`${SESSION_COOKIE}=`));
  if (matches.length !== 1) return null;
  const value = matches[0].slice(SESSION_COOKIE.length + 1);
  return /^[A-Za-z0-9_-]{43}$/.test(value) ? value : null;
}

export async function createDashboardData(db, root) {
  const keyPath = join(root, 'connections.key');
  // Keep the key separate from DB backups. A missing key with saved ciphertext is an error.
  try { await lstat(keyPath); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    if (db.prepare('SELECT 1 FROM connections WHERE password_encrypted IS NOT NULL LIMIT 1').get()
        || db.prepare('SELECT 1 FROM mcp_tokens LIMIT 1').get()) throw new Error('Connection encryption key is missing');
    const file = await open(keyPath, 'wx', 0o600);
    try { await file.writeFile(randomBytes(32)); await file.sync(); } finally { await file.close(); }
    const directory = await open(root, 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  }
  const info = await lstat(keyPath);
  if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('Invalid connection encryption key');
  const key = await readFile(keyPath);
  if (key.length !== 32) throw new Error('Invalid connection encryption key');
  function encrypt(value, binding) {
    const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv);
    cipher.setAAD(Buffer.from(binding));
    const ciphertext = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]);
    return Buffer.concat([iv, cipher.getAuthTag(), ciphertext]);
  }
  const mcpBinding = (row) => JSON.stringify([row.token_hash, row.session_id, row.client_id, row.resource]);
  function validateMcp(input, registering = false) {
    const keys = registering ? 'client_id,resource,session,token_hash' : 'resource,token_hash';
    if (!input || Object.keys(input).sort().join(',') !== keys || !/^[a-f0-9]{64}$/.test(input.token_hash || '')
        || typeof input.resource !== 'string' || input.resource.length > 2048)
      throw new DashboardError('MCP 연결 입력을 확인하세요.');
    let url;
    try { url = new URL(input.resource); } catch { throw new DashboardError('MCP 리소스 주소를 확인하세요.'); }
    if (url.href !== input.resource || url.pathname !== '/mcp' || url.search || url.hash || url.username || url.password
        || !(url.protocol === 'https:' || url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))
      throw new DashboardError('MCP 리소스 주소를 확인하세요.');
    if (registering && (typeof input.session !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(input.session)
        || typeof input.client_id !== 'string' || !input.client_id || input.client_id.length > 2048 || /[\x00-\x1f]/.test(input.client_id)))
      throw new DashboardError('MCP 연결 소유 세션을 확인하세요.');
  }
  function live(id) {
    const row = db.prepare('SELECT * FROM sessions WHERE id = ? AND expires_at > ?').get(id, Date.now());
    if (!row) throw new DashboardError('세션이 만료되었습니다. 화면을 새로고침하세요.', 409, 'SESSION_EXPIRED');
    return row;
  }
  const visible = (row) => ({ id: row.id, provider: row.provider, label: row.label, console_url: row.console_url,
    username: row.username, has_password: row.password_encrypted !== null, created_at: row.created_at, updated_at: row.updated_at });
  return {
    saveMcpToken(input) {
      validateMcp(input, true);
      const session = live(hash(input.session)), now = Date.now();
      const row = { ...input, session_id: session.id };
      const previous = db.prepare('SELECT session_id, client_id, resource FROM mcp_tokens WHERE token_hash = ?').get(input.token_hash);
      if (previous) {
        if (['session_id', 'client_id', 'resource'].some(field => previous[field] !== row[field]))
          throw new DashboardError('이미 발급된 MCP 연결을 다른 소유자에게 연결할 수 없습니다.', 409, 'CONFLICT');
        return { registered: true };
      }
      db.prepare('DELETE FROM mcp_tokens WHERE expires_at <= ?').run(now);
      if (db.prepare('SELECT count(*) AS count FROM mcp_tokens').get().count >= 10000)
        throw new DashboardError('MCP 연결 보관 한도에 도달했습니다.', 429, 'CAPACITY_EXCEEDED');
      db.prepare('INSERT INTO mcp_tokens VALUES (?, ?, ?, ?, ?, ?, ?)').run(input.token_hash, session.id,
        input.client_id, input.resource, encrypt(input.session, mcpBinding(row)), session.expires_at, now);
      return { registered: true };
    },
    mcpToken(input) {
      validateMcp(input);
      const row = db.prepare(`SELECT t.* FROM mcp_tokens t JOIN sessions s ON s.id = t.session_id
        WHERE t.token_hash = ? AND t.resource = ? AND t.expires_at > ? AND s.expires_at > ?`)
        .get(input.token_hash, input.resource, Date.now(), Date.now());
      if (!row) throw new DashboardError('MCP 연결을 찾을 수 없습니다.', 404, 'NOT_FOUND');
      const ciphertext = Buffer.from(row.session_encrypted), decipher = createDecipheriv('aes-256-gcm', key, ciphertext.subarray(0, 12));
      decipher.setAAD(Buffer.from(mcpBinding(row))); decipher.setAuthTag(ciphertext.subarray(12, 28));
      const session = Buffer.concat([decipher.update(ciphertext.subarray(28)), decipher.final()]).toString('utf8');
      if (hash(session) !== row.session_id) throw new Error('MCP session binding differs');
      return { session, client_id: row.client_id, resource: row.resource, expires_at: row.expires_at };
    },
    isOwnerSession(id) { return Boolean(id && db.prepare('SELECT 1 FROM owners WHERE session_id = ?').get(id)); },
    owner(token) {
      if (!token) return null;
      const row = db.prepare('SELECT id, session_id FROM owners WHERE token_hash = ?').get(hash(token));
      return row || null;
    },
    createOwner() {
      if (db.prepare('SELECT count(*) AS count FROM owners').get().count >= 10000) throw new DashboardError('소유자 보관 한도에 도달했습니다.', 409, 'CAPACITY_EXCEEDED');
      const id = randomUUID(), token = randomBytes(32).toString('base64url'), recovery = randomBytes(32).toString('base64url');
      const sessionId = hash(randomBytes(32)), now = Date.now();
      db.exec('BEGIN IMMEDIATE');
      try {
        db.prepare('INSERT INTO sessions VALUES (?, ?, ?, ?)').run(sessionId, now, now, 8640000000000000);
        db.prepare('INSERT INTO owners VALUES (?, ?, ?, ?, ?)').run(id, sessionId, hash(token), hash(recovery), new Date().toISOString());
        db.exec('COMMIT');
      } catch (error) { db.exec('ROLLBACK'); throw error; }
      return { id, session_id: sessionId, token, recovery_key: recovery };
    },
    recoverOwner(input) {
      if (!input || Object.keys(input).length !== 1 || typeof input.recovery_key !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(input.recovery_key)) throw new DashboardError('복구키를 확인하세요.', 401, 'UNAUTHENTICATED');
      const row = db.prepare('SELECT id, session_id FROM owners WHERE recovery_hash = ?').get(hash(input.recovery_key));
      if (!row) throw new DashboardError('복구키를 확인하세요.', 401, 'UNAUTHENTICATED');
      const token = randomBytes(32).toString('base64url'), recovery = randomBytes(32).toString('base64url');
      db.prepare('UPDATE owners SET token_hash = ?, recovery_hash = ? WHERE id = ?').run(hash(token), hash(recovery), row.id);
      return { ...row, token, recovery_key: recovery };
    },
    session(token) {
      const now = Date.now(), id = token && hash(token);
      const existing = id && db.prepare('SELECT * FROM sessions WHERE id = ? AND expires_at > ?').get(id, now);
      if (existing) {
        if (now - existing.last_seen_at > 60000) db.prepare('UPDATE sessions SET last_seen_at = ? WHERE id = ?').run(now, id);
        return { id, expires_at: new Date(existing.expires_at).toISOString(), token: null };
      }
      if (db.prepare('SELECT count(*) AS count FROM sessions').get().count >= 10000) throw new DashboardError('세션 보관 한도에 도달했습니다.', 503, 'CAPACITY_EXCEEDED');
      const fresh = randomBytes(32).toString('base64url'), freshId = hash(fresh), expires = SESSION_EXPIRES_AT;
      db.prepare('INSERT INTO sessions VALUES (?, ?, ?, ?)').run(freshId, now, now, expires);
      return { id: freshId, expires_at: new Date(expires).toISOString(), token: fresh };
    },
    preferences(id, input) {
      live(id);
      if (input !== undefined) {
        if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).some((key) => !Object.hasOwn(defaults, key))
            || ('view' in input && !['deploy', 'history', 'monitor', 'connections', 'personal'].includes(input.view))
            || ('environment' in input && !['cloud', 'onprem'].includes(input.environment))
            || ('provider' in input && !['', 'aws', 'gcp', 'openstack', 'proxmox'].includes(input.provider))) throw new DashboardError('화면 설정을 확인하세요.');
        const previous = db.prepare('SELECT data FROM preferences WHERE session_id = ?').get(id);
        const data = { ...defaults, ...(previous ? JSON.parse(previous.data) : {}), ...input };
        db.prepare('INSERT INTO preferences VALUES (?, ?) ON CONFLICT(session_id) DO UPDATE SET data=excluded.data').run(id, JSON.stringify(data));
      }
      const row = db.prepare('SELECT data FROM preferences WHERE session_id = ?').get(id);
      return row ? JSON.parse(row.data) : { ...defaults };
    },
    connections(id) { live(id); return db.prepare('SELECT * FROM connections WHERE session_id = ? ORDER BY created_at DESC, id').all(id).map(visible); },
    saveConnection(id, connectionId, input) {
      live(id);
      const previous = connectionId && db.prepare('SELECT * FROM connections WHERE id = ? AND session_id = ?').get(connectionId, id);
      if (connectionId && !previous) throw missing();
      if (!input || typeof input !== 'object' || Array.isArray(input)
          || Object.keys(input).some((key) => !['label', 'console_url', 'username', 'password'].includes(key))
          || typeof input.label !== 'string' || !input.label.trim() || input.label.length > 80 || /[\x00-\x1f]/.test(input.label)
          || typeof input.console_url !== 'string' || input.console_url.length > 2048
          || typeof input.username !== 'string' || input.username.length > 256 || /[\x00-\x1f]/.test(input.username)
          || ('password' in input && input.password !== null && (typeof input.password !== 'string' || !input.password.length || input.password.length > 4096 || input.password.includes('\0')))) throw new DashboardError('OpenStack 연결 입력을 확인하세요.');
      let url;
      try { url = new URL(input.console_url); } catch { throw new DashboardError('올바른 콘솔 URL을 입력하세요.'); }
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new DashboardError('콘솔 URL에는 ID·비밀번호·쿼리·fragment를 포함할 수 없습니다.');
      if (!previous && db.prepare('SELECT count(*) AS count FROM connections WHERE session_id = ?').get(id).count >= 20) throw new DashboardError('연결 정보는 세션당 20개까지 저장할 수 있습니다.', 409, 'CAPACITY_EXCEEDED');
      const recordId = connectionId || randomUUID(), now = new Date().toISOString();
      let encrypted = previous?.password_encrypted ?? null;
      if (input.password === null) encrypted = null;
      else if (typeof input.password === 'string') {
        encrypted = encrypt(input.password, `${id}:${recordId}`);
      }
      db.prepare(`INSERT INTO connections VALUES (?, ?, 'openstack', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET label=excluded.label, console_url=excluded.console_url, username=excluded.username,
          password_encrypted=excluded.password_encrypted, updated_at=excluded.updated_at`)
        .run(recordId, id, input.label.trim(), url.href, input.username, encrypted, previous?.created_at || now, now);
      return visible(db.prepare('SELECT * FROM connections WHERE id = ? AND session_id = ?').get(recordId, id));
    },
    deleteConnection(id, connectionId) {
      live(id);
      if (!db.prepare('DELETE FROM connections WHERE id = ? AND session_id = ?').run(connectionId, id).changes) throw missing();
    },
  };
}

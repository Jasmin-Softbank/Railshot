import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from 'node:crypto';
import { chmod, lstat, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

export class ConnectionError extends Error {
  constructor(status, code, message) {
    super(message);
    Object.assign(this, { status, code });
  }
}

const invalid = (message) => new ConnectionError(422, 'INVALID_INPUT', message);
const upstream = () => new ConnectionError(502, 'UPSTREAM_FAILURE', 'OpenStack 인증 서버에서 연결을 확인하지 못했습니다.');
const hash = (token) => createHash('sha256').update(token).digest('hex');
const CONNECTION_TOKEN = /^[A-Za-z0-9_-]{43}$/;
const identifier = (value) => typeof value === 'string' && value.length > 0 && value.length <= 255 && !/[\s\x00-\x1f]/.test(value);
const secret = (value) => typeof value === 'string' && value.length > 0 && value.length <= 8192 && !/[\r\n\x00]/.test(value);

function keystoneBaseUrl(authUrl) {
  try {
    const url = new URL(authUrl);
    if (url.protocol === 'https:' && /^\/v3\/?$/.test(url.pathname)
        && !url.username && !url.password && !url.search && !url.hash) {
      return `${url.origin}${url.pathname.replace(/\/$/, '')}`;
    }
  } catch { /* Invalid URLs are reported with the same configuration error. */ }
  throw new Error('RAILSHOT_OPENSTACK_AUTH_URL must be an HTTPS Keystone v3 URL');
}

function encryptionKeyFrom(value) {
  const key = Buffer.from(value || '', 'base64');
  if (key.length !== 32 || key.toString('base64') !== value) {
    throw new Error('RAILSHOT_CONNECTION_KEY must be a base64 encoded 32-byte key');
  }
  return key;
}

function seal(value, key) {
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  const ciphertext = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]);
  return Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString('base64');
}

function unseal(value, key) {
  const data = Buffer.from(value, 'base64');
  const decipher = createDecipheriv('aes-256-gcm', key, data.subarray(0, 12));
  decipher.setAuthTag(data.subarray(12, 28));
  return Buffer.concat([decipher.update(data.subarray(28)), decipher.final()]).toString('utf8');
}

function credentials(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)
      || !identifier(input.project_id) || !identifier(input.user_id)) {
    throw invalid('OpenStack 프로젝트 ID와 사용자 ID를 입력하세요.');
  }
  if (input.auth_type === 'token'
      && Object.keys(input).sort().join(',') === 'auth_type,project_id,token,user_id'
      && secret(input.token) && !/\s/.test(input.token)) {
    return { type: 'token', credentialId: '', credentialSecret: input.token };
  }
  if (input.auth_type === 'application_credential'
      && Object.keys(input).sort().join(',') === 'application_credential_id,application_credential_secret,auth_type,project_id,user_id'
      && identifier(input.application_credential_id) && secret(input.application_credential_secret)) {
    return { type: 'application_credential', credentialId: input.application_credential_id,
      credentialSecret: input.application_credential_secret };
  }
  throw invalid('프로젝트 범위 토큰 또는 Application Credential ID와 secret을 입력하세요.');
}

export async function createConnectionService({
  directory,
  authUrl = process.env.RAILSHOT_OPENSTACK_AUTH_URL,
  encryptionKey = process.env.RAILSHOT_CONNECTION_KEY,
  projectId = process.env.RAILSHOT_OPENSTACK_PROJECT_ID,
  fetcher = fetch,
} = {}) {
  if (!authUrl) return null;
  const baseUrl = keystoneBaseUrl(authUrl);
  const key = encryptionKeyFrom(encryptionKey);

  await mkdir(directory, { recursive: true, mode: 0o700 });
  const root = await lstat(directory);
  if (!root.isDirectory() || root.uid !== process.getuid() || (root.mode & 0o077)) {
    throw new Error('A private connection directory is required');
  }

  const path = join(directory, 'connections.sqlite3');
  const db = new DatabaseSync(path);
  await chmod(path, 0o600);
  db.exec(`CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    credential_ciphertext TEXT NOT NULL,
    created_at TEXT NOT NULL,
    auth_type TEXT NOT NULL DEFAULT 'application_credential',
    user_id TEXT NOT NULL DEFAULT ''
  )`);
  const columns = new Set(db.prepare('PRAGMA table_info(connections)').all().map((column) => column.name));
  if (!columns.has('auth_type')) db.exec("ALTER TABLE connections ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'application_credential'");
  if (!columns.has('user_id')) db.exec("ALTER TABLE connections ADD COLUMN user_id TEXT NOT NULL DEFAULT ''");

  async function keystone(suffix, { method = 'GET', token, subject, body } = {}) {
    let response;
    try {
      response = await fetcher(`${baseUrl}${suffix}`, {
        method, redirect: 'error', signal: AbortSignal.timeout(15000),
        headers: {
          Accept: 'application/json',
          ...(token ? { 'X-Auth-Token': token } : {}),
          ...(subject ? { 'X-Subject-Token': subject } : {}),
          ...(body ? { 'Content-Type': 'application/json' } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
    } catch { throw upstream(); }
    if (!response.ok) {
      if ([401, 403].includes(response.status)) {
        throw new ConnectionError(422, 'INVALID_OPENSTACK_CREDENTIALS', 'OpenStack 인증 정보가 유효하지 않거나 필요한 권한이 없습니다.');
      }
      throw upstream();
    }
    try { return { value: await response.json(), subject: response.headers.get('x-subject-token') }; }
    catch { throw upstream(); }
  }

  async function authenticate(type, credentialId, credentialSecret) {
    if (type === 'token') {
      return (await keystone('/auth/tokens', { token: credentialSecret, subject: credentialSecret })).value?.token;
    }
    const issued = await keystone('/auth/tokens', {
      method: 'POST',
      body: { auth: { identity: { methods: ['application_credential'],
        application_credential: { id: credentialId, secret: credentialSecret } } } },
    });
    if (!issued.subject) throw upstream();
    return issued.value?.token;
  }

  function checkedIdentity(identity, expectedProject, expectedUser) {
    if (!identifier(identity?.project?.id) || !identifier(identity?.user?.id)
        || !Array.isArray(identity.roles) || identity.roles.length === 0) {
      throw new ConnectionError(422, 'INVALID_OPENSTACK_CREDENTIALS', '프로젝트 범위와 역할이 있는 OpenStack 인증 정보가 필요합니다.');
    }
    if (identity.project.id !== expectedProject || (expectedUser && identity.user.id !== expectedUser)) {
      throw new ConnectionError(403, 'FORBIDDEN', '인증 정보의 사용자 또는 프로젝트가 입력한 식별자와 일치하지 않습니다.');
    }
    if (projectId && identity.project.id !== projectId) {
      throw new ConnectionError(403, 'FORBIDDEN', '운영자가 설정한 OpenStack 프로젝트와 일치하지 않습니다.');
    }
    return identity;
  }

  function resolve(token) {
    if (typeof token !== 'string' || !CONNECTION_TOKEN.test(token)) throw invalid('기존 연결 토큰을 확인하세요.');
    const row = db.prepare(`SELECT id, project_id, project_name, credential_id, credential_ciphertext, auth_type, user_id
      FROM connections WHERE token_hash = ?`).get(hash(token));
    if (!row) throw new ConnectionError(404, 'NOT_FOUND', '해당 연결 토큰을 찾을 수 없습니다.');
    return { id: row.id, project_id: row.project_id, project_name: row.project_name,
      user_id: row.user_id, auth_type: row.auth_type, credential_id: row.credential_id,
      credential_secret: unseal(row.credential_ciphertext, key) };
  }

  return {
    async register(input) {
      const submitted = credentials(input);
      if (projectId && input.project_id !== projectId) {
        throw new ConnectionError(403, 'FORBIDDEN', '운영자가 설정한 OpenStack 프로젝트와 일치하지 않습니다.');
      }
      const identity = checkedIdentity(await authenticate(submitted.type, submitted.credentialId,
        submitted.credentialSecret), input.project_id, input.user_id);
      const id = randomUUID();
      const connectionToken = randomBytes(32).toString('base64url');
      const projectName = typeof identity.project.name === 'string' ? identity.project.name : identity.project.id;
      db.prepare(`INSERT INTO connections
        (id, token_hash, project_id, project_name, credential_id, credential_ciphertext, created_at, auth_type, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(id, hash(connectionToken), identity.project.id,
        projectName, submitted.credentialId, seal(submitted.credentialSecret, key),
        new Date().toISOString(), submitted.type, identity.user.id);
      return { id, provider: 'openstack', project_id: identity.project.id, project_name: projectName,
        user_id: identity.user.id, auth_type: submitted.type, connection_token: connectionToken };
    },
    resolve,
    async verify(token) {
      const connection = resolve(token);
      const identity = checkedIdentity(await authenticate(connection.auth_type, connection.credential_id,
        connection.credential_secret), connection.project_id, connection.user_id);
      return { id: connection.id, project_id: identity.project.id, project_name: connection.project_name,
        user_id: identity.user.id };
    },
    close() { db.close(); },
  };
}

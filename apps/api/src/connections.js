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

function keystoneBaseUrl(authUrl) {
  try {
    const url = new URL(authUrl);
    const isV3 = /^\/v3\/?$/.test(url.pathname);
    if (url.protocol === 'https:' && isV3 && !url.username && !url.password && !url.search && !url.hash) {
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

function selectProject(projects, projectId) {
  const matches = Array.isArray(projects)
    ? projects.filter((project) => typeof project?.id === 'string' && project.id && (!projectId || project.id === projectId))
    : [];
  if (matches.length === 1 && (projectId || projects.length === 1)) return matches[0];
  throw invalid(projectId
    ? '토큰으로 운영자가 설정한 OpenStack 프로젝트에 접근할 수 없습니다.'
    : '접근 가능한 OpenStack 프로젝트가 정확히 하나여야 합니다.');
}

function seal(secret, key) {
  const nonce = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, nonce);
  const ciphertext = Buffer.concat([cipher.update(secret, 'utf8'), cipher.final()]);
  return Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString('base64');
}

function unseal(value, key) {
  const data = Buffer.from(value, 'base64');
  const decipher = createDecipheriv('aes-256-gcm', key, data.subarray(0, 12));
  decipher.setAuthTag(data.subarray(12, 28));
  return Buffer.concat([decipher.update(data.subarray(28)), decipher.final()]).toString('utf8');
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
    created_at TEXT NOT NULL
  )`);

  async function keystone(suffix, { method = 'GET', token, subject, body } = {}) {
    let response;
    try {
      response = await fetcher(`${baseUrl}${suffix}`, {
        method,
        redirect: 'error',
        signal: AbortSignal.timeout(15000),
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
        throw new ConnectionError(422, 'INVALID_OPENSTACK_TOKEN', 'OpenStack 토큰이 유효하지 않거나 필요한 권한이 없습니다.');
      }
      throw upstream();
    }

    try {
      return { value: await response.json(), subject: response.headers.get('x-subject-token') };
    } catch { throw upstream(); }
  }

  function resolve(token) {
    if (typeof token !== 'string' || !CONNECTION_TOKEN.test(token)) {
      throw invalid('기존 연결 토큰을 확인하세요.');
    }
    const row = db.prepare(`SELECT id, project_id, project_name, credential_id, credential_ciphertext
      FROM connections WHERE token_hash = ?`).get(hash(token));
    if (!row) throw new ConnectionError(404, 'NOT_FOUND', '해당 연결 토큰을 찾을 수 없습니다.');
    return {
      id: row.id,
      project_id: row.project_id,
      project_name: row.project_name,
      credential_id: row.credential_id,
      credential_secret: unseal(row.credential_ciphertext, key),
    };
  }

  return {
    async register(input) {
      const tokenIsValid = typeof input?.unscoped_token === 'string'
        && Boolean(input.unscoped_token.trim())
        && input.unscoped_token.length <= 8192
        && !/[\r\n]/.test(input.unscoped_token);
      if (!input || Object.keys(input).length !== 1 || !tokenIsValid) {
        throw invalid('OpenStack unscoped 토큰 하나만 입력하세요.');
      }

      const token = input.unscoped_token;
      // Validate the token itself before using it to discover project scopes.
      const inspected = await keystone('/auth/tokens', { method: 'GET', token, subject: token });
      const identity = inspected.value?.token;
      if (!identity?.user?.id || identity.project || identity.domain || identity.system) {
        throw invalid('프로젝트 범위가 없는 unscoped 토큰만 입력하세요.');
      }

      const projects = (await keystone('/auth/projects', { token })).value.projects;
      const project = selectProject(projects, projectId);
      const scoped = await keystone('/auth/tokens', {
        method: 'POST',
        body: {
          auth: {
            identity: { methods: ['token'], token: { id: token } },
            scope: { project: { id: project.id } },
          },
        },
      });
      const scopedIdentity = scoped.value?.token;
      if (!scoped.subject || scopedIdentity?.project?.id !== project.id
          || !scopedIdentity.user?.id || !scopedIdentity.roles?.length) {
        throw upstream();
      }

      const credentialResponse = await keystone(`/users/${encodeURIComponent(scopedIdentity.user.id)}/application_credentials`, {
        method: 'POST',
        token: scoped.subject,
        body: {
          application_credential: {
            name: `railshot-${randomUUID()}`,
            description: 'RailShot on-premises deployment',
          },
        },
      });
      const credential = credentialResponse.value.application_credential;
      if (typeof credential?.id !== 'string' || typeof credential?.secret !== 'string'
          || !credential.id || !credential.secret) {
        throw upstream();
      }

      const id = randomUUID();
      const connectionToken = randomBytes(32).toString('base64url');
      const projectName = typeof project.name === 'string' ? project.name : project.id;
      db.prepare('INSERT INTO connections VALUES (?, ?, ?, ?, ?, ?, ?)').run(
        id,
        hash(connectionToken),
        project.id,
        projectName,
        credential.id,
        seal(credential.secret, key),
        new Date().toISOString(),
      );

      return { id, provider: 'openstack', project_name: projectName, connection_token: connectionToken };
    },
    resolve,
    async verify(token) {
      const connection = resolve(token);
      const authenticated = await keystone('/auth/tokens', {
        method: 'POST',
        body: {
          auth: {
            identity: {
              methods: ['application_credential'],
              application_credential: {
                id: connection.credential_id,
                secret: connection.credential_secret,
              },
            },
          },
        },
      });
      const scopedIdentity = authenticated.value?.token;
      if (!authenticated.subject || scopedIdentity?.project?.id !== connection.project_id
          || !scopedIdentity.roles?.length) {
        throw upstream();
      }
      return {
        id: connection.id,
        project_id: connection.project_id,
        project_name: connection.project_name,
      };
    },
    close() {
      db.close();
    },
  };
}

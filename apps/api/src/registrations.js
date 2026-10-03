import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { DashboardError } from './sessions.js';

export const LINK_TOKEN_LIFETIME_MS = 10 * 60 * 1000;
const hash = (value) => createHash('sha256').update(value).digest('hex');
const missing = () => new DashboardError('등록 요청을 찾을 수 없습니다.', 404, 'NOT_FOUND');
const visible = (row) => ({ id: row.id, provider: row.provider, project_id: row.project_id,
  user_id: row.user_id, auth_type: row.auth_type, status: row.status,
  created_at: row.created_at, claimed_at: row.claimed_at });

export function createRegistrations(db) {
  function owned(id, sessionId) {
    const row = db.prepare('SELECT * FROM registrations WHERE id = ? AND session_id = ?').get(id, sessionId);
    if (!row) throw missing();
    return row;
  }
  return {
    create(sessionId, input) {
      if (!sessionId || !db.prepare('SELECT 1 FROM sessions WHERE id = ? AND expires_at > ?').get(sessionId, Date.now())) {
        throw new DashboardError('세션이 만료되었습니다. 화면을 새로고침하세요.', 409, 'SESSION_EXPIRED');
      }
      if (!input || typeof input !== 'object' || Array.isArray(input)
        || Object.keys(input).some((key) => !['provider', 'project_id', 'user_id', 'auth_type'].includes(key))
        || input.provider !== 'openstack'
        || !['project_id', 'user_id'].every((key) => typeof input[key] === 'string' && /^[A-Za-z0-9._-]{1,255}$/.test(input[key]))
        || !['token', 'application_credential'].includes(input.auth_type)) {
        throw new DashboardError('OpenStack 등록 정보의 ID와 인증 형식을 확인하세요. 인증 비밀값은 서버에 보내지 마세요.');
      }
      if (db.prepare('SELECT count(*) AS count FROM registrations WHERE session_id = ?').get(sessionId).count >= 20) {
        throw new DashboardError('등록 요청은 세션당 20개까지 저장할 수 있습니다.', 409, 'CAPACITY_EXCEEDED');
      }
      const record = { id: randomUUID(), session_id: sessionId, provider: 'openstack',
        project_id: input.project_id, user_id: input.user_id, auth_type: input.auth_type,
        status: 'pending', created_at: new Date().toISOString(), claimed_at: null };
      db.prepare('INSERT INTO registrations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
        .run(record.id, sessionId, record.provider, record.project_id, record.user_id, record.auth_type,
          record.status, record.created_at, record.claimed_at);
      return visible(record);
    },
    list(sessionId) {
      return db.prepare('SELECT * FROM registrations WHERE session_id = ? ORDER BY created_at DESC, id DESC')
        .all(sessionId).map(visible);
    },
    get(sessionId, id) { return visible(owned(id, sessionId)); },
    issue(sessionId, id) {
      const row = owned(id, sessionId);
      if (row.status !== 'pending') throw new DashboardError('이미 연계된 등록 요청입니다.', 409, 'CONFLICT');
      const now = Date.now();
      const previous = db.prepare('SELECT expires_at, consumed_at FROM registration_tokens WHERE registration_id = ?').get(id);
      if (previous && !previous.consumed_at && previous.expires_at > now) {
        throw new DashboardError('유효한 연계 토큰이 이미 발급되었습니다. 만료 후 재발급하세요.', 409, 'TOKEN_ACTIVE');
      }
      const token = `rsl_${randomBytes(32).toString('base64url')}`;
      const expiresAt = now + LINK_TOKEN_LIFETIME_MS;
      db.prepare(`INSERT INTO registration_tokens VALUES (?, ?, ?, NULL, ?)
        ON CONFLICT(registration_id) DO UPDATE SET token_hash=excluded.token_hash,
        expires_at=excluded.expires_at, consumed_at=NULL, created_at=excluded.created_at`)
        .run(id, hash(token), expiresAt, now);
      return { registration_id: id, token, expires_at: new Date(expiresAt).toISOString() };
    },
    // The customer-side enrollment flow can call this after the WireGuard handshake is implemented.
    // A token is consumed atomically; a second claim never succeeds.
    claim(token) {
      if (typeof token !== 'string' || !/^rsl_[A-Za-z0-9_-]{43}$/.test(token)) return null;
      const now = Date.now();
      db.exec('BEGIN IMMEDIATE');
      try {
        const row = db.prepare(`SELECT r.* FROM registration_tokens t JOIN registrations r ON r.id = t.registration_id
          WHERE t.token_hash = ? AND t.expires_at > ? AND t.consumed_at IS NULL AND r.status = 'pending'`)
          .get(hash(token), now);
        if (!row) { db.exec('ROLLBACK'); return null; }
        db.prepare('UPDATE registration_tokens SET consumed_at = ? WHERE registration_id = ?').run(now, row.id);
        db.prepare("UPDATE registrations SET status = 'claimed', claimed_at = ? WHERE id = ?")
          .run(new Date(now).toISOString(), row.id);
        db.exec('COMMIT');
        return visible({ ...row, status: 'claimed', claimed_at: new Date(now).toISOString() });
      } catch (error) { db.exec('ROLLBACK'); throw error; }
    },
  };
}

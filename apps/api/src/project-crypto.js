import { createCipheriv, createDecipheriv, createHmac, randomBytes } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { privateJson } from './environments.js';

const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;

// This key is explicitly provisioned by the operator outside runtime/state lifetime.
// No generated fallback, default secret, or key material is persisted in the database.
export async function createProjectCipher({ keyFile, stateDirectory }) {
  if (!keyFile) return null;
  const keyPath = await realpath(keyFile), statePath = await realpath(stateDirectory);
  if (keyPath === statePath || keyPath.startsWith(statePath + sep)) throw new Error('External project key required');
  const config = await privateJson(keyFile);
  if (config.version !== 1 || !/^[A-Za-z0-9._-]{1,80}$/.test(config.active_key_id || '')
      || !config.keys || Array.isArray(config.keys)) throw new Error('Invalid project key configuration');
  const keys = new Map(Object.entries(config.keys).map(([id, value]) => {
    if (!/^[A-Za-z0-9._-]{1,80}$/.test(id) || typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw new Error('Invalid project key');
    return [id, Buffer.from(value, 'hex')];
  }));
  if (!keys.has(config.active_key_id)) throw new Error('Active project key absent');
  const encrypt = (key, value, context) => {
    const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv);
    cipher.setAAD(Buffer.from(context));
    const encrypted = Buffer.concat([cipher.update(value), cipher.final()]);
    return { iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64'), data: encrypted.toString('base64') };
  };
  const decrypt = (key, box, context) => {
    const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(box.iv, 'base64'));
    decipher.setAAD(Buffer.from(context)); decipher.setAuthTag(Buffer.from(box.tag, 'base64'));
    return Buffer.concat([decipher.update(Buffer.from(box.data, 'base64')), decipher.final()]);
  };
  return {
    seal(value, context) {
      const key = randomBytes(32), key_id = config.active_key_id;
      try { return { version: 1, key_id, wrapped_key: encrypt(keys.get(key_id), key, 'key:' + context), payload: encrypt(key, Buffer.from(value), context) }; }
      finally { key.fill(0); }
    },
    open(box, context) {
      if (box?.version !== 1 || !keys.has(box.key_id)) throw new Error('Project decryption unavailable');
      const key = decrypt(keys.get(box.key_id), box.wrapped_key, 'key:' + context);
      try { return decrypt(key, box.payload, context).toString('utf8'); } finally { key.fill(0); }
    },
    fingerprint(input, keyId = config.active_key_id) {
      if (!keys.has(keyId)) throw new Error('Project key unavailable');
      return { key_id: keyId, digest: createHmac('sha256', keys.get(keyId)).update(JSON.stringify(canonical(input))).digest('hex') };
    },
  };
}

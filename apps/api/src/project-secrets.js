import { lstatSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { unlink, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { EnvironmentError, privateJson, privateDirectory, savePrivate, runEnvironmentCommand } from './environments.js';
const script = fileURLToPath(new URL('../../../deployment/scripts/secrets_delivery.py', import.meta.url));
const hash = v => createHash('sha256').update(JSON.stringify(v)).digest('hex');
export async function createSecretsAdapter({ configPath, stateDirectory, python = 'python3', runner = runEnvironmentCommand }) {
  const config = await privateJson(configPath), fingerprint = hash(config);
  if (config.version !== 1 || !config.environments || Array.isArray(config.environments)) throw new EnvironmentError('SECRETS_CONFIGURATION_INVALID', 503);
  await privateDirectory(stateDirectory);
  // Cleanup runs only after the product store has acquired its exclusive owner lock.
  async function cleanup() {
    for (const name of await readdir(stateDirectory)) if (/^[a-f0-9-]{36}\.(prepare|apply|verify)\.json(?:\.[a-f0-9-]{36}\.tmp)?$/.test(name)) await unlink(join(stateDirectory, name));
  }
  function registeredEnvironment(id) {
    if (Object.hasOwn(config.environments, id)) return configPath;
    if (typeof config.environment_registry_dir !== 'string' || !config.environment_registry_dir.startsWith('/')) return false;
    try {
      const privateFile = path => { const info = lstatSync(path); if (!info.isFile() || info.uid !== process.getuid() || info.mode & 0o077 || info.size > 1048576) throw new Error(); return readFileSync(path); };
      for (const name of readdirSync(config.environment_registry_dir)) {
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) continue;
        const home = join(config.environment_registry_dir, name), info = lstatSync(home);
        if (!info.isDirectory() || info.uid !== process.getuid() || info.mode & 0o077) continue;
        try {
          const receipt = JSON.parse(privateFile(join(home, 'secrets-registration.json')));
          if (receipt.version !== 1 || receipt.status !== 'succeeded' || receipt.environment_id !== id || receipt.environment_operation_id !== name) continue;
          if (receipt.registration_file !== join(home, 'registration.json') || receipt.delivery_config_file !== join(home, 'secrets-delivery.json')) continue;
          const registrationBytes = privateFile(receipt.registration_file), deliveryBytes = privateFile(receipt.delivery_config_file);
          if (createHash('sha256').update(registrationBytes).digest('hex') !== receipt.registration_sha256 || createHash('sha256').update(deliveryBytes).digest('hex') !== receipt.delivery_config_sha256) continue;
          const registration = JSON.parse(registrationBytes), delivery = JSON.parse(deliveryBytes);
          if (registration.status === 'succeeded' && registration.target_id === id && registration.environment_id === name && delivery.environment_id === id) return receipt.delivery_config_file;
        } catch { /* Incomplete registrations never advertise capability. */ }
      }
    } catch { return false; }
    return false;
  }
  return {
    cleanup,
    supports: id => Boolean(registeredEnvironment(id)),
    async deliver(input) {
      if (!/^[a-f0-9-]{36}$/.test(input.operation_id || '') || !['prepare', 'apply', 'verify'].includes(input.phase)) throw new EnvironmentError('SECRETS_INPUT_INVALID', 422);
      if (hash(await privateJson(configPath)) !== fingerprint) throw new EnvironmentError('SECRETS_POLICY_CHANGED', 409);
      const selectedConfig = registeredEnvironment(input.environment_id);
      if (!selectedConfig) throw new EnvironmentError('SECRETS_NOT_CONFIGURED', 409);
      const request = join(stateDirectory, input.operation_id + '.' + input.phase + '.json');
      try {
        await savePrivate(request, { version: 1, ...input });
        const result = await runner(python, [script, '--config', selectedConfig, '--request', request], { mutation: input.phase !== 'verify', timeout: 1800000 });
        for (const field of ['operation_id', 'project_id', 'binding_id', 'revision_id']) if (result[field] !== input[field]) throw new EnvironmentError('SECRETS_RECEIPT_INVALID', 502, true);
        const refs = result.configuration;
        if (refs) {
          const fields = ['project_id', 'binding_id', 'revision_id', 'namespace', 'configmap_name', 'secret_name', 'external_secret_name', 'plain_names', 'secret_names'];
          if (Object.keys(refs).some(key => !fields.includes(key)) || ['project_id', 'binding_id', 'revision_id'].some(key => refs[key] !== input[key])
              || ['namespace', 'configmap_name', 'secret_name', 'external_secret_name'].some(key => typeof refs[key] !== 'string' || !/^[a-z0-9][a-z0-9.-]{0,252}$/.test(refs[key]))
              || ['plain_names', 'secret_names'].some(key => !Array.isArray(refs[key]) || refs[key].some(name => !/^[A-Z_][A-Z0-9_]{0,127}$/.test(name)))) throw new EnvironmentError('SECRETS_RECEIPT_INVALID', 502, true);
        }
        // Explicitly project receipts; unexpected executor fields cannot leak value material.
        return { status: result.status, observed_revision_id: result.observed_revision_id,
          checks: { synchronized: result.checks?.synchronized === true, workload_ready: result.checks?.workload_ready === true, service_ready: result.checks?.service_ready === true },
          ...(result.configuration ? { configuration: result.configuration } : {}) };
      } finally { await unlink(request).catch(() => {}); }
    },
  };
}

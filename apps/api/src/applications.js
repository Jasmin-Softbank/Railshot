import { createHash } from 'node:crypto';
import { join, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { lstat } from 'node:fs/promises';
import { APP_NAME, TARGET_ID, TENANT_NAME } from './contract.js';
import { EnvironmentError, privateJson, privateDirectory, savePrivate, runEnvironmentCommand } from './environments.js';
import { createCdAdapter } from './cd.js';
import { createAppLogsObserver } from './logs.js';

const REGISTER = fileURLToPath(new URL('../../../deployment/scripts/applications.py', import.meta.url));
const FINALIZE = fileURLToPath(new URL('../../../deployment/scripts/application_routes.py', import.meta.url));
const hash = (value) => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const fail = (code, status = 409, unknown = false) => new EnvironmentError(code, status, unknown);

/** An operator registers existing environments; uploads allocate only application bindings. */
export async function createApplicationAdapter({ configPath, ciIdentity, loadPublished, python = 'python3', runner = runEnvironmentCommand }) {
  const config = await privateJson(configPath);
  if (config.version !== 1 || !isAbsolute(config.state_dir || '') || !config.environments
      || Array.isArray(config.environments) || typeof config.environments !== 'object'
      || typeof loadPublished !== 'function') throw fail('APPLICATION_CONFIGURATION_INVALID', 503);
  await privateDirectory(config.state_dir);
  const fingerprint = hash(config);
  const targets = Object.fromEntries(Object.entries(config.environments).map(([id, value]) => {
    if (!TARGET_ID.test(id) || !['aws', 'gcp', 'openstack'].includes(value.provider)
        || !TENANT_NAME.test(value.tenant || '')) throw fail('APPLICATION_CONFIGURATION_INVALID', 503);
    if (!ciIdentity || value.tenant !== ciIdentity.tenant || value.source_repository !== ciIdentity.sourceRepository)
      throw fail('APPLICATION_CI_CONFIGURATION_MISMATCH', 503);
    const routeFiles = ['edge_config_file', 'dns_config_file',
      ...(value.provider === 'openstack' ? ['tunnel_config_file'] : [])];
    const automaticDelivery = routeFiles.every((key) => isAbsolute(value.ingress?.[key] || ''));
    return [id, Object.freeze({ provider: value.provider, tenant: value.tenant, deploymentScope: 'environment', automaticDelivery })];
  }));
  function describe(environmentId, app) {
    const environment = Object.hasOwn(targets, environmentId) ? targets[environmentId] : null;
    if (!environment || typeof app !== 'string' || !APP_NAME.test(app)) throw fail('APPLICATION_INPUT_INVALID', 422);
    const id = 'app-' + hash([environmentId, environment.tenant, app]).slice(0, 24);
    return { id, app, target_id: id, environment_target_id: environmentId, provider: environment.provider };
  }
  async function current(application) {
    if (hash(await privateJson(configPath)) !== fingerprint) throw fail('APPLICATION_POLICY_CHANGED');
    const expected = describe(application.environment_target_id, application.app);
    if (expected.id !== application.id || expected.target_id !== application.target_id) throw fail('APPLICATION_BINDING_MISMATCH');
    return join(config.state_dir, expected.id);
  }
  function resultStatus(result, application) {
    if (result?.application_id !== application.id || result.environment_id !== application.environment_target_id
        || result.target_id !== application.target_id || result.app !== application.app) throw fail('APPLICATION_BINDING_MISMATCH', 502, true);
    return result;
  }
  return {
    targets: Object.freeze(targets), describe,
    async register(application) {
      const home = await current(application);
      await privateDirectory(home);
      const request = join(home, 'request.json');
      await savePrivate(request, { environment_id: application.environment_target_id, app: application.app, application_id: application.id });
      const result = resultStatus(await runner(python, [REGISTER, '--config', configPath, '--request', request],
        { mutation: true, timeout: 600_000 }), application);
      if (result.status !== 'succeeded') throw fail(result.error?.code || 'APPLICATION_REGISTRATION_FAILED', 502, result.status === 'unknown');
      return { ...application, status: 'ready', namespace: result.namespace, node_port: result.node_port,
        hostname: result.hostname, registered_at: new Date().toISOString() };
    },
    async deployPublished(application, args) {
      const home = await current(application);
      if (!/^[a-f0-9-]{36}$/.test(args.deploymentId) || args.app !== application.app || args.targetId !== application.target_id
          || args.publication?.app !== args.app || args.publication?.target_id !== args.targetId
          || args.publication?.tenant !== targets[application.environment_target_id].tenant
          || args.publication?.source_commit !== args.sourceCommit) throw fail('APPLICATION_PUBLICATION_MISMATCH');
      const run = join(home, 'deployments', args.deploymentId);
      await privateDirectory(run);
      const files = await loadPublished(args.publication);
      const request = join(run, 'publication.json');
      const payload = { deployment_id: args.deploymentId, application_id: application.id,
        environment_id: application.environment_target_id, publication: args.publication,
        files: Object.fromEntries(files.map(({ path, content }) => [path, content.toString('base64')])) };
      let exists;
      try { exists = await lstat(request); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (exists) {
        if (hash(await privateJson(request, { maxBytes: 14_000_000 })) !== hash(payload)) throw fail('APPLICATION_PUBLICATION_MISMATCH');
      } else await savePrivate(request, payload);
      const result = resultStatus(await runner(python, [FINALIZE, '--config', configPath, '--request', request],
        { mutation: true, timeout: 1_800_000 }), application);
      if (result.status !== 'succeeded') throw fail(result.error?.code || 'APPLICATION_PUBLIC_ROUTE_UNVERIFIED', 502, result.status === 'unknown');
      const deploy = createCdAdapter({ configPath: join(run, 'cd.json'), loadPublished, python });
      return deploy(args);
    },
    async observeLogs(application, record) {
      const home = await current(application);
      if (!/^[a-f0-9-]{36}$/.test(record.id)) throw fail('APPLICATION_BINDING_MISMATCH');
      return createAppLogsObserver({ configPath: join(home, 'deployments', record.id, 'cd.json'), python })(record);
    },
  };
}

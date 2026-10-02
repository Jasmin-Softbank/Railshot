import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { lstat, mkdir, open, readFile, realpath, rename } from 'node:fs/promises';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = fileURLToPath(new URL('../../../', import.meta.url));
const PROVISION = join(REPO, 'infrastructure/providers/terraform_tools/provision.py');
const ANSIBLE = join(REPO, 'infrastructure/ansible/run.py');
const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SHA = /^[a-f0-9]{64}$/;
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const canonical = (value) => Array.isArray(value) ? value.map(canonical) : object(value)
  ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value;
const hash = (value) => createHash('sha256').update(JSON.stringify(canonical(value))).digest('hex');
const validId = (value) => typeof value === 'string' && ID.test(value);
const exact = (value, required, optional = []) => object(value) && required.every((key) => key in value)
  && Object.keys(value).every((key) => [...required, ...optional].includes(key));

export class EnvironmentError extends Error {
  constructor(code, status = 400, outcomeUnknown = false) {
    super(code); this.code = code; this.status = status; this.outcomeUnknown = outcomeUnknown;
  }
}

async function privateJson(path) {
  try {
    const info = await lstat(path);
    if (!isAbsolute(path) || !info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077)
        || info.size > 1024 * 1024) throw new Error();
    return JSON.parse(await readFile(path, 'utf8'));
  } catch { throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503); }
}

async function privateDirectory(path) {
  if (!isAbsolute(path)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)
      || await realpath(path) !== resolve(path)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
}

async function savePrivate(path, value) {
  const temporary = `${path}.${randomUUID()}.tmp`;
  const file = await open(temporary, 'wx', 0o600);
  try { await file.writeFile(JSON.stringify(value)); await file.sync(); } finally { await file.close(); }
  await rename(temporary, path);
  const directory = await open(dirname(path), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

// Fixed scripts and argv only. Bound the process group so a timeout cannot leave Terraform running unseen.
export function runEnvironmentCommand(python, args, { timeout = 120000, mutation = false } = {}) {
  return new Promise((resolveCommand, reject) => {
    const child = spawn(python, args, { cwd: REPO, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', interrupted = false;
    const stop = () => { interrupted = true; try { process.kill(-child.pid, 'SIGKILL'); } catch {} };
    const timer = setTimeout(stop, timeout);
    const collect = (which, chunk) => {
      if (which === 'stdout') stdout += chunk; else stderr += chunk;
      if (Buffer.byteLength(stdout) + Buffer.byteLength(stderr) > 1024 * 1024) stop();
    };
    child.stdout.on('data', (chunk) => collect('stdout', chunk));
    child.stderr.on('data', (chunk) => collect('stderr', chunk));
    child.on('error', () => { clearTimeout(timer); reject(new EnvironmentError('EXECUTOR_UNAVAILABLE', 503)); });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (interrupted) { reject(new EnvironmentError('EXECUTOR_OUTCOME_UNKNOWN', 503, mutation)); return; }
      let result;
      try { result = JSON.parse(stdout || stderr); } catch {
        reject(new EnvironmentError('EXECUTOR_RESPONSE_INVALID', 502, mutation)); return;
      }
      if (code !== 0) {
        const error = result?.error;
        const unknown = error?.outcome_unknown === true || error?.outcome === 'UNKNOWN'
          || (mutation && !object(error))
          || (mutation && ['possible', 'unknown', 'completed'].includes(error?.side_effect));
        reject(new EnvironmentError(/^[A-Z][A-Z0-9_]{1,95}$/.test(error?.code) ? error.code : 'EXECUTOR_FAILED',
          error?.outcome === 'BLOCKED' || result.status === 'blocked' ? 409 : 502, unknown)); return;
      }
      resolveCommand(result);
    });
  });
}

/** Operator profiles authorize one fixed runtime target each; HTTP never supplies paths, SSH or provider variables. */
export async function createEnvironmentAdapter({ profilesFile, stateDir, python = 'python3', runner = runEnvironmentCommand,
  now = () => Date.now() } = {}) {
  if (!profilesFile) return {
    profiles: () => [],
    plan: async () => { throw new EnvironmentError('PROFILES_NOT_CONFIGURED', 503); },
    verifyPlan: async () => { throw new EnvironmentError('PROFILES_NOT_CONFIGURED', 503); },
    execute: async () => { throw new EnvironmentError('PROFILES_NOT_CONFIGURED', 503); },
  };
  await privateDirectory(stateDir);
  if (resolve(stateDir).startsWith(resolve(REPO) + '/')) throw new EnvironmentError('PRIVATE_STATE_OUTSIDE_REPOSITORY_REQUIRED', 503);

  async function configuration() {
    const config = await privateJson(profilesFile);
    if (!exact(config, ['version', 'policy_revision', 'profiles']) || config.version !== 1
        || typeof config.policy_revision !== 'string' || !ID.test(config.policy_revision)
        || !Array.isArray(config.profiles) || config.profiles.length > 100)
      throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
    const ids = new Set(), targets = new Set();
    for (const profile of config.profiles) {
      if (!exact(profile, ['id', 'label', 'provider', 'site', 'purposes', 'target_file', 'state_root', 'ssh', 'allow_apply'], ['timeout_seconds'])
          || !validId(profile.id) || ids.has(profile.id) || typeof profile.label !== 'string' || !profile.label.trim()
          || typeof profile.site !== 'string' || !profile.site || !Array.isArray(profile.purposes)
          || profile.purposes.some((purpose) => purpose !== 'runtime') || typeof profile.allow_apply !== 'boolean'
          || typeof profile.target_file !== 'string' || !isAbsolute(profile.target_file)
          || typeof profile.state_root !== 'string' || !isAbsolute(profile.state_root)
          || !exact(profile.ssh, ['user', 'identity_file', 'known_hosts_file'])
          || !/^[a-z_][a-z0-9_-]{0,31}$/.test(profile.ssh.user)
          || ![profile.ssh.identity_file, profile.ssh.known_hosts_file].every((path) => typeof path === 'string' && /^\/[A-Za-z0-9_./-]+$/.test(path) && !path.split('/').includes('..'))
          || !Number.isInteger(profile.timeout_seconds ?? 1200) || (profile.timeout_seconds ?? 1200) < 30 || (profile.timeout_seconds ?? 1200) > 1800)
        throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      const target = await privateJson(profile.target_file);
      if (target.provider_kind !== profile.provider || typeof target.target_id !== 'string' || !/^[a-z][a-z0-9-]{2,39}$/.test(target.target_id)
          || targets.has(target.target_id)) throw new EnvironmentError('ENVIRONMENT_CONFIGURATION_INVALID', 503);
      ids.add(profile.id); targets.add(target.target_id);
      profile.target = target;
    }
    return config;
  }

  function blockersFor(profile) {
    const blockers = [];
    if (!['aws', 'gcp'].includes(profile.provider)) blockers.push('PROVIDER_RUNTIME_UNSUPPORTED');
    if (!profile.purposes.includes('runtime')) blockers.push('PROFILE_PURPOSE_UNSUPPORTED');
    if (!profile.allow_apply) blockers.push('PROFILE_EXECUTION_DISABLED');
    return blockers;
  }
  // Reading current profiles also makes operator policy revocation effective before dispatch.
  const initial = await configuration();
  let listed = initial;
  return {
    profiles: () => listed.profiles.map((profile) => ({
      ...Object.fromEntries(['id', 'label', 'provider', 'site', 'purposes'].map((key) => [key, profile[key]])),
      supported: blockersFor(profile).length === 0, blockers: blockersFor(profile),
    })),

    async plan(input, { id }) {
      if (!validId(id) || !exact(input, ['name', 'runtime', 'database']) || typeof input.name !== 'string'
          || !/^[a-z][a-z0-9-]{2,62}$/.test(input.name)
          || !exact(input.runtime, ['profile_id', 'node_count']) || typeof input.runtime.profile_id !== 'string'
          || !Number.isInteger(input.runtime.node_count) || input.runtime.node_count < 1 || input.runtime.node_count > 64
          || !exact(input.database, ['mode'], ['placements']) || !['none', 'standalone', 'patroni'].includes(input.database.mode)
          || ('placements' in input.database && (!Array.isArray(input.database.placements) || input.database.placements.length > 16))
          || input.database.placements?.some((placement) => !exact(placement, ['profile_id', 'database_nodes', 'dcs_voters', 'proxy_nodes'])
            || typeof placement.profile_id !== 'string' || !ID.test(placement.profile_id)
            || ['database_nodes', 'dcs_voters', 'proxy_nodes'].some((key) => !Number.isInteger(placement[key]) || placement[key] < 0 || placement[key] > 31))
          || (input.database.mode === 'none' && input.database.placements?.length))
        throw new EnvironmentError('INVALID_PLAN_REQUEST');
      listed = await configuration();
      const profile = listed.profiles.find((row) => row.id === input.runtime.profile_id);
      if (!profile) throw new EnvironmentError('PROFILE_NOT_REGISTERED', 404);
      const blockers = blockersFor(profile);
      if (input.runtime.node_count !== 1) blockers.push('SINGLE_NODE_ONLY');
      if (input.database.mode !== 'none') blockers.push('DATABASE_EXECUTION_NOT_CONNECTED');
      try {
        for (const [key, mask] of [['identity_file', 0o077], ['known_hosts_file', 0o022]]) {
          const info = await lstat(profile.ssh[key]);
          if (!info.isFile() || !info.size || info.uid !== process.getuid() || (info.mode & mask)) throw new Error();
        }
      } catch { blockers.push('SSH_REFERENCES_UNAVAILABLE'); }
      const home = join(stateDir, id);
      await mkdir(home, { mode: 0o700 }); // A plan ID and its input snapshot are never overwritten.
      const targetFile = join(home, 'target.json');
      await savePrivate(targetFile, profile.target);
      let review = null;
      if (!blockers.length) {
        try {
          review = await runner(python, [PROVISION, 'plan', '--target', targetFile, '--state-root', profile.state_root]);
          if (!SHA.test(review.plan_sha256) || review.target_id !== profile.target.target_id || !Array.isArray(review.changes))
            throw new EnvironmentError('PROVIDER_PLAN_INVALID', 502);
          // Public demo creation cannot authorize changing/deleting an existing operator resource.
          if (review.destructive || review.maintenance_required
              || review.changes.some((change) => !Array.isArray(change.actions) || change.actions.some((action) => !['create', 'read'].includes(action))))
            blockers.push('EXISTING_RESOURCE_CHANGE_REQUIRES_OPERATOR');
          if (!review.changes.some((change) => change.actions.includes('create'))) blockers.push('NO_NEW_RESOURCES');
        } catch (error) {
          blockers.push(error instanceof EnvironmentError ? error.code : 'PROVIDER_PLAN_FAILED');
        }
      }
      const snapshot = { profile, policy_revision: listed.policy_revision, target_file: targetFile, input_sha256: hash(input) };
      return {
        public: { id, ...structuredClone(input), policy_revision: listed.policy_revision,
          expires_at: new Date(now() + 15 * 60 * 1000).toISOString(), executable: blockers.length === 0,
          blockers, cost: null, steps: ['resources', 'guest', 'runtime'], plan_sha256: review?.plan_sha256 ?? null },
        private: { ...snapshot, snapshot_sha256: hash(snapshot) },
      };
    },

    async verifyPlan(record) {
      const { public: plan, private: snapshot } = record;
      if (!plan?.executable || !SHA.test(plan.plan_sha256)) throw new EnvironmentError('PLAN_NOT_EXECUTABLE', 409);
      if (!Number.isFinite(Date.parse(plan.expires_at)) || Date.parse(plan.expires_at) <= now()) throw new EnvironmentError('PLAN_EXPIRED', 409);
      const { snapshot_sha256: checksum, ...original } = snapshot;
      if (checksum !== hash(original) || hash(await privateJson(snapshot.target_file)) !== hash(snapshot.profile.target)
          || snapshot.input_sha256 !== hash({ name: plan.name, runtime: plan.runtime, database: plan.database }))
        throw new EnvironmentError('PLAN_SNAPSHOT_CHANGED', 409);
      const current = await configuration();
      const profile = current.profiles.find((row) => row.id === snapshot.profile.id);
      if (current.policy_revision !== plan.policy_revision || !profile || hash(profile) !== hash(snapshot.profile))
        throw new EnvironmentError('PROFILE_POLICY_CHANGED', 409);
      const manifest = await privateJson(join(profile.state_root, profile.target.target_id, 'plan-manifest.json'));
      if (manifest.plan_sha256 !== plan.plan_sha256) throw new EnvironmentError('SAVED_PLAN_REPLACED', 409);
      if (manifest.apply_attempted) throw new EnvironmentError('PLAN_ALREADY_EXECUTED', 409);
    },

    async execute(record, { id, onProgress = async () => {} }) {
      if (!validId(id)) throw new EnvironmentError('INVALID_ENVIRONMENT_ID');
      await this.verifyPlan(record);
      const { public: plan, private: { profile, target_file: targetFile } } = record;
      const home = join(stateDir, id);
      await mkdir(home, { mode: 0o700 });
      const state = { status: 'running', stage: 'resources', resources: { status: 'running' },
        guest: { status: 'queued' }, runtime: { status: 'queued' }, database: { status: 'skipped' },
        runtime_target_id: null, deployment_supported: false, blockers: ['DEPLOYMENT_TARGET_NOT_REGISTERED'], error: null };
      const progress = async () => onProgress(structuredClone(state));
      await progress();
      try {
        const applied = await runner(python, [PROVISION, 'apply', '--target', targetFile, '--state-root', profile.state_root,
          '--plan-sha256', plan.plan_sha256], { timeout: 30 * 60 * 1000, mutation: true });
        const descriptor = applied.node_descriptor;
        if (applied.apply_status !== 'completed' || applied.plan_sha256 !== plan.plan_sha256
            || !object(descriptor) || descriptor.target_id !== profile.target.target_id || descriptor.provider_kind !== profile.provider)
          throw new EnvironmentError('PROVIDER_RESULT_INVALID', 502, true);
        state.resources = { status: 'succeeded' }; state.stage = 'guest'; state.guest = { status: 'running' }; await progress();
        const descriptorFile = join(home, 'descriptor.json');
        await savePrivate(descriptorFile, descriptor);
        const registry = { version: 1, targets: { [descriptor.target_id]: {
          descriptor_file: descriptorFile, ssh: profile.ssh, timeout_seconds: profile.timeout_seconds ?? 1200, purpose: 'runtime',
        } } };
        // A per-environment registry is immutable for both native executions; an API restart is unnecessary.
        await savePrivate(join(home, 'targets.json'), registry);
        const registered = registry.targets[descriptor.target_id];
        const nativeArgs = (operation) => [ANSIBLE, '--node-descriptor', registered.descriptor_file,
          '--request-id', `${id}.${operation}`, '--operation', operation, '--ssh-user', registered.ssh.user,
          '--identity-file', registered.ssh.identity_file, '--known-hosts-file', registered.ssh.known_hosts_file,
          '--timeout-seconds', String(registered.timeout_seconds), '--state-dir', join(home, 'ansible')];
        const validated = await runner(python, [...nativeArgs('guest.check'), '--validate-only']);
        if (validated.status !== 'validated' || validated.target_id !== descriptor.target_id)
          throw new EnvironmentError('REGISTERED_TARGET_INVALID', 502, true);
        state.runtime_target_id = descriptor.target_id;
        for (const [stage, operation] of [['guest', 'guest.check'], ['runtime', 'runtime.install']]) {
          state.stage = stage; state[stage] = { status: 'running' }; await progress();
          if (hash(await privateJson(descriptorFile)) !== hash(descriptor)
              || hash(await privateJson(join(home, 'targets.json'))) !== hash(registry))
            throw new EnvironmentError('REGISTERED_TARGET_CHANGED', 409);
          const result = await runner(python, nativeArgs(operation), { timeout: (registered.timeout_seconds + 120) * 1000,
            mutation: operation === 'runtime.install' });
          if (result.status !== 'succeeded' || result.request_id !== `${id}.${operation}` || result.target_id !== descriptor.target_id
              || result.operation !== operation || result[`${stage}_ready`] !== true)
            throw new EnvironmentError('READINESS_RESULT_INVALID', 502, operation === 'runtime.install');
          state[stage] = { status: 'succeeded', ansible_job_id: result.request_id }; await progress();
        }
        state.status = 'succeeded'; state.stage = 'runtime'; return state;
      } catch (error) {
        state.status = error.outcomeUnknown ? 'unknown' : error.status === 409 || error.status === 503 ? 'blocked' : 'failed';
        state[state.stage] = { status: state.status };
        state.error = { code: error instanceof EnvironmentError ? error.code : 'ENVIRONMENT_OUTCOME_UNKNOWN',
          message: '환경 단계의 결과를 확인하고 서버 실행 기록을 점검하세요.', request_id: randomUUID(),
          retryable: false, outcome_unknown: error.outcomeUnknown || !(error instanceof EnvironmentError) };
        if (state.error.outcome_unknown) { state.status = 'unknown'; state[state.stage].status = 'unknown'; }
        return state;
      }
    },
  };
}

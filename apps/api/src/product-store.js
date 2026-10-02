import { open, mkdir, readFile, rename, unlink, lstat, realpath, readdir, stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';

const run = promisify(execFile);
async function processIdentity(pid) {
  if (process.platform === 'linux') {
    try {
      const value = await readFile(`/proc/${pid}/stat`, 'utf8');
      return `${(await readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim()}:${value.slice(value.lastIndexOf(')') + 2).split(' ')[19]}`;
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  try { return (await run('ps', ['-o', 'lstart=', '-p', String(pid)])).stdout.trim() || null; }
  catch (error) { if (error.code === 1) return null; throw error; }
}

// One process owns one private workspace. Atomic, fsynced records precede external writes.
export async function createProductStore(directory) {
  let root = resolve(directory);
  await mkdir(root, { recursive: true, mode: 0o700 });
  const info = await lstat(root);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('A private owned workspace directory is required');
  root = await realpath(root);
  async function readPrivate(path) {
    const file = await lstat(path);
    if (!file.isFile() || file.uid !== process.getuid() || (file.mode & 0o077)) throw new Error('Invalid private state file');
    return JSON.parse(await readFile(path, 'utf8'));
  }
  const lockPath = join(root, 'owner.json');
  const owner = { pid: process.pid, identity: await processIdentity(process.pid), nonce: randomUUID() };
  // ponytail: local PID ownership is sufficient for this single-host, single-replica demo; no distributed queue/lease.
  for (let attempt = 0; ; attempt++) {
    try {
      const lock = await open(lockPath, 'wx', 0o600);
      try { await lock.writeFile(JSON.stringify(owner)); await lock.sync(); } finally { await lock.close(); }
      break;
    } catch (error) {
      if (error.code !== 'EEXIST' || attempt > 0) throw error;
      const previous = await readPrivate(lockPath);
      if (!Number.isSafeInteger(previous.pid) || previous.pid < 1) throw new Error('Invalid workspace owner');
      const identity = await processIdentity(previous.pid);
      if (identity && (!previous.identity || identity === previous.identity)) throw new Error('Workspace is already open');
      await unlink(lockPath);
    }
  }
  let closed = false, poisoned = false;
  let snapshotBytes = 0;
  for (const name of await readdir(root)) if (name.endsWith('.source.json')) snapshotBytes += (await stat(join(root, name))).size;
  async function atomic(name, data) {
    const destination = join(root, name), temporary = `${destination}.${randomUUID()}.tmp`;
    const file = await open(temporary, 'wx', 0o600);
    try { await file.writeFile(JSON.stringify(data)); await file.sync(); }
    catch (error) { await unlink(temporary).catch(() => {}); throw error; }
    finally { await file.close(); }
    await rename(temporary, destination);
    const directoryHandle = await open(root, 'r');
    try { await directoryHandle.sync(); } finally { await directoryHandle.close(); }
  }
  let state;
  try {
    try { state = await readPrivate(join(root, 'state.json')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; state = { version: 1, operations: {}, keys: {}, bindings: {}, plans: {} }; }
    if (state.version !== 1 || !state.operations || !state.keys || !state.bindings || !state.plans) throw new Error('Invalid workspace state');
    for (const operation of Object.values(state.operations)) {
      if (['queued', 'running'].includes(operation.status)) {
        operation.status = 'unknown';
        operation.error = { code: 'INTERRUPTED', request_id: randomUUID(), message: '실행이 중단되어 결과를 다시 확인해야 합니다.', retryable: false, outcome_unknown: true };
      }
    }
    await atomic('state.json', state);
  } catch (error) { await unlink(lockPath); throw error; }
  let tail = Promise.resolve();
  return {
    read: () => structuredClone(state),
    snapshotBytes: () => snapshotBytes,
    transaction(update) {
      const pending = tail.then(async () => {
        if (closed || poisoned) throw new Error('Workspace requires recovery');
        const next = structuredClone(state);
        const result = await update(next);
        try { await atomic('state.json', next); } catch (error) { poisoned = true; throw error; }
        state = next;
        return result;
      });
      tail = pending.catch(() => {});
      return pending;
    },
    async snapshot(id, files) {
      if (!/^[a-f0-9-]{36}$/.test(id)) throw new Error('Invalid snapshot identifier');
      try {
        await atomic(`${id}.source.json`, files.map(({ path, content }) => ({ path, content: content.toString('base64') })));
        snapshotBytes += (await stat(join(root, `${id}.source.json`))).size;
      } catch (error) { poisoned = true; throw error; }
    },
    async close() {
      await tail;
      if (closed) return;
      closed = true;
      const current = await readPrivate(lockPath);
      if (current.nonce === owner.nonce) await unlink(lockPath);
    },
  };
}

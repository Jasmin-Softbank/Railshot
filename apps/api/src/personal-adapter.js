import { join } from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, lstat, writeFile, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { privateJson, privateDirectory, savePrivate, runEnvironmentCommand } from './environments.js';

const run = promisify(execFile);
const gatewayScript = fileURLToPath(new URL('../../../deployment/scripts/personal_wireguard.py', import.meta.url));
const MAX_RESPONSE = 1024 * 1024;

function sshTransport(executable, args, { input, timeout = 60000 }) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { detached: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let output = '', invalid = false;
    const stop = () => { invalid = true; if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} } };
    const timer = setTimeout(stop, timeout);
    child.stdout.on('data', (chunk) => { output += chunk.toString(); if (Buffer.byteLength(output) > MAX_RESPONSE) stop(); });
    child.stderr.resume(); // Provider stderr can contain local auth details. Never expose it.
    child.stdin.on('error', () => { invalid = true; });
    child.on('error', () => { invalid = true; });
    child.on('close', (code) => { clearTimeout(timer); if (invalid || ![0, 1].includes(code)) reject(new Error('OpenStack transport outcome unknown')); else resolve({ stdout: output, code }); });
    child.stdin.end(input);
  });
}

/** Private server adapter. Browser requests cannot choose executables, hosts, paths or credentials. */
export async function createPersonalAdapter({ configPath, stateDirectory, base, python = 'python3', runner = runEnvironmentCommand, transport = sshTransport }) {
  const config = await privateJson(configPath);
  if (config.version !== 1 || !config.gateway?.config_path?.startsWith('/')) throw new Error('Invalid personal configuration');
  for (const key of ['public_url', 'installer_url', 'artifact_url']) {
    const url = new URL(config[key]);
    if (url.protocol !== 'https:' || url.username || url.password || url.hash || url.search) throw new Error('Personal endpoints require HTTPS');
  }
  if (!/^[a-f0-9]{64}$/.test(config.artifact_sha256 || '')) throw new Error('Pinned personal artifact required');
  if (config.gateway.command && !config.gateway.command.startsWith('/')) throw new Error('Absolute privileged gateway wrapper required');
  const accessKeys = new Map();
  async function gateway(action, target) {
    const folder = join(stateDirectory, 'gateway'); await privateDirectory(folder);
    const request = join(folder, `${target.id}-${target.generation}-${action}.json`);
    await savePrivate(request, { action, target_id: target.id, generation: target.generation, public_key: target.public_key });
    const executable = config.gateway.command ? '/usr/bin/sudo' : python;
    const args = config.gateway.command ? ['-n', config.gateway.command, '--request', request] : [gatewayScript, '--config', config.gateway.config_path, '--request', request];
    return runner(executable, args, { mutation: action !== 'verify', timeout: 60000 });
  }
  async function execute(target, { job_id = randomUUID(), argv, delete_data, public_key } = {}) {
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(job_id) || !Array.isArray(argv) || !argv.length || argv.length > 64
        || argv.some((part) => typeof part !== 'string' || !part.length || part.length > 4096 || /[\x00-\x1f]/.test(part))
        || delete_data !== undefined && delete_data !== true || public_key !== undefined && typeof public_key !== 'string') throw new Error('Invalid OpenStack operation');
    const address = target.tunnel?.address?.split('/')[0], source = target.tunnel?.allowed_ips?.split('/')[0];
    if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(address || '') || !/^\d{1,3}(?:\.\d{1,3}){3}$/.test(source || '')) throw new Error('Registered gateway route required');
    const gatewayState = await gateway('verify', target);
    if (gatewayState.status !== 'succeeded' || gatewayState.reachable !== true) throw new Error('OpenStack gateway is not connected');
    const folder = join(stateDirectory, target.id), request = { version: 1, job_id, action: 'openstack.execute', params: {
      argv, ...(delete_data !== undefined ? { delete_data } : {}), ...(public_key !== undefined ? { public_key } : {}),
    } };
    const encoded = JSON.stringify(request);
    if (Buffer.byteLength(encoded) > 16384) throw new Error('OpenStack operation too large');
    const args = ['-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
      '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=10',
      '-o', `UserKnownHostsFile=${join(folder, 'known_hosts')}`, '-i', join(folder, 'runtime_ed25519'),
      '-b', source, '-p', '2222', `railshot-openstack@${address}`, 'railshot-openstack-v1'];
    const response = await transport('ssh', args, { input: encoded, timeout: 120000 });
    const raw = typeof response === 'string' ? response : response.stdout;
    const code = typeof response === 'string' ? 0 : response.code;
    if (![0, 1].includes(code) || typeof raw !== 'string') throw new Error('OpenStack transport failed');
    if (Buffer.byteLength(raw) > MAX_RESPONSE) throw new Error('OpenStack response too large');
    let result;
    try { result = JSON.parse(raw); } catch { throw new Error('OpenStack response invalid'); }
    if (result?.version !== 1 || result.job_id !== job_id || result.action !== request.action || typeof result.ok !== 'boolean' || result.ok !== (code === 0)
        || result.ok && result.error !== null || !result.ok && (!result.error || typeof result.error.code !== 'string')) throw new Error('OpenStack response binding invalid');
    return result;
  }
  return {
    config, application: base, execute,
    deploymentReady: (target) => Boolean(base?.targets?.[target.id]?.automaticDelivery),
    runtimeAccess: (target) => accessKeys.get(target.id),
    async prepare(target) {
      const folder = join(stateDirectory, target.id); await privateDirectory(folder);
      if (!/^ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}$/.test(target.runtime?.ssh_host_key || '')) throw new Error('Customer management host key missing');
      const address = target.tunnel?.address?.split('/')[0];
      if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(address || '')) throw new Error('Gateway address invalid');
      const identity = join(folder, 'runtime_ed25519'), knownHosts = join(folder, 'known_hosts');
      try { const info = await lstat(identity); if (!info.isFile() || info.uid !== process.getuid() || info.mode & 0o077) throw new Error('Private management identity invalid'); }
      catch (error) {
        if (error.code !== 'ENOENT') throw error;
        await run('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-C', `railshot:${target.id}`, '-f', identity]);
      }
      const known = `[${address}]:2222 ${target.runtime.ssh_host_key}\n`;
      try { if (await readFile(knownHosts, 'utf8') !== known) throw new Error('Management host identity changed'); }
      catch (error) { if (error.code !== 'ENOENT') throw error; await writeFile(knownHosts, known, { mode: 0o600, flag: 'wx' }); }
      accessKeys.set(target.id, { public_key: (await readFile(identity + '.pub', 'utf8')).trim() });
      // Runtime creation/configuration belongs to its existing integration. Registration does not create a cluster.
      return Boolean(base?.targets?.[target.id]?.automaticDelivery);
    },
    register: (target) => gateway('register', target),
    async verify(target) {
      const result = await gateway('verify', target);
      if (result.status !== 'succeeded' || !result.reachable) return { ...result, openstack_verified: false };
      try {
        const response = await execute(target, { argv: ['server', 'list'] });
        return { ...result, openstack_verified: response.ok && Array.isArray(response.result) };
      } catch { return { ...result, openstack_verified: false }; }
    },
    remove: async (target) => {
      const result = await gateway('remove', target);
      if (result.status === 'succeeded') {
        for (const file of ['runtime_ed25519', 'runtime_ed25519.pub', 'known_hosts']) {
          await unlink(join(stateDirectory, target.id, file)).catch((error) => { if (error.code !== 'ENOENT') throw error; });
        }
        accessKeys.delete(target.id);
      }
      return result;
    },
  };
}

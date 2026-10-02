import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const config = '/var/lib/railshot/config';
mkdirSync(config, { recursive: true, mode: 0o700 });
chmodSync(config, 0o700);
for (const name of readdirSync('/run/config').filter((value) => !value.startsWith('.'))) {
  const destination = `${config}/${name}`;
  if (existsSync(destination) && !lstatSync(destination).isFile()) throw new Error('Private config must be a regular file');
  writeFileSync(destination, readFileSync(`/run/config/${name}`), { mode: 0o600 });
  chmodSync(destination, 0o600);
}
const repository = '/var/lib/railshot/repository';
const git = (...args) => execFileSync('git', args, { stdio: ['ignore', 'pipe', 'pipe'], timeout: 120_000 }).toString().trim();
if (!existsSync(repository)) {
  git('clone', '--single-branch', '--branch', 'deployment/apps', '--no-tags',
    'https://github.com/Jasmin-Softbank/Railshot.git', repository);
  chmodSync(repository, 0o700);
}
if (!lstatSync(repository).isDirectory() || git('-C', repository, 'branch', '--show-current') !== 'deployment/apps'
    || git('-C', repository, 'remote', 'get-url', 'origin') !== 'https://github.com/Jasmin-Softbank/Railshot.git') {
  throw new Error('Registered config checkout differs');
}
git('-C', repository, 'config', 'user.name', 'Railshot product API');
git('-C', repository, 'config', 'user.email', 'railshot@users.noreply.github.com');

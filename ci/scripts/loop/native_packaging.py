"""Generate packaging for unambiguous static Vite uploads without an SDK call."""
import json
import re
from pathlib import Path

import yaml


def prepare_packaging(workspace, app_id):
    ws = Path(workspace)
    if not app_id or any((ws / name).exists() for name in (
            '.railshot/railshot.yaml', '.jasmin/jasmin.yaml', 'Dockerfile', 'go.mod')):
        return {'status': 'unchanged'}
    manifest = ws / 'package.json'
    if not manifest.is_file() or not (ws / 'index.html').is_file():
        return {'status': 'unsupported'}
    try:
        package = json.loads(manifest.read_text())
    except ValueError:
        return {'status': 'unsupported'}
    if (not isinstance(package, dict) or any(not isinstance(package.get(key, {}), dict)
            for key in ('dependencies', 'devDependencies', 'scripts'))
            or not isinstance(package.get('packageManager', ''), str)):
        return {'status': 'unsupported'}
    dependencies = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
    build = package.get('scripts', {}).get('build', '')
    # ponytail: only default dist output; custom/SSR builds keep the existing adapter path.
    if ('vite' not in dependencies or package.get('workspaces')
            or build not in ('vite build', 'tsc && vite build', 'tsc -b && vite build')):
        return {'status': 'unsupported'}
    for config in ws.glob('vite.config.*'):
        if re.search(r'\b(outDir|ssr|lib|rollupOptions)\s*:', config.read_text()):
            return {'status': 'unsupported'}
    locks = [name for name in ('package-lock.json', 'yarn.lock', 'pnpm-lock.yaml') if (ws / name).is_file()]
    if len(locks) != 1:
        return {'status': 'unsupported'}
    manager = package.get('packageManager', '')
    if locks == ['package-lock.json'] and (not manager or manager.startswith('npm@')):
        setup, install, command = '', 'npm ci', 'npm run build'
    elif locks == ['yarn.lock'] and (not manager or re.fullmatch(r'yarn@1\.[0-9]+\.[0-9]+', manager)):
        if '# yarn lockfile v1' not in (ws / 'yarn.lock').read_text()[:200]:
            return {'status': 'unsupported'}
        setup, install, command = 'RUN npm install --global --force yarn@1.22.22\n', 'yarn install --frozen-lockfile', 'yarn build'
    elif locks == ['pnpm-lock.yaml'] and re.fullmatch(r'pnpm@[0-9]+\.[0-9]+\.[0-9]+', manager):
        setup, install, command = f'RUN npm install --global {manager}\n', 'pnpm install --frozen-lockfile', 'pnpm run build'
    else:
        return {'status': 'unsupported'}
    dockerfile = (f'FROM node:24-slim AS build\nWORKDIR /app\n{setup}'
                  f'COPY package.json {locks[0]} ./\nRUN {install}\nCOPY . .\nRUN {command}\n'
                  'FROM nginxinc/nginx-unprivileged:stable-alpine\n'
                  'COPY --from=build /app/dist /usr/share/nginx/html\n'
                  'USER 65532\nEXPOSE 8080\nCMD ["nginx", "-g", "daemon off;"]\n')
    spec = {'apiVersion': 'railshot/v0', 'app': app_id, 'services': [
        {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8080, 'health': '/', 'route': '/'}]}
    (ws / '.railshot').mkdir(exist_ok=True)
    (ws / '.railshot/railshot.yaml').write_text(yaml.safe_dump(spec, sort_keys=False))
    (ws / 'Dockerfile').write_text(dockerfile)
    ignore = ws / '.dockerignore'
    original = ignore.read_text() if ignore.exists() else ''
    ignore.write_text(original + '\n.git\n.env*\nnode_modules\ndist\n')
    return {'status': 'prepared', 'profile': 'vite-static',
            'written': ['Dockerfile', '.dockerignore', '.railshot/railshot.yaml']}

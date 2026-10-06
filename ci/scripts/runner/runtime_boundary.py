"""Private runtime validation; returned bindings never contain credential values."""
import os
import hashlib
import json
import re
from pathlib import Path
import stat
import tomllib


def private_directory(path):
    """Require the current OS owner and restrict the run root before any output."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError('run directory owner mismatch')
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path.resolve()


def effective_auth_route(provider='codex', env=None):
    """Resolve the same non-secret route for invocation and resume binding."""
    env = os.environ if env is None else env
    if provider == 'codex':
        mode = env.get('RAILSHOT_AUTH_MODE', 'subscription')
        home = (env.get('RAILSHOT_CODEX_HOME') or env.get('CODEX_HOME')) if mode == 'subscription' else None
        # Keep relative paths invalid; resolving one here would silently grant it validity.
        if home and Path(home).is_absolute():
            home = str(Path(home).resolve())
        route = {'provider': provider, 'mode': mode, 'credential_home': home}
        if home and Path(home).is_absolute():
            binding = Path(home) / 'railshot-account.json'
            if binding.exists() or binding.is_symlink():
                documents = []
                for path in (binding, Path(home) / 'auth.json'):
                    info = path.lstat()
                    if (path.resolve() != path or not stat.S_ISREG(info.st_mode)
                            or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o077):
                        raise ValueError('private platform credential binding required')
                    documents.append(json.loads(path.read_bytes()))
                policy, auth = documents
                if (policy.get('version') != 1 or policy.get('owner') != 'platform'
                        or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,79}', policy.get('model', ''))
                        or not re.fullmatch(r'[a-f0-9]{32}', policy.get('rotation_id', ''))
                        or hashlib.sha256(auth['tokens']['account_id'].encode()).hexdigest() != policy.get('account_sha256')):
                    raise ValueError('platform account binding mismatch')
                config = Path(home) / 'config.toml'
                if config.exists():
                    settings = tomllib.loads(config.read_text())
                    if (settings.get('forced_login_method', 'chatgpt') != 'chatgpt'
                            or settings.get('forced_chatgpt_workspace_id', auth['tokens']['account_id']) != auth['tokens']['account_id']):
                        raise ValueError('platform account configuration mismatch')
                route.update(model=policy['model'], credential_rotation=policy['rotation_id'])
        return route
    return {'provider': provider, 'endpoint': env.get('ANTHROPIC_BASE_URL')}

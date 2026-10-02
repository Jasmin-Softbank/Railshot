#!/usr/bin/env python3
"""JSON stdin/file -> adapter -> engine -> one JSON stdout response."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from engine import DeploymentEngine
from input_adapter import InputError, parse_input
from models import DeploymentResult


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def load(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(InputError(f'invalid JSON constant: {x}')))


def main(argv=None):
    provider, environment, action = None, None, None
    try:
        parser = Parser(description='Railshot local Linux deployment runtime (provisional JSON contract 0.1)')
        parser.add_argument('action', choices=('deploy', 'verify', 'cleanup'))
        parser.add_argument('--input', default='-', help='JSON path or - for stdin')
        parser.add_argument('--all', action='store_true', help='remove managed K3s/Cilium, not just workload')
        parser.add_argument('--disposable-node', action='store_true')
        args = parser.parse_args(argv)
        action = args.action
        if (args.all or args.disposable_node) and args.action != 'cleanup':
            raise InputError('--all/--disposable-node apply only to cleanup')
        text = sys.stdin.read(1048577) if args.input == '-' else Path(args.input).read_text()
        if len(text.encode()) > 1048576:
            raise InputError('input exceeds 1 MiB')
        request = parse_input(load(text))
        provider, environment = request.context.provider, request.spec.environment_id
        result = DeploymentEngine(request.spec).execute(action, args.all, args.disposable_node)
    except (InputError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        result = DeploymentResult(error={'code': 'INVALID_INPUT', 'stage': 'INPUT_ADAPTER', 'message': str(exc),
                                         'exit_code': None, 'diagnostics': None}, states=[{'state': 'FAILED', 'timestamp': None}])
    payload = {'schema_version': '0.1', 'action': action, 'provider': provider, 'environment_id': environment, **asdict(result)}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if result.status in ('ready', 'cleaned') else 1


if __name__ == '__main__':
    sys.exit(main())

import argparse
import json
from pathlib import Path
import platform
import shutil
import sys

from .contracts import canonical
from .exports import export_run
from .pipeline import Worker, generate, qa, worker_payload
from .resource_guard import resources
from .sources import audit, prepare
from .storage import Store


def main():
    parser = argparse.ArgumentParser(prog='lumen-data')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    sources = commands.add_parser('sources').add_subparsers(dest='source_command', required=True)
    source_audit = sources.add_parser('audit')
    source_audit.add_argument('--registry', default='sources/registry.yaml')
    source_audit.add_argument('--output', required=True)
    source_prepare = sources.add_parser('prepare')
    source_prepare.add_argument('--audit', required=True)
    source_prepare.add_argument('--source', required=True)
    source_prepare.add_argument('--output', required=True)
    source_prepare.add_argument('--max-shards', type=int, default=1)
    source_prepare.add_argument('--max-rows', type=int)
    renderable = sources.add_parser('renderable')
    renderable.add_argument('--manifest', required=True)
    renderable.add_argument('--output', required=True)
    renderable.add_argument('--assets', required=True)
    capture = sources.add_parser('capture')
    capture.add_argument('--manifest', required=True)
    capture.add_argument('--output', required=True)
    capture.add_argument('--limit', required=True, type=int)
    plan = commands.add_parser('plan')
    plan.add_argument('--config', required=True)
    generate_command = commands.add_parser('generate')
    generate_command.add_argument('--config', required=True)
    generate_command.add_argument('--run', required=True)
    generate_command.add_argument('--target', type=int)
    generate_command.add_argument('--resume', action='store_true')
    for name in ('qa', 'profile', 'review'):
        command = commands.add_parser(name)
        command.add_argument('--run', required=True)
    export = commands.add_parser('export')
    export.add_argument('--run', required=True)
    export.add_argument('--output', required=True)
    export.add_argument('--format', required=True)
    export.add_argument('--strict', action='store_true')
    export.add_argument('--public-safe', action='store_true')
    export.add_argument('--tile-size', type=int)
    export.add_argument('--tile-overlap', type=int, default=128)
    export.add_argument('--tile-uncovered', choices=('error', 'original'), default='error')
    reproduce = commands.add_parser('reproduce')
    reproduce.add_argument('--run', required=True)
    reproduce.add_argument('--case', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            result = {'python': platform.python_version(), 'node': shutil.which('node'),
                      'resources': resources('.')}
        elif args.command == 'sources':
            if args.source_command == 'audit':
                result = audit(args.registry, args.output)
            elif args.source_command == 'prepare':
                result = prepare(args.audit, args.source, args.output, args.max_shards, args.max_rows)
            elif args.source_command == 'renderable':
                from .renderable import prepare_renderable
                result = prepare_renderable(args.manifest, args.output, args.assets)
            else:
                from .public_sources import capture_sources
                result = capture_sources(args.manifest, args.output, args.limit)
        elif args.command == 'plan':
            import yaml
            config = yaml.safe_load(Path(args.config).read_text())
            result = {'target': config['target'], 'attempt_budget': config['max_attempts'],
                      'variants_per_scene': config['variants_per_scene'],
                      'disk_estimate': 'requires pilot measurements', 'training': False}
        elif args.command == 'generate':
            result = generate(args.config, args.run, args.target, args.resume)
        elif args.command == 'qa':
            result = qa(args.run)
        elif args.command == 'profile':
            result = json.loads((Path(args.run) / 'profile.json').read_text())
        elif args.command == 'export':
            result = export_run(args.run, args.output, args.format, args.strict, args.public_safe,
                                args.tile_size, args.tile_overlap, args.tile_uncovered)
        elif args.command == 'review':
            from .review import build_review
            result = build_review(args.run)
        else:
            import base64
            import hashlib
            store = Store(args.run)
            row = store.db.execute('SELECT record FROM cases WHERE id=?', (args.case,)).fetchone()
            if not row:
                raise ValueError('unknown case')
            record = json.loads(row[0])
            worker = Worker(store.root / 'assets')
            try:
                replay = worker.request(worker_payload(store, record['recipe']))
                digest = hashlib.sha256(base64.b64decode(replay['current'], validate=True)).hexdigest()
                if digest != record['assets']['current']:
                    raise ValueError('reproduction hash mismatch')
                result = {'case_id': args.case, 'reproduced': True}
            finally:
                worker.close()
        print(canonical(result))
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(canonical({'error': str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

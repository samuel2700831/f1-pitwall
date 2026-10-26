"""Small local interface for exercising the Part 1 replay foundation."""
import argparse
import json
import sys
import uuid
from pathlib import Path

from .data import ROOT, load_timeline
from .replay import Replay


def main():
    parser = argparse.ArgumentParser(description='F1 Pitwall Part 1: saved, time-gated historical replay')
    parser.add_argument('--database', type=Path, default=ROOT / '.local' / 'replays.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('create', 'jump', 'status', 'step', 'events', 'context', 'remember'):
        sub = commands.add_parser(name)
        sub.add_argument('--run', required=True)
        if name in ('create', 'jump'):
            sub.add_argument('--lap', type=int, required=name == 'jump')
            sub.add_argument('--boundary', choices=('start', 'end'), default='start')
        if name in ('step', 'jump'):
            sub.add_argument('--action-id', default=None)
        if name in ('step', 'jump', 'remember'):
            sub.add_argument('--expected-version', type=int)
        if name == 'step':
            sub.add_argument('--seconds', type=float, default=60)
        if name == 'remember':
            sub.add_argument('--text', required=True)
            sub.add_argument('--memory-id')
            sub.add_argument('--kind', choices=('note', 'assumption', 'decision', 'human_override'), default='note')
        if name == 'events':
            sub.add_argument('--kind')
            sub.add_argument('--through')
            sub.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    try:
        project=json.loads((ROOT/'project.json').read_text())
        path=ROOT/'data'/project.get('data_bundle','spa-2025-demo.json')
        if not path.exists():
            raise ValueError('Download the race first with: python -m pitwall.import_race')
        replay = Replay(args.database, load_timeline(path))
        if args.command == 'create':
            result = replay.create(args.run, lap=args.lap, boundary=args.boundary)
        elif args.command == 'jump':
            version = args.expected_version
            if version is None:
                try:
                    version = replay.status(args.run)['version']
                except ValueError as error:
                    if str(error) != 'Unknown replay run':
                        raise
            result = replay.jump_to_lap(args.run, args.lap, boundary=args.boundary,
                                       action_id=args.action_id or str(uuid.uuid4()), expected_version=version)
        elif args.command == 'remember':
            version = args.expected_version
            if version is None:
                version = replay.status(args.run)['version']
            result = replay.remember(args.run, args.text, args.memory_id or str(uuid.uuid4()),
                                     version, kind=args.kind)
        elif args.command == 'context':
            result = replay.decision_context(args.run)
        elif args.command == 'step':
            version = args.expected_version
            if version is None:
                version = replay.status(args.run)['version']
            result = replay.step(args.run, args.seconds, args.action_id or str(uuid.uuid4()), version)
        elif args.command == 'events':
            if args.limit < 1:
                raise ValueError('Limit must be positive')
            result = replay.observations(args.run, through=args.through, kind=args.kind)[-args.limit:]
        else:
            result = replay.status(args.run)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, OSError) as error:
        print(f'Cannot complete this replay operation: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

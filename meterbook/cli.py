import argparse
import json
import sys
from pathlib import Path
from meterbook.service import Meterbook
from meterbook.values import loads, canonical, MeterError, reject
from meterbook.ports import files


def read_json(path):
    source = Path(path)
    if source.stat().st_size > 33554432:
        reject('input_size', 'Command input exceeds 32 MiB')
    return loads(source.read_text(encoding='utf-8'))


def replay(app, fixture):
    if not isinstance(fixture, dict) or set(fixture) != {'synthetic', 'steps'} or fixture['synthetic'] is not True:
        reject('fixture', 'Replay requires an explicitly synthetic local fixture')
    steps = fixture['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 10000:
        reject('fixture_steps', 'Fixture requires a bounded step list')
    results = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or 'request' not in step or set(step) - {'request', 'expect'}:
            reject('fixture_step', 'Fixture step has unsupported structure', index=index)
        result = app.execute(step['request'])
        for path, expected in step.get('expect', {}).items():
            actual = result
            try:
                for component in path.split('.'):
                    actual = actual[int(component)] if isinstance(actual, list) else actual[component]
            except (KeyError, IndexError, ValueError, TypeError):
                reject('fixture_path', 'Expected result path is absent', index=index, path=path)
            if actual != expected:
                reject('fixture_assertion', 'Observed business result differs', index=index, path=path, expected=expected, actual=actual)
        results.append({'index': index, 'operation': step['request']['operation'], 'assertions': len(step.get('expect', {}))})
    report = app.execute({'operation': 'check.check'})
    if not report['ok']:
        reject('fixture_integrity', 'Replay leaves inconsistent settlement state', report=report)
    return {'ok': True, 'steps': results, 'checks': report}


def parser():
    result = argparse.ArgumentParser(prog='meterbook', description='Local bilateral data-usage settlement ledger')
    result.add_argument('--database', default=':memory:')
    commands = result.add_subparsers(dest='command', required=True)
    commands.add_parser('capabilities')
    execute = commands.add_parser('execute')
    execute.add_argument('request')
    batch = commands.add_parser('batch')
    batch.add_argument('requests')
    batch.add_argument('--atomic', action='store_true')
    ingest = commands.add_parser('ingest')
    ingest.add_argument('input')
    ingest.add_argument('--format', choices=['jsonl', 'csv'], required=True)
    ingest.add_argument('--atomic', action='store_true')
    demo = commands.add_parser('replay')
    demo.add_argument('fixture')
    export = commands.add_parser('export')
    export.add_argument('invoice_id')
    export.add_argument('--format', choices=['json', 'csv'], default='json')
    commands.add_parser('check')
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        with Meterbook(arguments.database) as app:
            if arguments.command == 'capabilities':
                result = app.capabilities()
            elif arguments.command == 'execute':
                result = app.execute(read_json(arguments.request))
            elif arguments.command == 'batch':
                result = app.batch(read_json(arguments.requests), arguments.atomic)
            elif arguments.command == 'ingest':
                content = Path(arguments.input).read_text(encoding='utf-8')
                values = files.parse_csv(content) if arguments.format == 'csv' else files.parse_jsonlines(content)
                result = files.ingest(app.store, values, arguments.atomic)
            elif arguments.command == 'export':
                if arguments.format == 'csv':
                    print(files.invoice_csv(app.store, arguments.invoice_id)['csv'], end='')
                    return 0
                result = files.invoice_export(app.store, arguments.invoice_id)
            elif arguments.command == 'check':
                result = app.execute({'operation': 'check.check'})
                if not result['ok']:
                    print(canonical(result))
                    return 1
            else:
                result = replay(app, read_json(arguments.fixture))
            print(canonical(result))
            if isinstance(result, dict) and result.get('failed', 0):
                return 1
            if isinstance(result, list) and any(isinstance(row, dict) and row.get('ok') is False for row in result):
                return 1
            return 0
    except MeterError as exc:
        print(canonical({'ok': False, 'error': exc.payload()}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(canonical({'ok': False, 'error': {'code': 'io', 'message': str(exc)}}), file=sys.stderr)
        return 2

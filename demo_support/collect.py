import json
import subprocess
import sys
import tempfile
from pathlib import Path
from source import start, ROOT
sys.path.insert(0, str(ROOT))
from meterbook.service import Meterbook
from meterbook.contracts import registry, rates
from meterbook.events import receiver
from meterbook.audit import checks

MAPPING = {'contract_id': 'contract1', 'side': 'provider', 'columns': {'usage_id': 'usage_id', 'at': 'at', 'quantity': 'quantity', 'meter': 'meter', 'purpose': 'purpose', 'region': 'region'}, 'defaults': {}, 'quantity_factor': '0.1'}


def prepare(db):
    app = Meterbook(str(db), clock=lambda: '2026-11-12T00:00:00.000000Z')
    try:
        registry.create(app.store, 'contract1', 'provider-a', 'consumer-b', 'CNY', {'valid_from': '2026-01-01T00:00:00Z', 'valid_until': '2027-01-01T00:00:00Z', 'meters': ['requests'], 'purposes': ['research'], 'regions': ['CN'], 'tolerance': {'requests': '0'}})
        rates.add(app.store, 'rate-1', 'contract1', 'requests', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', {'tiers': [{'up_to': None, 'unit_price': '0.5'}]})
        app.imports.put('supplier', 0, MAPPING)
    finally:
        app.close()


def collect(db, url, session):
    return subprocess.run([sys.executable, '-m', 'meterbook', '--database', str(db), 'collect', url, '--session', session, '--mapping', 'supplier'], capture_output=True, text=True, cwd=str(ROOT))


def report(result):
    assert result.returncode != 2, result.stderr
    return json.loads(result.stdout)


server, base = start()
try:
    with tempfile.TemporaryDirectory() as temporary:
        db = Path(temporary) / 'demo.db'
        prepare(db)

        cut = collect(db, base + '/usage?cut=1', 's-cut')
        assert cut.returncode == 1
        cut = report(cut)
        assert cut['state'] == 'incomplete' and not cut['complete']
        assert 'gzip_stream_incomplete' in cut['failure']['details']['reasons']
        assert cut['posted']['received'] == 0

        bad = report(collect(db, base + '/usage?bad=1', 's-bad'))
        assert bad['state'] == 'mismatched' and not bad['complete']
        assert bad['raw']['detail_records'] == 2
        assert bad['reconciliation']['unverifiable'] is True
        assert len(bad['quarantined']) == 1 and bad['quarantined'][0]['line_no'] == 3
        assert '未知' in bad['quarantined'][0]['raw']
        assert bad['posted']['received'] == 0

        total = report(collect(db, base + '/usage?total=1', 's-total'))
        assert total['state'] == 'mismatched'
        assert total['reconciliation']['totals']['requests'] == {'declared': '4', 'observed': '3.75', 'ok': False}

        main = report(collect(db, base + '/usage', 's-main'))
        assert main['state'] == 'reconciled' and main['complete']
        assert main['raw']['observed_totals'] == {'requests': '3.75'}
        assert main['raw']['declared'] == {'count': 2, 'totals': {'requests': '3.75'}}
        assert main['posted']['received'] == 2 and main['posted']['duplicates'] == 0
        assert len(main['valid']) == 2 and main['quarantined'] == []

        retry = report(collect(db, base + '/usage', 's-retry'))
        assert retry['state'] == 'duplicate' and retry['duplicate_of'] == 's-main'
        assert retry['original']['posted']['received'] == 2

        conflict = collect(db, base + '/usage?total=1', 's-conflict')
        assert conflict.returncode == 2
        assert 'export_conflict' in conflict.stderr

        revision = report(collect(db, base + '/usage?rev=1', 's-rev'))
        assert revision['state'] == 'reconciled'
        assert revision['posted']['received'] == 2

        app = Meterbook(str(db), clock=lambda: '2026-11-12T00:00:00.000000Z')
        try:
            assert app.store.scalar('SELECT COUNT(*) FROM events') == 4
            drained = receiver.drain(app.store, 'contract1', 'provider')
            assert drained['blocked'] is None and drained['cursor'] == 4
            first = receiver.head(app.store, 'contract1', 'provider', 'u1')
            second = receiver.head(app.store, 'contract1', 'provider', 'u2')
            assert first['revision'] == 2 and first['payload']['quantity'] == '0.15'
            assert second['state'] == 'cancelled'
            assert checks.check(app.store)['ok']
        finally:
            app.close()
finally:
    server.shutdown()
    server.server_close()
print('采集入口演练通过：流式核平、故障隔离、幂等重试、同号异文冲突与后续修订行为符合约定')

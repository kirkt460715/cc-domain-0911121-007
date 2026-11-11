import copy
import io
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tests.support import Case, event, usage, definition, tariff
from meterbook.values import loads, canonical, digest, month, instant, decimal, MeterError
from meterbook.cli import main, replay
from meterbook.events import receiver, reconcile, disputes, recovery
from meterbook.contracts import rates, registry
from meterbook.settlement import invoices, pricing, payments
from meterbook.ports import bundles
from meterbook.audit import verification, checks


class Boundaries(Case):
    def test_strict_decoding_and_time(self):
        self.error('duplicate_key', loads, '{"x":1,"x":2}')
        self.error('nonfinite', loads, '{"x":NaN}')
        self.error('json_decode', loads, '{')
        self.error('time', instant, '2026-11-11T10:00:00')
        self.error('period', month, '2026-13')
        self.error('period', month, '1999-12')
        self.assertEqual(month('2026-01'), '2026-01')
        self.assertEqual(instant('2026-11-11T08:00:00+08:00'), '2026-11-11T00:00:00.000000Z')
        self.assertEqual(loads(canonical({'中': ['文', 3]})), {'中': ['文', 3]})
        self.assertEqual(str(decimal('0.000001')), '0.000001')

    def test_rate_schema_rejects_unbounded_middle_and_caps(self):
        self.error('rate_infinity', rates.normalize, {'tiers': [{'up_to': None, 'unit_price': '1'}, {'up_to': '10', 'unit_price': '1'}]})
        self.error('rate_unbounded', rates.normalize, {'tiers': [{'up_to': '10', 'unit_price': '1'}]})
        self.error('rate_order', rates.normalize, {'tiers': [{'up_to': '10', 'unit_price': '1'}, {'up_to': '10', 'unit_price': '2'}, {'up_to': None, 'unit_price': '1'}]})
        self.error('rate_caps', rates.normalize, tariff(minimum_charge='5', maximum_charge='4'))
        self.error('decimal_range', rates.normalize, tariff('1.000000001'))
        self.error('rate_coverage', rates.add, self.store, 'outside', 'contract-a', 'records', '2025-12-01T00:00:00Z', '2026-01-02T00:00:00Z', tariff())
        self.assertEqual(len(rates.listing(self.store, 'contract-a')), 1)
        self.assertEqual(rates.get(self.store, 'rate-a')['state'], 'active')

    def test_invoice_tampered_lines_cannot_hide_behind_sum(self):
        self.pair()
        self.close_invoice()
        row = invoices.get(self.store, 'invoice-a')['lines'][0]['payload']
        row['after']['meter'] = 'other'
        self.store.execute("UPDATE invoice_lines SET payload=? WHERE invoice_id='invoice-a'", (canonical(row),))
        self.error('invoice_line_corrupt', invoices.get, self.store, 'invoice-a')
        report = checks.check(self.store)
        self.assertFalse(report['ok'])
        self.assertIn('invoice_line_corrupt', [row['code'] for row in report['problems']])
        self.assertEqual(self.store.scalar('SELECT total_cents FROM invoices'), 250)

    def test_audit_bundle_rejects_rehashed_source_substitution(self):
        self.pair()
        self.close_invoice()
        envelope = bundles.audit_package(self.store, 'invoice-a')
        source = envelope['payload']['events'][0]
        source['payload']['usage']['quantity'] = '999'
        source['digest'] = digest(source['payload'])
        envelope['sha256'] = digest(envelope['payload'])
        self.error('audit_event_binding', bundles.verify_audit_package, envelope)
        valid = bundles.audit_package(self.store, 'invoice-a')
        self.assertTrue(bundles.verify_audit_package(valid)['integrity'])
        self.assertEqual(receiver.get(self.store, 'consumer-1')['payload']['usage']['quantity'], '10')

    def test_large_invoice_fails_before_sqlite_overflow(self):
        rates.withdraw(self.store, 'rate-a', 'high unit price')
        rates.add(self.store, 'high', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', {'tiers': [{'up_to': None, 'unit_price': '1000000000'}]})
        self.pair('1000000000')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        self.assertEqual(pricing.calculate(self.store, 'run')['total_cents'], 10**20)
        self.error('invoice_amount_limit', invoices.prepare, self.store, 'huge', 'run')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM invoices'), 0)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM ledger_entries'), 0)
        self.assertTrue(reconcile.get(self.store, 'run')['fresh'])

    def test_cli_failure_returns_structured_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'request.json'
            path.write_text(canonical({'operation': 'invoice.get', 'arguments': {'invoice_id': 'missing'}}))
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(['execute', str(path)])
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), '')
            report = loads(errors.getvalue())
            self.assertFalse(report['ok'])
            self.assertEqual(report['error']['code'], 'invoice_missing')

    def test_replay_fixture_assertions_and_no_implicit_download(self):
        fixture = {'synthetic': True, 'steps': [{'request': {'operation': 'contract.get', 'arguments': {'contract_id': 'contract-a'}}, 'expect': {'currency': 'CNY', 'revision': 1}}]}
        result = replay(self.app, fixture)
        self.assertTrue(result['ok'])
        self.assertEqual(result['steps'][0]['assertions'], 2)
        invalid = copy.deepcopy(fixture)
        invalid['steps'][0]['expect']['currency'] = 'USD'
        self.error('fixture_assertion', replay, self.app, invalid)
        invalid['steps'][0]['expect'] = {'missing': 1}
        self.error('fixture_path', replay, self.app, invalid)
        self.error('fixture', replay, self.app, {'synthetic': False, 'steps': []})
        self.assertTrue(checks.check(self.store)['ok'])

    def test_stream_import_order_failure_is_atomic(self):
        self.pair()
        envelope = recovery.export_stream(self.store, 'contract-a', 'provider')
        envelope['payload']['events'].append(copy.deepcopy(envelope['payload']['events'][0]))
        envelope['sha256'] = digest(envelope['payload'])
        before = self.store.scalar('SELECT COUNT(*) FROM events')
        self.error('stream_order', recovery.import_stream, self.store, envelope)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), before)
        self.assertEqual(receiver.stream_status(self.store, 'contract-a', 'provider')['cursor'], 1)
        self.assertTrue(recovery.inspect(self.store, 'contract-a', 'provider')['replayable'])
        self.assertTrue(recovery.replay(self.store, 'contract-a', 'provider')['ok'])

    def test_refund_identity_and_capacity(self):
        self.pair()
        self.close_invoice()
        payments.receive(self.store, 'payment', 'contract-a', 400, 'CNY', 'bank')
        self.assertEqual(self.app.credits.refundable('contract-a')['available_cents'], 150)
        first = self.app.credits.refund('refund', 'contract-a', 100, 'bank-refund', 'approver')
        self.assertEqual(self.app.credits.refund('refund', 'contract-a', 100, 'bank-refund', 'approver'), first)
        self.assertEqual(self.app.credits.refundable('contract-a')['available_cents'], 50)
        self.error('refund_conflict', self.app.credits.refund, 'refund', 'contract-a', 101, 'bank-refund', 'approver')
        self.error('refund_capacity', self.app.credits.refund, 'too-much', 'contract-a', 51, 'bank-refund', 'approver')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM refunds'), 1)

    def test_resolution_move_again_invalidates_old_destination(self):
        self.pair()
        base = digest(reconcile.pair(self.store, 'contract-a', 'usage-a'))
        disputes.decide(self.store, 'contract-a', 'usage-a', 0, base, {'action': 'accept', 'usage': usage(at='2026-12-01T00:00:00Z')}, 'approver', 'move December')
        reconcile.run(self.store, 'december', 'contract-a', '2026-12')
        self.assertTrue(reconcile.get(self.store, 'december')['fresh'])
        disputes.decide(self.store, 'contract-a', 'usage-a', 1, base, {'action': 'accept', 'usage': usage(at='2026-10-01T00:00:00Z')}, 'reviewer', 'correct October')
        self.assertFalse(reconcile.get(self.store, 'december')['fresh'])
        october = reconcile.run(self.store, 'october', 'contract-a', '2026-10')
        self.assertEqual(october['rows'][0]['status'], 'resolved')
        december = reconcile.run(self.store, 'new-december', 'contract-a', '2026-12')
        self.assertEqual(december['rows'], [])
        self.assertEqual(len(disputes.history(self.store, 'contract-a', 'usage-a')), 2)

    def test_contract_revision_cannot_remove_active_rate_meter(self):
        changed = definition(meters=['bytes'], tolerance={})
        self.error('contract_rate_meter', registry.revise, self.store, 'contract-a', 1, changed, 'remove meter')
        self.assertEqual(registry.get(self.store, 'contract-a')['definition']['meters'], ['records'])
        rates.withdraw(self.store, 'rate-a', 'retire old meter')
        revised = registry.revise(self.store, 'contract-a', 1, changed, 'new meter')
        self.assertEqual(revised['definition']['meters'], ['bytes'])
        self.assertEqual(revised['revision'], 2)
        self.assertEqual(len(registry.history(self.store, 'contract-a')), 2)
        self.assertEqual(rates.get(self.store, 'rate-a')['state'], 'withdrawn')

    def test_transfer_manifest_rejects_bad_chunk_geometry(self):
        self.pair()
        self.close_invoice()
        manifest = self.app.transfers.prepare('export', 'invoice-a', 128)
        malformed = copy.deepcopy(manifest)
        malformed['chunks'].pop()
        self.error('transfer_manifest', self.app.transfers.start, 'bad', malformed)
        self.assertIsNone(self.store.one("SELECT transfer_id FROM evidence_imports WHERE transfer_id='bad'"))
        valid = self.app.transfers.start('valid', manifest)
        self.assertEqual(valid['state'], 'receiving')
        self.assertEqual(valid['received'], [])

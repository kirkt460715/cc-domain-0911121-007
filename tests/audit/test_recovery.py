import tempfile
from pathlib import Path
from tests.support import Case, definition, event, usage
from meterbook.audit import checks, anomalies, explanations, closing
from meterbook.events import receiver, reconcile, recovery
from meterbook.settlement import invoices, pricing, preflight
from meterbook.contracts import rates, registry
from meterbook.service import Meterbook
from meterbook.values import canonical


class Recovery(Case):
    def test_replay_projection_compares_with_heads(self):
        self.pair()
        self.pair('11', 2, 2)
        replayed = recovery.replay(self.store, 'contract-a', 'provider')
        self.assertTrue(replayed['ok'])
        self.assertEqual(replayed['cursor'], 2)
        self.assertEqual(replayed['usage_ids'], ['usage-a'])
        current = receiver.head(self.store, 'contract-a', 'provider', 'usage-a')['payload']
        current['quantity'] = '999'
        self.store.execute("UPDATE usage_versions SET payload=? WHERE side='provider' AND revision=2", (canonical(current),))
        failed = recovery.replay(self.store, 'contract-a', 'provider')
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['failures'][0]['code'], 'replay_projection')

    def test_audit_chain_reports_first_tampered_position(self):
        self.pair()
        good = checks.journal(self.store)
        self.assertTrue(good['ok'])
        self.assertGreater(good['count'], 5)
        self.store.execute("UPDATE audit SET body='{}' WHERE position=2")
        bad = checks.journal(self.store)
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['position'], 2)
        report = checks.check(self.store)
        self.assertFalse(report['ok'])
        self.assertEqual(report['problems'][0]['code'], 'audit')

    def test_ledger_amount_tampering_detected(self):
        self.pair()
        self.close_invoice()
        self.store.execute("UPDATE ledger_entries SET amount_cents=1 WHERE account='receivable'")
        report = checks.check(self.store)
        self.assertFalse(report['ok'])
        self.assertIn('ledger_balance', [row['code'] for row in report['problems']])
        trial = closing.ledger_trial(self.store, 'contract-a')
        self.assertFalse(trial['balanced'])
        self.assertEqual(trial['balance_cents'], -249)
        self.error('statement_imbalance', self.app.statements.capture, 'bad', 'contract-a')

    def test_nested_savepoint_does_not_lose_outer_write(self):
        with self.store.atomic():
            receiver.receive(self.store, event())
            try:
                with self.store.atomic():
                    receiver.receive(self.store, event('consumer'))
                    raise RuntimeError('rollback inner unit')
            except RuntimeError:
                pass
            self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 1)
        self.assertEqual(receiver.get(self.store, 'provider-1')['state'], 'pending')
        self.error('event_missing', receiver.get, self.store, 'consumer-1')
        self.assertEqual(self.store.depth, 0)
        self.assertTrue(checks.check(self.store)['ok'])

    def test_campaign_failure_isolated_and_retryable(self):
        receiver.receive(self.store, event())
        receiver.drain(self.store, 'contract-a', 'provider')
        self.app.campaigns.create('campaign', [{'contract_id': 'contract-a', 'period': '2026-11'}, {'contract_id': 'contract-a', 'period': '2026-12'}])
        failed = self.app.campaigns.advance('campaign', 'approver')['campaign']
        self.assertEqual(failed['tasks'][0]['state'], 'failed')
        self.assertEqual(failed['tasks'][0]['error']['code'], 'unresolved_disputes')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM invoices'), 0)
        second = self.app.campaigns.advance('campaign', 'approver')['campaign']
        self.assertEqual(second['tasks'][1]['state'], 'complete')
        self.error('campaign_failures', self.app.campaigns.finish, 'campaign')
        receiver.receive(self.store, event('consumer'))
        receiver.drain(self.store, 'contract-a', 'consumer')
        self.app.campaigns.retry('campaign', 0)
        retried = self.app.campaigns.advance('campaign', 'approver')['campaign']
        self.assertEqual(retried['tasks'][0]['attempt'], 2)
        self.assertEqual(retried['tasks'][0]['state'], 'complete')
        self.assertEqual(self.app.campaigns.finish('campaign')['state'], 'complete')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM invoices'), 2)

    def test_campaign_partial_requires_explicit_choice(self):
        receiver.receive(self.store, event())
        receiver.drain(self.store, 'contract-a', 'provider')
        self.app.campaigns.create('campaign', [{'contract_id': 'contract-a', 'period': '2026-11'}])
        self.error('campaign_pending', self.app.campaigns.finish, 'campaign')
        self.app.campaigns.advance('campaign', 'approver')
        self.error('campaign_failures', self.app.campaigns.finish, 'campaign')
        self.assertEqual(self.app.campaigns.finish('campaign', True)['state'], 'partial')
        self.error('campaign_retry', self.app.campaigns.retry, 'campaign', 0)
        self.error('campaign_closed', self.app.campaigns.advance, 'campaign', 'approver')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM invoices'), 0)

    def test_anomaly_reports_do_not_change_billing(self):
        for index, quantity in enumerate(['10', '10', '10', '100'], 1):
            self.pair(quantity, index, identity=f'usage-{index}')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        before = pricing.calculate(self.store, 'run')
        report = anomalies.outliers(self.store, 'contract-a', '2026-11')
        self.assertEqual(report['reports'][0]['median'], '10')
        self.assertEqual(report['reports'][0]['flagged'], [{'usage_id': 'usage-4', 'quantity': '100'}])
        self.assertEqual(report['effect'], 'diagnostic_only_no_billing_mutation')
        statistics = anomalies.discrepancy_statistics(self.store, 'contract-a', '2026-11')
        self.assertEqual(statistics['quantity_pairs_compared'], 4)
        self.assertEqual(statistics['absolute_delta'], '0')
        self.assertEqual(pricing.calculate(self.store, 'run'), before)

    def test_period_drift_and_correction_trace(self):
        self.pair('10')
        self.pair('20', 2, identity='usage-b', at='2026-12-01T00:00:00Z')
        report = anomalies.drift(self.store, 'contract-a', '2026-11', '2026-12')
        self.assertEqual(report['meters'][0]['ratio'], '2')
        self.assertEqual(report['meters'][0]['delta'], '10')
        self.pair('12', 3, 2)
        changed = anomalies.usage_changes(self.store, 'contract-a', 'provider')['changes']
        self.assertEqual(changed[0]['quantity_delta'], '2')
        self.assertEqual(changed[0]['changed_fields'], ['quantity'])
        self.assertFalse(changed[0]['period_changed'])
        self.assertEqual(explanations.period_report(self.store, 'contract-a', '2026-11')['matched_quantities'], {'records': '12'})

    def test_preflight_reports_gaps_and_uncovered_rates(self):
        receiver.receive(self.store, event(sequence=3))
        report = preflight.readiness(self.store, 'contract-a', '2026-11')
        self.assertFalse(report['ready'])
        self.assertIn('stream_gap', [row['code'] for row in report['problems']])
        self.assertEqual(report['invoice_kind'], 'original')
        self.assertEqual(receiver.stream_status(self.store, 'contract-a', 'provider')['gaps'][0]['until_inclusive'], 2)
        self.pair('10')
        rates.withdraw(self.store, 'rate-a', 'temporarily incomplete schedule')
        report = preflight.readiness(self.store, 'contract-a', '2026-11')
        self.assertIn('uncovered_usage', [row['code'] for row in report['problems']])
        self.assertEqual(report['usage_count'], 1)

    def test_durable_reopen_and_two_connection_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'ledger.sqlite'
            with Meterbook(path) as first:
                registry.create(first.store, 'contract-b', 'provider', 'consumer', 'CNY', definition())
                first.store.log('synthetic.marker', {'value': 1})
            with Meterbook(path) as reopened, Meterbook(path) as competitor:
                self.assertEqual(registry.get(reopened.store, 'contract-b')['revision'], 1)
                registry.revise(reopened.store, 'contract-b', 1, definition(payment_days=15), 'new payment days')
                self.error('contract_stale', registry.revise, competitor.store, 'contract-b', 1, definition(payment_days=20), 'stale update')
                self.assertEqual(registry.get(competitor.store, 'contract-b')['definition']['payment_days'], 15)
                self.assertEqual(len(registry.history(competitor.store, 'contract-b')), 2)
                self.assertTrue(checks.check(competitor.store)['ok'])
                self.assertEqual(competitor.store.integrity()['checks'], ['ok'])

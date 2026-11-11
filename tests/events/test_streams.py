from tests.support import Case, event, usage, definition
from meterbook.events import receiver, reconcile, disputes, recovery, protocol
from meterbook.contracts import registry
from meterbook.values import digest, canonical


class Streams(Case):
    def test_out_of_order_waits_for_missing_predecessor(self):
        receiver.receive(self.store, event(sequence=2, usage_id='usage-b'))
        blocked = receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(blocked['cursor'], 0)
        self.assertEqual(blocked['applied'], [])
        self.assertEqual(receiver.stream_status(self.store, 'contract-a', 'provider')['gaps'], [{'from': 1, 'until_inclusive': 1}])
        receiver.receive(self.store, event())
        result = receiver.drain(self.store, 'contract-a', 'provider', 1)
        self.assertEqual(result['cursor'], 1)
        self.assertIsNone(receiver.head(self.store, 'contract-a', 'provider', 'usage-b'))
        self.assertEqual(receiver.drain(self.store, 'contract-a', 'provider')['cursor'], 2)
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'usage-b')['revision'], 1)

    def test_duplicate_and_sequence_identity_conflicts(self):
        original = receiver.receive(self.store, event())
        self.assertFalse(original['duplicate'])
        duplicate = receiver.receive(self.store, event())
        self.assertTrue(duplicate['duplicate'])
        self.error('event_conflict', receiver.receive, self.store, event(usage=usage('11')))
        self.error('event_sequence_conflict', receiver.receive, self.store, event(event_id='other'))
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 1)
        receiver.drain(self.store, 'contract-a', 'provider')
        self.assertTrue(receiver.receive(self.store, event())['duplicate'])
        self.assertEqual(self.store.revision('contract-a', '2026-11'), 1)

    def test_rejection_blocks_and_explicit_skip_advances(self):
        receiver.receive(self.store, event(usage=usage(region='US')))
        receiver.receive(self.store, event(sequence=2, usage_id='usage-b'))
        result = receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(result['cursor'], 0)
        self.assertEqual(result['blocked']['error']['code'], 'usage_authorization')
        self.assertIsNone(receiver.head(self.store, 'contract-a', 'provider', 'usage-a'))
        self.assertEqual(receiver.get(self.store, 'provider-2')['state'], 'pending')
        skipped = receiver.skip(self.store, 'provider-1', 'invalid source region')
        self.assertEqual(skipped['cursor'], 2)
        self.assertEqual(receiver.get(self.store, 'provider-1')['state'], 'skipped')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'usage-b')['state'], 'active')
        self.error('event_skip_state', receiver.skip, self.store, 'provider-2', 'not rejected')

    def test_retry_after_contract_fix(self):
        receiver.receive(self.store, event(usage=usage(region='US')))
        receiver.drain(self.store, 'contract-a', 'provider')
        registry.revise(self.store, 'contract-a', 1, definition(regions=['CN', 'US']), 'region corrected')
        result = recovery.retry_rejected(self.store, 'provider-1', 'contract corrected')
        self.assertEqual(receiver.get(self.store, 'provider-1')['state'], 'applied')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'usage-a')['payload']['region'], 'US')
        self.assertEqual(receiver.stream_status(self.store, 'contract-a', 'provider')['cursor'], 1)
        self.assertEqual(len(receiver.history(self.store, 'contract-a', 'provider', 'usage-a')), 1)

    def test_correction_moves_period_and_cancellation_terminal(self):
        self.pair()
        november_revision = self.store.revision('contract-a', '2026-11')
        self.pair('15', 2, 2, at='2026-12-01T00:00:00Z')
        self.assertGreater(self.store.revision('contract-a', '2026-11'), november_revision)
        self.assertEqual(self.store.revision('contract-a', '2026-12'), 2)
        self.assertEqual(reconcile.run(self.store, 'nov', 'contract-a', '2026-11')['rows'], [])
        self.assertEqual(reconcile.run(self.store, 'dec', 'contract-a', '2026-12')['rows'][0]['chosen']['quantity'], '15')
        for side in ('provider', 'consumer'):
            receiver.receive(self.store, event(side, 3, revision=3, action='cancel'))
            receiver.drain(self.store, 'contract-a', side)
        self.assertEqual(reconcile.run(self.store, 'cancel', 'contract-a', '2026-12')['rows'][0]['status'], 'cancelled')
        receiver.receive(self.store, event(sequence=4, revision=4, action='correct'))
        result = receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(result['blocked']['error']['code'], 'usage_terminal')
        self.assertEqual(len(receiver.history(self.store, 'contract-a', 'provider', 'usage-a')), 3)

    def test_quantity_tolerance_uses_provider_and_metadata_mismatch(self):
        receiver.receive(self.store, event(usage=usage('10.1')))
        receiver.receive(self.store, event('consumer', usage=usage('10')))
        for side in ('provider', 'consumer'):
            receiver.drain(self.store, 'contract-a', side)
        row = reconcile.run(self.store, 'run', 'contract-a', '2026-11')['rows'][0]
        self.assertEqual(row['status'], 'within_tolerance')
        self.assertEqual(row['chosen']['quantity'], '10.1')
        receiver.receive(self.store, event('consumer', 2, revision=2, action='correct', usage=usage('10.1', dimensions={'source': 'other'})))
        receiver.drain(self.store, 'contract-a', 'consumer')
        changed = reconcile.run(self.store, 'changed', 'contract-a', '2026-11')['rows'][0]
        self.assertEqual(changed['status'], 'disputed')
        self.assertEqual(changed['reasons'], ['dimensions_mismatch'])
        self.assertFalse(reconcile.get(self.store, 'run')['fresh'])
        self.assertEqual(len(reconcile.compare(self.store, 'run', 'changed')['changes']), 1)

    def test_resolution_cross_month_is_selected_and_invalidated(self):
        self.pair()
        base = digest(reconcile.pair(self.store, 'contract-a', 'usage-a'))
        chosen = usage('12', at='2026-12-02T00:00:00Z')
        result = disputes.decide(self.store, 'contract-a', 'usage-a', 0, base, {'action': 'accept', 'usage': chosen}, 'approver', 'correct timestamp')
        self.assertTrue(result['fresh'])
        december = reconcile.run(self.store, 'dec', 'contract-a', '2026-12')
        self.assertEqual(december['rows'][0]['status'], 'resolved')
        self.assertEqual(december['rows'][0]['chosen']['quantity'], '12')
        november = reconcile.run(self.store, 'nov', 'contract-a', '2026-11')
        self.assertEqual(november['rows'][0]['status'], 'outside_period')
        self.pair('11', 2, 2)
        self.assertFalse(reconcile.get(self.store, 'dec')['fresh'])
        self.assertFalse(disputes.get(self.store, 'contract-a', 'usage-a')['fresh'])
        self.assertEqual(len(disputes.history(self.store, 'contract-a', 'usage-a')), 1)

    def test_resolution_stale_and_role_checks(self):
        self.pair()
        evidence = digest(reconcile.pair(self.store, 'contract-a', 'usage-a'))
        self.error('actor_permission', disputes.decide, self.store, 'contract-a', 'usage-a', 0, evidence, {'action': 'exclude'}, 'analyst', 'review')
        self.error('resolution_stale_evidence', disputes.decide, self.store, 'contract-a', 'usage-a', 0, 'wrong', {'action': 'exclude'}, 'approver', 'review')
        result = disputes.decide(self.store, 'contract-a', 'usage-a', 0, evidence, {'action': 'exclude'}, 'approver', 'review')
        self.assertEqual(result['revision'], 1)
        self.error('resolution_stale', disputes.decide, self.store, 'contract-a', 'usage-a', 0, evidence, {'action': 'exclude'}, 'approver', 'review')
        row = reconcile.run(self.store, 'run', 'contract-a', '2026-11')['rows'][0]
        self.assertEqual(row['status'], 'excluded')
        self.assertIsNone(row['chosen'])
        self.assertEqual(disputes.queue(self.store, 'contract-a', '2026-11'), [])

    def test_corruption_detected_before_usage_replay(self):
        self.pair()
        self.store.execute("UPDATE events SET payload=? WHERE event_id='provider-1'", (canonical(event(usage=usage('99'))),))
        self.error('event_corrupt', receiver.get, self.store, 'provider-1')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'usage-a')['payload']['quantity'], '10')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM usage_versions'), 2)

    def test_protocol_rejects_ambiguous_quantities(self):
        for quantity in [1.2, True, 'NaN', '-1', '0.0000001']:
            with self.subTest(quantity=quantity):
                with self.assertRaises(Exception) as caught:
                    protocol.usage(usage(quantity))
                self.assertIn(caught.exception.code, {'decimal', 'decimal_range'})
        normalized = protocol.usage(usage('10.000000', at='2026-11-10T20:00:00+08:00'))
        self.assertEqual(normalized['quantity'], '10')
        self.assertEqual(normalized['at'], '2026-11-10T12:00:00.000000Z')
        self.error('fields', protocol.event, {**event(), 'unexpected': True})

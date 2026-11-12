import base64
import gzip
import json
from tests.support import Case, event, usage
from meterbook.ports.exports import Exports
from meterbook.events import receiver, reconcile
from meterbook.settlement import invoices


def pack(lines, members=2, trailing_newline=True):
    text = '\n'.join(lines) + ('\n' if trailing_newline else '')
    if members == 1:
        return gzip.compress(text.encode())
    cut = len(text) // 2
    return gzip.compress(text[:cut].encode()) + gzip.compress(text[cut:].encode())


class ExportCollection(Case):
    def setUp(self):
        super().setUp()
        self.app.imports.put('mapping', 0, {'contract_id': 'contract-a', 'side': 'provider', 'columns': {'usage_id': 'usage_id', 'at': 'at', 'quantity': 'quantity', 'meter': 'meter', 'purpose': 'purpose', 'region': 'region'}, 'defaults': {}, 'quantity_factor': '0.1'})

    def header(self, export_id='exp-a', mapping_revision=1, **changes):
        line = {'kind': 'header', 'export_id': export_id, 'contract_id': 'contract-a', 'side': 'provider', 'period': '2026-11', 'mapping_revision': mapping_revision}
        line.update(changes)
        return json.dumps(line)

    def detail(self, usage_id='u1', revision=1, action='report', quantity='1.25', reason=None, **changes):
        line = {'kind': 'usage', 'usage_id': usage_id, 'revision': revision, 'action': action}
        if action != 'cancel':
            line.update(at='2026-11-10T12:00:00Z', quantity=quantity, meter='records', purpose='research', region='CN')
        if reason is not None:
            line['reason'] = reason
        line.update(changes)
        return json.dumps(line)

    def trailer(self, count=2, totals=None):
        return json.dumps({'kind': 'trailer', 'count': count, 'totals': totals if totals is not None else {'records': '3.75'}})

    def standard(self, **changes):
        return [self.header(), self.detail('u1', quantity='1.25'), self.detail('u2', quantity='2.50'), self.trailer(2, {'records': '3.75'})]

    def collect(self, session, data, chunk=7):
        self.app.exports.begin(session, 'mapping')
        for index in range(0, len(data), chunk):
            self.app.exports.feed(session, base64.b64encode(data[index:index + chunk]).decode())
        return self.app.exports.finish(session)

    def test_reconciled_stream_reports_raw_valid_quarantined_posted(self):
        report = self.collect('s', pack(self.standard()))
        self.assertEqual(report['state'], 'reconciled')
        self.assertTrue(report['complete'])
        self.assertEqual(report['raw']['detail_records'], 2)
        self.assertEqual(report['raw']['observed_totals'], {'records': '3.75'})
        self.assertEqual(report['raw']['declared'], {'count': 2, 'totals': {'records': '3.75'}})
        self.assertEqual([row['line_no'] for row in report['valid']], [2, 3])
        self.assertEqual(report['valid'][0]['quantity'], '0.125')
        self.assertEqual(report['quarantined'], [])
        self.assertEqual(report['posted']['received'], 2)
        self.assertEqual(report['posted']['duplicates'], 0)
        self.assertTrue(report['reconciliation']['ok'])
        receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'u1')['payload']['quantity'], '0.125')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'u2')['payload']['quantity'], '0.25')

    def test_truncated_gzip_stream_is_never_complete(self):
        report = self.collect('s', pack(self.standard())[:-5])
        self.assertEqual(report['state'], 'incomplete')
        self.assertFalse(report['complete'])
        self.assertIn('gzip_stream_incomplete', report['failure']['details']['reasons'])
        self.assertEqual(report['posted']['received'], 0)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)

    def test_missing_trailer_is_incomplete(self):
        report = self.collect('s', pack(self.standard()[:3]))
        self.assertEqual(report['state'], 'incomplete')
        self.assertIn('trailer_missing', report['failure']['details']['reasons'])
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)

    def test_final_line_without_newline_is_truncated(self):
        report = self.collect('s', pack(self.standard(), trailing_newline=False))
        self.assertEqual(report['state'], 'incomplete')
        self.assertIn('final_line_truncated', report['failure']['details']['reasons'])

    def test_bad_row_counted_quarantined_and_unverifiable(self):
        bad = json.dumps({'kind': 'usage', 'quantity': '未知'}, ensure_ascii=False)
        report = self.collect('s', pack([self.header(), self.detail('u1', quantity='1.25'), bad, self.trailer(2, {'records': '3.75'})]))
        self.assertEqual(report['state'], 'mismatched')
        self.assertFalse(report['complete'])
        self.assertEqual(report['raw']['detail_records'], 2)
        self.assertEqual(report['raw']['observed_totals'], {'records': '1.25'})
        self.assertTrue(report['reconciliation']['unverifiable'])
        self.assertFalse(report['reconciliation']['ok'])
        self.assertEqual(len(report['quarantined']), 1)
        self.assertEqual(report['quarantined'][0]['line_no'], 3)
        self.assertIn('未知', report['quarantined'][0]['raw'])
        self.assertEqual(report['posted']['received'], 0)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)

    def test_blank_and_undecodable_lines_join_quarantine(self):
        report = self.collect('s', pack([self.header(), '', 'not json', self.detail('u1'), self.trailer(3, {'records': '1.25'})]))
        self.assertEqual(report['state'], 'mismatched')
        self.assertEqual(report['raw']['detail_records'], 3)
        self.assertEqual([row['error']['code'] for row in report['quarantined']], ['blank', 'json_decode'])

    def test_total_mismatch_blocks_posting(self):
        report = self.collect('s', pack([self.header(), self.detail('u1', quantity='1.25'), self.detail('u2', quantity='2.50'), self.trailer(2, {'records': '4'})]))
        self.assertEqual(report['state'], 'mismatched')
        self.assertEqual(report['reconciliation']['totals']['records'], {'declared': '4', 'observed': '3.75', 'ok': False})
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)

    def test_count_mismatch_blocks_posting(self):
        report = self.collect('s', pack([self.header(), self.detail('u1'), self.detail('u2', quantity='2.50'), self.trailer(3, {'records': '3.75'})]))
        self.assertEqual(report['state'], 'mismatched')
        self.assertEqual(report['reconciliation']['count'], {'declared': 3, 'observed': 2, 'ok': False})
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)

    def test_cancel_contributes_no_quantity(self):
        lines = [self.header(), self.detail('u1', quantity='1.50'), self.detail('u2', quantity='2.00'), self.detail('u2', 2, 'cancel', reason='重复上报'), self.trailer(3, {'records': '3.5'})]
        report = self.collect('s', pack(lines))
        self.assertEqual(report['state'], 'reconciled')
        self.assertEqual(report['raw']['observed_totals'], {'records': '3.5'})
        self.assertEqual(report['posted']['received'], 3)
        receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'u2')['state'], 'cancelled')

    def test_retry_same_export_never_posts_twice(self):
        first = self.collect('s1', pack(self.standard()))
        self.assertEqual(first['state'], 'reconciled')
        second = self.collect('s2', pack(self.standard()))
        self.assertEqual(second['state'], 'duplicate')
        self.assertEqual(second['duplicate_of'], 's1')
        self.assertEqual(second['original']['posted']['received'], 2)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 2)
        again = self.app.exports.finish('s1')
        self.assertEqual(again['state'], 'reconciled')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 2)

    def test_same_export_id_with_different_content_conflicts(self):
        self.collect('s1', pack(self.standard()))
        changed = [self.header(), self.detail('u1', quantity='9.99'), self.detail('u2', quantity='2.50'), self.trailer(2, {'records': '12.49'})]
        self.app.exports.begin('s2', 'mapping')
        for index in range(0, len(pack(changed)), 7):
            self.app.exports.feed('s2', base64.b64encode(pack(changed)[index:index + 7]).decode())
        failure = self.error('export_conflict', self.app.exports.finish, 's2')
        self.assertEqual(failure.details['export_id'], 'exp-a')
        self.assertNotEqual(failure.details['existing_digest'], failure.details['received_digest'])
        self.assertEqual(self.app.exports.status('s2')['state'], 'conflicted')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 2)

    def test_mismatched_export_does_not_block_later_reconcile(self):
        bad = [self.header(), self.detail('u1'), self.detail('u2', quantity='2.50'), self.trailer(2, {'records': '4'})]
        self.assertEqual(self.collect('s1', pack(bad))['state'], 'mismatched')
        self.assertEqual(self.collect('s2', pack(self.standard()))['state'], 'reconciled')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 2)

    def test_revision_and_cancel_flow_through_existing_receiver(self):
        self.collect('s1', pack(self.standard()))
        receiver.drain(self.store, 'contract-a', 'provider')
        lines = [self.header('exp-b'), self.detail('u1', 2, 'correct', quantity='1.50', reason='计量修正'), self.detail('u2', 2, 'cancel', reason='重复上报'), self.trailer(2, {'records': '1.5'})]
        report = self.collect('s2', pack(lines))
        self.assertEqual(report['state'], 'reconciled')
        receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'u1')['payload']['quantity'], '0.15')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'u1')['revision'], 2)
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'u2')['state'], 'cancelled')

    def test_settled_period_keeps_old_invoice_and_uses_adjustment(self):
        lines = [self.header(), self.detail('u1', quantity='100'), self.trailer(1, {'records': '100'})]
        self.assertEqual(self.collect('s1', pack(lines))['state'], 'reconciled')
        receiver.drain(self.store, 'contract-a', 'provider')
        receiver.receive(self.store, event('consumer', 1, 'u1', usage=usage('10', dimensions={})))
        receiver.drain(self.store, 'contract-a', 'consumer')
        self.close_invoice()
        revision = [self.header('exp-b'), self.detail('u1', 2, 'correct', quantity='120', reason='计量修正'), self.trailer(1, {'records': '120'})]
        self.assertEqual(self.collect('s2', pack(revision))['state'], 'reconciled')
        receiver.drain(self.store, 'contract-a', 'provider')
        receiver.receive(self.store, event('consumer', 2, 'u1', 2, 'correct', usage=usage('12', dimensions={})))
        receiver.drain(self.store, 'contract-a', 'consumer')
        self.close_invoice('adjust', 'run-b', kind='adjustment')
        self.assertEqual(invoices.get(self.store, 'invoice-a')['total_cents'], 250)
        self.assertEqual(invoices.get(self.store, 'adjust')['total_cents'], 50)
        chain = invoices.chain(self.store, 'contract-a', '2026-11')
        self.assertEqual(chain['cumulative_cents'], 300)
        self.assertEqual(chain['latest_recalculated_cents'], 300)

    def test_mapping_revision_is_pinned_per_session(self):
        self.app.exports.begin('s', 'mapping')
        data = pack([self.header(mapping_revision=2), self.detail('u1'), self.trailer(1, {'records': '1.25'})])
        self.error('export_mapping', self.app.exports.feed, 's', base64.b64encode(data).decode())
        self.assertEqual(self.app.exports.status('s')['state'], 'incomplete')
        changed = {'contract_id': 'contract-a', 'side': 'provider', 'columns': {'usage_id': 'usage_id', 'at': 'at', 'quantity': 'quantity', 'meter': 'meter', 'purpose': 'purpose', 'region': 'region'}, 'defaults': {}, 'quantity_factor': '1'}
        self.app.exports.begin('pinned', 'mapping')
        self.app.imports.put('mapping', 1, changed)
        report = self.collect('pinned', pack([self.header(), self.detail('u1'), self.trailer(1, {'records': '1.25'})]))
        self.assertEqual(report['state'], 'reconciled')
        self.assertEqual(report['mapping_revision'], 1)
        self.assertEqual(report['valid'][0]['quantity'], '0.125')
        self.assertEqual(self.app.exports.begin('next', 'mapping')['mapping_revision'], 2)

    def test_header_binding_mismatch_is_rejected(self):
        self.app.exports.begin('s', 'mapping')
        data = pack([self.header(contract_id='contract-b'), self.detail('u1'), self.trailer(1, {'records': '1.25'})])
        self.error('export_mapping', self.app.exports.feed, 's', base64.b64encode(data).decode())
        self.assertEqual(self.app.exports.status('s')['state'], 'incomplete')

    def test_gzip_checksum_failure_is_incomplete(self):
        data = bytearray(pack(self.standard(), members=1))
        data[-5] ^= 255
        self.app.exports.begin('s', 'mapping')
        self.error('export_gzip', self.app.exports.feed, 's', base64.b64encode(bytes(data)).decode())
        self.assertEqual(self.app.exports.status('s')['state'], 'incomplete')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)

    def test_inflated_limit_aborts_session(self):
        limited = Exports(self.store, limits={'inflated_bytes': 64})
        limited.begin('big', 'mapping')
        self.error('export_limit', limited.feed, 'big', base64.b64encode(pack(self.standard())).decode())
        self.assertEqual(limited.status('big')['state'], 'incomplete')

    def test_session_begin_is_idempotent_and_mapping_bound(self):
        first = self.app.exports.begin('s', 'mapping')
        self.assertEqual(first['state'], 'receiving')
        self.assertEqual(self.app.exports.begin('s', 'mapping'), first)
        self.error('export_session_conflict', self.app.exports.begin, 's', 'other')
        self.error('mapping_missing', self.app.exports.begin, 'fresh', 'other')
        self.error('export_session_missing', self.app.exports.status, 'missing')

    def test_records_after_trailer_are_rejected(self):
        lines = self.standard()
        lines.insert(3, self.trailer(2, {'records': '3.75'}))
        self.app.exports.begin('s', 'mapping')
        self.error('export_structure', self.app.exports.feed, 's', base64.b64encode(pack(lines)).decode())
        self.assertEqual(self.app.exports.status('s')['state'], 'incomplete')

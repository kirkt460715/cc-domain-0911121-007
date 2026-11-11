import copy
import json
from tests.support import Case, event, usage
from meterbook.ports import files, bundles
from meterbook.events import receiver, recovery, reconcile
from meterbook.audit import verification
from meterbook.service import Meterbook
from meterbook.values import digest, canonical


class EvidencePorts(Case):
    def test_configuration_and_stream_roundtrip(self):
        self.pair()
        config = bundles.configuration(self.store, 'contract-a')
        stream = recovery.export_stream(self.store, 'contract-a', 'provider')
        with Meterbook() as destination:
            result = bundles.import_configuration(destination.store, config)
            self.assertEqual(result['contract_id'], 'contract-a')
            self.assertEqual(result['rate_ids'], ['rate-a'])
            first = recovery.import_stream(destination.store, stream)
            self.assertEqual(first['received'], 1)
            self.assertEqual(first['duplicates'], 0)
            self.assertEqual(recovery.import_stream(destination.store, stream)['duplicates'], 1)
            receiver.drain(destination.store, 'contract-a', 'provider')
            self.assertEqual(receiver.head(destination.store, 'contract-a', 'provider', 'usage-a')['payload']['quantity'], '10')
            self.assertTrue(recovery.replay(destination.store, 'contract-a', 'provider')['ok'])

    def test_atomic_batch_import_rolls_back_prior_rows(self):
        bad = event(sequence=2, event_id='provider-1', usage_id='usage-b')
        self.error('event_conflict', files.ingest, self.store, [event(), bad], True)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)
        independent = files.ingest(self.store, [event(), bad], False)
        self.assertEqual(independent['failed'], 1)
        self.assertTrue(independent['results'][0]['ok'])
        self.assertFalse(independent['results'][1]['ok'])
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 1)
        self.assertEqual(receiver.drain(self.store, 'contract-a', 'provider')['cursor'], 1)

    def test_jsonlines_csv_and_header_validation(self):
        raw = canonical(event()) + chr(10) + canonical(event('consumer'))
        self.assertEqual(len(files.parse_jsonlines(raw)), 2)
        self.error('input_blank', files.parse_jsonlines, raw + chr(10) * 2)
        self.error('duplicate_key', files.parse_jsonlines, '{"a":1,"a":2}')
        header = 'event_id,contract_id,side,sequence,usage_id,revision,action,meter,at,quantity,purpose,region,reason'
        row = 'provider-1,contract-a,provider,1,usage-a,1,report,records,2026-11-10T12:00:00Z,10,research,CN,'
        parsed = files.parse_csv(header + chr(10) + row)
        self.assertEqual(parsed[0]['sequence'], 1)
        self.assertEqual(parsed[0]['usage']['quantity'], '10')
        self.error('csv_integer', files.parse_csv, header + chr(10) + row.replace(',1,usage-a', ',01,usage-a'))
        self.error('csv_header', files.parse_csv, 'event_id,event_id' + chr(10) + 'one,two')
        self.error('csv_width', files.parse_csv, header + chr(10) + row + ',extra')

    def test_invoice_export_integrity_and_local_scope(self):
        self.pair()
        self.close_invoice()
        exported = files.invoice_export(self.store, 'invoice-a')
        result = files.verify_invoice(exported)
        self.assertTrue(result['integrity'])
        self.assertFalse(result['live_status_checked'])
        self.assertEqual(result['total_cents'], 250)
        broken = copy.deepcopy(exported)
        broken['payload']['total_cents'] += 1
        self.error('export_digest', files.verify_invoice, broken)
        broken['sha256'] = digest(broken['payload'])
        self.error('export_total', files.verify_invoice, broken)
        csv = files.invoice_csv(self.store, 'invoice-a')['csv']
        self.assertIn('invoice-a,rate-a,CNY,0,250,250', csv)

    def test_billing_proof_recomputes_original_and_adjustment(self):
        self.pair()
        self.close_invoice()
        original = verification.proof(self.store, 'invoice-a')
        result = verification.verify_proof(original)
        self.assertTrue(result['valid'])
        self.assertEqual(result['calculation']['total_cents'], 250)
        self.assertEqual(result['calculation']['usage_count'], 1)
        self.pair('12', 2, 2)
        self.close_invoice('adjust', 'run-b', kind='adjustment')
        adjusted = verification.proof(self.store, 'adjust')
        self.assertEqual(verification.verify_proof(adjusted)['adjustment']['delta_cents'], 50)
        self.assertEqual(verification.verify_proof(original)['calculation']['total_cents'], 250)
        self.assertEqual(result['authentication'], 'unsigned_local_evidence')

    def test_proof_detects_changed_frozen_rate_even_rehashed(self):
        self.pair()
        self.close_invoice()
        proof = verification.proof(self.store, 'invoice-a')
        proof['payload']['rate_definitions']['rate-a']['tiers'][0]['unit_price'] = '9'
        proof['sha256'] = digest(proof['payload'])
        self.error('calculation_rate_digest', verification.verify_proof, proof)
        self.assertEqual(self.store.scalar('SELECT total_cents FROM invoices'), 250)
        package = bundles.audit_package(self.store, 'invoice-a')
        self.assertEqual(bundles.verify_audit_package(package)['events'], 2)
        package['payload']['events'].pop()
        package['sha256'] = digest(package['payload'])
        self.error('audit_event_missing', bundles.verify_audit_package, package)

    def test_resumable_transfer_out_of_order_and_duplicate(self):
        self.pair()
        self.close_invoice()
        manifest = self.app.transfers.prepare('export', 'invoice-a', 128)
        started = self.app.transfers.start('import', manifest)
        self.assertEqual(len(started['missing']), len(manifest['chunks']))
        self.error('transfer_incomplete', self.app.transfers.finish, 'import')
        first = self.app.transfers.chunk('export', 0)
        self.error('transfer_integrity', self.app.transfers.receive, 'import', 0, 'YQ==')
        self.app.transfers.receive('import', 0, first['base64'])
        self.app.transfers.receive('import', 0, first['base64'])
        self.assertEqual(self.app.transfers.status('import')['received'], [0])
        for ordinal in reversed(range(1, len(manifest['chunks']))):
            chunk = self.app.transfers.chunk('export', ordinal)
            self.app.transfers.receive('import', ordinal, chunk['base64'])
        artifact = self.app.transfers.finish('import')
        self.assertEqual(artifact, files.invoice_export(self.store, 'invoice-a'))
        self.assertEqual(self.app.transfers.status('import')['state'], 'verified')
        self.assertEqual(self.app.transfers.finish('import'), artifact)

    def test_delivery_receipt_and_successor_notice(self):
        self.pair()
        self.close_invoice()
        delivered = self.app.receipts.prepare('delivery', 'invoice-a', 'customer')
        self.error('delivery_identity', self.app.receipts.acknowledge, 'delivery', 'wrong', delivered['digest'], 'receipt')
        acknowledged = self.app.receipts.acknowledge('delivery', 'customer', delivered['digest'], 'receipt')
        self.assertEqual(acknowledged['state'], 'acknowledged')
        self.assertEqual(self.app.receipts.acknowledge('delivery', 'customer', delivered['digest'], 'receipt'), acknowledged)
        self.pair('11', 2, 2)
        self.close_invoice('adjust', 'run-b', kind='adjustment')
        notices = self.app.receipts.reconcile()
        self.assertEqual(notices['count'], 1)
        self.assertEqual(self.app.receipts.reconcile()['count'], 0)
        pending = self.app.receipts.pending('customer')
        self.assertEqual(pending[0]['payload']['delta_cents'], 25)
        self.app.receipts.acknowledge_notice(pending[0]['notice_id'], 'customer', 'notice-receipt')
        self.assertEqual(self.app.receipts.pending('customer'), [])

    def mapping(self):
        return {'contract_id': 'contract-a', 'side': 'provider', 'columns': {'usage_id': 'id', 'at': 'timestamp', 'quantity': 'count'}, 'defaults': {'meter': 'records', 'purpose': 'research', 'region': 'CN'}, 'quantity_factor': '0.1'}

    def test_mapped_import_checkpoint_pins_definition(self):
        self.app.imports.put('mapping', 0, self.mapping())
        records = [{'id': 'usage-a', 'timestamp': '2026-11-10T12:00:00Z', 'count': '100'}, {'id': 'usage-b', 'timestamp': '2026-11-10T13:00:00Z', 'count': '200'}]
        self.app.imports.start('import', 'mapping', records, 1)
        first = self.app.imports.advance('import', 1)
        self.assertEqual(first['cursor'], 1)
        self.assertEqual(first['state'], 'running')
        changed = self.mapping()
        changed['quantity_factor'] = '1'
        self.app.imports.put('mapping', 1, changed)
        completed = self.app.imports.advance('import', 1)
        self.assertEqual(completed['state'], 'completed')
        self.assertEqual(completed['mapping_revision'], 1)
        self.assertEqual(completed['failed'], 0)
        receiver.drain(self.store, 'contract-a', 'provider')
        self.assertEqual(receiver.head(self.store, 'contract-a', 'provider', 'usage-b')['payload']['quantity'], '20')
        self.assertEqual(self.app.imports.advance('import')['cursor'], 2)

    def test_mapped_bad_row_preserves_gap_and_error(self):
        self.app.imports.put('mapping', 0, self.mapping())
        records = [{'id': 'bad', 'timestamp': 'not-a-time', 'count': '1'}, {'id': 'good', 'timestamp': '2026-11-10T00:00:00Z', 'count': '10'}]
        self.app.imports.start('import', 'mapping', records, 1)
        completed = self.app.imports.advance('import')
        self.assertEqual(completed['failed'], 1)
        self.assertEqual(completed['results'][0]['error']['code'], 'time')
        self.assertEqual(completed['results'][1]['ordinal'], 1)
        self.assertEqual(receiver.stream_status(self.store, 'contract-a', 'provider')['gaps'], [{'from': 1, 'until_inclusive': 1}])
        self.assertEqual(receiver.drain(self.store, 'contract-a', 'provider')['cursor'], 0)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 1)
        self.error('mapping_import_conflict', self.app.imports.start, 'import', 'mapping', records, 2)

    def test_request_dispatch_and_atomic_service_batch(self):
        capabilities = self.app.capabilities()
        route = next(row for row in capabilities if row['operation'] == 'invoice.close')
        self.assertEqual(route['required'], ['invoice_id', 'expected_revision', 'approver'])
        self.error('operation', self.app.execute, {'operation': 'shell'})
        self.error('arguments', self.app.execute, {'operation': 'event.receive', 'arguments': {}})
        requests = [{'operation': 'event.receive', 'arguments': {'value': event()}}, {'operation': 'unknown'}]
        self.error('operation', self.app.batch, requests, True)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM events'), 0)
        independent = self.app.batch(requests)
        self.assertTrue(independent[0]['ok'])
        self.assertFalse(independent[1]['ok'])
        self.assertEqual(independent[1]['error']['code'], 'operation')

import json
from meterbook.values import key, canonical, digest, reject, prose, fields
from meterbook.ports.files import invoice_export, verify_invoice
from meterbook.settlement import invoices

DDL = '''
CREATE TABLE IF NOT EXISTS invoice_deliveries(delivery_id TEXT PRIMARY KEY,invoice_id TEXT NOT NULL,recipient TEXT NOT NULL,envelope TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adjustment_notices(notice_id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL,successor_invoice_id TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,created_at TEXT NOT NULL);
'''


class Receipts:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('receipt_transaction', 'Initialize delivery receipts outside transactions')
        store.db.executescript(DDL)

    def prepare(self, delivery_id, invoice_id, recipient):
        key(delivery_id)
        key(recipient)
        with self.store.atomic():
            previous = self.store.one('SELECT * FROM invoice_deliveries WHERE delivery_id=?', (delivery_id,))
            if previous:
                if previous['invoice_id'] != invoice_id or previous['recipient'] != recipient:
                    reject('delivery_conflict', 'Delivery ID binds another invoice or recipient')
                return self.get(delivery_id)
            exported = invoice_export(self.store, invoice_id)
            self.store.execute("INSERT INTO invoice_deliveries VALUES(?,?,?,?,?,'prepared',NULL,?)", (delivery_id, invoice_id, recipient, canonical(exported), digest(exported), self.store.clock()))
            self.store.log('invoice.delivery_prepared', {'delivery_id': delivery_id, 'invoice_id': invoice_id, 'recipient': recipient})
            return self.get(delivery_id)

    def get(self, delivery_id):
        row = self.store.one('SELECT * FROM invoice_deliveries WHERE delivery_id=?', (delivery_id,))
        if row is None:
            reject('delivery_missing', 'Invoice delivery does not exist')
        row['envelope'] = json.loads(row['envelope'])
        if digest(row['envelope']) != row['digest']:
            reject('delivery_corrupt', 'Delivery checksum mismatch')
        verify_invoice(row['envelope'])
        return row

    def acknowledge(self, delivery_id, recipient, expected_digest, receipt):
        key(receipt)
        with self.store.atomic():
            row = self.get(delivery_id)
            if row['recipient'] != recipient or row['digest'] != expected_digest:
                reject('delivery_identity', 'Recipient or content acknowledgement differs')
            if row['state'] == 'acknowledged':
                if row['receipt'] != receipt:
                    reject('delivery_receipt', 'Delivery already has another receipt')
                return row
            self.store.execute("UPDATE invoice_deliveries SET state='acknowledged',receipt=? WHERE delivery_id=?", (receipt, delivery_id))
            self.store.log('invoice.delivery_acknowledged', {'delivery_id': delivery_id, 'receipt': receipt})
            return self.get(delivery_id)

    def reconcile(self):
        created = []
        with self.store.atomic():
            for delivery in self.store.all('SELECT delivery_id,invoice_id,recipient FROM invoice_deliveries ORDER BY delivery_id'):
                old = invoices.get(self.store, delivery['invoice_id'])
                chain = invoices.chain(self.store, old['contract_id'], old['period'])['invoices']
                index = next((i for i, invoice in enumerate(chain) if invoice['invoice_id'] == old['invoice_id']), None)
                if index is None:
                    reject('delivery_chain', 'Delivered invoice is absent from closed chain')
                for successor in chain[index + 1:]:
                    payload = {'delivery_id': delivery['delivery_id'], 'recipient': delivery['recipient'], 'original_invoice_id': old['invoice_id'], 'successor_invoice_id': successor['invoice_id'], 'delta_cents': successor['total_cents'], 'currency': successor['currency'], 'successor_digest': successor['digest']}
                    notice_id = 'notice-' + digest(payload)
                    if self.store.one('SELECT notice_id FROM adjustment_notices WHERE notice_id=?', (notice_id,)):
                        continue
                    self.store.execute("INSERT INTO adjustment_notices VALUES(?,?,?,?,'pending',NULL,?)", (notice_id, delivery['delivery_id'], successor['invoice_id'], canonical(payload), self.store.clock()))
                    created.append(notice_id)
            if created:
                self.store.log('invoice.adjustment_notices', {'notice_ids': created})
        return {'created': created, 'count': len(created)}

    def pending(self, recipient):
        rows = self.store.all("SELECT n.* FROM adjustment_notices n JOIN invoice_deliveries d ON d.delivery_id=n.delivery_id WHERE d.recipient=? AND n.state='pending' ORDER BY n.created_at,n.notice_id", (recipient,))
        for row in rows:
            row['payload'] = json.loads(row['payload'])
            if row['notice_id'] != 'notice-' + digest(row['payload']):
                reject('notice_corrupt', 'Adjustment notice checksum mismatch')
        return rows

    def acknowledge_notice(self, notice_id, recipient, receipt):
        key(receipt)
        with self.store.atomic():
            row = self.store.one('SELECT n.*,d.recipient FROM adjustment_notices n JOIN invoice_deliveries d ON d.delivery_id=n.delivery_id WHERE n.notice_id=?', (notice_id,))
            if row is None or row['recipient'] != recipient:
                reject('notice_recipient', 'Adjustment notice belongs to another recipient')
            if row['state'] == 'acknowledged' and row['receipt'] != receipt:
                reject('notice_receipt', 'Adjustment notice has another receipt')
            self.store.execute("UPDATE adjustment_notices SET state='acknowledged',receipt=? WHERE notice_id=?", (receipt, notice_id))
            self.store.log('invoice.adjustment_acknowledged', {'notice_id': notice_id, 'receipt': receipt})
            return {'notice_id': notice_id, 'state': 'acknowledged', 'receipt': receipt}

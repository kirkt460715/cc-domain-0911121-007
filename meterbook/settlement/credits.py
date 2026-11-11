from meterbook.values import key, number, prose, reject
from meterbook.settlement import invoices, payments
from meterbook.events import disputes

DDL = '''
CREATE TABLE IF NOT EXISTS credit_applications(application_id TEXT PRIMARY KEY,credit_invoice_id TEXT NOT NULL,debit_invoice_id TEXT NOT NULL,amount_cents INTEGER NOT NULL,state TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refunds(refund_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,state TEXT NOT NULL,reference TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
'''


class Credits:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('credit_transaction', 'Initialize credit manager outside transactions')
        store.db.executescript(DDL)

    def balance(self, invoice_id):
        invoice = invoices.get(self.store, invoice_id)
        base = payments.invoice_balance(self.store, invoice_id)
        outgoing = self.store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications WHERE credit_invoice_id=? AND state='active'", (invoice_id,))
        incoming = self.store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications WHERE debit_invoice_id=? AND state='active'", (invoice_id,))
        return {**base, 'credits_applied_cents': incoming, 'credit_consumed_cents': outgoing, 'effective_unpaid_cents': base['unpaid_cents'] - incoming + outgoing}

    def apply(self, application_id, credit_invoice_id, debit_invoice_id, amount_cents, actor, reason):
        key(application_id)
        number(amount_cents, 'amount_cents', 1, 10**15)
        disputes.actor(self.store, actor, 'approver')
        reason = prose(reason, 'reason')
        with self.store.atomic():
            existing = self.store.one('SELECT * FROM credit_applications WHERE application_id=?', (application_id,))
            if existing:
                if (existing['credit_invoice_id'], existing['debit_invoice_id'], existing['amount_cents']) != (credit_invoice_id, debit_invoice_id, amount_cents):
                    reject('credit_conflict', 'Application ID already binds other credit allocation')
                return existing
            credit = invoices.get(self.store, credit_invoice_id)
            debit = invoices.get(self.store, debit_invoice_id)
            if credit['state'] != 'closed' or debit['state'] != 'closed' or credit['total_cents'] >= 0 or debit['total_cents'] <= 0:
                reject('credit_invoice', 'Application needs a closed negative invoice and closed positive invoice')
            if credit['contract_id'] != debit['contract_id'] or credit['currency'] != debit['currency']:
                reject('credit_scope', 'Credit and debit belong to different scopes')
            available = -min(0, self.balance(credit_invoice_id)['effective_unpaid_cents'])
            due = max(0, self.balance(debit_invoice_id)['effective_unpaid_cents'])
            if amount_cents > available or amount_cents > due:
                reject('credit_capacity', 'Application exceeds available credit or outstanding debit')
            self.store.execute("INSERT INTO credit_applications VALUES(?,?,?,?,'active',?,?,?)", (application_id, credit_invoice_id, debit_invoice_id, amount_cents, actor, reason, self.store.clock()))
            self.store.log('credit.applied', {'application_id': application_id, 'credit_invoice_id': credit_invoice_id, 'debit_invoice_id': debit_invoice_id, 'amount_cents': amount_cents})
            return self.store.one('SELECT * FROM credit_applications WHERE application_id=?', (application_id,))

    def reverse(self, application_id, actor, reason):
        disputes.actor(self.store, actor, 'approver')
        reason = prose(reason, 'reason')
        with self.store.atomic():
            row = self.store.one('SELECT * FROM credit_applications WHERE application_id=?', (application_id,))
            if row is None:
                reject('credit_missing', 'Credit application does not exist')
            if row['state'] == 'reversed':
                return row
            self.store.execute("UPDATE credit_applications SET state='reversed' WHERE application_id=?", (application_id,))
            self.store.log('credit.reversed', {'application_id': application_id, 'actor': actor, 'reason': reason})
            return self.store.one('SELECT * FROM credit_applications WHERE application_id=?', (application_id,))

    def refundable(self, contract_id):
        statement = payments.statement(self.store, contract_id)
        refunded = self.store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE contract_id=? AND state='posted'", (contract_id,))
        return {'contract_id': contract_id, 'currency': statement['currency'], 'net_due_cents': statement['net_due_cents'], 'refunded_cents': refunded, 'available_cents': max(0, -statement['net_due_cents'] - refunded)}

    def refund(self, refund_id, contract_id, amount_cents, reference, actor):
        key(refund_id)
        number(amount_cents, 'amount_cents', 1, 10**15)
        reference = prose(reference, 'reference')
        disputes.actor(self.store, actor, 'approver')
        with self.store.atomic():
            existing = self.store.one('SELECT * FROM refunds WHERE refund_id=?', (refund_id,))
            if existing:
                if (existing['contract_id'], existing['amount_cents'], existing['reference']) != (contract_id, amount_cents, reference):
                    reject('refund_conflict', 'Refund identifier already binds another payment')
                return existing
            balance = self.refundable(contract_id)
            if amount_cents > balance['available_cents']:
                reject('refund_capacity', 'Refund exceeds net customer credit')
            self.store.execute("INSERT INTO refunds VALUES(?,?,?,?,'posted',?,?,?)", (refund_id, contract_id, amount_cents, balance['currency'], reference, actor, self.store.clock()))
            self.store.log('refund.posted', {'refund_id': refund_id, 'contract_id': contract_id, 'amount_cents': amount_cents})
            return self.store.one('SELECT * FROM refunds WHERE refund_id=?', (refund_id,))


def applied_credit(store, invoice_id):
    table = store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='credit_applications'")
    if not table:
        return 0
    return store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications WHERE debit_invoice_id=? AND state='active'", (invoice_id,))

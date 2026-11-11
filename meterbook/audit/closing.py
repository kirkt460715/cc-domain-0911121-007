import json
from collections import defaultdict
from meterbook.values import month, key, canonical, digest, reject
from meterbook.contracts import registry
from meterbook.settlement import invoices, payments


def ledger_trial(store, contract_id, period=None):
    registry.get(store, contract_id)
    if period is not None:
        month(period)
    query = 'SELECT e.*,i.period,i.contract_id FROM ledger_entries e JOIN invoices i ON i.invoice_id=e.invoice_id WHERE i.contract_id=?'
    params = [contract_id]
    if period:
        query += ' AND i.period=?'
        params.append(period)
    rows = store.all(query + ' ORDER BY e.position', params)
    totals = defaultdict(int)
    per_invoice = defaultdict(int)
    for row in rows:
        totals[row['account']] += row['amount_cents']
        per_invoice[row['invoice_id']] += row['amount_cents']
    failures = [{'invoice_id': identity, 'imbalance_cents': amount} for identity, amount in sorted(per_invoice.items()) if amount]
    return {'contract_id': contract_id, 'period': period, 'accounts': dict(sorted(totals.items())), 'balance_cents': sum(totals.values()), 'entries': len(rows), 'balanced': not failures and sum(totals.values()) == 0, 'failures': failures}


def receivables(store, contract_id):
    contract = registry.get(store, contract_id)
    credit_table = store.one("SELECT name FROM sqlite_master WHERE name='credit_applications'")
    refund_table = store.one("SELECT name FROM sqlite_master WHERE name='refunds'")
    detail = []
    for invoice in invoices.listing(store, contract_id, 'closed'):
        balance = payments.invoice_balance(store, invoice['invoice_id'])
        incoming = outgoing = 0
        if credit_table:
            incoming = store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications WHERE debit_invoice_id=? AND state='active'", (invoice['invoice_id'],))
            outgoing = store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM credit_applications WHERE credit_invoice_id=? AND state='active'", (invoice['invoice_id'],))
        detail.append({**balance, 'period': invoice['period'], 'credit_in_cents': incoming, 'credit_out_cents': outgoing, 'effective_cents': balance['unpaid_cents'] - incoming + outgoing})
    statement = payments.statement(store, contract_id)
    refunds = store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE contract_id=? AND state='posted'", (contract_id,)) if refund_table else 0
    unapplied = statement['unallocated_cents']
    net = sum(row['effective_cents'] for row in detail) - unapplied + refunds
    expected = statement['billed_cents'] - statement['received_cents'] + refunds
    return {'contract_id': contract_id, 'currency': contract['currency'], 'invoices': detail, 'billed_cents': statement['billed_cents'], 'received_cents': statement['received_cents'], 'unapplied_payment_cents': unapplied, 'refund_cents': refunds, 'net_due_cents': net, 'balanced': net == expected, 'trial': ledger_trial(store, contract_id)}


def period_bridge(store, contract_id, period):
    month(period)
    chain = invoices.chain(store, contract_id, period)
    rows = []
    running = 0
    prior_usage = {}
    for invoice in chain['invoices']:
        calculation = invoice['snapshot']['calculation']
        usage = {item['usage_id']: item for line in calculation['lines'] for item in line['usage']}
        changed = []
        for identity in sorted(set(prior_usage) | set(usage)):
            before = prior_usage.get(identity, {}).get('amount_cents', 0)
            after = usage.get(identity, {}).get('amount_cents', 0)
            if prior_usage.get(identity) != usage.get(identity):
                changed.append({'usage_id': identity, 'before_cents': before, 'after_cents': after, 'delta_cents': after - before})
        running += invoice['total_cents']
        rows.append({'invoice_id': invoice['invoice_id'], 'kind': invoice['kind'], 'delta_cents': invoice['total_cents'], 'running_cents': running, 'recalculated_cents': calculation['total_cents'], 'changes': changed, 'balanced': running == calculation['total_cents']})
        prior_usage = usage
    return {'contract_id': contract_id, 'period': period, 'steps': rows, 'balanced': all(row['balanced'] for row in rows), 'total_cents': running}


class Statements:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('statement_transaction', 'Initialize statements outside transactions')
        store.execute('CREATE TABLE IF NOT EXISTS account_statements(statement_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,through_position INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL)')

    def capture(self, statement_id, contract_id):
        key(statement_id)
        with self.store.atomic():
            if self.store.one('SELECT statement_id FROM account_statements WHERE statement_id=?', (statement_id,)):
                reject('statement_exists', 'Statement identity is immutable')
            report = receivables(self.store, contract_id)
            if not report['balanced'] or not report['trial']['balanced']:
                reject('statement_imbalance', 'Cannot capture unbalanced account')
            position = self.store.scalar('SELECT COALESCE(MAX(position),0) FROM audit')
            self.store.execute('INSERT INTO account_statements VALUES(?,?,?,?,?,?)', (statement_id, contract_id, position, canonical(report), digest(report), self.store.clock()))
            return self.get(statement_id)

    def get(self, statement_id):
        row = self.store.one('SELECT * FROM account_statements WHERE statement_id=?', (statement_id,))
        if row is None:
            reject('statement_missing', 'Account statement does not exist')
        row['payload'] = json.loads(row['payload'])
        if digest(row['payload']) != row['digest']:
            reject('statement_corrupt', 'Account statement checksum differs')
        row['current'] = digest(receivables(self.store, row['contract_id'])) == row['digest']
        return row

    def compare(self, before, after):
        left = self.get(before)
        right = self.get(after)
        if left['contract_id'] != right['contract_id']:
            reject('statement_scope', 'Statements belong to different contracts')
        fields = ('billed_cents', 'received_cents', 'refund_cents', 'net_due_cents')
        return {'before': before, 'after': after, 'delta': {field: right['payload'][field] - left['payload'][field] for field in fields}}

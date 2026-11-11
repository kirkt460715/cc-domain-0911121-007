import json
from decimal import Decimal
from meterbook.values import fields, key, decimal, encoded, canonical, digest, reject, number
from meterbook.settlement import invoices, pricing
from meterbook.events import disputes

DDL = '''
CREATE TABLE IF NOT EXISTS allocation_rules(rule_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS allocation_rule_history(rule_id TEXT NOT NULL,revision INTEGER NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(rule_id,revision));
CREATE TABLE IF NOT EXISTS cost_allocations(allocation_id TEXT PRIMARY KEY,invoice_id TEXT NOT NULL,invoice_digest TEXT NOT NULL,rule_id TEXT NOT NULL,rule_revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def normalize(value):
    fields(value, {'weights'}, {'meter_overrides'})
    def weights(mapping):
        if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 100:
            reject('cost_weights', 'Cost allocation requires one to one hundred centers')
        result = {key(center): encoded(decimal(weight, 'weight')) for center, weight in sorted(mapping.items())}
        if not sum(Decimal(weight) for weight in result.values()):
            reject('cost_zero', 'Cost allocation weights cannot all be zero')
        return result
    defaults = weights(value['weights'])
    overrides = value.get('meter_overrides', {})
    if not isinstance(overrides, dict) or len(overrides) > 100:
        reject('cost_overrides', 'Meter overrides must be bounded mapping')
    return {'weights': defaults, 'meter_overrides': {key(meter): weights(mapping) for meter, mapping in sorted(overrides.items())}}


class CostCenters:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('cost_transaction', 'Initialize cost centers outside transactions')
        store.db.executescript(DDL)

    def put(self, rule_id, expected_revision, definition):
        key(rule_id)
        normalized = normalize(definition)
        with self.store.atomic():
            previous = self.store.one('SELECT revision FROM allocation_rules WHERE rule_id=?', (rule_id,))
            revision = previous['revision'] if previous else 0
            if revision != expected_revision:
                reject('cost_rule_stale', 'Cost allocation rule changed')
            self.store.execute('INSERT INTO allocation_rules VALUES(?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET revision=excluded.revision,definition=excluded.definition,digest=excluded.digest', (rule_id, revision + 1, canonical(normalized), digest(normalized)))
            self.store.execute('INSERT INTO allocation_rule_history VALUES(?,?,?,?)', (rule_id, revision + 1, canonical(normalized), digest(normalized)))
            self.store.log('cost.rule_saved', {'rule_id': rule_id, 'revision': revision + 1})
            return self.rule(rule_id)

    def rule(self, rule_id, revision=None):
        if revision is None:
            row = self.store.one('SELECT * FROM allocation_rules WHERE rule_id=?', (rule_id,))
        else:
            row = self.store.one('SELECT * FROM allocation_rule_history WHERE rule_id=? AND revision=?', (rule_id, revision))
        if row is None:
            reject('cost_rule_missing', 'Cost allocation rule version does not exist')
        row['definition'] = json.loads(row['definition'])
        if digest(row['definition']) != row['digest']:
            reject('cost_rule_corrupt', 'Cost allocation rule checksum differs')
        return row

    def allocate(self, allocation_id, invoice_id, rule_id):
        key(allocation_id)
        with self.store.atomic():
            if self.store.one('SELECT allocation_id FROM cost_allocations WHERE allocation_id=?', (allocation_id,)):
                reject('cost_allocation_exists', 'Cost allocation identifier exists')
            invoice = invoices.get(self.store, invoice_id)
            if invoice['state'] != 'closed':
                reject('cost_invoice', 'Cost allocations require a closed invoice')
            rule = self.rule(rule_id)
            lines = []
            totals = {}
            for line in invoice['snapshot']['lines']:
                actual = line['after'] or line['before']
                meter = actual['meter']
                weights = rule['definition']['meter_overrides'].get(meter, rule['definition']['weights'])
                allocated = pricing.allocate(line['amount_cents'], weights)
                lines.append({'line_id': line['line_id'], 'meter': meter, 'amount_cents': line['amount_cents'], 'centers': allocated})
                for center, amount in allocated.items():
                    totals[center] = totals.get(center, 0) + amount
            payload = {'invoice_id': invoice_id, 'invoice_digest': invoice['digest'], 'currency': invoice['currency'], 'rule_id': rule_id, 'rule_revision': rule['revision'], 'rule_digest': rule['digest'], 'lines': lines, 'centers': dict(sorted(totals.items())), 'total_cents': sum(totals.values())}
            if payload['total_cents'] != invoice['total_cents']:
                reject('cost_balance', 'Cost allocations do not balance to invoice')
            self.store.execute('INSERT INTO cost_allocations VALUES(?,?,?,?,?,?,?,?)', (allocation_id, invoice_id, invoice['digest'], rule_id, rule['revision'], canonical(payload), digest(payload), self.store.clock()))
            self.store.log('cost.allocated', {'allocation_id': allocation_id, 'invoice_id': invoice_id, 'total_cents': payload['total_cents']})
            return self.get(allocation_id)

    def get(self, allocation_id):
        row = self.store.one('SELECT * FROM cost_allocations WHERE allocation_id=?', (allocation_id,))
        if row is None:
            reject('cost_allocation_missing', 'Cost allocation does not exist')
        row['payload'] = json.loads(row['payload'])
        if digest(row['payload']) != row['digest']:
            reject('cost_allocation_corrupt', 'Cost allocation checksum differs')
        if sum(row['payload']['centers'].values()) != row['payload']['total_cents']:
            reject('cost_allocation_balance', 'Cost centers do not balance')
        return row

    def statement(self, contract_id):
        rows = self.store.all('SELECT a.allocation_id FROM cost_allocations a JOIN invoices i ON i.invoice_id=a.invoice_id WHERE i.contract_id=? ORDER BY a.created_at,a.allocation_id', (contract_id,))
        result = {}
        seen = set()
        for row in rows:
            allocation = self.get(row['allocation_id'])
            if allocation['invoice_id'] in seen:
                reject('cost_duplicate_invoice', 'Choose one allocation per invoice before aggregating')
            seen.add(allocation['invoice_id'])
            for center, amount in allocation['payload']['centers'].items():
                result[center] = result.get(center, 0) + amount
        return {'contract_id': contract_id, 'centers': dict(sorted(result.items())), 'total_cents': sum(result.values()), 'invoices': sorted(seen)}

import json
from decimal import Decimal
from meterbook.values import fields, key, number, month, decimal, encoded, canonical, digest, reject
from meterbook.contracts import registry
from meterbook.events import reconcile
from meterbook.settlement import pricing, invoices

DDL = '''
CREATE TABLE IF NOT EXISTS budgets(budget_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,period TEXT NOT NULL,revision INTEGER NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_history(budget_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(budget_id,revision));
CREATE TABLE IF NOT EXISTS budget_assessments(assessment_id TEXT PRIMARY KEY,budget_id TEXT NOT NULL,budget_revision INTEGER NOT NULL,run_id TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def normalize(value):
    fields(value, {'maximum_cents'}, {'warning_percent', 'meter_limits'})
    maximum = number(value['maximum_cents'], 'maximum_cents', 0, 10**15)
    warning = number(value.get('warning_percent', 80), 'warning_percent', 1, 100)
    limits = value.get('meter_limits', {})
    if not isinstance(limits, dict) or len(limits) > 100:
        reject('budget_meters', 'Budget meter limits must be a bounded mapping')
    return {'maximum_cents': maximum, 'warning_percent': warning, 'meter_limits': {key(meter): encoded(decimal(quantity, 'quantity')) for meter, quantity in sorted(limits.items())}}


class Budgets:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('budget_transaction', 'Initialize budgets outside transactions')
        store.db.executescript(DDL)

    def put(self, budget_id, contract_id, period, expected_revision, definition):
        key(budget_id)
        month(period)
        normalized = normalize(definition)
        contract = registry.get(self.store, contract_id)
        if set(normalized['meter_limits']) - set(contract['definition']['meters']):
            reject('budget_meter', 'Budget refers to unknown meter')
        with self.store.atomic():
            previous = self.store.one('SELECT * FROM budgets WHERE budget_id=?', (budget_id,))
            revision = previous['revision'] if previous else 0
            if revision != expected_revision:
                reject('budget_stale', 'Budget revision changed')
            if previous and (previous['contract_id'], previous['period']) != (contract_id, period):
                reject('budget_identity', 'Budget contract and period cannot change')
            self.store.execute('INSERT INTO budgets VALUES(?,?,?,?,?,?) ON CONFLICT(budget_id) DO UPDATE SET revision=excluded.revision,definition=excluded.definition,digest=excluded.digest', (budget_id, contract_id, period, revision + 1, canonical(normalized), digest(normalized)))
            result = self.get(budget_id)
            self.store.execute('INSERT INTO budget_history VALUES(?,?,?)', (budget_id, revision + 1, canonical(result)))
            self.store.log('budget.saved', {'budget_id': budget_id, 'revision': revision + 1})
            return result

    def get(self, budget_id):
        row = self.store.one('SELECT * FROM budgets WHERE budget_id=?', (budget_id,))
        if row is None:
            reject('budget_missing', 'Budget does not exist')
        row['definition'] = json.loads(row['definition'])
        if digest(row['definition']) != row['digest']:
            reject('budget_corrupt', 'Budget definition checksum mismatch')
        return row

    def assess(self, assessment_id, budget_id, run_id):
        key(assessment_id)
        with self.store.atomic():
            budget = self.get(budget_id)
            calculated = pricing.calculate(self.store, run_id)
            if (budget['contract_id'], budget['period']) != (calculated['contract_id'], calculated['period']):
                reject('budget_scope', 'Budget and reconciliation have different scopes')
            if self.store.one('SELECT assessment_id FROM budget_assessments WHERE assessment_id=?', (assessment_id,)):
                reject('budget_assessment_exists', 'Assessment identifiers are immutable')
            rules = budget['definition']
            projected = calculated['total_cents']
            findings = []
            if projected > rules['maximum_cents']:
                findings.append({'code': 'amount_exceeded', 'actual_cents': projected, 'maximum_cents': rules['maximum_cents']})
            elif projected * 100 >= rules['warning_percent'] * rules['maximum_cents']:
                findings.append({'code': 'amount_warning', 'actual_cents': projected, 'maximum_cents': rules['maximum_cents']})
            for meter, maximum in rules['meter_limits'].items():
                actual = Decimal(calculated['meter_quantities'].get(meter, '0'))
                if actual > Decimal(maximum):
                    findings.append({'code': 'meter_exceeded', 'meter': meter, 'actual': encoded(actual), 'maximum': maximum})
            closed = invoices.chain(self.store, budget['contract_id'], budget['period'])
            payload = {'budget_id': budget_id, 'budget_revision': budget['revision'], 'run_id': run_id, 'calculation_digest': calculated['digest'], 'projected_cents': projected, 'already_closed_cents': closed['cumulative_cents'], 'unclosed_delta_cents': projected - closed['cumulative_cents'], 'findings': findings, 'headroom_cents': rules['maximum_cents'] - projected, 'effect': 'advisory_only'}
            self.store.execute('INSERT INTO budget_assessments VALUES(?,?,?,?,?,?,?)', (assessment_id, budget_id, budget['revision'], run_id, canonical(payload), digest(payload), self.store.clock()))
            self.store.log('budget.assessed', {'assessment_id': assessment_id, 'budget_id': budget_id, 'findings': findings})
            return self.assessment(assessment_id)

    def assessment(self, assessment_id):
        row = self.store.one('SELECT * FROM budget_assessments WHERE assessment_id=?', (assessment_id,))
        if row is None:
            reject('budget_assessment_missing', 'Budget assessment does not exist')
        row['payload'] = json.loads(row['payload'])
        if digest(row['payload']) != row['digest']:
            reject('budget_assessment_corrupt', 'Budget assessment checksum differs')
        row['fresh'] = self.get(row['budget_id'])['revision'] == row['budget_revision'] and reconcile.get(self.store, row['run_id'])['fresh']
        if row['fresh']:
            row['fresh'] = pricing.calculate(self.store, row['run_id'])['digest'] == row['payload']['calculation_digest']
        return row

    def history(self, budget_id):
        self.get(budget_id)
        return [json.loads(row['payload']) for row in self.store.all('SELECT payload FROM budget_history WHERE budget_id=? ORDER BY revision', (budget_id,))]

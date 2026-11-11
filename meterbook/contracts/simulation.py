from decimal import Decimal
from meterbook.values import fields, decimal, encoded, digest, reject, key, prose, canonical
from meterbook.contracts import rates, registry
from meterbook.events import reconcile, disputes
from meterbook.settlement import pricing
import json

DDL = '''
CREATE TABLE IF NOT EXISTS rate_proposals(proposal_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,base_fingerprint TEXT NOT NULL,candidate TEXT NOT NULL,analysis TEXT NOT NULL,analysis_hash TEXT NOT NULL,author TEXT NOT NULL,state TEXT NOT NULL,reviewer TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def compare_quantities(before, after, quantities):
    left = rates.normalize(before)
    right = rates.normalize(after)
    if not isinstance(quantities, list) or not 1 <= len(quantities) <= 1000:
        reject('simulation_quantities', 'Rate simulation needs a bounded quantity list')
    rows = []
    for quantity in quantities:
        old = pricing.tier_charge(quantity, left)
        new = pricing.tier_charge(quantity, right)
        rows.append({'quantity': old['quantity'], 'before_cents': old['amount_cents'], 'after_cents': new['amount_cents'], 'delta_cents': new['amount_cents'] - old['amount_cents'], 'before': old, 'after': new})
    return {'rows': rows, 'before_digest': digest(left), 'after_digest': digest(right)}


class RateDesk:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('proposal_transaction', 'Initialize rate proposals outside transactions')
        store.db.executescript(DDL)

    def fingerprint(self, contract_id):
        contract = registry.get(self.store, contract_id)
        return digest({'rates': rates.fingerprint(self.store, contract_id), 'contract_revision': contract['revision'], 'period_revisions': self.store.all('SELECT period,revision FROM period_revisions WHERE contract_id=? ORDER BY period', (contract_id,))})

    def simulate(self, candidate, run_ids):
        fields(candidate, {'rate_id', 'contract_id', 'meter', 'start_at', 'end_at', 'definition'}, {'withdraw_rate_id'})
        if not isinstance(run_ids, list) or len(run_ids) > 100:
            reject('proposal_runs', 'Proposal accepts up to one hundred reconciliation runs')
        before = []
        for run_id in run_ids:
            run = reconcile.get(self.store, run_id)
            if run['contract_id'] != candidate['contract_id']:
                reject('proposal_scope', 'Reconciliation belongs to another contract')
            before.append(pricing.calculate(self.store, run_id))
        with self.store.atomic():
            self.store.execute('SAVEPOINT rate_simulation')
            try:
                if candidate.get('withdraw_rate_id'):
                    old = rates.get(self.store, candidate['withdraw_rate_id'])
                    if old['contract_id'] != candidate['contract_id']:
                        reject('proposal_scope', 'Withdrawn rate belongs to another contract')
                    rates.withdraw(self.store, old['rate_id'], 'Rate proposal simulation')
                added = rates.add(self.store, **{name: value for name, value in candidate.items() if name != 'withdraw_rate_id'})
                after = [pricing.calculate(self.store, run_id) for run_id in run_ids]
            finally:
                self.store.execute('ROLLBACK TO rate_simulation')
                self.store.execute('RELEASE rate_simulation')
            return {'candidate': candidate, 'runs': [{'run_id': old['run_id'], 'before_cents': old['total_cents'], 'after_cents': new['total_cents'], 'delta_cents': new['total_cents'] - old['total_cents'], 'before_digest': old['digest'], 'after_digest': new['digest']} for old, new in zip(before, after)], 'base_fingerprint': self.fingerprint(candidate['contract_id'])}

    def submit(self, proposal_id, candidate, run_ids, author, reason):
        key(proposal_id)
        disputes.actor(self.store, author)
        reason = prose(reason, 'reason')
        with self.store.atomic():
            if self.store.one('SELECT proposal_id FROM rate_proposals WHERE proposal_id=?', (proposal_id,)):
                reject('proposal_exists', 'Rate proposal identifier exists')
            analysis = self.simulate(candidate, run_ids)
            self.store.execute("INSERT INTO rate_proposals VALUES(?,?,?,?,?,?,?,'pending',NULL,?,?)", (proposal_id, candidate['contract_id'], analysis['base_fingerprint'], canonical(candidate), canonical(analysis), digest(analysis), author, reason, self.store.clock()))
            self.store.log('rate.proposal_submitted', {'proposal_id': proposal_id, 'analysis_hash': digest(analysis)})
            return self.get(proposal_id)

    def get(self, proposal_id):
        row = self.store.one('SELECT * FROM rate_proposals WHERE proposal_id=?', (proposal_id,))
        if row is None:
            reject('proposal_missing', 'Rate proposal does not exist')
        row['candidate'] = json.loads(row['candidate'])
        row['analysis'] = json.loads(row['analysis'])
        if digest(row['analysis']) != row['analysis_hash']:
            reject('proposal_corrupt', 'Rate proposal analysis checksum differs')
        return row

    def decide(self, proposal_id, reviewer, accept, expected_analysis_hash):
        disputes.actor(self.store, reviewer, 'approver')
        if type(accept) is not bool:
            reject('proposal_decision', 'Proposal decision must be boolean')
        with self.store.atomic():
            proposal = self.get(proposal_id)
            if proposal['state'] != 'pending':
                reject('proposal_terminal', 'Proposal already decided')
            if proposal['author'] == reviewer:
                reject('proposal_separation', 'Proposal author cannot approve own change')
            if proposal['analysis_hash'] != expected_analysis_hash:
                reject('proposal_analysis', 'Reviewer must acknowledge current analysis')
            if accept:
                if self.fingerprint(proposal['contract_id']) != proposal['base_fingerprint']:
                    reject('proposal_stale', 'Contract, rates or usage changed after analysis')
                candidate = proposal['candidate']
                if candidate.get('withdraw_rate_id'):
                    rates.withdraw(self.store, candidate['withdraw_rate_id'], proposal['reason'])
                rates.add(self.store, **{name: value for name, value in candidate.items() if name != 'withdraw_rate_id'})
            state = 'applied' if accept else 'rejected'
            self.store.execute('UPDATE rate_proposals SET state=?,reviewer=? WHERE proposal_id=?', (state, reviewer, proposal_id))
            self.store.log('rate.proposal_decided', {'proposal_id': proposal_id, 'state': state, 'reviewer': reviewer})
            return self.get(proposal_id)

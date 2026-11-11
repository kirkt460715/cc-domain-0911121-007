from meterbook.values import exact
import json
from decimal import Decimal
from meterbook.values import key, month, canonical, digest, reject, encoded
from meterbook.events import receiver
from meterbook.contracts import registry


def identities(store, contract_id, period):
    result = set()
    rows = store.all('SELECT v.usage_id,v.payload FROM usage_heads h JOIN usage_versions v ON v.contract_id=h.contract_id AND v.side=h.side AND v.usage_id=h.usage_id AND v.revision=h.revision WHERE h.contract_id=?', (contract_id,))
    for row in rows:
        if json.loads(row['payload'])['at'][:7] == period:
            result.add(row['usage_id'])
    for row in store.all("SELECT usage_id,decision FROM resolutions WHERE contract_id=? AND state='active'", (contract_id,)):
        decision = json.loads(row['decision'])
        if decision['action'] == 'accept' and decision['usage']['at'][:7] == period:
            result.add(row['usage_id'])
    return sorted(result)


def pair(store, contract_id, usage_id):
    provider = receiver.head(store, contract_id, 'provider', usage_id)
    consumer = receiver.head(store, contract_id, 'consumer', usage_id)
    def evidence(row):
        if row is None:
            return None
        return {key: row[key] for key in ('revision', 'event_id', 'payload', 'state')}
    return {'usage_id': usage_id, 'provider': evidence(provider), 'consumer': evidence(consumer)}


@exact
def classify(store, contract_id, usage_id, period):
    contract = registry.get(store, contract_id)
    evidence = pair(store, contract_id, usage_id)
    provider = evidence['provider']
    consumer = evidence['consumer']
    base_hash = digest(evidence)
    chosen = None
    reasons = []
    status = 'disputed'
    if provider is None or consumer is None:
        reasons.append('missing_provider' if provider is None else 'missing_consumer')
    elif provider['state'] == consumer['state'] == 'cancelled':
        status = 'cancelled'
    elif provider['state'] != consumer['state']:
        reasons.append('cancellation_mismatch')
    else:
        left = provider['payload']
        right = consumer['payload']
        for field in ('meter', 'at', 'purpose', 'region', 'dimensions'):
            if left[field] != right[field]:
                reasons.append(field + '_mismatch')
        delta = abs(Decimal(left['quantity']) - Decimal(right['quantity']))
        tolerance = Decimal(contract['definition']['tolerance'].get(left['meter'], '0'))
        if delta > tolerance:
            reasons.append('quantity_mismatch')
        if not reasons:
            status = 'matched' if delta == 0 else 'within_tolerance'
            chosen = dict(left)
    resolution = store.one("SELECT * FROM resolutions WHERE contract_id=? AND usage_id=? AND state='active'", (contract_id, usage_id))
    resolution_info = None
    if resolution:
        resolution_info = {'revision': resolution['revision'], 'base_hash': resolution['base_hash'], 'fresh': resolution['base_hash'] == base_hash}
        if resolution['base_hash'] == base_hash:
            decision = json.loads(resolution['decision'])
            if decision['action'] == 'exclude':
                status = 'excluded'
                chosen = None
            else:
                chosen = decision['usage']
                status = 'resolved'
            reasons = []
    if chosen is not None and chosen['at'][:7] != period:
        status = 'outside_period'
        chosen = None
    if chosen is not None:
        allowed = registry.authorize(store, contract_id, chosen)
        if not allowed['allowed']:
            status = 'disputed'
            reasons = ['current_contract_' + reason for reason in allowed['failures']]
            chosen = None
    return {'usage_id': usage_id, 'status': status, 'chosen': chosen, 'reasons': reasons, 'base_hash': base_hash, 'evidence': evidence, 'resolution': resolution_info}


def run(store, run_id, contract_id, period):
    key(run_id)
    month(period)
    with store.atomic():
        contract = registry.get(store, contract_id)
        if store.one('SELECT run_id FROM reconciliations WHERE run_id=?', (run_id,)):
            reject('reconciliation_exists', 'Reconciliation run identifiers are immutable')
        rows = [classify(store, contract_id, identity, period) for identity in identities(store, contract_id, period)]
        definition = {'selection': 'current_usage_heads', 'quantity_rule': 'provider_quantity_when_within_absolute_tolerance', 'statuses': {status: sum(row['status'] == status for row in rows) for status in sorted({row['status'] for row in rows})}, 'usage_ids': [row['usage_id'] for row in rows]}
        sha = digest({'definition': definition, 'rows': rows})
        store.execute('INSERT INTO reconciliations VALUES(?,?,?,?,?,?,?,?)', (run_id, contract_id, period, store.revision(contract_id, period), contract['revision'], canonical(definition), sha, store.clock()))
        for row in rows:
            store.execute('INSERT INTO matches VALUES(?,?,?,?,?)', (run_id, row['usage_id'], row['status'], canonical(row), digest(row)))
        store.log('reconciliation.completed', {'run_id': run_id, 'contract_id': contract_id, 'period': period, 'statuses': definition['statuses']})
        return get(store, run_id)


def get(store, run_id):
    row = store.one('SELECT * FROM reconciliations WHERE run_id=?', (run_id,))
    if row is None:
        reject('reconciliation_missing', 'Reconciliation run does not exist')
    row['definition'] = json.loads(row['definition'])
    row['rows'] = []
    for item in store.all('SELECT * FROM matches WHERE run_id=? ORDER BY usage_id', (run_id,)):
        payload = json.loads(item['payload'])
        if digest(payload) != item['digest'] or payload['status'] != item['status']:
            reject('reconciliation_row_corrupt', 'Stored reconciliation row differs')
        row['rows'].append(payload)
    if digest({'definition': row['definition'], 'rows': row['rows']}) != row['digest']:
        reject('reconciliation_corrupt', 'Reconciliation manifest differs')
    row['fresh'] = row['revision'] == store.revision(row['contract_id'], row['period']) and row['contract_revision'] == registry.get(store, row['contract_id'])['revision']
    return row


def compare(store, before, after):
    left = get(store, before)
    right = get(store, after)
    if (left['contract_id'], left['period']) != (right['contract_id'], right['period']):
        reject('reconciliation_scope', 'Only matching contract periods can be compared')
    old = {row['usage_id']: row for row in left['rows']}
    new = {row['usage_id']: row for row in right['rows']}
    return {'before': before, 'after': after, 'changes': [{'usage_id': identity, 'before': old.get(identity), 'after': new.get(identity)} for identity in sorted(set(old) | set(new)) if old.get(identity) != new.get(identity)]}

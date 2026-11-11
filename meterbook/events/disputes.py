import json
from meterbook.values import key, prose, fields, canonical, digest, reject, period_of
from meterbook.events import reconcile, protocol
from meterbook.contracts import registry


def register(store, actor_id, role):
    key(actor_id)
    if role not in {'analyst', 'approver'}:
        reject('actor_role', 'Unknown settlement role')
    with store.atomic():
        existing = store.one('SELECT * FROM actors WHERE actor_id=?', (actor_id,))
        if existing:
            if existing['role'] != role:
                reject('actor_conflict', 'Actor role is immutable')
            return existing
        store.execute("INSERT INTO actors VALUES(?,?,'active')", (actor_id, role))
        store.log('actor.registered', {'actor_id': actor_id, 'role': role})
        return actor(store, actor_id)


def actor(store, actor_id, role=None):
    row = store.one('SELECT * FROM actors WHERE actor_id=?', (actor_id,))
    if row is None or row['state'] != 'active' or (role and row['role'] != role):
        reject('actor_permission', 'Actor lacks the required active role')
    return row


def decide(store, contract_id, usage_id, expected_revision, expected_base_hash, decision, reviewer, reason):
    actor(store, reviewer, 'approver')
    reason = prose(reason, 'reason')
    fields(decision, {'action'}, {'usage'})
    if decision['action'] not in {'exclude', 'accept'}:
        reject('resolution_action', 'Resolution must exclude or accept explicit usage')
    if decision['action'] == 'accept':
        if 'usage' not in decision:
            reject('resolution_usage', 'Accept requires explicit reconciled usage')
        normalized = {'action': 'accept', 'usage': protocol.usage(decision['usage'])}
        authorization = registry.authorize(store, contract_id, normalized['usage'])
        if not authorization['allowed']:
            reject('resolution_authorization', 'Resolution violates contract', failures=authorization['failures'])
    else:
        if 'usage' in decision:
            reject('resolution_usage', 'Exclusion cannot carry chargeable usage')
        normalized = {'action': 'exclude'}
    with store.atomic():
        pair = reconcile.pair(store, contract_id, usage_id)
        if pair['provider'] is None and pair['consumer'] is None:
            reject('resolution_missing_usage', 'Neither side has reported this usage')
        actual_hash = digest(pair)
        if actual_hash != expected_base_hash:
            reject('resolution_stale_evidence', 'Usage changed since review')
        previous = store.one('SELECT * FROM resolutions WHERE contract_id=? AND usage_id=?', (contract_id, usage_id))
        revision = previous['revision'] if previous else 0
        if revision != expected_revision:
            reject('resolution_stale', 'Resolution revision changed')
        store.execute("INSERT INTO resolutions VALUES(?,?,?,'active',?,?,?,?,?) ON CONFLICT(contract_id,usage_id) DO UPDATE SET revision=excluded.revision,state=excluded.state,base_hash=excluded.base_hash,decision=excluded.decision,reason=excluded.reason,actor=excluded.actor,created_at=excluded.created_at", (contract_id, usage_id, revision + 1, actual_hash, canonical(normalized), reason, reviewer, store.clock()))
        row = get(store, contract_id, usage_id)
        store.execute('INSERT INTO resolution_history VALUES(?,?,?,?)', (contract_id, usage_id, revision + 1, canonical(row)))
        periods = {period_of(pair[side]['payload']['at']) for side in ('provider', 'consumer') if pair[side]}
        if previous:
            old_decision = json.loads(previous['decision'])
            if old_decision['action'] == 'accept':
                periods.add(period_of(old_decision['usage']['at']))
        if normalized['action'] == 'accept':
            periods.add(period_of(normalized['usage']['at']))
        for period in sorted(periods):
            store.touch(contract_id, period)
        store.log('dispute.resolved', {'contract_id': contract_id, 'usage_id': usage_id, 'revision': revision + 1, 'reviewer': reviewer})
        return row


def get(store, contract_id, usage_id):
    row = store.one('SELECT * FROM resolutions WHERE contract_id=? AND usage_id=?', (contract_id, usage_id))
    if row is None:
        reject('resolution_missing', 'Usage resolution does not exist')
    row['decision'] = json.loads(row['decision'])
    row['fresh'] = digest(reconcile.pair(store, contract_id, usage_id)) == row['base_hash']
    return row


def history(store, contract_id, usage_id):
    get(store, contract_id, usage_id)
    return [json.loads(row['payload']) for row in store.all('SELECT payload FROM resolution_history WHERE contract_id=? AND usage_id=? ORDER BY revision', (contract_id, usage_id))]


def queue(store, contract_id, period):
    registry.get(store, contract_id)
    rows = [reconcile.classify(store, contract_id, identity, period) for identity in reconcile.identities(store, contract_id, period)]
    return [row for row in rows if row['status'] == 'disputed']

import json
from meterbook.values import fields, key, number, instant, canonical, decimal, encoded, prose, reject


def normalize(value):
    fields(value, {'valid_from', 'valid_until', 'meters', 'purposes', 'regions'}, {'tolerance', 'quota', 'payment_days'})
    start = instant(value['valid_from'])
    end = instant(value['valid_until'])
    if start >= end:
        reject('contract_interval', 'Contract interval must be nonempty')
    result = {'valid_from': start, 'valid_until': end}
    for dimension in ('meters', 'purposes', 'regions'):
        values = value[dimension]
        if not isinstance(values, list) or not 1 <= len(values) <= 100 or len(set(values)) != len(values):
            reject('contract_dimension', 'Contract dimensions require distinct nonempty symbol lists')
        result[dimension] = sorted(key(item, dimension) for item in values)
    tolerance = value.get('tolerance', {})
    quotas = value.get('quota', {})
    for mapping in (tolerance, quotas):
        if not isinstance(mapping, dict) or set(mapping) - set(result['meters']):
            reject('contract_meter', 'Meter settings reference an unknown meter')
    result['tolerance'] = {meter: encoded(decimal(tolerance.get(meter, '0'), 'tolerance')) for meter in result['meters']}
    result['quota'] = {meter: encoded(decimal(value, 'quota')) for meter, value in sorted(quotas.items())}
    result['payment_days'] = number(value.get('payment_days', 30), 'payment_days', 0, 365)
    return result


def get(store, contract_id):
    row = store.one('SELECT * FROM contracts WHERE contract_id=?', (contract_id,))
    if row is None:
        reject('contract_missing', 'Contract does not exist')
    row['definition'] = json.loads(row['definition'])
    return row


def create(store, contract_id, provider, consumer, currency, definition):
    for value in (contract_id, provider, consumer):
        key(value)
    if provider == consumer:
        reject('contract_parties', 'Provider and consumer must be distinct')
    if currency not in {'CNY', 'USD', 'EUR'}:
        reject('contract_currency', 'Unsupported two-decimal currency')
    normalized = normalize(definition)
    with store.atomic():
        if store.one('SELECT contract_id FROM contracts WHERE contract_id=?', (contract_id,)):
            reject('contract_exists', 'Contract already exists')
        store.execute("INSERT INTO contracts VALUES(?,?,?,?,1,'active',?,?)", (contract_id, provider, consumer, currency, canonical(normalized), store.clock()))
        result = get(store, contract_id)
        store.execute('INSERT INTO contract_history VALUES(?,?,?)', (contract_id, 1, canonical(result)))
        for side in ('provider', 'consumer'):
            store.execute('INSERT INTO stream_heads VALUES(?,?,0)', (contract_id, side))
        store.log('contract.created', {'contract_id': contract_id, 'currency': currency})
        return result


def revise(store, contract_id, expected_revision, definition, reason):
    normalized = normalize(definition)
    reason = prose(reason, 'reason')
    with store.atomic():
        previous = get(store, contract_id)
        if previous['revision'] != expected_revision:
            reject('contract_stale', 'Contract revision changed')
        if previous['state'] != 'active':
            reject('contract_closed', 'Closed contracts cannot be revised')
        existing_meters = {row['meter'] for row in store.all("SELECT DISTINCT meter FROM rates WHERE contract_id=? AND state='active'", (contract_id,))}
        if existing_meters - set(normalized['meters']):
            reject('contract_rate_meter', 'Active rate uses a removed meter')
        store.execute('UPDATE contracts SET revision=revision+1,definition=? WHERE contract_id=?', (canonical(normalized), contract_id))
        result = get(store, contract_id)
        store.execute('INSERT INTO contract_history VALUES(?,?,?)', (contract_id, result['revision'], canonical(result)))
        store.log('contract.revised', {'contract_id': contract_id, 'revision': result['revision'], 'reason': reason})
        return result


def close(store, contract_id, expected_revision, reason):
    reason = prose(reason, 'reason')
    with store.atomic():
        previous = get(store, contract_id)
        if previous['revision'] != expected_revision:
            reject('contract_stale', 'Contract revision changed')
        if previous['state'] == 'closed':
            return previous
        store.execute("UPDATE contracts SET state='closed',revision=revision+1 WHERE contract_id=?", (contract_id,))
        result = get(store, contract_id)
        store.execute('INSERT INTO contract_history VALUES(?,?,?)', (contract_id, result['revision'], canonical(result)))
        store.log('contract.closed', {'contract_id': contract_id, 'reason': reason})
        return result


def authorize(store, contract_id, usage):
    contract = get(store, contract_id)
    definition = contract['definition']
    failures = []
    if usage['meter'] not in definition['meters']:
        failures.append('meter')
    if usage['purpose'] not in definition['purposes']:
        failures.append('purpose')
    if usage['region'] not in definition['regions']:
        failures.append('region')
    if not definition['valid_from'] <= usage['at'] < definition['valid_until']:
        failures.append('validity')
    return {'allowed': not failures, 'failures': failures, 'contract_revision': contract['revision']}


def listing(store):
    return [get(store, row['contract_id']) for row in store.all('SELECT contract_id FROM contracts ORDER BY contract_id')]


def history(store, contract_id):
    get(store, contract_id)
    return [json.loads(row['payload']) for row in store.all('SELECT payload FROM contract_history WHERE contract_id=? ORDER BY revision', (contract_id,))]

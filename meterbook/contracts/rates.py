import json
from decimal import Decimal, localcontext
from meterbook.values import fields, key, instant, decimal, encoded, canonical, digest, reject, prose
from meterbook.contracts import registry


def normalize(value):
    fields(value, {'tiers'}, {'included', 'minimum_charge', 'maximum_charge'})
    tiers = value['tiers']
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 100:
        reject('rate_tiers', 'Rate requires one to one hundred tiers')
    result = []
    previous = Decimal(0)
    for index, tier in enumerate(tiers):
        fields(tier, {'up_to', 'unit_price'})
        bound = None if tier['up_to'] is None else decimal(tier['up_to'], 'up_to')
        price = decimal(tier['unit_price'], 'unit_price', maximum=Decimal('1e9'), scale=8)
        if bound is None and index != len(tiers) - 1:
            reject('rate_infinity', 'Unbounded tier must be last')
        if bound is not None and bound <= previous:
            reject('rate_order', 'Tier boundaries must strictly increase')
        if bound is not None:
            previous = bound
        result.append({'up_to': encoded(bound) if bound is not None else None, 'unit_price': encoded(price)})
    if result[-1]['up_to'] is not None:
        reject('rate_unbounded', 'Final tier must cover all remaining usage')
    included = decimal(value.get('included', '0'), 'included')
    minimum = decimal(value.get('minimum_charge', '0'), 'minimum_charge', scale=2)
    maximum = None if value.get('maximum_charge') is None else decimal(value['maximum_charge'], 'maximum_charge', scale=2)
    if maximum is not None and maximum < minimum:
        reject('rate_caps', 'Maximum charge cannot be below minimum')
    return {'tiers': result, 'included': encoded(included), 'minimum_charge': encoded(minimum), 'maximum_charge': encoded(maximum) if maximum is not None else None}


def add(store, rate_id, contract_id, meter, start_at, end_at, definition):
    key(rate_id)
    key(meter)
    start = instant(start_at)
    end = instant(end_at)
    normalized = normalize(definition)
    if start >= end:
        reject('rate_interval', 'Rate interval must be nonempty')
    with store.atomic():
        contract = registry.get(store, contract_id)
        if meter not in contract['definition']['meters'] or contract['state'] != 'active':
            reject('rate_contract', 'Rate requires an active contract and declared meter')
        if start < contract['definition']['valid_from'] or end > contract['definition']['valid_until']:
            reject('rate_coverage', 'Rate interval must lie inside contract validity')
        if store.one('SELECT rate_id FROM rates WHERE rate_id=?', (rate_id,)):
            reject('rate_exists', 'Rate identifier exists')
        overlap = store.all("SELECT rate_id FROM rates WHERE contract_id=? AND meter=? AND state='active' AND start_at<? AND end_at>?", (contract_id, meter, end, start))
        if overlap:
            reject('rate_overlap', 'Active rate intervals cannot overlap', rates=overlap)
        store.execute("INSERT INTO rates VALUES(?,?,?,?,?,?,?,'active')", (rate_id, contract_id, meter, start, end, canonical(normalized), digest(normalized)))
        store.log('rate.added', {'rate_id': rate_id, 'contract_id': contract_id, 'meter': meter})
        return get(store, rate_id)


def get(store, rate_id):
    row = store.one('SELECT * FROM rates WHERE rate_id=?', (rate_id,))
    if row is None:
        reject('rate_missing', 'Rate does not exist')
    row['definition'] = json.loads(row['definition'])
    if digest(row['definition']) != row['digest']:
        reject('rate_corrupt', 'Rate checksum mismatch')
    return row


def at(store, contract_id, meter, timestamp):
    timestamp = instant(timestamp)
    rows = store.all("SELECT rate_id FROM rates WHERE contract_id=? AND meter=? AND state='active' AND start_at<=? AND end_at>?", (contract_id, meter, timestamp, timestamp))
    if len(rows) != 1:
        reject('rate_uncovered', 'Usage timestamp must resolve to exactly one active rate', contract_id=contract_id, meter=meter, at=timestamp)
    return get(store, rows[0]['rate_id'])


def withdraw(store, rate_id, reason):
    reason = prose(reason, 'reason')
    with store.atomic():
        rate = get(store, rate_id)
        if rate['state'] != 'active':
            return rate
        store.execute("UPDATE rates SET state='withdrawn' WHERE rate_id=?", (rate_id,))
        store.log('rate.withdrawn', {'rate_id': rate_id, 'reason': reason})
        return get(store, rate_id)


def listing(store, contract_id, meter=None):
    registry.get(store, contract_id)
    query = 'SELECT rate_id FROM rates WHERE contract_id=?'
    args = [contract_id]
    if meter:
        query += ' AND meter=?'
        args.append(meter)
    return [get(store, row['rate_id']) for row in store.all(query + ' ORDER BY meter,start_at,rate_id', args)]


def fingerprint(store, contract_id):
    return digest(listing(store, contract_id))

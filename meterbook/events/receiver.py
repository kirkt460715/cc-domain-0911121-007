import json
from meterbook.values import canonical, digest, reject, number, period_of, MeterError
from meterbook.events import protocol
from meterbook.contracts import registry


def get(store, event_id):
    row = store.one('SELECT * FROM events WHERE event_id=?', (event_id,))
    if row is None:
        reject('event_missing', 'Usage event does not exist')
    row['payload'] = json.loads(row['payload'])
    row['error'] = json.loads(row['error']) if row['error'] else None
    if digest(row['payload']) != row['digest']:
        reject('event_corrupt', 'Event digest differs from stored payload')
    return row


def receive(store, value):
    normalized = protocol.event(value)
    sha = digest(normalized)
    with store.atomic():
        registry.get(store, normalized['contract_id'])
        existing = store.one('SELECT event_id,digest FROM events WHERE event_id=?', (normalized['event_id'],))
        if existing:
            if existing['digest'] != sha:
                reject('event_conflict', 'Event ID already binds different content')
            return {'duplicate': True, 'event': get(store, normalized['event_id'])}
        slot = store.one('SELECT event_id FROM events WHERE contract_id=? AND side=? AND sequence=?', (normalized['contract_id'], normalized['side'], normalized['sequence']))
        if slot:
            reject('event_sequence_conflict', 'Stream sequence already occupied', existing=slot['event_id'])
        store.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?, 'pending',NULL,?)", (normalized['event_id'], normalized['contract_id'], normalized['side'], normalized['sequence'], normalized['usage_id'], normalized['revision'], canonical(normalized), sha, store.clock()))
        store.log('event.received', {'event_id': normalized['event_id'], 'sequence': normalized['sequence'], 'side': normalized['side']})
        return {'duplicate': False, 'event': get(store, normalized['event_id'])}


def head(store, contract_id, side, usage_id):
    row = store.one('SELECT v.* FROM usage_heads h JOIN usage_versions v ON v.contract_id=h.contract_id AND v.side=h.side AND v.usage_id=h.usage_id AND v.revision=h.revision WHERE h.contract_id=? AND h.side=? AND h.usage_id=?', (contract_id, side, usage_id))
    if row:
        row['payload'] = json.loads(row['payload'])
    return row


def apply(store, event_id):
    row = get(store, event_id)
    value = row['payload']
    prior = head(store, value['contract_id'], value['side'], value['usage_id'])
    expected = prior['revision'] + 1 if prior else 1
    if value['revision'] != expected:
        reject('usage_revision', 'Usage revision does not follow current head', expected=expected, received=value['revision'])
    if value['action'] == 'report' and prior is not None:
        reject('usage_exists', 'Usage already reported')
    if value['action'] != 'report' and prior is None:
        reject('usage_predecessor', 'Correction has no predecessor')
    if prior and prior['state'] == 'cancelled':
        reject('usage_terminal', 'Cancelled usage identity cannot be reactivated')
    payload = prior['payload'] if value['action'] == 'cancel' else value['usage']
    if value['action'] != 'cancel':
        decision = registry.authorize(store, value['contract_id'], payload)
        if not decision['allowed']:
            reject('usage_authorization', 'Reported usage violates contract', failures=decision['failures'])
    state = 'cancelled' if value['action'] == 'cancel' else 'active'
    store.execute('INSERT INTO usage_versions VALUES(?,?,?,?,?,?,?)', (value['contract_id'], value['side'], value['usage_id'], value['revision'], event_id, canonical(payload), state))
    store.execute('INSERT INTO usage_heads VALUES(?,?,?,?,?) ON CONFLICT(contract_id,side,usage_id) DO UPDATE SET revision=excluded.revision,event_id=excluded.event_id', (value['contract_id'], value['side'], value['usage_id'], value['revision'], event_id))
    periods = {period_of(payload['at'])}
    if prior:
        periods.add(period_of(prior['payload']['at']))
    resolution = store.one('SELECT decision FROM resolutions WHERE contract_id=? AND usage_id=?', (value['contract_id'], value['usage_id']))
    if resolution:
        chosen = json.loads(resolution['decision'])
        if chosen['action'] == 'accept':
            periods.add(period_of(chosen['usage']['at']))
    for period in sorted(periods):
        store.touch(value['contract_id'], period)
    store.execute("UPDATE events SET state='applied',error=NULL WHERE event_id=?", (event_id,))
    store.log('event.applied', {'event_id': event_id, 'periods': sorted(periods), 'usage_revision': value['revision']})
    return get(store, event_id)


def drain(store, contract_id, side, limit=100):
    number(limit, 'limit', 1, 10000)
    if side not in {'provider', 'consumer'}:
        reject('event_side', 'Unknown stream side')
    with store.atomic():
        registry.get(store, contract_id)
        cursor = store.scalar('SELECT sequence FROM stream_heads WHERE contract_id=? AND side=?', (contract_id, side))
        applied = []
        blocked = None
        for _ in range(limit):
            row = store.one('SELECT event_id,state FROM events WHERE contract_id=? AND side=? AND sequence=?', (contract_id, side, cursor + 1))
            if row is None:
                break
            if row['state'] == 'rejected':
                blocked = get(store, row['event_id'])
                break
            if row['state'] == 'skipped':
                cursor += 1
                continue
            try:
                with store.atomic():
                    apply(store, row['event_id'])
                applied.append(row['event_id'])
                cursor += 1
            except MeterError as exc:
                store.execute("UPDATE events SET state='rejected',error=? WHERE event_id=?", (canonical(exc.payload()), row['event_id']))
                store.log('event.rejected', {'event_id': row['event_id'], 'error': exc.payload()})
                blocked = get(store, row['event_id'])
                break
        store.execute('UPDATE stream_heads SET sequence=? WHERE contract_id=? AND side=?', (cursor, contract_id, side))
        return {'contract_id': contract_id, 'side': side, 'cursor': cursor, 'applied': applied, 'blocked': blocked}


def skip(store, event_id, reason):
    from meterbook.values import prose
    reason = prose(reason, 'reason')
    with store.atomic():
        row = get(store, event_id)
        if row['state'] != 'rejected':
            reject('event_skip_state', 'Only rejected events may be explicitly skipped')
        cursor = store.scalar('SELECT sequence FROM stream_heads WHERE contract_id=? AND side=?', (row['contract_id'], row['side']))
        if row['sequence'] != cursor + 1:
            reject('event_skip_order', 'Only the next blocked stream event can be skipped')
        store.execute("UPDATE events SET state='skipped' WHERE event_id=?", (event_id,))
        store.log('event.skipped', {'event_id': event_id, 'reason': reason})
        return drain(store, row['contract_id'], row['side'])


def history(store, contract_id, side, usage_id):
    rows = store.all('SELECT * FROM usage_versions WHERE contract_id=? AND side=? AND usage_id=? ORDER BY revision', (contract_id, side, usage_id))
    for row in rows:
        row['payload'] = json.loads(row['payload'])
    return rows


def stream_status(store, contract_id, side):
    registry.get(store, contract_id)
    cursor = store.scalar('SELECT sequence FROM stream_heads WHERE contract_id=? AND side=?', (contract_id, side))
    if cursor is None:
        reject('event_side', 'Unknown stream side')
    pending = store.all('SELECT sequence,event_id,state FROM events WHERE contract_id=? AND side=? AND sequence>? ORDER BY sequence', (contract_id, side, cursor))
    maximum = max([cursor] + [row['sequence'] for row in pending])
    present = {row['sequence'] for row in pending}
    gaps = []
    start = cursor + 1
    for sequence in sorted(present):
        if start < sequence:
            gaps.append({'from': start, 'until_inclusive': sequence - 1})
        start = sequence + 1
    return {'cursor': cursor, 'maximum_received': maximum, 'pending': pending, 'gaps': gaps}

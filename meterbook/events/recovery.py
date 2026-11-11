import json
from meterbook.values import canonical, digest, reject, prose, number
from meterbook.events import receiver, protocol
from meterbook.contracts import registry


def inspect(store, contract_id, side):
    status = receiver.stream_status(store, contract_id, side)
    problems = []
    for row in store.all('SELECT event_id,sequence,state FROM events WHERE contract_id=? AND side=? ORDER BY sequence', (contract_id, side)):
        event = receiver.get(store, row['event_id'])
        if row['sequence'] <= status['cursor'] and row['state'] not in {'applied', 'skipped'}:
            problems.append({'code': 'stream_cursor', 'event_id': row['event_id']})
        if row['sequence'] > status['cursor'] and row['state'] == 'applied':
            problems.append({'code': 'stream_ahead', 'event_id': row['event_id']})
    return {'contract_id': contract_id, 'side': side, 'status': status, 'problems': problems, 'replayable': not problems}


def replay(store, contract_id, side):
    registry.get(store, contract_id)
    rows = store.all('SELECT event_id FROM events WHERE contract_id=? AND side=? ORDER BY sequence', (contract_id, side))
    expected_heads = store.all('SELECT * FROM usage_heads WHERE contract_id=? AND side=? ORDER BY usage_id', (contract_id, side))
    current_cursor = receiver.stream_status(store, contract_id, side)['cursor']
    events = [receiver.get(store, row['event_id']) for row in rows]
    reconstructed = {}
    failures = []
    for event in events:
        if event['sequence'] > current_cursor:
            continue
        if event['state'] == 'skipped':
            continue
        value = event['payload']
        previous = reconstructed.get(value['usage_id'])
        expected_revision = previous['revision'] + 1 if previous else 1
        if value['revision'] != expected_revision:
            failures.append({'code': 'replay_revision', 'event_id': value['event_id']})
            continue
        payload = previous['payload'] if value['action'] == 'cancel' and previous else value['usage']
        reconstructed[value['usage_id']] = {'revision': value['revision'], 'event_id': value['event_id'], 'payload': payload, 'state': 'cancelled' if value['action'] == 'cancel' else 'active'}
    expected = {row['usage_id']: {'revision': row['revision'], 'event_id': row['event_id'], 'payload': receiver.head(store, contract_id, side, row['usage_id'])['payload'], 'state': receiver.head(store, contract_id, side, row['usage_id'])['state']} for row in expected_heads}
    if reconstructed != expected:
        failures.append({'code': 'replay_projection', 'expected_digest': digest(expected), 'replayed_digest': digest(reconstructed)})
    return {'ok': not failures, 'failures': failures, 'cursor': current_cursor, 'usage_ids': sorted(reconstructed), 'digest': digest(reconstructed)}


def retry_rejected(store, event_id, reason):
    reason = prose(reason, 'reason')
    with store.atomic():
        row = receiver.get(store, event_id)
        if row['state'] != 'rejected':
            reject('event_retry_state', 'Only rejected events can be retried')
        status = receiver.stream_status(store, row['contract_id'], row['side'])
        if row['sequence'] != status['cursor'] + 1:
            reject('event_retry_order', 'Only the next rejected event can be retried')
        store.execute("UPDATE events SET state='pending',error=NULL WHERE event_id=?", (event_id,))
        store.log('event.retry_requested', {'event_id': event_id, 'reason': reason})
        return receiver.drain(store, row['contract_id'], row['side'])


def export_stream(store, contract_id, side, after=0, limit=1000):
    number(after, 'after')
    number(limit, 'limit', 1, 10000)
    registry.get(store, contract_id)
    rows = store.all('SELECT event_id FROM events WHERE contract_id=? AND side=? AND sequence>? ORDER BY sequence LIMIT ?', (contract_id, side, after, limit))
    events = [receiver.get(store, row['event_id'])['payload'] for row in rows]
    payload = {'format': 'meterbook-event-stream', 'version': 1, 'contract_id': contract_id, 'side': side, 'after': after, 'events': events}
    return {'payload': payload, 'sha256': digest(payload)}


def import_stream(store, envelope):
    from meterbook.values import fields
    fields(envelope, {'payload', 'sha256'})
    payload = envelope['payload']
    if digest(payload) != envelope['sha256']:
        reject('stream_digest', 'Stream envelope checksum mismatch')
    fields(payload, {'format', 'version', 'contract_id', 'side', 'after', 'events'})
    if payload['format'] != 'meterbook-event-stream' or payload['version'] != 1:
        reject('stream_format', 'Unsupported stream format')
    with store.atomic():
        result = []
        previous = payload['after']
        for value in payload['events']:
            if value['contract_id'] != payload['contract_id'] or value['side'] != payload['side'] or value['sequence'] <= previous:
                reject('stream_order', 'Envelope scope or event order differs')
            previous = value['sequence']
            event = {key: item for key, item in value.items() if not (key in {'reason', 'usage'} and item is None)}
            result.append(receiver.receive(store, event))
        return {'received': len(result), 'duplicates': sum(row['duplicate'] for row in result)}

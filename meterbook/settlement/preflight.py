from meterbook.values import month, reject, MeterError
from meterbook.events import reconcile, receiver
from meterbook.contracts import registry, rates
from meterbook.audit import explanations
from meterbook.settlement import invoices, pricing


def readiness(store, contract_id, period):
    month(period)
    contract = registry.get(store, contract_id)
    problems = []
    for side in ('provider', 'consumer'):
        stream = receiver.stream_status(store, contract_id, side)
        if stream['gaps']:
            problems.append({'code': 'stream_gap', 'side': side, 'gaps': stream['gaps']})
        if any(row['state'] == 'rejected' for row in stream['pending']):
            problems.append({'code': 'rejected_event', 'side': side})
        elif stream['pending']:
            problems.append({'code': 'unprocessed_events', 'side': side})
    rows = [reconcile.classify(store, contract_id, identity, period) for identity in reconcile.identities(store, contract_id, period)]
    if any(row['status'] == 'disputed' for row in rows):
        problems.append({'code': 'disputed_usage', 'usage_ids': [row['usage_id'] for row in rows if row['status'] == 'disputed']})
    uncovered = []
    for row in rows:
        if row['chosen']:
            try:
                rates.at(store, contract_id, row['chosen']['meter'], row['chosen']['at'])
            except MeterError as exc:
                uncovered.append({'usage_id': row['usage_id'], 'error': exc.payload()})
    if uncovered:
        problems.append({'code': 'uncovered_usage', 'usages': uncovered})
    control = store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='period_controls'")
    if control and store.one('SELECT contract_id FROM period_controls WHERE contract_id=? AND period=?', (contract_id, period)):
        from meterbook.contracts.calendar import Calendar
        calendar = object.__new__(Calendar)
        calendar.store = store
        checked = calendar.readiness(contract_id, period)
        if not checked['ready']:
            problems.append({'code': 'period_controls', 'details': checked})
    head = invoices.current(store, contract_id, period)
    return {'ready': not problems, 'problems': problems, 'contract_id': contract_id, 'period': period, 'contract_revision': contract['revision'], 'period_revision': store.revision(contract_id, period), 'invoice_kind': 'adjustment' if head else 'original', 'parent_invoice_id': head['invoice_id'] if head else None, 'usage_count': len(rows)}


def estimate_adjustment(store, run_id):
    run = reconcile.get(store, run_id)
    calculated = pricing.calculate(store, run_id)
    previous = invoices.current(store, run['contract_id'], run['period'])
    old_total = previous['snapshot']['calculation']['total_cents'] if previous else 0
    delta = calculated['total_cents'] - old_total
    old_usage = {}
    if previous:
        for line in previous['snapshot']['calculation']['lines']:
            for usage in line['usage']:
                old_usage[usage['usage_id']] = usage
    new_usage = {usage['usage_id']: usage for line in calculated['lines'] for usage in line['usage']}
    changes = []
    for identity in sorted(set(old_usage) | set(new_usage)):
        before = old_usage.get(identity)
        after = new_usage.get(identity)
        if before != after:
            changes.append({'usage_id': identity, 'before_cents': before['amount_cents'] if before else 0, 'after_cents': after['amount_cents'] if after else 0, 'delta_cents': (after['amount_cents'] if after else 0) - (before['amount_cents'] if before else 0), 'evidence_changed': (before or {}).get('base_hash') != (after or {}).get('base_hash')})
    return {'run_id': run_id, 'parent_invoice_id': previous['invoice_id'] if previous else None, 'before_total_cents': old_total, 'after_total_cents': calculated['total_cents'], 'adjustment_cents': delta, 'usage_changes': changes, 'allocation_delta_balanced': sum(row['delta_cents'] for row in changes) == delta}


def pending_adjustments(store):
    result = []
    for row in store.all('SELECT * FROM invoice_heads ORDER BY contract_id,period'):
        invoice = invoices.get(store, row['invoice_id'])
        snapshot = invoice['snapshot']['calculation']
        reasons = []
        if snapshot['period_revision'] != store.revision(row['contract_id'], row['period']):
            reasons.append('usage_changed')
        if snapshot['contract_revision'] != registry.get(store, row['contract_id'])['revision']:
            reasons.append('contract_changed')
        if snapshot['rates_fingerprint'] != rates.fingerprint(store, row['contract_id']):
            reasons.append('rates_changed')
        if reasons:
            result.append({'contract_id': row['contract_id'], 'period': row['period'], 'invoice_id': row['invoice_id'], 'reasons': reasons})
    return result

import json
from meterbook.values import digest, MeterError
from meterbook.contracts import registry, rates
from meterbook.events import receiver
from meterbook.settlement import invoices, payments


def journal(store):
    previous = '0' * 64
    count = 0
    for row in store.all('SELECT * FROM audit ORDER BY position'):
        expected = digest([previous, row['action'], row['body'], row['at']])
        if row['previous_hash'] != previous or row['entry_hash'] != expected:
            return {'ok': False, 'position': row['position'], 'count': count}
        previous = expected
        count += 1
    return {'ok': True, 'count': count, 'head': previous}


def check(store):
    problems = []
    database = store.integrity()
    chain = journal(store)
    if not database['ok']:
        problems.append({'code': 'database', 'details': database})
    if not chain['ok']:
        problems.append({'code': 'audit', 'details': chain})
    for row in store.all('SELECT event_id FROM events ORDER BY event_id'):
        try:
            receiver.get(store, row['event_id'])
        except MeterError as exc:
            problems.append(exc.payload())
    for head in store.all('SELECT * FROM stream_heads ORDER BY contract_id,side'):
        applied = store.all('SELECT sequence,state FROM events WHERE contract_id=? AND side=? AND sequence<=? ORDER BY sequence', (head['contract_id'], head['side'], head['sequence']))
        if [row['sequence'] for row in applied] != list(range(1, head['sequence'] + 1)) or any(row['state'] not in {'applied', 'skipped'} for row in applied):
            problems.append({'code': 'stream_prefix', 'contract_id': head['contract_id'], 'side': head['side']})
    for row in store.all('SELECT * FROM usage_heads ORDER BY contract_id,side,usage_id'):
        history = receiver.history(store, row['contract_id'], row['side'], row['usage_id'])
        if [item['revision'] for item in history] != list(range(1, row['revision'] + 1)):
            problems.append({'code': 'usage_history', 'usage_id': row['usage_id']})
        if not history or history[-1]['event_id'] != row['event_id']:
            problems.append({'code': 'usage_head', 'usage_id': row['usage_id']})
    for row in store.all('SELECT invoice_id FROM invoices ORDER BY invoice_id'):
        try:
            invoice = invoices.get(store, row['invoice_id'])
            ledger = store.all('SELECT * FROM ledger_entries WHERE invoice_id=? ORDER BY position', (row['invoice_id'],))
            if invoice['state'] == 'closed':
                if len(ledger) != 2 or sum(item['amount_cents'] for item in ledger) != 0:
                    problems.append({'code': 'ledger_balance', 'invoice_id': row['invoice_id']})
                elif {item['account']: item['amount_cents'] for item in ledger} != {'receivable': invoice['total_cents'], 'revenue': -invoice['total_cents']}:
                    problems.append({'code': 'ledger_amount', 'invoice_id': row['invoice_id']})
            elif ledger:
                problems.append({'code': 'premature_ledger', 'invoice_id': row['invoice_id']})
        except MeterError as exc:
            problems.append(exc.payload())
    for row in store.all('SELECT * FROM invoice_heads'):
        try:
            result = invoices.chain(store, row['contract_id'], row['period'])
            if result['cumulative_cents'] != result['latest_recalculated_cents']:
                problems.append({'code': 'adjustment_chain_total', 'contract_id': row['contract_id'], 'period': row['period']})
        except MeterError as exc:
            problems.append(exc.payload())
    for row in store.all('SELECT payment_id FROM payments'):
        payment = payments.get(store, row['payment_id'])
        if payment['allocated_cents'] > payment['amount_cents']:
            problems.append({'code': 'payment_overallocated', 'payment_id': row['payment_id']})
    return {'ok': not problems, 'problems': problems, 'database': database, 'audit': chain}


def overview(store):
    return {'contracts': store.scalar('SELECT COUNT(*) FROM contracts'), 'events': {row['state']: row['count'] for row in store.all('SELECT state,COUNT(*) AS count FROM events GROUP BY state')}, 'invoices': {row['state']: row['count'] for row in store.all('SELECT state,COUNT(*) AS count FROM invoices GROUP BY state')}, 'ledger_totals': store.all('SELECT currency,account,SUM(amount_cents) AS amount_cents FROM ledger_entries GROUP BY currency,account ORDER BY currency,account')}


def history(store, after=0, limit=100):
    from meterbook.values import number
    number(after, 'after')
    number(limit, 'limit', 1, 1000)
    rows = store.all('SELECT * FROM audit WHERE position>? ORDER BY position LIMIT ?', (after, limit))
    for row in rows:
        row['body'] = json.loads(row['body'])
    return rows

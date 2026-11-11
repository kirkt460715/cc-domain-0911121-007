import datetime
import json
from meterbook.values import key, number, canonical, digest, prose, reject
from meterbook.settlement import pricing
from meterbook.contracts import rates, registry
from meterbook.events import reconcile, disputes


def get(store, invoice_id):
    row = store.one('SELECT * FROM invoices WHERE invoice_id=?', (invoice_id,))
    if row is None:
        reject('invoice_missing', 'Invoice does not exist')
    row['snapshot'] = json.loads(row['snapshot'])
    if digest(row['snapshot']) != row['digest']:
        reject('invoice_corrupt', 'Invoice snapshot digest mismatch')
    row['lines'] = []
    for line in store.all('SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY line_id', (invoice_id,)):
        line['payload'] = json.loads(line['payload'])
        if line['amount_cents'] != line['payload']['amount_cents']:
            reject('invoice_line_corrupt', 'Invoice line amount differs from payload')
        row['lines'].append(line)
    stored_lines = [line['payload'] for line in row['lines']]
    if stored_lines != row['snapshot']['lines']:
        reject('invoice_line_corrupt', 'Stored lines differ from frozen snapshot')
    if row['total_cents'] != row['snapshot']['total_cents']:
        reject('invoice_total_corrupt', 'Stored total differs from frozen snapshot')
    calculated = row['snapshot']['calculation']
    if (row['contract_id'], row['period'], row['currency'], row['run_id']) != (calculated['contract_id'], calculated['period'], calculated['currency'], calculated['run_id']):
        reject('invoice_scope_corrupt', 'Invoice scope differs from frozen calculation')
    if row['parent_id'] != row['snapshot']['parent_id']:
        reject('invoice_parent_corrupt', 'Invoice parent differs from frozen snapshot')
    if sum(line['amount_cents'] for line in row['lines']) != row['total_cents']:
        reject('invoice_total_corrupt', 'Invoice lines do not sum to total')
    return row


def current(store, contract_id, period):
    row = store.one('SELECT invoice_id FROM invoice_heads WHERE contract_id=? AND period=?', (contract_id, period))
    return get(store, row['invoice_id']) if row else None


def prepare(store, invoice_id, run_id, kind='original', allow_disputes=False):
    key(invoice_id)
    if kind not in {'original', 'adjustment'}:
        reject('invoice_kind', 'Invoice kind must be original or adjustment')
    with store.atomic():
        if store.one('SELECT invoice_id FROM invoices WHERE invoice_id=?', (invoice_id,)):
            reject('invoice_exists', 'Invoice identifier is immutable')
        calculated = pricing.calculate(store, run_id, allow_disputes)
        parent = current(store, calculated['contract_id'], calculated['period'])
        if kind == 'original' and parent is not None:
            reject('invoice_period_closed', 'Period already has a closed invoice; prepare an adjustment')
        if kind == 'adjustment' and parent is None:
            reject('invoice_adjustment_parent', 'Adjustment requires a previously closed period')
        prior_calculation = parent['snapshot']['calculation'] if parent else None
        old_lines = {line['line_id']: line for line in prior_calculation['lines']} if prior_calculation else {}
        new_lines = {line['line_id']: line for line in calculated['lines']}
        lines = []
        for identity in sorted(set(old_lines) | set(new_lines)):
            before = old_lines.get(identity, {}).get('amount_cents', 0)
            after = new_lines.get(identity, {}).get('amount_cents', 0)
            delta = after - before
            if kind == 'adjustment' and delta == 0 and old_lines.get(identity) == new_lines.get(identity):
                continue
            lines.append({'line_id': identity, 'before_cents': before, 'after_cents': after, 'amount_cents': delta, 'before': old_lines.get(identity), 'after': new_lines.get(identity)})
        total = sum(line['amount_cents'] for line in lines)
        if abs(total) > 9000000000000000 or any(abs(line['amount_cents']) > 9000000000000000 for line in lines):
            reject('invoice_amount_limit', 'Invoice exceeds supported integer accounting range')
        if kind == 'adjustment' and not lines:
            reject('invoice_no_change', 'Recalculation contains no changed lines')
        snapshot = {'calculation': calculated, 'parent_id': parent['invoice_id'] if parent else None, 'parent_digest': parent['digest'] if parent else None, 'lines': lines, 'total_cents': total, 'allow_disputes': allow_disputes}
        store.execute("INSERT INTO invoices VALUES(?,?,?,?,?,1,'prepared',?,?,?,?,?, ?,NULL)", (invoice_id, calculated['contract_id'], calculated['period'], kind, snapshot['parent_id'], run_id, canonical(snapshot), digest(snapshot), total, calculated['currency'], store.clock()))
        for line in lines:
            store.execute('INSERT INTO invoice_lines VALUES(?,?,?,?)', (invoice_id, line['line_id'], canonical(line), line['amount_cents']))
        store.log('invoice.prepared', {'invoice_id': invoice_id, 'kind': kind, 'total_cents': total, 'parent_id': snapshot['parent_id']})
        return get(store, invoice_id)


def close(store, invoice_id, expected_revision, approver):
    disputes.actor(store, approver, 'approver')
    with store.atomic():
        invoice = get(store, invoice_id)
        if invoice['revision'] != expected_revision:
            reject('invoice_stale', 'Invoice revision changed')
        if invoice['state'] != 'prepared':
            reject('invoice_state', 'Only prepared invoices may close')
        run = reconcile.get(store, invoice['run_id'])
        if not run['fresh']:
            reject('invoice_usage_changed', 'Usage or contract changed after invoice preparation')
        calculation = invoice['snapshot']['calculation']
        if calculation['rates_fingerprint'] != rates.fingerprint(store, invoice['contract_id']):
            reject('invoice_rates_changed', 'Rate schedule changed after preparation')
        parent = current(store, invoice['contract_id'], invoice['period'])
        if (parent['invoice_id'] if parent else None) != invoice['parent_id']:
            reject('invoice_head_changed', 'Another invoice closed for this period')
        from meterbook.contracts.calendar import enforce_if_configured
        enforce_if_configured(store, invoice['contract_id'], invoice['period'])
        from meterbook.settlement.reviews import enforce
        enforce(store, invoice)
        verified = pricing.calculate(store, invoice['run_id'], invoice['snapshot']['allow_disputes'])
        if verified != calculation:
            reject('invoice_calculation_changed', 'Current calculation differs from frozen snapshot')
        store.execute("UPDATE invoices SET state='closed',revision=revision+1,closed_at=? WHERE invoice_id=?", (store.clock(), invoice_id))
        store.execute('INSERT INTO invoice_heads VALUES(?,?,?) ON CONFLICT(contract_id,period) DO UPDATE SET invoice_id=excluded.invoice_id', (invoice['contract_id'], invoice['period'], invoice_id))
        for account, amount in [('receivable', invoice['total_cents']), ('revenue', -invoice['total_cents'])]:
            store.execute('INSERT INTO ledger_entries(invoice_id,account,amount_cents,currency,at) VALUES(?,?,?,?,?)', (invoice_id, account, amount, invoice['currency'], store.clock()))
        store.log('invoice.closed', {'invoice_id': invoice_id, 'approver': approver, 'total_cents': invoice['total_cents']})
        return get(store, invoice_id)


def abandon(store, invoice_id, expected_revision, reason):
    reason = prose(reason, 'reason')
    with store.atomic():
        invoice = get(store, invoice_id)
        if invoice['revision'] != expected_revision:
            reject('invoice_stale', 'Invoice revision changed')
        if invoice['state'] != 'prepared':
            reject('invoice_state', 'Only prepared invoices may be abandoned')
        store.execute("UPDATE invoices SET state='abandoned',revision=revision+1 WHERE invoice_id=?", (invoice_id,))
        store.log('invoice.abandoned', {'invoice_id': invoice_id, 'reason': reason})
        return get(store, invoice_id)


def chain(store, contract_id, period):
    head = current(store, contract_id, period)
    result = []
    seen = set()
    while head:
        if head['invoice_id'] in seen:
            reject('invoice_chain_cycle', 'Invoice adjustment chain contains a cycle')
        seen.add(head['invoice_id'])
        result.append(head)
        head = get(store, head['parent_id']) if head['parent_id'] else None
    result.reverse()
    return {'contract_id': contract_id, 'period': period, 'invoices': result, 'cumulative_cents': sum(row['total_cents'] for row in result), 'latest_recalculated_cents': result[-1]['snapshot']['calculation']['total_cents'] if result else 0}


def listing(store, contract_id=None, state=None):
    conditions = []
    args = []
    if contract_id:
        conditions.append('contract_id=?')
        args.append(contract_id)
    if state:
        conditions.append('state=?')
        args.append(state)
    query = 'SELECT invoice_id FROM invoices'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    return [get(store, row['invoice_id']) for row in store.all(query + ' ORDER BY created_at,invoice_id', args)]

from meterbook.values import key, number, prose, reject
from meterbook.contracts import registry
from meterbook.settlement import invoices


def get(store, payment_id):
    row = store.one('SELECT * FROM payments WHERE payment_id=?', (payment_id,))
    if row is None:
        reject('payment_missing', 'Payment does not exist')
    row['allocations'] = store.all('SELECT invoice_id,amount_cents FROM payment_allocations WHERE payment_id=? ORDER BY invoice_id', (payment_id,))
    row['allocated_cents'] = sum(item['amount_cents'] for item in row['allocations'])
    row['unallocated_cents'] = row['amount_cents'] - row['allocated_cents']
    return row


def receive(store, payment_id, contract_id, amount_cents, currency, reference):
    key(payment_id)
    number(amount_cents, 'amount_cents', 1, 10**15)
    reference = prose(reference, 'reference')
    with store.atomic():
        contract = registry.get(store, contract_id)
        if currency != contract['currency']:
            reject('payment_currency', 'Payment currency differs from contract')
        previous = store.one('SELECT * FROM payments WHERE payment_id=?', (payment_id,))
        if previous:
            if (previous['contract_id'], previous['amount_cents'], previous['currency'], previous['reference']) != (contract_id, amount_cents, currency, reference):
                reject('payment_conflict', 'Payment identity already binds different data')
            return get(store, payment_id)
        store.execute("INSERT INTO payments VALUES(?,?,?,?,?,'received',?)", (payment_id, contract_id, amount_cents, currency, reference, store.clock()))
        store.log('payment.received', {'payment_id': payment_id, 'contract_id': contract_id, 'amount_cents': amount_cents})
        return get(store, payment_id)


def invoice_balance(store, invoice_id):
    invoice = invoices.get(store, invoice_id)
    allocated = store.scalar("SELECT COALESCE(SUM(a.amount_cents),0) FROM payment_allocations a JOIN payments p ON p.payment_id=a.payment_id WHERE a.invoice_id=? AND p.state='received'", (invoice_id,))
    return {'invoice_id': invoice_id, 'state': invoice['state'], 'total_cents': invoice['total_cents'], 'paid_cents': allocated, 'unpaid_cents': invoice['total_cents'] - allocated, 'currency': invoice['currency']}


def allocate(store, payment_id, invoice_id, amount_cents):
    number(amount_cents, 'amount_cents', 1, 10**15)
    with store.atomic():
        payment = get(store, payment_id)
        invoice = invoices.get(store, invoice_id)
        if payment['state'] != 'received' or invoice['state'] != 'closed':
            reject('payment_state', 'Allocation requires a live payment and closed invoice')
        if payment['contract_id'] != invoice['contract_id'] or payment['currency'] != invoice['currency']:
            reject('payment_scope', 'Payment and invoice belong to different contract or currency')
        existing = store.one('SELECT amount_cents FROM payment_allocations WHERE payment_id=? AND invoice_id=?', (payment_id, invoice_id))
        if existing:
            if existing['amount_cents'] != amount_cents:
                reject('payment_allocation_conflict', 'Payment has a different allocation to this invoice')
            return get(store, payment_id)
        if amount_cents > payment['unallocated_cents']:
            reject('payment_capacity', 'Payment has insufficient unallocated funds')
        balance = invoice_balance(store, invoice_id)
        from meterbook.settlement.credits import applied_credit
        if amount_cents > balance['unpaid_cents'] - applied_credit(store, invoice_id):
            reject('payment_overallocation', 'Allocation exceeds invoice outstanding amount')
        store.execute('INSERT INTO payment_allocations VALUES(?,?,?)', (payment_id, invoice_id, amount_cents))
        store.log('payment.allocated', {'payment_id': payment_id, 'invoice_id': invoice_id, 'amount_cents': amount_cents})
        return get(store, payment_id)


def reverse(store, payment_id, reason):
    reason = prose(reason, 'reason')
    with store.atomic():
        payment = get(store, payment_id)
        if payment['state'] == 'reversed':
            return payment
        store.execute("UPDATE payments SET state='reversed' WHERE payment_id=?", (payment_id,))
        store.log('payment.reversed', {'payment_id': payment_id, 'reason': reason})
        return get(store, payment_id)


def statement(store, contract_id):
    contract = registry.get(store, contract_id)
    closed = invoices.listing(store, contract_id, 'closed')
    total = sum(invoice['total_cents'] for invoice in closed)
    paid = store.scalar("SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE contract_id=? AND state='received'", (contract_id,))
    allocations = store.scalar("SELECT COALESCE(SUM(a.amount_cents),0) FROM payment_allocations a JOIN payments p ON p.payment_id=a.payment_id WHERE p.contract_id=? AND p.state='received'", (contract_id,))
    return {'contract_id': contract_id, 'currency': contract['currency'], 'billed_cents': total, 'received_cents': paid, 'allocated_cents': allocations, 'unallocated_cents': paid - allocations, 'net_due_cents': total - paid, 'invoice_balances': [invoice_balance(store, row['invoice_id']) for row in closed]}


def auto_allocate(store, payment_id):
    with store.atomic():
        payment = get(store, payment_id)
        if payment['state'] != 'received':
            reject('payment_state', 'Reversed payments cannot allocate')
        rows = [row for row in invoices.listing(store, payment['contract_id'], 'closed') if row['total_cents'] > 0]
        rows.sort(key=lambda row: (row['period'], row['closed_at'], row['invoice_id']))
        applied = []
        for invoice in rows:
            payment = get(store, payment_id)
            if not payment['unallocated_cents']:
                break
            if any(row['invoice_id'] == invoice['invoice_id'] for row in payment['allocations']):
                continue
            balance = invoice_balance(store, invoice['invoice_id'])
            from meterbook.settlement.credits import applied_credit
            amount = min(payment['unallocated_cents'], max(0, balance['unpaid_cents'] - applied_credit(store, invoice['invoice_id'])))
            if amount:
                allocate(store, payment_id, invoice['invoice_id'], amount)
                applied.append({'invoice_id': invoice['invoice_id'], 'amount_cents': amount})
        return {'payment': get(store, payment_id), 'applied': applied}

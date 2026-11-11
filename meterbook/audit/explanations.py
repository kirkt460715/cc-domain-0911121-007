from decimal import Decimal
from meterbook.values import month, month_bounds, encoded, number, reject
from meterbook.events import reconcile, receiver
from meterbook.contracts import registry, rates
from meterbook.settlement import invoices, payments


def usage_trace(store, contract_id, usage_id):
    registry.get(store, contract_id)
    provider = receiver.history(store, contract_id, 'provider', usage_id)
    consumer = receiver.history(store, contract_id, 'consumer', usage_id)
    if not provider and not consumer:
        reject('trace_missing', 'No reported usage exists')
    periods = sorted({row['payload']['at'][:7] for row in provider + consumer})
    billed = []
    for invoice in invoices.listing(store, contract_id):
        for line in invoice['snapshot']['calculation']['lines']:
            matching = [row for row in line['usage'] if row['usage_id'] == usage_id]
            if matching:
                billed.append({'invoice_id': invoice['invoice_id'], 'state': invoice['state'], 'kind': invoice['kind'], 'period': invoice['period'], 'rate_id': line['rate_id'], 'allocation': matching[0]})
    return {'contract_id': contract_id, 'usage_id': usage_id, 'provider_history': provider, 'consumer_history': consumer, 'periods': periods, 'current_pair': reconcile.pair(store, contract_id, usage_id), 'billing_history': billed}


def period_report(store, contract_id, period):
    month(period)
    rows = [reconcile.classify(store, contract_id, identity, period) for identity in reconcile.identities(store, contract_id, period)]
    status = {}
    reasons = {}
    quantities = {}
    for row in rows:
        status[row['status']] = status.get(row['status'], 0) + 1
        for reason in row['reasons']:
            reasons[reason] = reasons.get(reason, 0) + 1
        if row['chosen']:
            meter = row['chosen']['meter']
            quantities[meter] = quantities.get(meter, Decimal(0)) + Decimal(row['chosen']['quantity'])
    chain = invoices.chain(store, contract_id, period)
    return {'contract_id': contract_id, 'period': period, 'period_revision': store.revision(contract_id, period), 'statuses': status, 'dispute_reasons': reasons, 'matched_quantities': {key: encoded(value) for key, value in sorted(quantities.items())}, 'closed_invoice_ids': [row['invoice_id'] for row in chain['invoices']], 'cumulative_cents': chain['cumulative_cents'], 'latest_recalculated_cents': chain['latest_recalculated_cents'], 'adjustment_chain_balanced': chain['cumulative_cents'] == chain['latest_recalculated_cents']}


def rate_coverage(store, contract_id, period):
    contract = registry.get(store, contract_id)
    start, end = month_bounds(period)
    start = max(start, contract['definition']['valid_from'])
    end = min(end, contract['definition']['valid_until'])
    output = []
    for meter in contract['definition']['meters']:
        rows = [row for row in rates.listing(store, contract_id, meter) if row['state'] == 'active' and row['start_at'] < end and row['end_at'] > start]
        cursor = start
        gaps = []
        for row in rows:
            if row['start_at'] > cursor:
                gaps.append({'start': cursor, 'end': min(row['start_at'], end)})
            cursor = max(cursor, row['end_at'])
        if cursor < end:
            gaps.append({'start': cursor, 'end': end})
        output.append({'meter': meter, 'covered': not gaps and start < end, 'gaps': gaps, 'rate_ids': [row['rate_id'] for row in rows]})
    return {'contract_id': contract_id, 'period': period, 'meters': output, 'inside_contract': start < end}


def aging(store, contract_id, at):
    import datetime
    from meterbook.values import instant
    at = instant(at)
    today = datetime.datetime.fromisoformat(at.replace('Z', '+00:00'))
    contract = registry.get(store, contract_id)
    buckets = {'not_due': 0, 'days_1_30': 0, 'days_31_60': 0, 'days_61_90': 0, 'over_90': 0, 'credit': 0}
    rows = []
    for invoice in invoices.listing(store, contract_id, 'closed'):
        balance = payments.invoice_balance(store, invoice['invoice_id'])
        closed = datetime.datetime.fromisoformat(invoice['closed_at'].replace('Z', '+00:00'))
        due = closed + datetime.timedelta(days=contract['definition']['payment_days'])
        days = (today.date() - due.date()).days
        amount = balance['unpaid_cents']
        if amount < 0:
            bucket = 'credit'
        elif days <= 0:
            bucket = 'not_due'
        elif days <= 30:
            bucket = 'days_1_30'
        elif days <= 60:
            bucket = 'days_31_60'
        elif days <= 90:
            bucket = 'days_61_90'
        else:
            bucket = 'over_90'
        buckets[bucket] += amount
        rows.append({'invoice_id': invoice['invoice_id'], 'due_at': due.isoformat().replace('+00:00', 'Z'), 'days_past_due': max(0, days), 'bucket': bucket, 'unpaid_cents': amount})
    return {'contract_id': contract_id, 'currency': contract['currency'], 'at': at, 'buckets': buckets, 'invoices': rows}

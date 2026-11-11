from meterbook.values import exact
from decimal import Decimal, localcontext
from meterbook.values import decimal, encoded, cents, reject, digest
from meterbook.contracts import rates, registry
from meterbook.events import reconcile


@exact
def tier_charge(quantity, definition):
    quantity = decimal(quantity)
    included = Decimal(definition['included'])
    remaining = max(Decimal(0), quantity - included)
    charge = Decimal(0)
    lower = Decimal(0)
    details = []
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        for index, tier in enumerate(definition['tiers']):
            upper = None if tier['up_to'] is None else Decimal(tier['up_to'])
            available = remaining if upper is None else max(Decimal(0), upper - lower)
            used = min(remaining, available)
            price = Decimal(tier['unit_price'])
            amount = used * price
            details.append({'tier': index, 'quantity': encoded(used), 'unit_price': encoded(price), 'amount': encoded(amount), 'lower': encoded(lower), 'upper': encoded(upper) if upper is not None else None})
            charge += amount
            remaining -= used
            if upper is not None:
                lower = upper
            if remaining == 0:
                break
        uncapped = charge
        if quantity:
            charge = max(charge, Decimal(definition['minimum_charge']))
            if definition['maximum_charge'] is not None:
                charge = min(charge, Decimal(definition['maximum_charge']))
    return {'quantity': encoded(quantity), 'included_used': encoded(min(quantity, included)), 'tiers': details, 'uncapped_amount': encoded(uncapped), 'amount': encoded(charge), 'amount_cents': cents(charge), 'rounding': 'half_even_once_per_rate_segment'}


@exact
def allocate(total_cents, weights):
    if type(total_cents) is not int:
        reject('allocation_total', 'Allocation amount must be integer minor units')
    if not isinstance(weights, dict) or not weights:
        reject('allocation_weights', 'Allocation needs a nonempty keyed weight mapping')
    parsed = {key: decimal(value, 'weight') for key, value in weights.items()}
    total_weight = sum(parsed.values())
    if not total_weight:
        if total_cents:
            reject('allocation_zero', 'Nonzero charge cannot allocate to zero total weight')
        return {key: 0 for key in sorted(weights)}
    sign = -1 if total_cents < 0 else 1
    amount = abs(total_cents)
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        exact = {key: Decimal(amount) * weight / total_weight for key, weight in parsed.items()}
        result = {key: int(value) for key, value in exact.items()}
        remainder = amount - sum(result.values())
        order = sorted(exact, key=lambda key: (-(exact[key] - result[key]), key))
        for key in order[:remainder]:
            result[key] += 1
    return {key: sign * result[key] for key in sorted(result)}


@exact
def calculate(store, run_id, allow_disputes=False):
    if type(allow_disputes) is not bool:
        reject('pricing_disputes', 'allow_disputes must be boolean')
    run = reconcile.get(store, run_id)
    if not run['fresh']:
        reject('reconciliation_stale', 'Reconcile current usage before pricing')
    disputes = [row for row in run['rows'] if row['status'] == 'disputed']
    if disputes and not allow_disputes:
        reject('unresolved_disputes', 'Settlement contains unresolved usage disputes', usage_ids=[row['usage_id'] for row in disputes])
    contract = registry.get(store, run['contract_id'])
    groups = {}
    meter_quantities = {}
    for row in run['rows']:
        usage = row['chosen']
        if usage is None:
            continue
        rate = rates.at(store, run['contract_id'], usage['meter'], usage['at'])
        group = groups.setdefault(rate['rate_id'], {'rate': rate, 'rows': []})
        group['rows'].append(row)
        meter_quantities[usage['meter']] = meter_quantities.get(usage['meter'], Decimal(0)) + Decimal(usage['quantity'])
    for meter, quantity in meter_quantities.items():
        quota = contract['definition']['quota'].get(meter)
        if quota is not None and quantity > Decimal(quota):
            reject('usage_quota', 'Monthly reconciled usage exceeds contract quota', meter=meter, quantity=encoded(quantity), quota=quota)
    lines = []
    for rate_id in sorted(groups):
        group = groups[rate_id]
        weights = {row['usage_id']: row['chosen']['quantity'] for row in group['rows']}
        quantity = sum(Decimal(value) for value in weights.values())
        priced = tier_charge(encoded(quantity), group['rate']['definition'])
        allocated = allocate(priced['amount_cents'], weights)
        usage = [{'usage_id': row['usage_id'], 'quantity': row['chosen']['quantity'], 'amount_cents': allocated[row['usage_id']], 'at': row['chosen']['at'], 'base_hash': row['base_hash'], 'status': row['status']} for row in sorted(group['rows'], key=lambda row: row['usage_id'])]
        line = {'line_id': rate_id, 'rate_id': rate_id, 'rate_digest': group['rate']['digest'], 'meter': group['rate']['meter'], 'interval': [group['rate']['start_at'], group['rate']['end_at']], 'pricing': priced, 'usage': usage, 'amount_cents': priced['amount_cents']}
        lines.append(line)
    result = {'contract_id': run['contract_id'], 'period': run['period'], 'run_id': run_id, 'run_digest': run['digest'], 'period_revision': run['revision'], 'contract_revision': contract['revision'], 'rates_fingerprint': rates.fingerprint(store, run['contract_id']), 'currency': contract['currency'], 'lines': lines, 'total_cents': sum(line['amount_cents'] for line in lines), 'excluded_disputes': [row['usage_id'] for row in disputes], 'meter_quantities': {key: encoded(value) for key, value in sorted(meter_quantities.items())}, 'billing_scope': 'monthly_quantity_grouped_by_effective_rate_interval'}
    return {**result, 'digest': digest(result)}

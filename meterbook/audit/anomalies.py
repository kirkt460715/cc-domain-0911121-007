from meterbook.values import exact
from decimal import Decimal, localcontext
from meterbook.values import number, decimal, encoded, reject, month
from meterbook.events import receiver, reconcile
from meterbook.contracts import registry


def usage_changes(store, contract_id, side):
    registry.get(store, contract_id)
    result = []
    for row in store.all('SELECT usage_id FROM usage_heads WHERE contract_id=? AND side=? ORDER BY usage_id', (contract_id, side)):
        history = receiver.history(store, contract_id, side, row['usage_id'])
        for previous, current in zip(history, history[1:]):
            before = previous['payload']
            after = current['payload']
            differences = [key for key in before if before[key] != after[key]]
            result.append({'usage_id': row['usage_id'], 'from_revision': previous['revision'], 'to_revision': current['revision'], 'changed_fields': differences, 'quantity_delta': encoded(Decimal(after['quantity']) - Decimal(before['quantity'])), 'period_changed': before['at'][:7] != after['at'][:7], 'cancelled': current['state'] == 'cancelled'})
    return {'contract_id': contract_id, 'side': side, 'changes': result}


@exact
def outliers(store, contract_id, period, multiplier='3'):
    month(period)
    factor = decimal(multiplier, 'multiplier', minimum=Decimal(1), maximum=Decimal(100))
    groups = {}
    for identity in reconcile.identities(store, contract_id, period):
        row = reconcile.classify(store, contract_id, identity, period)
        if row['chosen']:
            groups.setdefault(row['chosen']['meter'], []).append((identity, Decimal(row['chosen']['quantity'])))
    reports = []
    for meter, values in sorted(groups.items()):
        ordered = sorted(quantity for _, quantity in values)
        median = ordered[(len(ordered) - 1) // 2]
        deviations = sorted(abs(quantity - median) for quantity in ordered)
        mad = deviations[(len(deviations) - 1) // 2]
        threshold = median + factor * mad
        flagged = [{'usage_id': identity, 'quantity': encoded(quantity)} for identity, quantity in values if quantity > threshold]
        reports.append({'meter': meter, 'samples': len(values), 'median': encoded(median), 'median_absolute_deviation': encoded(mad), 'threshold': encoded(threshold), 'flagged': flagged, 'insufficient': len(values) < 3})
    return {'contract_id': contract_id, 'period': period, 'reports': reports, 'effect': 'diagnostic_only_no_billing_mutation'}


def discrepancy_statistics(store, contract_id, period):
    counts = {}
    absolute = Decimal(0)
    signed = Decimal(0)
    compared = 0
    for identity in reconcile.identities(store, contract_id, period):
        row = reconcile.classify(store, contract_id, identity, period)
        counts[row['status']] = counts.get(row['status'], 0) + 1
        provider = row['evidence']['provider']
        consumer = row['evidence']['consumer']
        if provider and consumer and provider['state'] == consumer['state'] == 'active' and provider['payload']['meter'] == consumer['payload']['meter']:
            delta = Decimal(provider['payload']['quantity']) - Decimal(consumer['payload']['quantity'])
            absolute += abs(delta)
            signed += delta
            compared += 1
    return {'contract_id': contract_id, 'period': period, 'statuses': counts, 'quantity_pairs_compared': compared, 'signed_delta': encoded(signed), 'absolute_delta': encoded(absolute)}


@exact
def drift(store, contract_id, baseline_period, current_period):
    from meterbook.audit.explanations import period_report
    before = period_report(store, contract_id, baseline_period)
    after = period_report(store, contract_id, current_period)
    result = []
    for meter in sorted(set(before['matched_quantities']) | set(after['matched_quantities'])):
        left = Decimal(before['matched_quantities'].get(meter, '0'))
        right = Decimal(after['matched_quantities'].get(meter, '0'))
        with localcontext() as arithmetic:
            arithmetic.prec = 40
            ratio = None if not left else encoded(right / left)
        result.append({'meter': meter, 'before_quantity': encoded(left), 'after_quantity': encoded(right), 'delta': encoded(right - left), 'ratio': ratio})
    return {'baseline_period': baseline_period, 'current_period': current_period, 'meters': result, 'before_disputes': before['statuses'].get('disputed', 0), 'after_disputes': after['statuses'].get('disputed', 0)}

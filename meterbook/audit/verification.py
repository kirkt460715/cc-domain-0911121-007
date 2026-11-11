from decimal import Decimal
from meterbook.values import fields, digest, reject, decimal, encoded, exact
from meterbook.events import protocol
from meterbook.contracts import rates
from meterbook.settlement import pricing
from meterbook.ports import bundles, files


def verify_calculation(calculation, rate_definitions):
    fields(calculation, {'contract_id', 'period', 'run_id', 'run_digest', 'period_revision', 'contract_revision', 'rates_fingerprint', 'currency', 'lines', 'total_cents', 'excluded_disputes', 'meter_quantities', 'billing_scope', 'digest'})
    payload = {name: value for name, value in calculation.items() if name != 'digest'}
    if digest(payload) != calculation['digest']:
        reject('calculation_digest', 'Calculation checksum differs')
    if not isinstance(rate_definitions, dict):
        reject('calculation_rates', 'Verification requires keyed frozen rate definitions')
    seen_lines = set()
    seen_usage = set()
    quantities = {}
    total = 0
    for line in calculation['lines']:
        identity = line['rate_id']
        if identity in seen_lines or line['line_id'] != identity:
            reject('calculation_line', 'Rate line is duplicate or has inconsistent identity')
        seen_lines.add(identity)
        if identity not in rate_definitions:
            reject('calculation_rate_missing', 'Frozen rate definition is absent', rate_id=identity)
        definition = rates.normalize(rate_definitions[identity])
        if digest(definition) != line['rate_digest']:
            reject('calculation_rate_digest', 'Frozen rate differs from calculation')
        weights = {}
        for row in line['usage']:
            if row['usage_id'] in seen_usage:
                reject('calculation_usage_duplicate', 'Usage occurs in multiple invoice lines')
            seen_usage.add(row['usage_id'])
            weights[row['usage_id']] = row['quantity']
            if row['at'][:7] != calculation['period'] or not line['interval'][0] <= row['at'] < line['interval'][1]:
                reject('calculation_usage_interval', 'Billed usage lies outside rate or period')
        quantity = sum((decimal(value) for value in weights.values()), Decimal(0))
        computed = pricing.tier_charge(encoded(quantity), definition)
        if computed != line['pricing'] or computed['amount_cents'] != line['amount_cents']:
            reject('calculation_pricing', 'Recomputed tier amount differs')
        expected = pricing.allocate(computed['amount_cents'], weights)
        for row in line['usage']:
            if row['amount_cents'] != expected[row['usage_id']]:
                reject('calculation_allocation', 'Usage allocation differs from largest remainder rule')
        quantities[line['meter']] = quantities.get(line['meter'], Decimal(0)) + quantity
        total += line['amount_cents']
    if total != calculation['total_cents']:
        reject('calculation_total', 'Calculation lines do not sum to total')
    normalized = {meter: encoded(quantity) for meter, quantity in sorted(quantities.items())}
    if normalized != calculation['meter_quantities']:
        reject('calculation_quantities', 'Meter quantities do not reconcile')
    if set(calculation['excluded_disputes']) & seen_usage:
        reject('calculation_excluded', 'Excluded dispute was charged')
    return {'valid': True, 'lines': len(seen_lines), 'usage_count': len(seen_usage), 'total_cents': total}


def verify_adjustment(invoice, parent=None):
    files.verify_invoice(invoice)
    payload = invoice['payload']
    snapshot = payload['snapshot']
    if snapshot['total_cents'] != payload['total_cents']:
        reject('adjustment_total', 'Invoice and snapshot totals differ')
    if payload['kind'] == 'original':
        if parent is not None or payload['parent_id'] is not None:
            reject('adjustment_parent', 'Original invoice cannot reference a parent')
        before = {}
    elif payload['kind'] == 'adjustment':
        if parent is None:
            reject('adjustment_parent', 'Adjustment requires frozen parent evidence')
        files.verify_invoice(parent)
        previous = parent['payload']
        if previous['invoice_id'] != payload['parent_id'] or previous['snapshot_digest'] != snapshot['parent_digest']:
            reject('adjustment_parent', 'Parent identity or checksum differs')
        if (previous['contract_id'], previous['period'], previous['currency']) != (payload['contract_id'], payload['period'], payload['currency']):
            reject('adjustment_scope', 'Adjustment and parent scopes differ')
        before = {row['line_id']: row for row in previous['snapshot']['calculation']['lines']}
    else:
        reject('adjustment_kind', 'Unknown invoice kind')
    after = {row['line_id']: row for row in snapshot['calculation']['lines']}
    actual = {}
    for row in snapshot['lines']:
        if row['line_id'] in actual:
            reject('adjustment_duplicate', 'Duplicate adjustment line')
        actual[row['line_id']] = row
    required = {identity for identity in set(before) | set(after) if before.get(identity) != after.get(identity)}
    if set(actual) != required:
        reject('adjustment_lines', 'Adjustment omits changed lines or adds unchanged lines')
    for identity, row in actual.items():
        old = before.get(identity)
        new = after.get(identity)
        old_amount = old['amount_cents'] if old else 0
        new_amount = new['amount_cents'] if new else 0
        if row['before'] != old or row['after'] != new:
            reject('adjustment_evidence', 'Adjustment line differs from source calculations')
        if (row['before_cents'], row['after_cents'], row['amount_cents']) != (old_amount, new_amount, new_amount - old_amount):
            reject('adjustment_delta', 'Adjustment delta does not reconcile')
    return {'valid': True, 'invoice_id': payload['invoice_id'], 'delta_cents': payload['total_cents']}


def proof(store, invoice_id):
    from meterbook.settlement import invoices
    invoice = invoices.get(store, invoice_id)
    definitions = {}
    for line in invoice['snapshot']['calculation']['lines']:
        definition = rates.get(store, line['rate_id'])['definition']
        if digest(definition) != line['rate_digest']:
            reject('proof_rate', 'Frozen rate no longer matches invoice')
        definitions[line['rate_id']] = definition
    package = bundles.audit_package(store, invoice_id)
    parent = files.invoice_export(store, invoice['parent_id']) if invoice['parent_id'] else None
    payload = {'format': 'meterbook-billing-proof', 'version': 1, 'package': package, 'parent': parent, 'rate_definitions': definitions}
    return {'payload': payload, 'sha256': digest(payload)}


@exact
def verify_proof(envelope):
    fields(envelope, {'payload', 'sha256'})
    if digest(envelope['payload']) != envelope['sha256']:
        reject('proof_digest', 'Billing proof checksum differs')
    payload = envelope['payload']
    fields(payload, {'format', 'version', 'package', 'parent', 'rate_definitions'})
    if payload['format'] != 'meterbook-billing-proof' or payload['version'] != 1:
        reject('proof_format', 'Unsupported billing proof')
    bundles.verify_audit_package(payload['package'])
    package = payload['package']['payload']
    invoice = package['invoice']
    calculation = invoice['payload']['snapshot']['calculation']
    computed = verify_calculation(calculation, payload['rate_definitions'])
    delta = verify_adjustment(invoice, payload['parent'])
    matches = {row['usage_id']: row for row in package['reconciliation']['payload']['rows']}
    for line in calculation['lines']:
        for charged in line['usage']:
            source = matches.get(charged['usage_id'])
            if source is None or source['chosen'] is None:
                reject('proof_usage', 'Charged usage lacks reconciled source')
            if source['base_hash'] != charged['base_hash'] or source['status'] != charged['status']:
                reject('proof_usage', 'Charged usage source binding differs')
            if source['chosen']['quantity'] != charged['quantity'] or source['chosen']['at'] != charged['at'] or source['chosen']['meter'] != line['meter']:
                reject('proof_usage', 'Charged usage values differ from reconciliation')
    return {'valid': True, 'calculation': computed, 'adjustment': delta, 'authentication': 'unsigned_local_evidence', 'current_entitlements_checked': False}

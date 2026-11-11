from meterbook.values import fields, canonical, digest, reject
from meterbook.contracts import registry, rates
from meterbook.events import receiver, recovery
from meterbook.ports import files


def configuration(store, contract_id):
    contract = registry.get(store, contract_id)
    if contract['state'] != 'active':
        reject('configuration_closed', 'Only active contracts can be exported as configuration')
    rate_rows = []
    for rate in rates.listing(store, contract_id):
        if rate['state'] == 'active':
            rate_rows.append({key: rate[key] for key in ('rate_id', 'contract_id', 'meter', 'start_at', 'end_at', 'definition')})
    payload = {'format': 'meterbook-configuration', 'version': 1, 'contract': {key: contract[key] for key in ('contract_id', 'provider', 'consumer', 'currency', 'definition')}, 'rates': rate_rows}
    return {'payload': payload, 'sha256': digest(payload)}


def import_configuration(store, envelope):
    fields(envelope, {'payload', 'sha256'})
    if digest(envelope['payload']) != envelope['sha256']:
        reject('configuration_digest', 'Configuration checksum differs')
    payload = envelope['payload']
    fields(payload, {'format', 'version', 'contract', 'rates'})
    if payload['format'] != 'meterbook-configuration' or payload['version'] != 1:
        reject('configuration_format', 'Unsupported configuration format')
    fields(payload['contract'], {'contract_id', 'provider', 'consumer', 'currency', 'definition'})
    if not isinstance(payload['rates'], list) or len(payload['rates']) > 1000:
        reject('configuration_rates', 'Invalid rate collection')
    with store.atomic():
        contract = registry.create(store, **payload['contract'])
        created = []
        for row in payload['rates']:
            fields(row, {'rate_id', 'contract_id', 'meter', 'start_at', 'end_at', 'definition'})
            if row['contract_id'] != contract['contract_id']:
                reject('configuration_scope', 'Rate refers to another contract')
            created.append(rates.add(store, **row)['rate_id'])
        store.log('configuration.imported', {'contract_id': contract['contract_id'], 'rates': created})
        return {'contract_id': contract['contract_id'], 'rate_ids': created}


def reconciliation_export(store, run_id):
    from meterbook.events import reconcile
    run = reconcile.get(store, run_id)
    payload = {'format': 'meterbook-reconciliation', 'version': 1, 'run_id': run_id, 'contract_id': run['contract_id'], 'period': run['period'], 'period_revision': run['revision'], 'contract_revision': run['contract_revision'], 'definition': run['definition'], 'rows': run['rows'], 'digest': run['digest']}
    return {'payload': payload, 'sha256': digest(payload)}


def verify_reconciliation(envelope):
    fields(envelope, {'payload', 'sha256'})
    if digest(envelope['payload']) != envelope['sha256']:
        reject('reconciliation_envelope', 'Reconciliation envelope checksum differs')
    payload = envelope['payload']
    if payload.get('format') != 'meterbook-reconciliation' or payload.get('version') != 1:
        reject('reconciliation_format', 'Unsupported reconciliation format')
    if digest({'definition': payload['definition'], 'rows': payload['rows']}) != payload['digest']:
        reject('reconciliation_manifest', 'Reconciliation manifest checksum differs')
    identities = [row['usage_id'] for row in payload['rows']]
    if identities != sorted(set(identities)):
        reject('reconciliation_order', 'Reconciliation usages must be unique and sorted')
    for row in payload['rows']:
        if digest(row['evidence']) != row['base_hash']:
            reject('reconciliation_evidence', 'Reconciliation source evidence checksum differs')
    return {'integrity': True, 'usage_count': len(identities), 'live_status_checked': False}


def audit_package(store, invoice_id):
    from meterbook.settlement import invoices
    invoice = invoices.get(store, invoice_id)
    exported = files.invoice_export(store, invoice_id)
    reconciled = reconciliation_export(store, invoice['run_id'])
    event_ids = set()
    for row in reconciled['payload']['rows']:
        for side in ('provider', 'consumer'):
            if row['evidence'][side]:
                event_ids.add(row['evidence'][side]['event_id'])
    evidence = []
    for identity in sorted(event_ids):
        row = receiver.get(store, identity)
        evidence.append({'event_id': identity, 'payload': row['payload'], 'digest': row['digest']})
    payload = {'format': 'meterbook-audit-package', 'version': 1, 'invoice': exported, 'reconciliation': reconciled, 'events': evidence}
    return {'payload': payload, 'sha256': digest(payload)}


def verify_audit_package(envelope):
    fields(envelope, {'payload', 'sha256'})
    if digest(envelope['payload']) != envelope['sha256']:
        reject('audit_package_digest', 'Audit package checksum mismatch')
    payload = envelope['payload']
    if payload.get('format') != 'meterbook-audit-package' or payload.get('version') != 1:
        reject('audit_package_format', 'Unsupported audit package')
    files.verify_invoice(payload['invoice'])
    verify_reconciliation(payload['reconciliation'])
    indexed = {}
    for row in payload['events']:
        if row['event_id'] in indexed or row['event_id'] != row['payload']['event_id'] or digest(row['payload']) != row['digest']:
            reject('audit_event', 'Audit package event identity or checksum differs')
        indexed[row['event_id']] = row
    for row in payload['reconciliation']['payload']['rows']:
        for side in ('provider', 'consumer'):
            leaf = row['evidence'][side]
            if leaf and leaf['event_id'] not in indexed:
                reject('audit_event_missing', 'Reconciliation source event is absent')
            if leaf:
                event = indexed[leaf['event_id']]['payload']
                scope = payload['reconciliation']['payload']
                if (event['contract_id'], event['side'], event['usage_id'], event['revision']) != (scope['contract_id'], side, row['usage_id'], leaf['revision']):
                    reject('audit_event_binding', 'Source event identity differs from reconciliation evidence')
                if event['action'] != 'cancel' and event['usage'] != leaf['payload']:
                    reject('audit_event_binding', 'Source event usage differs from reconciliation evidence')
                expected_state = 'cancelled' if event['action'] == 'cancel' else 'active'
                if leaf['state'] != expected_state:
                    reject('audit_event_binding', 'Source event state differs from reconciliation evidence')
    invoice = payload['invoice']['payload']
    reconciliation = payload['reconciliation']['payload']
    if invoice['snapshot']['calculation']['run_digest'] != reconciliation['digest']:
        reject('audit_link', 'Invoice does not refer to the packaged reconciliation')
    return {'integrity': True, 'events': len(indexed), 'invoice_id': invoice['invoice_id'], 'authentication': 'unsigned_local_evidence'}

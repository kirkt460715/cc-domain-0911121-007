from meterbook.values import fields, key, number, instant, decimal, encoded, canonical, reject


def usage(value):
    fields(value, {'meter', 'at', 'quantity', 'purpose', 'region'}, {'dimensions'})
    dimensions = value.get('dimensions', {})
    if not isinstance(dimensions, dict) or len(dimensions) > 20:
        reject('usage_dimensions', 'Usage dimensions must be a bounded mapping')
    for name, entry in dimensions.items():
        key(name, 'dimension')
        if not isinstance(entry, str) or len(entry) > 200:
            reject('usage_dimension_value', 'Dimension values must be bounded strings')
    return {'meter': key(value['meter'], 'meter'), 'at': instant(value['at']), 'quantity': encoded(decimal(value['quantity'])), 'purpose': key(value['purpose']), 'region': key(value['region']), 'dimensions': dict(sorted(dimensions.items()))}


def event(value):
    fields(value, {'event_id', 'contract_id', 'side', 'sequence', 'usage_id', 'revision', 'action'}, {'usage', 'reason'})
    if value['side'] not in {'provider', 'consumer'}:
        reject('event_side', 'Side must be provider or consumer')
    if value['action'] not in {'report', 'correct', 'cancel'}:
        reject('event_action', 'Unsupported usage action')
    revision = number(value['revision'], 'revision', 1)
    if value['action'] == 'report' and revision != 1:
        reject('event_initial_revision', 'Initial reports must use revision one')
    if value['action'] != 'report' and revision == 1:
        reject('event_correction_revision', 'Corrections and cancellations require a predecessor')
    if value['action'] == 'cancel':
        if 'usage' in value:
            reject('event_cancel_payload', 'Cancellation cannot supply replacement usage')
        body = None
    else:
        if 'usage' not in value:
            reject('event_usage', 'Report and correction require a full usage payload')
        body = usage(value['usage'])
    reason = value.get('reason')
    if value['action'] != 'report':
        from meterbook.values import prose
        reason = prose(reason, 'reason')
    return {'event_id': key(value['event_id']), 'contract_id': key(value['contract_id']), 'side': value['side'], 'sequence': number(value['sequence'], 'sequence', 1), 'usage_id': key(value['usage_id']), 'revision': revision, 'action': value['action'], 'usage': body, 'reason': reason}

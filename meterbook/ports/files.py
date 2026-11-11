import csv
import io
from meterbook.values import fields, loads, canonical, digest, reject, number
from meterbook.events import receiver
from meterbook.settlement import invoices


def parse_jsonlines(content):
    if not isinstance(content, str) or len(content.encode()) > 33554432:
        reject('input_size', 'Input exceeds supported size')
    result = []
    for index, line in enumerate(content.splitlines(), 1):
        if index > 10000:
            reject('input_count', 'Input has too many records')
        if not line.strip():
            reject('input_blank', 'Blank event record', line=index)
        result.append(loads(line))
    return result


def parse_csv(content):
    reader = csv.DictReader(io.StringIO(content, newline=''))
    required = {'event_id', 'contract_id', 'side', 'sequence', 'usage_id', 'revision', 'action', 'meter', 'at', 'quantity', 'purpose', 'region', 'reason'}
    if reader.fieldnames is None or set(reader.fieldnames) != required or len(reader.fieldnames) != len(required):
        reject('csv_header', 'CSV header differs from usage protocol')
    result = []
    for ordinal, row in enumerate(reader, 1):
        if ordinal > 10000:
            reject('input_count', 'Too many CSV records')
        if None in row or any(value is None for value in row.values()):
            reject('csv_width', 'CSV record width differs')
        try:
            sequence = int(row['sequence'])
            revision = int(row['revision'])
        except ValueError:
            reject('csv_integer', 'Sequence and revision must be integers', ordinal=ordinal)
        if str(sequence) != row['sequence'] or str(revision) != row['revision']:
            reject('csv_integer', 'Integer fields must be canonical decimal notation')
        event = {key: row[key] for key in ('event_id', 'contract_id', 'side', 'usage_id', 'action')}
        event.update(sequence=sequence, revision=revision)
        if row['action'] != 'cancel':
            event['usage'] = {key: row[key] for key in ('meter', 'at', 'quantity', 'purpose', 'region')}
        if row['reason']:
            event['reason'] = row['reason']
        result.append(event)
    return result


def ingest(store, events, atomic=False):
    if not isinstance(events, list) or not 1 <= len(events) <= 10000:
        reject('input_count', 'Input requires one to ten thousand events')
    from meterbook.values import MeterError
    def process():
        results = []
        for ordinal, event in enumerate(events):
            try:
                received = receiver.receive(store, event)
                results.append({'ordinal': ordinal, 'ok': True, 'event_id': received['event']['event_id'], 'duplicate': received['duplicate']})
            except MeterError as exc:
                if atomic:
                    raise
                results.append({'ordinal': ordinal, 'ok': False, 'error': exc.payload()})
        return {'results': results, 'failed': sum(not row['ok'] for row in results)}
    if atomic:
        with store.atomic():
            return process()
    return process()


def invoice_export(store, invoice_id):
    invoice = invoices.get(store, invoice_id)
    if invoice['state'] != 'closed':
        reject('export_invoice_state', 'Only closed invoices can be exported')
    payload = {'format': 'meterbook-invoice', 'version': 1, 'invoice_id': invoice_id, 'contract_id': invoice['contract_id'], 'period': invoice['period'], 'kind': invoice['kind'], 'parent_id': invoice['parent_id'], 'snapshot': invoice['snapshot'], 'snapshot_digest': invoice['digest'], 'total_cents': invoice['total_cents'], 'currency': invoice['currency']}
    return {'payload': payload, 'sha256': digest(payload)}


def verify_invoice(value):
    fields(value, {'payload', 'sha256'})
    payload = value['payload']
    if digest(payload) != value['sha256']:
        reject('export_digest', 'Invoice export checksum differs')
    if payload.get('format') != 'meterbook-invoice' or payload.get('version') != 1:
        reject('export_format', 'Unsupported invoice export format')
    if digest(payload['snapshot']) != payload['snapshot_digest']:
        reject('export_snapshot', 'Invoice snapshot checksum differs')
    if sum(row['amount_cents'] for row in payload['snapshot']['lines']) != payload['total_cents']:
        reject('export_total', 'Invoice lines do not sum to total')
    return {'integrity': True, 'invoice_id': payload['invoice_id'], 'total_cents': payload['total_cents'], 'live_status_checked': False}


def invoice_csv(store, invoice_id):
    exported = invoice_export(store, invoice_id)
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['invoice_id', 'line_id', 'currency', 'before_cents', 'after_cents', 'delta_cents'])
    for line in exported['payload']['snapshot']['lines']:
        writer.writerow([invoice_id, line['line_id'], exported['payload']['currency'], line['before_cents'], line['after_cents'], line['amount_cents']])
    return {'csv': buffer.getvalue(), 'manifest': exported}

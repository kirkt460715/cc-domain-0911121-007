import csv
import io
import json
from decimal import Decimal, localcontext
from meterbook.values import fields, key, number, decimal, encoded, instant, canonical, digest, reject
from meterbook.events import protocol, receiver
from meterbook.contracts import registry

DDL = '''
CREATE TABLE IF NOT EXISTS source_mappings(mapping_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,definition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_imports(import_id TEXT PRIMARY KEY,mapping_id TEXT NOT NULL,mapping_revision INTEGER NOT NULL,input_digest TEXT NOT NULL,state TEXT NOT NULL,cursor INTEGER NOT NULL,records TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_import_results(import_id TEXT NOT NULL,ordinal INTEGER NOT NULL,result TEXT NOT NULL,PRIMARY KEY(import_id,ordinal));
'''


def normalize(value):
    fields(value, {'contract_id', 'side', 'columns', 'defaults', 'quantity_factor'})
    key(value['contract_id'])
    if value['side'] not in {'provider', 'consumer'}:
        reject('mapping_side', 'Mapping requires provider or consumer')
    columns = value['columns']
    defaults = value['defaults']
    allowed = {'usage_id', 'at', 'quantity', 'meter', 'purpose', 'region'}
    if not isinstance(columns, dict) or not isinstance(defaults, dict):
        reject('mapping_fields', 'Mapping columns and defaults must be objects')
    if set(columns) | set(defaults) != allowed or set(columns) & set(defaults):
        reject('mapping_fields', 'Each usage field requires exactly one column or default')
    for field, source in columns.items():
        if not isinstance(source, str) or not source or len(source) > 100:
            reject('mapping_column', 'Source column name must be bounded nonempty text')
    if {'usage_id', 'at', 'quantity'} & set(defaults):
        reject('mapping_identity', 'Usage identity, timestamp and quantity require source columns')
    for name, default_value in defaults.items():
        key(default_value, name)
    return {'contract_id': value['contract_id'], 'side': value['side'], 'columns': dict(sorted(columns.items())), 'defaults': dict(sorted(defaults.items())), 'quantity_factor': encoded(decimal(value['quantity_factor'], 'quantity_factor', minimum=Decimal('0.000001'), maximum=Decimal('1000000')))}


def convert(row, definition, sequence, event_id):
    if not isinstance(row, dict):
        reject('mapping_row', 'Source record must be an object')
    selected = dict(definition['defaults'])
    for field, column in definition['columns'].items():
        if column not in row:
            reject('mapping_missing', 'Mapped source column is absent', column=column)
        selected[field] = row[column]
    with localcontext() as context:
        context.prec = 80
        quantity = decimal(selected['quantity']) * Decimal(definition['quantity_factor'])
    selected['quantity'] = encoded(quantity)
    identity = key(selected.pop('usage_id'))
    normalized = protocol.usage(selected)
    return protocol.event({'event_id': event_id, 'contract_id': definition['contract_id'], 'side': definition['side'], 'sequence': sequence, 'usage_id': identity, 'revision': 1, 'action': 'report', 'usage': normalized})


class Imports:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('mapping_transaction', 'Initialize mapped imports outside transactions')
        store.db.executescript(DDL)

    def put(self, mapping_id, expected_revision, definition):
        key(mapping_id)
        normalized = normalize(definition)
        registry.get(self.store, normalized['contract_id'])
        with self.store.atomic():
            previous = self.store.one('SELECT * FROM source_mappings WHERE mapping_id=?', (mapping_id,))
            revision = previous['revision'] if previous else 0
            if revision != expected_revision:
                reject('mapping_stale', 'Source mapping revision changed')
            self.store.execute('INSERT INTO source_mappings VALUES(?,?,?) ON CONFLICT(mapping_id) DO UPDATE SET revision=excluded.revision,definition=excluded.definition', (mapping_id, revision + 1, canonical(normalized)))
            return self.get(mapping_id)

    def get(self, mapping_id):
        row = self.store.one('SELECT * FROM source_mappings WHERE mapping_id=?', (mapping_id,))
        if row is None:
            reject('mapping_missing', 'Source mapping does not exist')
        row['definition'] = json.loads(row['definition'])
        return row

    def start(self, import_id, mapping_id, records, first_sequence):
        key(import_id)
        number(first_sequence, 'first_sequence', 1)
        if not isinstance(records, list) or not 1 <= len(records) <= 10000:
            reject('mapping_records', 'Mapped import requires bounded source records')
        if first_sequence + len(records) - 1 > 1000000000:
            reject('mapping_sequence', 'Import exceeds sequence limit')
        with self.store.atomic():
            mapping = self.get(mapping_id)
            payload = {'mapping': mapping['definition'], 'records': records, 'first_sequence': first_sequence}
            sha = digest(payload)
            old = self.store.one('SELECT * FROM source_imports WHERE import_id=?', (import_id,))
            if old:
                if old['input_digest'] != sha or old['mapping_id'] != mapping_id:
                    reject('mapping_import_conflict', 'Import identity binds different source input')
                return self.status(import_id)
            self.store.execute("INSERT INTO source_imports VALUES(?,?,?,?,'running',0,?,?)", (import_id, mapping_id, mapping['revision'], sha, canonical(payload), self.store.clock()))
            return self.status(import_id)

    def status(self, import_id):
        row = self.store.one('SELECT * FROM source_imports WHERE import_id=?', (import_id,))
        if row is None:
            reject('mapping_import_missing', 'Mapped import does not exist')
        payload = json.loads(row.pop('records'))
        if digest(payload) != row['input_digest']:
            reject('mapping_import_corrupt', 'Import source checksum differs')
        row['total'] = len(payload['records'])
        row['results'] = [json.loads(item['result']) for item in self.store.all('SELECT result FROM source_import_results WHERE import_id=? ORDER BY ordinal', (import_id,))]
        row['failed'] = sum(not item['ok'] for item in row['results'])
        return row

    def advance(self, import_id, limit=100):
        from meterbook.values import MeterError
        number(limit, 'limit', 1, 1000)
        with self.store.atomic():
            status = self.status(import_id)
            if status['state'] != 'running':
                return status
            payload = json.loads(self.store.scalar('SELECT records FROM source_imports WHERE import_id=?', (import_id,)))
            stop = min(status['cursor'] + limit, len(payload['records']))
            for ordinal in range(status['cursor'], stop):
                identity = 'mapped-' + digest([import_id, ordinal])[:32]
                try:
                    with self.store.atomic():
                        event = convert(payload['records'][ordinal], payload['mapping'], payload['first_sequence'] + ordinal, identity)
                        incoming = {name: value for name, value in event.items() if value is not None}
                        receiver.receive(self.store, incoming)
                        result = {'ordinal': ordinal, 'ok': True, 'event_id': identity}
                except MeterError as exc:
                    result = {'ordinal': ordinal, 'ok': False, 'error': exc.payload()}
                self.store.execute('INSERT INTO source_import_results VALUES(?,?,?)', (import_id, ordinal, canonical(result)))
            state = 'completed' if stop == len(payload['records']) else 'running'
            self.store.execute('UPDATE source_imports SET cursor=?,state=? WHERE import_id=?', (stop, state, import_id))
            return self.status(import_id)

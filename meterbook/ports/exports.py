import base64
import hashlib
import json
import zlib
from decimal import Decimal, localcontext
from meterbook.values import fields, key, number, month, decimal, encoded, prose, canonical, digest, loads, reject, MeterError
from meterbook.events import protocol, receiver
from meterbook.contracts import registry

DDL = '''
CREATE TABLE IF NOT EXISTS export_sessions(session_id TEXT PRIMARY KEY,export_id TEXT,mapping_id TEXT NOT NULL,mapping_revision INTEGER NOT NULL,state TEXT NOT NULL,compressed_bytes INTEGER NOT NULL,inflated_bytes INTEGER NOT NULL,compressed_sha256 TEXT,report TEXT,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS export_session_exports ON export_sessions(export_id);
CREATE TABLE IF NOT EXISTS export_lines(session_id TEXT NOT NULL,line_no INTEGER NOT NULL,role TEXT NOT NULL,raw TEXT NOT NULL,parsed TEXT,error TEXT,PRIMARY KEY(session_id,line_no));
CREATE TABLE IF NOT EXISTS export_chunks(session_id TEXT NOT NULL,ordinal INTEGER NOT NULL,content BLOB NOT NULL,PRIMARY KEY(session_id,ordinal));
'''

LIMITS = {'compressed_bytes': 33554432, 'inflated_bytes': 134217728, 'line_bytes': 1048576, 'lines': 100000, 'chunk_base64': 1400000}

EMPTY_POSTED = {'received': 0, 'duplicates': 0, 'failed': [], 'events': []}


class Inflator:
    def __init__(self, limit):
        self.limit = limit
        self.total = 0
        self.decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.members = 0

    def feed(self, data):
        output = bytearray()
        view = data
        while view:
            if self.decompressor.eof:
                self.decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            room = self.limit - self.total + 1
            try:
                piece = self.decompressor.decompress(view, room)
            except zlib.error as exc:
                reject('export_gzip', 'Compressed stream fails gzip validation', detail=str(exc))
            self.total += len(piece)
            if self.total > self.limit:
                reject('export_limit', 'Inflated content exceeds decompression limit', limit=self.limit)
            output += piece
            if self.decompressor.eof:
                self.members += 1
                view = self.decompressor.unused_data
            else:
                view = self.decompressor.unconsumed_tail
        return bytes(output)

    def finalize(self):
        return self.members >= 1 and self.decompressor.eof


class LineSplitter:
    def __init__(self, limit):
        self.limit = limit
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        lines = []
        while True:
            index = self.buffer.find(b'\n')
            if index < 0:
                if len(self.buffer) > self.limit:
                    reject('export_limit', 'Line exceeds length limit', limit=self.limit)
                break
            line = bytes(self.buffer[:index])
            del self.buffer[:index + 1]
            if len(line) > self.limit:
                reject('export_limit', 'Line exceeds length limit', limit=self.limit)
            lines.append(line)
        return lines

    def finalize(self):
        return not self.buffer


def _header(value):
    fields(value, {'kind', 'export_id', 'contract_id', 'side', 'period', 'mapping_revision'})
    if value['kind'] != 'header':
        reject('export_header', 'First record must declare kind header')
    if value['side'] not in {'provider', 'consumer'}:
        reject('export_header', 'Header side must be provider or consumer')
    return {'export_id': key(value['export_id'], 'export_id'), 'contract_id': key(value['contract_id'], 'contract_id'), 'side': value['side'], 'period': month(value['period']), 'mapping_revision': number(value['mapping_revision'], 'mapping_revision', 1)}


def _trailer(value):
    fields(value, {'kind', 'count', 'totals'})
    count = number(value['count'], 'count', 0)
    totals = value['totals']
    if not isinstance(totals, dict) or len(totals) > 100:
        reject('export_trailer', 'Trailer totals must be a bounded object')
    result = {}
    for meter, quantity in totals.items():
        result[key(meter, 'meter')] = encoded(decimal(quantity, 'totals'))
    return {'count': count, 'totals': result}


def _detail(value, factor):
    fields(value, {'kind', 'usage_id', 'revision', 'action'}, {'at', 'quantity', 'meter', 'purpose', 'region', 'reason'})
    if value['kind'] != 'usage':
        reject('export_kind', 'Detail record must declare kind usage')
    action = value['action']
    if action not in {'report', 'correct', 'cancel'}:
        reject('export_action', 'Unsupported usage action')
    revision = number(value['revision'], 'revision', 1)
    if action == 'report' and revision != 1:
        reject('export_revision', 'Initial reports must use revision one')
    if action != 'report' and revision == 1:
        reject('export_revision', 'Corrections and cancellations require a predecessor revision')
    detail = {'usage_id': key(value['usage_id'], 'usage_id'), 'revision': revision, 'action': action}
    if action == 'cancel':
        if any(name in value for name in ('at', 'quantity', 'meter', 'purpose', 'region')):
            reject('export_cancel_payload', 'Cancellation cannot carry usage fields')
        detail['usage'] = None
    else:
        for name in ('at', 'quantity', 'meter', 'purpose', 'region'):
            if name not in value:
                reject('export_usage', 'Usage record misses a required field', field=name)
        quantity = decimal(value['quantity'], 'quantity')
        with localcontext() as context:
            context.prec = 80
            converted = quantity * factor
        detail['raw_quantity'] = encoded(quantity)
        detail['usage'] = protocol.usage({'meter': value['meter'], 'at': value['at'], 'quantity': encoded(converted), 'purpose': value['purpose'], 'region': value['region']})
    if action != 'report':
        detail['reason'] = prose(value.get('reason'), 'reason')
    return detail


class Exports:
    def __init__(self, store, limits=None):
        self.store = store
        if store.depth:
            reject('export_transaction', 'Initialize export collection outside transactions')
        store.db.executescript(DDL)
        self.limits = dict(LIMITS)
        if limits:
            self.limits.update(limits)
        self.sessions = {}

    def begin(self, session_id, mapping_id):
        key(session_id)
        key(mapping_id)
        with self.store.atomic():
            existing = self.store.one('SELECT * FROM export_sessions WHERE session_id=?', (session_id,))
            if existing:
                if existing['mapping_id'] != mapping_id:
                    reject('export_session_conflict', 'Session identifier binds another mapping')
                return self.status(session_id)
            row = self.store.one('SELECT * FROM source_mappings WHERE mapping_id=?', (mapping_id,))
            if row is None:
                reject('mapping_missing', 'Source mapping does not exist')
            definition = json.loads(row['definition'])
            self.store.execute("INSERT INTO export_sessions VALUES(?,NULL,?,?,'receiving',0,0,NULL,NULL,?)", (session_id, mapping_id, row['revision'], self.store.clock()))
            self.sessions[session_id] = {'mapping_id': mapping_id, 'mapping_revision': row['revision'], 'mapping': definition, 'factor': Decimal(definition['quantity_factor']), 'inflator': Inflator(self.limits['inflated_bytes']), 'splitter': LineSplitter(self.limits['line_bytes']), 'sha': hashlib.sha256(), 'compressed': 0, 'inflated': 0, 'chunks': 0, 'physical': 0, 'details': 0, 'quarantined': 0, 'header': None, 'trailer': None, 'trailer_error': None, 'totals': {}}
            self.store.log('export.begin', {'session_id': session_id, 'mapping_id': mapping_id, 'mapping_revision': row['revision']})
            return self.status(session_id)

    def status(self, session_id):
        key(session_id)
        row = self.store.one('SELECT * FROM export_sessions WHERE session_id=?', (session_id,))
        if row is None:
            reject('export_session_missing', 'Export session does not exist')
        row['report'] = json.loads(row['report']) if row['report'] else None
        return row

    def feed(self, session_id, content):
        key(session_id)
        if not isinstance(content, str) or not content or len(content) > self.limits['chunk_base64']:
            reject('export_chunk', 'Chunk requires nonempty bounded base64 text')
        try:
            decoded = base64.b64decode(content, validate=True)
        except (ValueError, TypeError):
            reject('export_chunk', 'Invalid base64 chunk')
        if not decoded:
            reject('export_chunk', 'Chunk must not decode to empty bytes')
        failure = None
        with self.store.atomic():
            row = self.store.one('SELECT state FROM export_sessions WHERE session_id=?', (session_id,))
            if row is None:
                reject('export_session_missing', 'Export session does not exist')
            if row['state'] != 'receiving':
                reject('export_session_state', 'Export session no longer accepts chunks', state=row['state'])
            runtime = self.sessions.get(session_id)
            if runtime is None:
                reject('export_session_lost', 'Export session runtime is not resident in this process')
            try:
                self._feed(session_id, runtime, decoded)
            except MeterError as exc:
                failure = exc.payload()
            if failure is not None:
                report = self._report(session_id, runtime, 'incomplete', failure=failure)
                self._conclude(session_id, runtime, 'incomplete', None, report)
                self.store.log('export.incomplete', {'session_id': session_id, 'failure': failure})
            else:
                self.store.execute('UPDATE export_sessions SET compressed_bytes=?,inflated_bytes=? WHERE session_id=?', (runtime['compressed'], runtime['inflated'], session_id))
        if failure is not None:
            raise MeterError(failure['code'], failure['message'], **failure['details'])
        return self.status(session_id)

    def finish(self, session_id):
        key(session_id)
        conflict = None
        with self.store.atomic():
            row = self.store.one('SELECT * FROM export_sessions WHERE session_id=?', (session_id,))
            if row is None:
                reject('export_session_missing', 'Export session does not exist')
            if row['state'] != 'receiving':
                result = self.status(session_id)
            else:
                runtime = self.sessions.get(session_id)
                if runtime is None:
                    reject('export_session_lost', 'Export session runtime is not resident in this process')
                reasons = []
                if not runtime['inflator'].finalize():
                    reasons.append('gzip_stream_incomplete')
                if not runtime['splitter'].finalize():
                    reasons.append('final_line_truncated')
                if runtime['header'] is None:
                    reasons.append('header_missing')
                if runtime['trailer'] is None:
                    reasons.append('trailer_invalid' if runtime['trailer_error'] else 'trailer_missing')
                if reasons:
                    failure = {'code': 'export_incomplete', 'message': 'Export stream is not complete', 'details': {'reasons': reasons}}
                    reconciliation = self._reconcile(runtime) if runtime['trailer'] else None
                    result = self._report(session_id, runtime, 'incomplete', reconciliation=reconciliation, failure=failure)
                    self._conclude(session_id, runtime, 'incomplete', None, result)
                    self.store.log('export.incomplete', {'session_id': session_id, 'reasons': reasons})
                else:
                    sha = runtime['sha'].hexdigest()
                    export_id = runtime['header']['export_id']
                    prior = self.store.one("SELECT session_id,compressed_sha256,report FROM export_sessions WHERE export_id=? AND state='reconciled' AND session_id<>?", (export_id, session_id))
                    if prior and prior['compressed_sha256'] == sha:
                        result = {'session_id': session_id, 'state': 'duplicate', 'complete': True, 'export_id': export_id, 'duplicate_of': prior['session_id'], 'original': json.loads(prior['report'])}
                        self._conclude(session_id, runtime, 'duplicate', sha, result)
                        self.store.log('export.duplicate', {'session_id': session_id, 'export_id': export_id, 'duplicate_of': prior['session_id']})
                    elif prior:
                        failure = {'code': 'export_conflict', 'message': 'Export identifier already binds different reconciled content', 'details': {'export_id': export_id, 'existing_digest': prior['compressed_sha256'], 'received_digest': sha}}
                        result = self._report(session_id, runtime, 'conflicted', failure=failure)
                        self._conclude(session_id, runtime, 'conflicted', sha, result)
                        self.store.log('export.conflict', {'session_id': session_id, 'export_id': export_id})
                        conflict = failure['details']
                    else:
                        reconciliation = self._reconcile(runtime)
                        if not reconciliation['ok']:
                            result = self._report(session_id, runtime, 'mismatched', reconciliation=reconciliation)
                            self._conclude(session_id, runtime, 'mismatched', sha, result)
                            self.store.log('export.mismatched', {'session_id': session_id, 'export_id': export_id})
                        else:
                            posted = self._post(session_id, runtime)
                            result = self._report(session_id, runtime, 'reconciled', reconciliation=reconciliation, posted=posted)
                            self._conclude(session_id, runtime, 'reconciled', sha, result)
                            self.store.log('export.reconciled', {'session_id': session_id, 'export_id': export_id, 'received': posted['received']})
        if conflict is not None:
            reject('export_conflict', 'Export identifier already binds different reconciled content', **conflict)
        return result

    def _feed(self, session_id, runtime, data):
        runtime['sha'].update(data)
        runtime['compressed'] += len(data)
        if runtime['compressed'] > self.limits['compressed_bytes']:
            reject('export_limit', 'Compressed content exceeds limit', limit=self.limits['compressed_bytes'])
        self.store.execute('INSERT INTO export_chunks VALUES(?,?,?)', (session_id, runtime['chunks'], data))
        runtime['chunks'] += 1
        inflated = runtime['inflator'].feed(data)
        runtime['inflated'] += len(inflated)
        for raw in runtime['splitter'].feed(inflated):
            if runtime['physical'] >= self.limits['lines']:
                reject('export_limit', 'Export exceeds line limit', limit=self.limits['lines'])
            self._record_line(session_id, runtime, raw)

    def _record_line(self, session_id, runtime, raw):
        runtime['physical'] += 1
        line_no = runtime['physical']
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = None
        if line_no == 1:
            if text is None:
                reject('export_header', 'Header line is not UTF-8')
            try:
                header = _header(loads(text))
            except MeterError as exc:
                reject('export_header', 'Header line violates export protocol', error=exc.payload())
            mapping = runtime['mapping']
            if header['contract_id'] != mapping['contract_id'] or header['side'] != mapping['side']:
                reject('export_mapping', 'Header binding differs from pinned mapping', contract_id=header['contract_id'], side=header['side'])
            if header['mapping_revision'] != runtime['mapping_revision']:
                reject('export_mapping', 'Header mapping revision differs from pinned revision', pinned=runtime['mapping_revision'], declared=header['mapping_revision'])
            registry.get(self.store, header['contract_id'])
            runtime['header'] = header
            self.store.execute('UPDATE export_sessions SET export_id=? WHERE session_id=?', (header['export_id'], session_id))
            self._store_line(session_id, line_no, 'header', text, header, None)
            return
        if runtime['trailer'] is not None or runtime['trailer_error'] is not None:
            reject('export_structure', 'Records follow the trailer line')
        parsed = None
        error = None
        if text is not None and text.strip():
            try:
                parsed = loads(text)
            except MeterError as exc:
                error = exc.payload()
        if isinstance(parsed, dict) and parsed.get('kind') == 'trailer':
            try:
                runtime['trailer'] = _trailer(parsed)
                self._store_line(session_id, line_no, 'trailer', text, runtime['trailer'], None)
            except MeterError as exc:
                runtime['trailer_error'] = exc.payload()
                self._store_line(session_id, line_no, 'trailer', text, None, exc.payload())
            return
        runtime['details'] += 1
        if text is None:
            self._quarantine(session_id, runtime, line_no, base64.b64encode(raw).decode(), {'code': 'utf8', 'message': 'Line is not valid UTF-8; raw kept as base64', 'details': {'encoding': 'base64'}})
            return
        if not text.strip():
            self._quarantine(session_id, runtime, line_no, text, {'code': 'blank', 'message': 'Blank detail record', 'details': {}})
            return
        if error is not None:
            self._quarantine(session_id, runtime, line_no, text, error)
            return
        try:
            detail = _detail(parsed, runtime['factor'])
        except MeterError as exc:
            self._quarantine(session_id, runtime, line_no, text, exc.payload())
            return
        if detail['action'] != 'cancel':
            meter = detail['usage']['meter']
            runtime['totals'][meter] = runtime['totals'].get(meter, Decimal(0)) + Decimal(detail['raw_quantity'])
        self._store_line(session_id, line_no, 'detail', text, detail, None)

    def _quarantine(self, session_id, runtime, line_no, raw, error):
        runtime['quarantined'] += 1
        self._store_line(session_id, line_no, 'detail', raw, None, error)

    def _store_line(self, session_id, line_no, role, raw, parsed, error):
        self.store.execute('INSERT INTO export_lines VALUES(?,?,?,?,?,?)', (session_id, line_no, role, raw, canonical(parsed) if parsed is not None else None, canonical(error) if error is not None else None))

    def _reconcile(self, runtime):
        trailer = runtime['trailer']
        observed = {meter: encoded(value) for meter, value in sorted(runtime['totals'].items())}
        declared = trailer['totals']
        totals = {}
        totals_ok = True
        for meter in sorted(set(observed) | set(declared)):
            ok = meter in observed and meter in declared and Decimal(observed[meter]) == Decimal(declared[meter])
            totals[meter] = {'declared': declared.get(meter), 'observed': observed.get(meter), 'ok': ok}
            totals_ok = totals_ok and ok
        count_ok = runtime['details'] == trailer['count']
        unverifiable = runtime['quarantined'] > 0
        return {'count': {'declared': trailer['count'], 'observed': runtime['details'], 'ok': count_ok}, 'totals': totals, 'unverifiable': unverifiable, 'ok': count_ok and totals_ok and not unverifiable}

    def _post(self, session_id, runtime):
        header = runtime['header']
        sequence = self.store.scalar('SELECT COALESCE(MAX(sequence),0) FROM events WHERE contract_id=? AND side=?', (header['contract_id'], header['side']))
        events = []
        failed = []
        for row in self.store.all("SELECT line_no,parsed FROM export_lines WHERE session_id=? AND role='detail' AND error IS NULL ORDER BY line_no", (session_id,)):
            detail = json.loads(row['parsed'])
            sequence += 1
            event_id = 'export-' + digest([header['export_id'], row['line_no']])[:24]
            value = {'event_id': event_id, 'contract_id': header['contract_id'], 'side': header['side'], 'sequence': sequence, 'usage_id': detail['usage_id'], 'revision': detail['revision'], 'action': detail['action']}
            if detail['usage'] is not None:
                value['usage'] = detail['usage']
            if 'reason' in detail:
                value['reason'] = detail['reason']
            try:
                received = receiver.receive(self.store, value)
                events.append({'line_no': row['line_no'], 'event_id': event_id, 'sequence': sequence, 'duplicate': received['duplicate']})
            except MeterError as exc:
                failed.append({'line_no': row['line_no'], 'event_id': event_id, 'error': exc.payload()})
        return {'received': len(events), 'duplicates': sum(1 for event in events if event['duplicate']), 'failed': failed, 'events': events}

    def _report(self, session_id, runtime, state, reconciliation=None, posted=None, failure=None):
        header = runtime['header'] or {}
        valid = []
        for row in self.store.all("SELECT line_no,parsed FROM export_lines WHERE session_id=? AND role='detail' AND error IS NULL ORDER BY line_no", (session_id,)):
            detail = json.loads(row['parsed'])
            valid.append({'line_no': row['line_no'], 'usage_id': detail['usage_id'], 'revision': detail['revision'], 'action': detail['action'], 'meter': detail['usage']['meter'] if detail['usage'] else None, 'quantity': detail['usage']['quantity'] if detail['usage'] else None})
        quarantined = [{'line_no': row['line_no'], 'raw': row['raw'], 'error': json.loads(row['error'])} for row in self.store.all("SELECT line_no,raw,error FROM export_lines WHERE session_id=? AND role='detail' AND error IS NOT NULL ORDER BY line_no", (session_id,))]
        report = {'session_id': session_id, 'state': state, 'complete': state in ('reconciled', 'duplicate'), 'export_id': header.get('export_id'), 'contract_id': header.get('contract_id'), 'side': header.get('side'), 'period': header.get('period'), 'mapping_id': runtime['mapping_id'], 'mapping_revision': runtime['mapping_revision'], 'raw': {'compressed_bytes': runtime['compressed'], 'inflated_bytes': runtime['inflated'], 'physical_lines': runtime['physical'], 'detail_records': runtime['details'], 'declared': {'count': runtime['trailer']['count'], 'totals': runtime['trailer']['totals']} if runtime['trailer'] else None, 'observed_totals': {meter: encoded(value) for meter, value in sorted(runtime['totals'].items())}}, 'reconciliation': reconciliation, 'valid': valid, 'quarantined': quarantined, 'posted': posted if posted is not None else dict(EMPTY_POSTED)}
        if failure is not None:
            report['failure'] = failure
        return report

    def _conclude(self, session_id, runtime, state, sha, report):
        self.store.execute('UPDATE export_sessions SET state=?,compressed_bytes=?,inflated_bytes=?,compressed_sha256=?,report=? WHERE session_id=?', (state, runtime['compressed'], runtime['inflated'], sha, canonical(report), session_id))
        self.sessions.pop(session_id, None)

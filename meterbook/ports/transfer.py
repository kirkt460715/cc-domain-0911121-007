import base64
import hashlib
from meterbook.values import key, number, canonical, digest, reject, loads
from meterbook.ports.files import invoice_export, verify_invoice

DDL = '''
CREATE TABLE IF NOT EXISTS evidence_exports(transfer_id TEXT PRIMARY KEY,invoice_id TEXT NOT NULL,manifest TEXT NOT NULL,content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_imports(transfer_id TEXT PRIMARY KEY,manifest TEXT NOT NULL,state TEXT NOT NULL,artifact TEXT);
CREATE TABLE IF NOT EXISTS evidence_chunks(transfer_id TEXT NOT NULL,ordinal INTEGER NOT NULL,content BLOB NOT NULL,PRIMARY KEY(transfer_id,ordinal));
'''


class Transfers:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('transfer_transaction', 'Initialize transfers outside transactions')
        store.db.executescript(DDL)

    def prepare(self, transfer_id, invoice_id, chunk_size=4096):
        key(transfer_id)
        number(chunk_size, 'chunk_size', 64, 1048576)
        with self.store.atomic():
            content = canonical(invoice_export(self.store, invoice_id)).encode()
            if len(content) > 64 * 1024 * 1024:
                reject('transfer_size', 'Evidence exceeds local transfer size limit')
            chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
            manifest = {'schema': 1, 'invoice_id': invoice_id, 'size': len(content), 'chunk_size': chunk_size, 'sha256': hashlib.sha256(content).hexdigest(), 'chunks': [hashlib.sha256(chunk).hexdigest() for chunk in chunks]}
            previous = self.store.one('SELECT manifest FROM evidence_exports WHERE transfer_id=?', (transfer_id,))
            if previous:
                if loads(previous['manifest']) != manifest:
                    reject('transfer_conflict', 'Transfer identifier already binds different evidence')
                return manifest
            self.store.execute('INSERT INTO evidence_exports VALUES(?,?,?,?)', (transfer_id, invoice_id, canonical(manifest), content))
            self.store.log('transfer.prepared', {'transfer_id': transfer_id, 'digest': digest(manifest)})
            return manifest

    def chunk(self, transfer_id, ordinal):
        number(ordinal, 'ordinal')
        row = self.store.one('SELECT * FROM evidence_exports WHERE transfer_id=?', (transfer_id,))
        if row is None:
            reject('transfer_missing', 'Evidence export does not exist')
        manifest = loads(row['manifest'])
        if ordinal >= len(manifest['chunks']):
            reject('transfer_ordinal', 'Chunk index exceeds manifest')
        start = ordinal * manifest['chunk_size']
        content = row['content'][start:start + manifest['chunk_size']]
        return {'ordinal': ordinal, 'sha256': hashlib.sha256(content).hexdigest(), 'base64': base64.b64encode(content).decode()}

    def start(self, transfer_id, manifest):
        key(transfer_id)
        from meterbook.values import fields
        fields(manifest, {'schema', 'invoice_id', 'size', 'chunk_size', 'sha256', 'chunks'})
        if manifest['schema'] != 1:
            reject('transfer_schema', 'Unsupported transfer schema')
        key(manifest['invoice_id'])
        size = number(manifest['size'], 'size', 1, 64 * 1024 * 1024)
        chunk_size = number(manifest['chunk_size'], 'chunk_size', 64, 1048576)
        chunks = manifest['chunks']
        if not isinstance(chunks, list) or len(chunks) != (size + chunk_size - 1) // chunk_size:
            reject('transfer_manifest', 'Chunk count does not match total size')
        import re
        for checksum in [manifest['sha256'], *chunks]:
            if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
                reject('transfer_checksum', 'Malformed checksum')
        with self.store.atomic():
            previous = self.store.one('SELECT manifest FROM evidence_imports WHERE transfer_id=?', (transfer_id,))
            if previous:
                if loads(previous['manifest']) != manifest:
                    reject('transfer_conflict', 'Import identifier binds another manifest')
            else:
                self.store.execute("INSERT INTO evidence_imports VALUES(?,?,'receiving',NULL)", (transfer_id, canonical(manifest)))
            return self.status(transfer_id)

    def status(self, transfer_id):
        row = self.store.one('SELECT * FROM evidence_imports WHERE transfer_id=?', (transfer_id,))
        if row is None:
            reject('transfer_missing', 'Evidence import does not exist')
        manifest = loads(row['manifest'])
        received = [item['ordinal'] for item in self.store.all('SELECT ordinal FROM evidence_chunks WHERE transfer_id=? ORDER BY ordinal', (transfer_id,))]
        return {'transfer_id': transfer_id, 'state': row['state'], 'manifest': manifest, 'received': received, 'missing': sorted(set(range(len(manifest['chunks']))) - set(received))}

    def receive(self, transfer_id, ordinal, content):
        number(ordinal, 'ordinal')
        if not isinstance(content, str) or len(content) > 1400000:
            reject('transfer_content', 'Chunk requires bounded base64 text')
        try:
            decoded = base64.b64decode(content, validate=True)
        except (ValueError, TypeError):
            reject('transfer_content', 'Invalid base64 chunk')
        with self.store.atomic():
            status = self.status(transfer_id)
            manifest = status['manifest']
            if ordinal >= len(manifest['chunks']):
                reject('transfer_ordinal', 'Chunk index exceeds manifest')
            expected_size = min(manifest['chunk_size'], manifest['size'] - ordinal * manifest['chunk_size'])
            if len(decoded) != expected_size or hashlib.sha256(decoded).hexdigest() != manifest['chunks'][ordinal]:
                reject('transfer_integrity', 'Chunk differs from manifest')
            prior = self.store.one('SELECT content FROM evidence_chunks WHERE transfer_id=? AND ordinal=?', (transfer_id, ordinal))
            if prior:
                if prior['content'] != decoded:
                    reject('transfer_conflict', 'Chunk slot already occupied')
                return self.status(transfer_id)
            if status['state'] != 'receiving':
                reject('transfer_state', 'Import no longer accepts chunks')
            self.store.execute('INSERT INTO evidence_chunks VALUES(?,?,?)', (transfer_id, ordinal, decoded))
            return self.status(transfer_id)

    def finish(self, transfer_id):
        with self.store.atomic():
            status = self.status(transfer_id)
            if status['missing']:
                reject('transfer_incomplete', 'Not all evidence chunks have arrived', missing=status['missing'])
            content = b''.join(row['content'] for row in self.store.all('SELECT content FROM evidence_chunks WHERE transfer_id=? ORDER BY ordinal', (transfer_id,)))
            if hashlib.sha256(content).hexdigest() != status['manifest']['sha256']:
                reject('transfer_integrity', 'Whole evidence checksum differs')
            artifact = loads(content)
            verify_invoice(artifact)
            if artifact['payload']['invoice_id'] != status['manifest']['invoice_id']:
                reject('transfer_identity', 'Invoice identifier differs from manifest')
            self.store.execute("UPDATE evidence_imports SET state='verified',artifact=? WHERE transfer_id=?", (canonical(artifact), transfer_id))
            return artifact

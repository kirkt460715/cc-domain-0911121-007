import contextlib
import sqlite3
from meterbook.values import now, reject, canonical, digest

DDL = '''
CREATE TABLE IF NOT EXISTS contracts(contract_id TEXT PRIMARY KEY,provider TEXT NOT NULL,consumer TEXT NOT NULL,currency TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,definition TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract_history(contract_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(contract_id,revision));
CREATE TABLE IF NOT EXISTS rates(rate_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES contracts(contract_id),meter TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS rate_interval ON rates(contract_id,meter,start_at,end_at);
CREATE TABLE IF NOT EXISTS stream_heads(contract_id TEXT NOT NULL,side TEXT NOT NULL,sequence INTEGER NOT NULL,PRIMARY KEY(contract_id,side));
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,side TEXT NOT NULL,sequence INTEGER NOT NULL,usage_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,error TEXT,received_at TEXT NOT NULL,UNIQUE(contract_id,side,sequence));
CREATE TABLE IF NOT EXISTS usage_versions(contract_id TEXT NOT NULL,side TEXT NOT NULL,usage_id TEXT NOT NULL,revision INTEGER NOT NULL,event_id TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(contract_id,side,usage_id,revision));
CREATE TABLE IF NOT EXISTS usage_heads(contract_id TEXT NOT NULL,side TEXT NOT NULL,usage_id TEXT NOT NULL,revision INTEGER NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(contract_id,side,usage_id));
CREATE TABLE IF NOT EXISTS period_revisions(contract_id TEXT NOT NULL,period TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(contract_id,period));
CREATE TABLE IF NOT EXISTS reconciliations(run_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,period TEXT NOT NULL,revision INTEGER NOT NULL,contract_revision INTEGER NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS matches(run_id TEXT NOT NULL,usage_id TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(run_id,usage_id));
CREATE TABLE IF NOT EXISTS resolutions(contract_id TEXT NOT NULL,usage_id TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,base_hash TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(contract_id,usage_id));
CREATE TABLE IF NOT EXISTS resolution_history(contract_id TEXT NOT NULL,usage_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(contract_id,usage_id,revision));
CREATE TABLE IF NOT EXISTS invoices(invoice_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,period TEXT NOT NULL,kind TEXT NOT NULL,parent_id TEXT,revision INTEGER NOT NULL,state TEXT NOT NULL,run_id TEXT NOT NULL,snapshot TEXT NOT NULL,digest TEXT NOT NULL,total_cents INTEGER NOT NULL,currency TEXT NOT NULL,created_at TEXT NOT NULL,closed_at TEXT);
CREATE TABLE IF NOT EXISTS invoice_heads(contract_id TEXT NOT NULL,period TEXT NOT NULL,invoice_id TEXT NOT NULL,PRIMARY KEY(contract_id,period));
CREATE TABLE IF NOT EXISTS invoice_lines(invoice_id TEXT NOT NULL,line_id TEXT NOT NULL,payload TEXT NOT NULL,amount_cents INTEGER NOT NULL,PRIMARY KEY(invoice_id,line_id));
CREATE TABLE IF NOT EXISTS ledger_entries(position INTEGER PRIMARY KEY AUTOINCREMENT,invoice_id TEXT NOT NULL,account TEXT NOT NULL,amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(payment_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,reference TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payment_allocations(payment_id TEXT NOT NULL,invoice_id TEXT NOT NULL,amount_cents INTEGER NOT NULL,PRIMARY KEY(payment_id,invoice_id));
CREATE TABLE IF NOT EXISTS actors(actor_id TEXT PRIMARY KEY,role TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(position INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,body TEXT NOT NULL,previous_hash TEXT NOT NULL,entry_hash TEXT NOT NULL,at TEXT NOT NULL);
'''


class Store:
    def __init__(self, path=':memory:', clock=now):
        self.clock = clock
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            reject('store_version', 'Unsupported meterbook schema')
        self.db.executescript(DDL)
        self.db.execute('PRAGMA user_version=1')
        self.depth = 0

    def close(self):
        self.db.close()

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql, params=()):
        return [dict(row) for row in self.execute(sql, params)]

    def scalar(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return row[0] if row else None

    @contextlib.contextmanager
    def atomic(self):
        depth = self.depth
        self.execute(f'SAVEPOINT meter_{depth}' if depth else 'BEGIN IMMEDIATE')
        self.depth += 1
        try:
            yield self
        except BaseException:
            if depth:
                self.execute(f'ROLLBACK TO meter_{depth}')
                self.execute(f'RELEASE meter_{depth}')
            else:
                self.db.rollback()
            raise
        else:
            if depth:
                self.execute(f'RELEASE meter_{depth}')
            else:
                self.db.commit()
        finally:
            self.depth -= 1

    def touch(self, contract_id, period):
        self.execute('INSERT INTO period_revisions VALUES(?,?,1) ON CONFLICT(contract_id,period) DO UPDATE SET revision=revision+1', (contract_id, period))
        return self.revision(contract_id, period)

    def revision(self, contract_id, period):
        return self.scalar('SELECT revision FROM period_revisions WHERE contract_id=? AND period=?', (contract_id, period)) or 0

    def log(self, action, body):
        previous = self.scalar('SELECT entry_hash FROM audit ORDER BY position DESC LIMIT 1') or '0' * 64
        encoded = canonical(body)
        at = self.clock()
        sha = digest([previous, action, encoded, at])
        self.execute('INSERT INTO audit(action,body,previous_hash,entry_hash,at) VALUES(?,?,?,?,?)', (action, encoded, previous, sha, at))
        return sha

    def integrity(self):
        checks = [row[0] for row in self.execute('PRAGMA integrity_check')]
        foreign = self.all('PRAGMA foreign_key_check')
        return {'ok': checks == ['ok'] and not foreign, 'checks': checks, 'foreign_keys': foreign}

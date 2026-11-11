import json
from meterbook.values import key, month, instant, fields, canonical, digest, reject, prose, number, month_bounds
from meterbook.contracts import registry
from meterbook.events import receiver, disputes

DDL = '''
CREATE TABLE IF NOT EXISTS period_controls(contract_id TEXT NOT NULL,period TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,reason TEXT,actor TEXT,PRIMARY KEY(contract_id,period));
CREATE TABLE IF NOT EXISTS declarations(contract_id TEXT NOT NULL,period TEXT NOT NULL,side TEXT NOT NULL,revision INTEGER NOT NULL,sequence INTEGER NOT NULL,usage_count INTEGER NOT NULL,digest TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(contract_id,period,side));
CREATE TABLE IF NOT EXISTS declaration_history(contract_id TEXT NOT NULL,period TEXT NOT NULL,side TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(contract_id,period,side,revision));
'''


class Calendar:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('calendar_transaction', 'Initialize period calendar outside transactions')
        store.db.executescript(DDL)

    def open(self, contract_id, period):
        month(period)
        registry.get(self.store, contract_id)
        with self.store.atomic():
            self.store.execute("INSERT OR IGNORE INTO period_controls VALUES(?,?,'open',1,NULL,NULL)", (contract_id, period))
            return self.get(contract_id, period)

    def get(self, contract_id, period):
        row = self.store.one('SELECT * FROM period_controls WHERE contract_id=? AND period=?', (contract_id, period))
        if row is None:
            reject('period_missing', 'Period calendar entry does not exist')
        row['declarations'] = self.store.all('SELECT * FROM declarations WHERE contract_id=? AND period=? ORDER BY side', (contract_id, period))
        return row

    def side_manifest(self, contract_id, period, side):
        month(period)
        if side not in {'provider', 'consumer'}:
            reject('event_side', 'Unknown reporting side')
        status = receiver.stream_status(self.store, contract_id, side)
        rows = self.store.all('SELECT usage_id FROM usage_heads WHERE contract_id=? AND side=? ORDER BY usage_id', (contract_id, side))
        usages = []
        for row in rows:
            usage = receiver.head(self.store, contract_id, side, row['usage_id'])
            if usage['payload']['at'][:7] == period:
                usages.append({'usage_id': usage['usage_id'], 'revision': usage['revision'], 'state': usage['state'], 'payload': usage['payload']})
        return {'contract_id': contract_id, 'period': period, 'side': side, 'sequence': status['cursor'], 'usage_count': len(usages), 'digest': digest(usages), 'pending_events': len(status['pending']), 'usages': usages}

    def declare(self, contract_id, period, side, expected_revision, sequence, usage_count, expected_digest):
        number(sequence, 'sequence')
        number(usage_count, 'usage_count')
        with self.store.atomic():
            control = self.get(contract_id, period)
            if control['state'] != 'open':
                reject('period_state', 'Only open periods accept completeness declarations')
            existing = self.store.one('SELECT * FROM declarations WHERE contract_id=? AND period=? AND side=?', (contract_id, period, side))
            revision = existing['revision'] if existing else 0
            if revision != expected_revision:
                reject('declaration_stale', 'Declaration revision changed')
            current = self.side_manifest(contract_id, period, side)
            if current['pending_events']:
                reject('declaration_pending', 'Stream has unprocessed events or gaps')
            if (sequence, usage_count, expected_digest) != (current['sequence'], current['usage_count'], current['digest']):
                reject('declaration_manifest', 'Declaration differs from current applied stream')
            row = {'contract_id': contract_id, 'period': period, 'side': side, 'revision': revision + 1, 'sequence': sequence, 'usage_count': usage_count, 'digest': expected_digest, 'at': self.store.clock()}
            self.store.execute('INSERT INTO declarations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(contract_id,period,side) DO UPDATE SET revision=excluded.revision,sequence=excluded.sequence,usage_count=excluded.usage_count,digest=excluded.digest,at=excluded.at', tuple(row.values()))
            self.store.execute('INSERT INTO declaration_history VALUES(?,?,?,?,?)', (contract_id, period, side, revision + 1, canonical(row)))
            self.store.log('period.declared', row)
            return row

    def readiness(self, contract_id, period):
        control = self.get(contract_id, period)
        failures = []
        declarations = {row['side']: row for row in control['declarations']}
        if control['state'] != 'open':
            failures.append({'code': 'period_held', 'state': control['state']})
        for side in ('provider', 'consumer'):
            current = self.side_manifest(contract_id, period, side)
            declaration = declarations.get(side)
            if declaration is None:
                failures.append({'code': 'declaration_missing', 'side': side})
                continue
            if current['pending_events']:
                failures.append({'code': 'stream_pending', 'side': side})
            if current['digest'] != declaration['digest'] or current['usage_count'] != declaration['usage_count']:
                failures.append({'code': 'declaration_changed', 'side': side})
        return {'ready': not failures, 'failures': failures, 'control_revision': control['revision']}

    def hold(self, contract_id, period, expected_revision, actor, reason):
        disputes.actor(self.store, actor, 'approver')
        reason = prose(reason, 'reason')
        with self.store.atomic():
            current = self.get(contract_id, period)
            if current['revision'] != expected_revision:
                reject('period_stale', 'Period control revision changed')
            if current['state'] != 'open':
                reject('period_state', 'Period is already held')
            self.store.execute("UPDATE period_controls SET state='held',revision=revision+1,reason=?,actor=? WHERE contract_id=? AND period=?", (reason, actor, contract_id, period))
            self.store.log('period.held', {'contract_id': contract_id, 'period': period, 'actor': actor, 'reason': reason})
            return self.get(contract_id, period)

    def release_hold(self, contract_id, period, expected_revision, actor, reason):
        disputes.actor(self.store, actor, 'approver')
        reason = prose(reason, 'reason')
        with self.store.atomic():
            current = self.get(contract_id, period)
            if current['revision'] != expected_revision:
                reject('period_stale', 'Period control revision changed')
            if current['state'] != 'held':
                reject('period_state', 'Period is not held')
            self.store.execute("UPDATE period_controls SET state='open',revision=revision+1,reason=?,actor=? WHERE contract_id=? AND period=?", (reason, actor, contract_id, period))
            self.store.log('period.hold_released', {'contract_id': contract_id, 'period': period, 'actor': actor, 'reason': reason})
            return self.get(contract_id, period)


def enforce_if_configured(store, contract_id, period):
    exists = store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='period_controls'")
    if not exists:
        return {'configured': False}
    control = store.one('SELECT * FROM period_controls WHERE contract_id=? AND period=?', (contract_id, period))
    if not control:
        return {'configured': False}
    desk = object.__new__(Calendar)
    desk.store = store
    report = desk.readiness(contract_id, period)
    if not report['ready']:
        reject('period_not_ready', 'Configured period controls block closing', failures=report['failures'])
    return {'configured': True, **report}

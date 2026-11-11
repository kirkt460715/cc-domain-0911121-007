import json
from meterbook.values import key, month, fields, canonical, digest, reject, MeterError
from meterbook.contracts import registry
from meterbook.events import reconcile
from meterbook.settlement import invoices

DDL = '''
CREATE TABLE IF NOT EXISTS close_campaigns(campaign_id TEXT PRIMARY KEY,definition TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS close_tasks(campaign_id TEXT NOT NULL,ordinal INTEGER NOT NULL,contract_id TEXT NOT NULL,period TEXT NOT NULL,state TEXT NOT NULL,attempt INTEGER NOT NULL,invoice_id TEXT,error TEXT,PRIMARY KEY(campaign_id,ordinal));
'''


class Campaigns:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('campaign_transaction', 'Initialize close campaigns outside transactions')
        store.db.executescript(DDL)

    def create(self, campaign_id, periods):
        key(campaign_id)
        if not isinstance(periods, list) or not 1 <= len(periods) <= 100:
            reject('campaign_periods', 'Campaign requires one to one hundred contract periods')
        normalized = []
        seen = set()
        for period in periods:
            fields(period, {'contract_id', 'period'}, {'allow_disputes'})
            registry.get(self.store, period['contract_id'])
            month(period['period'])
            identity = (period['contract_id'], period['period'])
            if identity in seen:
                reject('campaign_duplicate', 'Contract period appears twice')
            seen.add(identity)
            allow = period.get('allow_disputes', False)
            if type(allow) is not bool:
                reject('campaign_flag', 'allow_disputes must be boolean')
            normalized.append({**period, 'allow_disputes': allow})
        with self.store.atomic():
            existing = self.store.one('SELECT digest FROM close_campaigns WHERE campaign_id=?', (campaign_id,))
            if existing:
                if existing['digest'] != digest(normalized):
                    reject('campaign_conflict', 'Campaign identifier binds other periods')
                return self.get(campaign_id)
            self.store.execute("INSERT INTO close_campaigns VALUES(?,?,?,'open',?)", (campaign_id, canonical(normalized), digest(normalized), self.store.clock()))
            for ordinal, item in enumerate(normalized):
                self.store.execute("INSERT INTO close_tasks VALUES(?,?,?,?,'pending',0,NULL,NULL)", (campaign_id, ordinal, item['contract_id'], item['period']))
            self.store.log('campaign.created', {'campaign_id': campaign_id, 'periods': len(normalized)})
            return self.get(campaign_id)

    def get(self, campaign_id):
        row = self.store.one('SELECT * FROM close_campaigns WHERE campaign_id=?', (campaign_id,))
        if row is None:
            reject('campaign_missing', 'Close campaign does not exist')
        row['definition'] = json.loads(row['definition'])
        if digest(row['definition']) != row['digest']:
            reject('campaign_corrupt', 'Campaign definition checksum differs')
        row['tasks'] = self.store.all('SELECT * FROM close_tasks WHERE campaign_id=? ORDER BY ordinal', (campaign_id,))
        for task in row['tasks']:
            task['error'] = json.loads(task['error']) if task['error'] else None
        return row

    def advance(self, campaign_id, approver):
        with self.store.atomic():
            campaign = self.get(campaign_id)
            if campaign['state'] != 'open':
                reject('campaign_closed', 'Campaign already finished')
            pending = [row for row in campaign['tasks'] if row['state'] == 'pending']
            if not pending:
                return {'advanced': False, 'campaign': campaign}
            task = pending[0]
            attempt = task['attempt'] + 1
            identity = digest([campaign_id, task['ordinal'], attempt])
            run_id = 'run-' + identity
            invoice_id = 'invoice-' + identity
            try:
                with self.store.atomic():
                    reconcile.run(self.store, run_id, task['contract_id'], task['period'])
                    kind = 'adjustment' if invoices.current(self.store, task['contract_id'], task['period']) else 'original'
                    invoices.prepare(self.store, invoice_id, run_id, kind, campaign['definition'][task['ordinal']]['allow_disputes'])
                    invoices.close(self.store, invoice_id, 1, approver)
                self.store.execute("UPDATE close_tasks SET state='complete',attempt=?,invoice_id=?,error=NULL WHERE campaign_id=? AND ordinal=?", (attempt, invoice_id, campaign_id, task['ordinal']))
            except MeterError as exc:
                self.store.execute("UPDATE close_tasks SET state='failed',attempt=?,error=? WHERE campaign_id=? AND ordinal=?", (attempt, canonical(exc.payload()), campaign_id, task['ordinal']))
            self.store.log('campaign.advanced', {'campaign_id': campaign_id, 'ordinal': task['ordinal'], 'attempt': attempt})
            return {'advanced': True, 'campaign': self.get(campaign_id)}

    def retry(self, campaign_id, ordinal):
        from meterbook.values import number
        number(ordinal, 'ordinal')
        with self.store.atomic():
            campaign = self.get(campaign_id)
            if campaign['state'] != 'open' or ordinal >= len(campaign['tasks']):
                reject('campaign_retry', 'Campaign or task index cannot be retried')
            task = campaign['tasks'][ordinal]
            if task['state'] != 'failed':
                reject('campaign_retry_state', 'Only failed tasks may retry')
            self.store.execute("UPDATE close_tasks SET state='pending',error=NULL WHERE campaign_id=? AND ordinal=?", (campaign_id, ordinal))
            self.store.log('campaign.retried', {'campaign_id': campaign_id, 'ordinal': ordinal})
            return self.get(campaign_id)

    def finish(self, campaign_id, allow_partial=False):
        if type(allow_partial) is not bool:
            reject('campaign_partial', 'allow_partial must be boolean')
        with self.store.atomic():
            campaign = self.get(campaign_id)
            if campaign['state'] != 'open':
                return campaign
            if any(row['state'] == 'pending' for row in campaign['tasks']):
                reject('campaign_pending', 'Campaign still has pending tasks')
            failed = any(row['state'] == 'failed' for row in campaign['tasks'])
            if failed and not allow_partial:
                reject('campaign_failures', 'Explicit partial completion required')
            state = 'partial' if failed else 'complete'
            self.store.execute('UPDATE close_campaigns SET state=? WHERE campaign_id=?', (state, campaign_id))
            self.store.log('campaign.finished', {'campaign_id': campaign_id, 'state': state})
            return self.get(campaign_id)

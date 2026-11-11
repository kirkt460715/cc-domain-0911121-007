import json
from meterbook.values import key, number, prose, canonical, digest, reject
from meterbook.settlement import invoices, pricing
from meterbook.events import disputes
from meterbook.contracts import rates

DDL = '''
CREATE TABLE IF NOT EXISTS approval_policies(contract_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,threshold_cents INTEGER NOT NULL,required_reviewers INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS closing_reviews(review_id TEXT PRIMARY KEY,invoice_id TEXT NOT NULL,invoice_digest TEXT NOT NULL,policy_revision INTEGER NOT NULL,author TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closing_votes(review_id TEXT NOT NULL,reviewer TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(review_id,reviewer));
'''


class Reviews:
    def __init__(self, store):
        self.store = store
        if store.depth:
            reject('review_transaction', 'Initialize closing reviews outside transactions')
        store.db.executescript(DDL)

    def policy(self, contract_id, expected_revision, threshold_cents, required_reviewers):
        from meterbook.contracts import registry
        registry.get(self.store, contract_id)
        number(threshold_cents, 'threshold_cents', 0, 10**15)
        number(required_reviewers, 'required_reviewers', 1, 5)
        with self.store.atomic():
            row = self.store.one('SELECT * FROM approval_policies WHERE contract_id=?', (contract_id,))
            revision = row['revision'] if row else 0
            if revision != expected_revision:
                reject('review_policy_stale', 'Closing approval policy changed')
            self.store.execute('INSERT INTO approval_policies VALUES(?,?,?,?) ON CONFLICT(contract_id) DO UPDATE SET revision=excluded.revision,threshold_cents=excluded.threshold_cents,required_reviewers=excluded.required_reviewers', (contract_id, revision + 1, threshold_cents, required_reviewers))
            self.store.log('review.policy', {'contract_id': contract_id, 'revision': revision + 1})
            return self.store.one('SELECT * FROM approval_policies WHERE contract_id=?', (contract_id,))

    def submit(self, review_id, invoice_id, author, reason):
        key(review_id)
        disputes.actor(self.store, author)
        reason = prose(reason, 'reason')
        with self.store.atomic():
            invoice = invoices.get(self.store, invoice_id)
            if invoice['state'] != 'prepared':
                reject('review_invoice_state', 'Only prepared invoices enter review')
            policy = self.store.one('SELECT * FROM approval_policies WHERE contract_id=?', (invoice['contract_id'],))
            if policy is None:
                reject('review_policy_missing', 'Configure a closing review policy first')
            if self.store.one('SELECT review_id FROM closing_reviews WHERE review_id=?', (review_id,)):
                reject('review_exists', 'Review identifier already exists')
            existing = self.store.one("SELECT review_id FROM closing_reviews WHERE invoice_id=? AND state='pending'", (invoice_id,))
            if existing:
                reject('review_pending', 'Invoice already has a pending review')
            self.store.execute("INSERT INTO closing_reviews VALUES(?,?,?,?,?,'pending',?,?)", (review_id, invoice_id, invoice['digest'], policy['revision'], author, reason, self.store.clock()))
            self.store.log('review.submitted', {'review_id': review_id, 'invoice_id': invoice_id, 'author': author})
            return self.get(review_id)

    def get(self, review_id):
        row = self.store.one('SELECT * FROM closing_reviews WHERE review_id=?', (review_id,))
        if row is None:
            reject('review_missing', 'Closing review does not exist')
        row['votes'] = self.store.all('SELECT * FROM closing_votes WHERE review_id=? ORDER BY reviewer', (review_id,))
        invoice = invoices.get(self.store, row['invoice_id'])
        policy = self.store.one('SELECT * FROM approval_policies WHERE contract_id=?', (invoice['contract_id'],))
        row['fresh'] = policy is not None and policy['revision'] == row['policy_revision'] and invoice['digest'] == row['invoice_digest']
        row['required_reviewers'] = policy['required_reviewers'] if policy else None
        row['approvals'] = sum(vote['decision'] == 'approve' for vote in row['votes'])
        return row

    def vote(self, review_id, reviewer, decision, reason):
        disputes.actor(self.store, reviewer, 'approver')
        if decision not in {'approve', 'reject'}:
            reject('review_decision', 'Review vote must approve or reject')
        reason = prose(reason, 'reason')
        with self.store.atomic():
            review = self.get(review_id)
            if review['state'] != 'pending' or not review['fresh']:
                reject('review_state', 'Review is terminal or policy changed')
            if reviewer == review['author']:
                reject('review_separation', 'Author cannot approve own invoice submission')
            existing = next((row for row in review['votes'] if row['reviewer'] == reviewer), None)
            if existing:
                if (existing['decision'], existing['reason']) != (decision, reason):
                    reject('review_vote_conflict', 'Reviewer already cast another vote')
                return review
            invoice = invoices.get(self.store, review['invoice_id'])
            if invoice['state'] != 'prepared':
                reject('review_invoice_state', 'Invoice is no longer prepared')
            calculated = pricing.calculate(self.store, invoice['run_id'], invoice['snapshot']['allow_disputes'])
            if calculated != invoice['snapshot']['calculation']:
                reject('review_calculation', 'Invoice calculation changed since submission')
            self.store.execute('INSERT INTO closing_votes VALUES(?,?,?,?,?)', (review_id, reviewer, decision, reason, self.store.clock()))
            if decision == 'reject':
                self.store.execute("UPDATE closing_reviews SET state='rejected' WHERE review_id=?", (review_id,))
            self.store.log('review.voted', {'review_id': review_id, 'reviewer': reviewer, 'decision': decision})
            return self.get(review_id)

    def cancel(self, review_id, author, reason):
        disputes.actor(self.store, author)
        reason = prose(reason, 'reason')
        with self.store.atomic():
            review = self.get(review_id)
            if review['author'] != author or review['state'] != 'pending':
                reject('review_cancel', 'Only submitter can cancel pending review')
            self.store.execute("UPDATE closing_reviews SET state='cancelled' WHERE review_id=?", (review_id,))
            self.store.log('review.cancelled', {'review_id': review_id, 'author': author, 'reason': reason})
            return self.get(review_id)

    def queue(self, contract_id):
        rows = self.store.all("SELECT r.review_id FROM closing_reviews r JOIN invoices i ON i.invoice_id=r.invoice_id WHERE i.contract_id=? AND r.state='pending' ORDER BY r.created_at,r.review_id", (contract_id,))
        return [self.get(row['review_id']) for row in rows]


def enforce(store, invoice):
    table = store.one("SELECT name FROM sqlite_master WHERE name='approval_policies'")
    if not table:
        return
    policy = store.one('SELECT * FROM approval_policies WHERE contract_id=?', (invoice['contract_id'],))
    if policy is None or abs(invoice['total_cents']) < policy['threshold_cents']:
        return
    rows = store.all("SELECT * FROM closing_reviews WHERE invoice_id=? AND state='pending' ORDER BY review_id", (invoice['invoice_id'],))
    for review in rows:
        if review['policy_revision'] != policy['revision'] or review['invoice_digest'] != invoice['digest']:
            continue
        votes = store.all("SELECT v.reviewer FROM closing_votes v JOIN actors a ON a.actor_id=v.reviewer WHERE v.review_id=? AND v.decision='approve' AND a.state='active' AND a.role='approver'", (review['review_id'],))
        distinct = {row['reviewer'] for row in votes} - {review['author']}
        if len(distinct) >= policy['required_reviewers']:
            store.execute("UPDATE closing_reviews SET state='consumed' WHERE review_id=?", (review['review_id'],))
            return
    reject('review_required', 'Invoice requires the configured independent approvals')

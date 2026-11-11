from tests.support import Case
from meterbook.events import reconcile
from meterbook.settlement import invoices, payments, preflight
from meterbook.audit import closing


class ReviewsAndAccounting(Case):
    def prepare(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'invoice', 'run')

    def test_two_reviewers_required_and_consumed_atomically(self):
        self.prepare()
        self.app.reviews.policy('contract-a', 0, 200, 2)
        self.app.reviews.submit('review', 'invoice', 'analyst', 'monthly settlement')
        self.error('review_required', invoices.close, self.store, 'invoice', 1, 'approver')
        vote = self.app.reviews.vote('review', 'approver', 'approve', 'checked rates')
        self.assertEqual(vote['approvals'], 1)
        self.error('review_required', invoices.close, self.store, 'invoice', 1, 'approver')
        self.assertEqual(self.app.reviews.get('review')['state'], 'pending')
        self.app.reviews.vote('review', 'reviewer', 'approve', 'checked quantities')
        invoices.close(self.store, 'invoice', 1, 'approver')
        self.assertEqual(self.app.reviews.get('review')['state'], 'consumed')
        self.assertEqual(self.app.reviews.queue('contract-a'), [])

    def test_review_author_separation_and_reject_terminal(self):
        self.prepare()
        self.app.reviews.policy('contract-a', 0, 0, 1)
        self.app.reviews.submit('review', 'invoice', 'approver', 'submit')
        self.error('review_separation', self.app.reviews.vote, 'review', 'approver', 'approve', 'self')
        rejected = self.app.reviews.vote('review', 'reviewer', 'reject', 'needs investigation')
        self.assertEqual(rejected['state'], 'rejected')
        self.error('review_state', self.app.reviews.vote, 'review', 'reviewer', 'approve', 'change')
        self.error('review_required', invoices.close, self.store, 'invoice', 1, 'approver')
        self.app.reviews.submit('again', 'invoice', 'analyst', 'resubmission')
        self.assertEqual(len(self.app.reviews.queue('contract-a')), 1)
        self.assertEqual(self.app.reviews.cancel('again', 'analyst', 'withdraw')['state'], 'cancelled')

    def test_policy_revision_invalidates_votes(self):
        self.prepare()
        self.app.reviews.policy('contract-a', 0, 0, 1)
        self.app.reviews.submit('review', 'invoice', 'analyst', 'submit')
        self.app.reviews.vote('review', 'approver', 'approve', 'checked')
        self.app.reviews.policy('contract-a', 1, 0, 2)
        self.assertFalse(self.app.reviews.get('review')['fresh'])
        self.error('review_required', invoices.close, self.store, 'invoice', 1, 'approver')
        self.error('review_policy_stale', self.app.reviews.policy, 'contract-a', 1, 0, 1)
        self.assertEqual(invoices.get(self.store, 'invoice')['state'], 'prepared')

    def test_cost_allocations_pin_rule_and_balance_signed_delta(self):
        self.pair('11')
        self.close_invoice()
        self.app.costcenters.put('rule', 0, {'weights': {'north': '1', 'south': '2'}})
        original = self.app.costcenters.allocate('cost-a', 'invoice-a', 'rule')
        self.assertEqual(original['payload']['centers'], {'north': 92, 'south': 183})
        self.app.costcenters.put('rule', 1, {'weights': {'north': '1', 'south': '1'}})
        self.assertEqual(self.app.costcenters.get('cost-a')['rule_revision'], 1)
        self.pair('10', 2, 2)
        self.close_invoice('adjust', 'run-b', kind='adjustment')
        adjustment = self.app.costcenters.allocate('cost-b', 'adjust', 'rule')
        self.assertEqual(adjustment['payload']['centers'], {'north': -13, 'south': -12})
        self.assertEqual(self.app.costcenters.statement('contract-a')['total_cents'], 250)
        self.app.costcenters.allocate('duplicate', 'adjust', 'rule')
        self.error('cost_duplicate_invoice', self.app.costcenters.statement, 'contract-a')

    def test_statement_capture_and_comparison(self):
        self.pair()
        self.close_invoice()
        before = self.app.statements.capture('before', 'contract-a')
        self.assertTrue(before['current'])
        self.assertTrue(before['payload']['balanced'])
        payments.receive(self.store, 'payment', 'contract-a', 100, 'CNY', 'bank')
        payments.auto_allocate(self.store, 'payment')
        after = self.app.statements.capture('after', 'contract-a')
        self.assertEqual(after['payload']['net_due_cents'], 150)
        self.assertFalse(self.app.statements.get('before')['current'])
        compared = self.app.statements.compare('before', 'after')
        self.assertEqual(compared['delta']['received_cents'], 100)
        self.assertEqual(compared['delta']['net_due_cents'], -100)
        self.assertEqual(closing.ledger_trial(self.store, 'contract-a')['balance_cents'], 0)

    def test_no_change_adjustment_rejected(self):
        self.pair()
        self.close_invoice()
        reconcile.run(self.store, 'again', 'contract-a', '2026-11')
        self.error('invoice_no_change', invoices.prepare, self.store, 'no-change', 'again', 'adjustment')
        self.assertEqual(len(invoices.listing(self.store)), 1)
        self.assertEqual(invoices.current(self.store, 'contract-a', '2026-11')['invoice_id'], 'invoice-a')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM ledger_entries'), 2)

    def test_credit_reversal_preserves_evidence(self):
        self.pair('20')
        self.close_invoice()
        self.pair('10', 2, 2)
        self.close_invoice('credit', 'run-b', kind='adjustment')
        self.app.credits.apply('allocation', 'credit', 'invoice-a', 250, 'approver', 'correction')
        self.assertEqual(self.app.credits.balance('invoice-a')['effective_unpaid_cents'], 250)
        self.app.credits.reverse('allocation', 'reviewer', 'wrong destination')
        self.assertEqual(self.app.credits.balance('invoice-a')['effective_unpaid_cents'], 500)
        self.assertEqual(self.app.credits.balance('credit')['effective_unpaid_cents'], -250)
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM credit_applications'), 1)
        self.assertEqual(closing.receivables(self.store, 'contract-a')['net_due_cents'], 250)

    def test_preflight_sees_late_adjustment(self):
        self.pair()
        self.close_invoice()
        self.assertEqual(preflight.pending_adjustments(self.store), [])
        self.pair('12', 2, 2)
        pending = preflight.pending_adjustments(self.store)
        self.assertEqual(len(pending), 1)
        reconcile.run(self.store, 'new', 'contract-a', '2026-11')
        report = preflight.estimate_adjustment(self.store, 'new')
        self.assertEqual(report['adjustment_cents'], 50)
        self.assertEqual(invoices.get(self.store, 'invoice-a')['total_cents'], 250)

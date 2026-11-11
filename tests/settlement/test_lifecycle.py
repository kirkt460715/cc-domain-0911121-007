from tests.support import Case, usage, event, definition, tariff
from meterbook.events import receiver, reconcile, disputes
from meterbook.settlement import invoices, payments, pricing
from meterbook.audit import checks, closing
from meterbook.contracts import registry, rates
from meterbook.values import digest


class InvoiceLifecycle(Case):
    def test_close_and_late_correction(self):
        self.pair('10')
        original = self.close_invoice()
        self.assertEqual(original['total_cents'], 250)
        self.assertEqual(original['state'], 'closed')
        self.pair('20', 2, 2)
        adjusted = self.close_invoice('adjust-a', 'run-b', kind='adjustment')
        self.assertEqual(adjusted['total_cents'], 250)
        self.assertEqual(adjusted['parent_id'], 'invoice-a')
        self.assertEqual(invoices.get(self.store, 'invoice-a')['total_cents'], 250)
        chain = invoices.chain(self.store, 'contract-a', '2026-11')
        self.assertEqual(chain['cumulative_cents'], 500)
        self.assertEqual(chain['latest_recalculated_cents'], 500)
        self.assertTrue(closing.period_bridge(self.store, 'contract-a', '2026-11')['balanced'])
        self.assertTrue(checks.check(self.store)['ok'])

    def test_competing_preparations_fence_head(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'first', 'run')
        invoices.prepare(self.store, 'second', 'run')
        invoices.close(self.store, 'first', 1, 'approver')
        self.error('invoice_head_changed', invoices.close, self.store, 'second', 1, 'approver')
        self.assertEqual(invoices.get(self.store, 'second')['state'], 'prepared')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM ledger_entries'), 2)
        self.assertEqual(invoices.current(self.store, 'contract-a', '2026-11')['invoice_id'], 'first')

    def test_usage_change_invalidates_prepared(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'first', 'run')
        self.pair('11', 2, 2)
        self.error('invoice_usage_changed', invoices.close, self.store, 'first', 1, 'approver')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM ledger_entries'), 0)
        abandoned = invoices.abandon(self.store, 'first', 1, 'usage updated')
        self.assertEqual(abandoned['state'], 'abandoned')
        self.error('invoice_state', invoices.close, self.store, 'first', 2, 'approver')

    def test_rate_change_invalidates_prepared(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'first', 'run')
        rates.withdraw(self.store, 'rate-a', 'new tariff')
        rates.add(self.store, 'rate-b', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff('0.5'))
        self.error('invoice_rates_changed', invoices.close, self.store, 'first', 1, 'approver')
        self.assertEqual(invoices.get(self.store, 'first')['snapshot']['calculation']['total_cents'], 250)
        self.assertEqual(pricing.calculate(self.store, 'run')['total_cents'], 500)

    def test_disputes_explicit_exclusion(self):
        receiver.receive(self.store, event())
        receiver.drain(self.store, 'contract-a', 'provider')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        self.error('unresolved_disputes', invoices.prepare, self.store, 'bad', 'run')
        result = invoices.prepare(self.store, 'partial', 'run', allow_disputes=True)
        self.assertEqual(result['total_cents'], 0)
        self.assertEqual(result['snapshot']['calculation']['excluded_disputes'], ['usage-a'])
        invoices.close(self.store, 'partial', 1, 'approver')
        self.assertEqual(invoices.chain(self.store, 'contract-a', '2026-11')['cumulative_cents'], 0)

    def test_negative_adjustment_and_credit(self):
        self.pair('20')
        self.close_invoice()
        self.pair('5', 2, 2)
        adjusted = self.close_invoice('credit-a', 'run-b', kind='adjustment')
        self.assertEqual(adjusted['total_cents'], -375)
        applied = self.app.credits.apply('credit-use', 'credit-a', 'invoice-a', 375, 'approver', 'net correction')
        self.assertEqual(applied['state'], 'active')
        self.assertEqual(self.app.credits.balance('invoice-a')['effective_unpaid_cents'], 125)
        self.assertEqual(self.app.credits.balance('credit-a')['effective_unpaid_cents'], 0)
        payments.receive(self.store, 'payment', 'contract-a', 200, 'CNY', 'bank')
        self.error('payment_overallocation', payments.allocate, self.store, 'payment', 'invoice-a', 126)
        payments.allocate(self.store, 'payment', 'invoice-a', 125)
        self.assertEqual(closing.receivables(self.store, 'contract-a')['net_due_cents'], -75)
        refund = self.app.credits.refund('refund', 'contract-a', 75, 'bank-refund', 'approver')
        self.assertEqual(refund['amount_cents'], 75)
        self.assertEqual(closing.receivables(self.store, 'contract-a')['net_due_cents'], 0)
        self.error('refund_capacity', self.app.credits.refund, 'refund-b', 'contract-a', 1, 'bank', 'approver')

    def test_payment_reversal_and_idempotency(self):
        self.pair()
        self.close_invoice()
        first = payments.receive(self.store, 'payment', 'contract-a', 200, 'CNY', 'bank')
        duplicate = payments.receive(self.store, 'payment', 'contract-a', 200, 'CNY', 'bank')
        self.assertEqual(first, duplicate)
        self.error('payment_conflict', payments.receive, self.store, 'payment', 'contract-a', 201, 'CNY', 'bank')
        result = payments.auto_allocate(self.store, 'payment')
        self.assertEqual(result['applied'][0]['amount_cents'], 200)
        self.assertEqual(payments.invoice_balance(self.store, 'invoice-a')['unpaid_cents'], 50)
        self.assertEqual(payments.auto_allocate(self.store, 'payment')['applied'], [])
        payments.reverse(self.store, 'payment', 'bank returned')
        self.assertEqual(payments.invoice_balance(self.store, 'invoice-a')['unpaid_cents'], 250)
        self.error('payment_state', payments.auto_allocate, self.store, 'payment')

    def test_invoice_permissions_and_revision(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'first', 'run')
        self.error('actor_permission', invoices.close, self.store, 'first', 1, 'analyst')
        self.error('invoice_stale', invoices.close, self.store, 'first', 2, 'approver')
        invoices.close(self.store, 'first', 1, 'approver')
        self.error('invoice_state', invoices.abandon, self.store, 'first', 2, 'change mind')
        self.error('invoice_period_closed', invoices.prepare, self.store, 'another', 'run')
        self.assertEqual(len(invoices.listing(self.store, state='closed')), 1)

    def test_contract_revision_rechecks_usage_policy(self):
        self.pair()
        self.close_invoice()
        changed = definition(purposes=['commercial'])
        registry.revise(self.store, 'contract-a', 1, changed, 'restrict use')
        self.assertFalse(reconcile.get(self.store, 'run-a')['fresh'])
        new = reconcile.run(self.store, 'run-b', 'contract-a', '2026-11')
        self.assertEqual(new['rows'][0]['status'], 'disputed')
        self.assertIn('current_contract_purpose', new['rows'][0]['reasons'])
        self.assertEqual(invoices.get(self.store, 'invoice-a')['total_cents'], 250)
        self.error('unresolved_disputes', pricing.calculate, self.store, 'run-b')

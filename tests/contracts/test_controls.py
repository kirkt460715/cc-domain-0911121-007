from tests.support import Case, event, usage, tariff, definition
from meterbook.contracts import registry, rates, simulation
from meterbook.events import reconcile, receiver
from meterbook.settlement import pricing, invoices
from meterbook.audit import explanations
from meterbook.values import digest


class ContractControls(Case):
    def test_rate_boundaries_are_half_open(self):
        rates.withdraw(self.store, 'rate-a', 'split intervals')
        rates.add(self.store, 'early', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2026-11-15T00:00:00Z', tariff('1'))
        rates.add(self.store, 'late', 'contract-a', 'records', '2026-11-15T00:00:00Z', '2027-01-01T00:00:00Z', tariff('2'))
        self.assertEqual(rates.at(self.store, 'contract-a', 'records', '2026-11-14T23:59:59Z')['rate_id'], 'early')
        self.assertEqual(rates.at(self.store, 'contract-a', 'records', '2026-11-15T00:00:00Z')['rate_id'], 'late')
        self.error('rate_overlap', rates.add, self.store, 'overlap', 'contract-a', 'records', '2026-11-14T00:00:00Z', '2026-11-16T00:00:00Z', tariff())
        self.error('rate_uncovered', rates.at, self.store, 'contract-a', 'records', '2027-01-01T00:00:00Z')
        self.pair('10', at='2026-11-14T00:00:00Z')
        self.pair('10', 2, identity='usage-b', at='2026-11-15T00:00:00Z')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        calculated = pricing.calculate(self.store, 'run')
        self.assertEqual(calculated['total_cents'], 3000)
        self.assertEqual(len(calculated['lines']), 2)

    def test_tiers_included_caps_and_half_even(self):
        rule = rates.normalize({'included': '2', 'tiers': [{'up_to': '3', 'unit_price': '1'}, {'up_to': None, 'unit_price': '0.5'}]})
        priced = pricing.tier_charge('7', rule)
        self.assertEqual(priced['amount_cents'], 400)
        self.assertEqual(priced['included_used'], '2')
        self.assertEqual([row['quantity'] for row in priced['tiers']], ['3', '2'])
        half = rates.normalize({'tiers': [{'up_to': None, 'unit_price': '0.005'}]})
        self.assertEqual(pricing.tier_charge('1', half)['amount_cents'], 0)
        self.assertEqual(pricing.tier_charge('3', half)['amount_cents'], 2)
        capped = rates.normalize(tariff('1', minimum_charge='2', maximum_charge='3'))
        self.assertEqual(pricing.tier_charge('0', capped)['amount_cents'], 0)
        self.assertEqual(pricing.tier_charge('1', capped)['amount_cents'], 200)
        self.assertEqual(pricing.tier_charge('100', capped)['amount_cents'], 300)

    def test_integer_allocation_balances_and_ties(self):
        self.assertEqual(pricing.allocate(2, {'c': '1', 'b': '1', 'a': '1'}), {'a': 1, 'b': 1, 'c': 0})
        self.assertEqual(pricing.allocate(-2, {'c': '1', 'b': '1', 'a': '1'}), {'a': -1, 'b': -1, 'c': 0})
        self.assertEqual(pricing.allocate(0, {'a': '0'}), {'a': 0})
        self.error('allocation_zero', pricing.allocate, 1, {'a': '0'})
        self.error('allocation_total', pricing.allocate, True, {'a': '1'})
        for amount in range(-15, 16):
            with self.subTest(amount=amount):
                result = pricing.allocate(amount, {'a': '0.1', 'b': '0.2', 'c': '0.7'})
                self.assertEqual(sum(result.values()), amount)
                self.assertEqual(list(result), ['a', 'b', 'c'])
                self.assertTrue(all(value * amount >= 0 for value in result.values()))

    def test_exact_arithmetic_ignores_caller_precision(self):
        from decimal import localcontext
        definition = rates.normalize({'tiers': [{'up_to': None, 'unit_price': '0.12345678'}]})
        with localcontext() as context:
            context.prec = 6
            priced = pricing.tier_charge('999999999999999999.999999', definition)
            result = pricing.allocate(999999, {'a': '999999999999999999.000001', 'b': '0.000001'})
        self.assertEqual(priced['amount_cents'], 12345678000000000000)
        self.assertEqual(result['a'], 999999)
        self.assertEqual(result['b'], 0)
        self.assertEqual(sum(result.values()), 999999)

    def test_monthly_quota_and_contract_revision(self):
        registry.revise(self.store, 'contract-a', 1, definition(quota={'records': '15'}), 'monthly limit')
        self.pair('10')
        self.pair('6', 2, identity='usage-b')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        failure = self.error('usage_quota', pricing.calculate, self.store, 'run')
        self.assertEqual(failure.details['quantity'], '16')
        self.assertEqual(failure.details['quota'], '15')
        self.assertEqual(len(registry.history(self.store, 'contract-a')), 2)
        self.error('contract_stale', registry.revise, self.store, 'contract-a', 1, definition(), 'stale')
        self.assertEqual(registry.get(self.store, 'contract-a')['revision'], 2)

    def test_closed_contract_keeps_historical_corrections(self):
        self.pair()
        registry.close(self.store, 'contract-a', 1, 'expired agreement')
        self.pair('12', 2, 2)
        result = reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        self.assertEqual(result['rows'][0]['chosen']['quantity'], '12')
        self.error('contract_closed', registry.revise, self.store, 'contract-a', 2, definition(), 'reopen')
        self.error('rate_contract', rates.add, self.store, 'new', 'contract-a', 'records', '2026-11-01T00:00:00Z', '2026-12-01T00:00:00Z', tariff())
        self.assertEqual(registry.history(self.store, 'contract-a')[-1]['state'], 'closed')

    def candidate(self):
        return dict(rate_id='rate-b', contract_id='contract-a', meter='records', start_at='2026-01-01T00:00:00Z', end_at='2027-01-01T00:00:00Z', definition=tariff('0.5'), withdraw_rate_id='rate-a')

    def test_simulation_leaves_no_mutations(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        before = self.store.scalar('SELECT COUNT(*) FROM audit')
        simulated = self.app.rates.simulate(self.candidate(), ['run'])
        self.assertEqual(simulated['runs'][0]['delta_cents'], 250)
        self.assertEqual(rates.get(self.store, 'rate-a')['state'], 'active')
        self.error('rate_missing', rates.get, self.store, 'rate-b')
        self.assertEqual(self.store.scalar('SELECT COUNT(*) FROM audit'), before)
        self.assertEqual(pricing.calculate(self.store, 'run')['total_cents'], 250)
        self.assertEqual(self.store.depth, 0)

    def test_rate_proposal_independent_approval(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        proposal = self.app.rates.submit('proposal', self.candidate(), ['run'], 'approver', 'new commercial schedule')
        self.error('proposal_separation', self.app.rates.decide, 'proposal', 'approver', True, proposal['analysis_hash'])
        self.error('proposal_analysis', self.app.rates.decide, 'proposal', 'reviewer', True, 'wrong')
        result = self.app.rates.decide('proposal', 'reviewer', True, proposal['analysis_hash'])
        self.assertEqual(result['state'], 'applied')
        self.assertEqual(rates.get(self.store, 'rate-a')['state'], 'withdrawn')
        self.assertEqual(pricing.calculate(self.store, 'run')['total_cents'], 500)
        self.error('proposal_terminal', self.app.rates.decide, 'proposal', 'reviewer', True, proposal['analysis_hash'])

    def test_rate_proposal_stale_usage(self):
        self.pair()
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        proposal = self.app.rates.submit('proposal', self.candidate(), ['run'], 'analyst', 'new schedule')
        self.pair('11', 2, 2)
        self.error('proposal_stale', self.app.rates.decide, 'proposal', 'reviewer', True, proposal['analysis_hash'])
        self.assertEqual(rates.get(self.store, 'rate-a')['state'], 'active')
        rejected = self.app.rates.decide('proposal', 'reviewer', False, proposal['analysis_hash'])
        self.assertEqual(rejected['state'], 'rejected')
        self.error('rate_missing', rates.get, self.store, 'rate-b')

    def test_completeness_declarations_gate_close(self):
        self.pair()
        self.app.calendar.open('contract-a', '2026-11')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        invoices.prepare(self.store, 'invoice', 'run')
        self.error('period_not_ready', invoices.close, self.store, 'invoice', 1, 'approver')
        for side in ('provider', 'consumer'):
            manifest = self.app.calendar.side_manifest('contract-a', '2026-11', side)
            declared = self.app.calendar.declare('contract-a', '2026-11', side, 0, manifest['sequence'], manifest['usage_count'], manifest['digest'])
            self.assertEqual(declared['revision'], 1)
        self.assertTrue(self.app.calendar.readiness('contract-a', '2026-11')['ready'])
        held = self.app.calendar.hold('contract-a', '2026-11', 1, 'approver', 'check upstream')
        self.assertEqual(held['state'], 'held')
        self.error('period_not_ready', invoices.close, self.store, 'invoice', 1, 'approver')
        self.app.calendar.release_hold('contract-a', '2026-11', 2, 'approver', 'checked')
        self.assertEqual(invoices.close(self.store, 'invoice', 1, 'approver')['state'], 'closed')

    def test_declaration_changed_by_correction(self):
        self.pair()
        self.app.calendar.open('contract-a', '2026-11')
        for side in ('provider', 'consumer'):
            value = self.app.calendar.side_manifest('contract-a', '2026-11', side)
            self.app.calendar.declare('contract-a', '2026-11', side, 0, value['sequence'], value['usage_count'], value['digest'])
        self.pair('20', 2, 2)
        report = self.app.calendar.readiness('contract-a', '2026-11')
        self.assertFalse(report['ready'])
        self.assertEqual({row['code'] for row in report['failures']}, {'declaration_changed'})
        self.assertEqual(len(report['failures']), 2)
        self.error('declaration_stale', self.app.calendar.declare, 'contract-a', '2026-11', 'provider', 0, 2, 1, 'wrong')

    def test_budget_is_advisory_and_versioned(self):
        self.pair('10')
        reconcile.run(self.store, 'run', 'contract-a', '2026-11')
        budget = self.app.budgets.put('budget', 'contract-a', '2026-11', 0, {'maximum_cents': 200, 'meter_limits': {'records': '9'}})
        report = self.app.budgets.assess('assessment', 'budget', 'run')
        self.assertEqual(budget['revision'], 1)
        self.assertEqual(report['payload']['headroom_cents'], -50)
        self.assertEqual({row['code'] for row in report['payload']['findings']}, {'amount_exceeded', 'meter_exceeded'})
        self.assertTrue(report['fresh'])
        self.assertEqual(invoices.prepare(self.store, 'invoice', 'run')['total_cents'], 250)
        self.app.budgets.put('budget', 'contract-a', '2026-11', 1, {'maximum_cents': 500})
        self.assertFalse(self.app.budgets.assessment('assessment')['fresh'])
        self.assertEqual(len(self.app.budgets.history('budget')), 2)

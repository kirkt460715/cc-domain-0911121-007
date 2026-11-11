import unittest
from meterbook.service import Meterbook
from meterbook.contracts import registry, rates
from meterbook.events import receiver, disputes, reconcile
from meterbook.settlement import invoices
from meterbook.values import MeterError


def definition(**changes):
    result = dict(valid_from='2026-01-01T00:00:00Z', valid_until='2027-01-01T00:00:00Z', meters=['records'], purposes=['research'], regions=['CN'], tolerance={'records': '0.1'})
    result.update(changes)
    return result


def tariff(price='0.25', **changes):
    result = {'tiers': [{'up_to': '100', 'unit_price': price}, {'up_to': None, 'unit_price': '0.1'}]}
    result.update(changes)
    return result


def usage(quantity='10', **changes):
    result = dict(meter='records', at='2026-11-10T12:00:00Z', quantity=quantity, purpose='research', region='CN', dimensions={'source': 'synthetic'})
    result.update(changes)
    return result


def event(side='provider', sequence=1, usage_id='usage-a', revision=1, action='report', **changes):
    result = dict(event_id=f'{side}-{sequence}', contract_id='contract-a', side=side, sequence=sequence, usage_id=usage_id, revision=revision, action=action)
    if action != 'cancel':
        result['usage'] = usage()
    if action != 'report':
        result['reason'] = 'synthetic correction'
    result.update(changes)
    return result


class Case(unittest.TestCase):
    def setUp(self):
        self.app = Meterbook(clock=lambda: '2026-11-11T00:00:00.000000Z')
        self.store = self.app.store
        registry.create(self.store, 'contract-a', 'provider-a', 'consumer-a', 'CNY', definition())
        rates.add(self.store, 'rate-a', 'contract-a', 'records', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', tariff())
        disputes.register(self.store, 'analyst', 'analyst')
        disputes.register(self.store, 'approver', 'approver')
        disputes.register(self.store, 'reviewer', 'approver')

    def tearDown(self):
        self.app.close()

    def pair(self, quantity='10', sequence=1, revision=1, identity='usage-a', at=None):
        for side in ('provider', 'consumer'):
            data = usage(quantity)
            if at:
                data['at'] = at
            receiver.receive(self.store, event(side, sequence, identity, revision, 'report' if revision == 1 else 'correct', usage=data))
            result = receiver.drain(self.store, 'contract-a', side)
            self.assertIsNone(result['blocked'])
        return reconcile.pair(self.store, 'contract-a', identity)

    def close_invoice(self, name='invoice-a', run='run-a', period='2026-11', kind='original'):
        reconcile.run(self.store, run, 'contract-a', period)
        invoices.prepare(self.store, name, run, kind)
        return invoices.close(self.store, name, 1, 'approver')

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(MeterError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

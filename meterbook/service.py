import inspect
from meterbook.store import Store
from meterbook.values import fields, reject, MeterError
from meterbook.contracts import registry, rates, simulation, calendar, budgets
from meterbook.events import receiver, reconcile, disputes, recovery
from meterbook.settlement import pricing, invoices, payments, credits, campaigns, costcenters, preflight
from meterbook.audit import checks, explanations, anomalies
from meterbook.ports import files, bundles, receipts, transfer, mapping
from meterbook.audit import closing, verification
from meterbook.settlement import reviews


class Meterbook:
    def __init__(self, path=':memory:', clock=None):
        self.store = Store(path, **({'clock': clock} if clock else {}))
        self.calendar = calendar.Calendar(self.store)
        self.rates = simulation.RateDesk(self.store)
        self.credits = credits.Credits(self.store)
        self.campaigns = campaigns.Campaigns(self.store)
        self.costcenters = costcenters.CostCenters(self.store)
        self.budgets = budgets.Budgets(self.store)
        self.receipts = receipts.Receipts(self.store)
        self.transfers = transfer.Transfers(self.store)
        self.imports = mapping.Imports(self.store)
        self.statements = closing.Statements(self.store)
        self.reviews = reviews.Reviews(self.store)
        self.routes = {}
        modules = {
            'contract': registry, 'rate': rates, 'event': receiver,
            'reconcile': reconcile, 'dispute': disputes, 'recovery': recovery,
            'price': pricing, 'invoice': invoices, 'payment': payments,
            'check': checks, 'explain': explanations, 'anomaly': anomalies,
            'verification': verification, 'closing': closing, 'file': files, 'bundle': bundles, 'preflight': preflight,
        }
        for prefix, module in modules.items():
            for name, function in inspect.getmembers(module, inspect.isfunction):
                if function.__module__ != module.__name__ or name.startswith('_'):
                    continue
                parameters = list(inspect.signature(function).parameters)
                if parameters and parameters[0] == 'store':
                    self.routes[prefix + '.' + name] = (function, True)
        for prefix, service in {
            'calendar': self.calendar, 'rate_desk': self.rates,
            'credit': self.credits, 'campaign': self.campaigns,
            'cost': self.costcenters, 'budget': self.budgets,
            'receipt': self.receipts, 'transfer': self.transfers,
            'import': self.imports, 'statement': self.statements, 'review': self.reviews,
        }.items():
            for name, method in inspect.getmembers(service, inspect.ismethod):
                if not name.startswith('_'):
                    self.routes[prefix + '.' + name] = (method, False)

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.close()

    def capabilities(self):
        result = []
        for operation, (function, needs_store) in sorted(self.routes.items()):
            parameters = list(inspect.signature(function).parameters.values())
            if needs_store:
                parameters = parameters[1:]
            result.append({'operation': operation, 'required': [p.name for p in parameters if p.default is inspect.Parameter.empty], 'optional': [p.name for p in parameters if p.default is not inspect.Parameter.empty]})
        return result

    def execute(self, request):
        fields(request, {'operation'}, {'arguments'})
        operation = request['operation']
        if not isinstance(operation, str) or operation not in self.routes:
            reject('operation', 'Unknown operation', operation=operation)
        arguments = request.get('arguments', {})
        if not isinstance(arguments, dict):
            reject('arguments', 'Operation arguments must be an object')
        function, needs_store = self.routes[operation]
        positional = [self.store] if needs_store else []
        try:
            inspect.signature(function).bind(*positional, **arguments)
        except TypeError as exc:
            raise MeterError('arguments', str(exc)) from exc
        return function(*positional, **arguments)

    def batch(self, requests, atomic=False):
        if not isinstance(requests, list) or not 1 <= len(requests) <= 10000:
            reject('batch', 'Batch needs one to ten thousand operations')
        if atomic:
            with self.store.atomic():
                return [self.execute(request) for request in requests]
        results = []
        for index, request in enumerate(requests):
            try:
                results.append({'index': index, 'ok': True, 'result': self.execute(request)})
            except MeterError as exc:
                results.append({'index': index, 'ok': False, 'error': exc.payload()})
        return results

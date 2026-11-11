import datetime
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN


class MeterError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self):
        return {'code': self.code, 'message': str(self), 'details': self.details}


def reject(code, message, **details):
    raise MeterError(code, message, **details)


def key(value, field='key'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,119}', value):
        reject('key', 'Invalid symbolic key', field=field)
    return value


def number(value, field, minimum=0, maximum=1000000000):
    if type(value) is not int or not minimum <= value <= maximum:
        reject('integer', 'Expected bounded integer', field=field)
    return value


def prose(value, field, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        reject('text', 'Expected nonempty bounded text', field=field)
    return value.strip()


def fields(value, required, optional=()):
    if not isinstance(value, dict):
        reject('object', 'Expected JSON object')
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing or extra:
        reject('fields', 'Object fields differ from schema', missing=sorted(missing), extra=sorted(extra))


def decimal(value, field='quantity', minimum=Decimal(0), maximum=Decimal('1e18'), scale=6):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        reject('decimal', 'Decimals must be exact strings or integers', field=field)
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise MeterError('decimal', 'Invalid decimal', field=field) from exc
    if not parsed.is_finite() or parsed < minimum or parsed > maximum or parsed.as_tuple().exponent < -scale:
        reject('decimal_range', 'Decimal outside range or scale', field=field)
    return parsed


def encoded(value):
    if not isinstance(value, Decimal):
        value = Decimal(value)
    if not value:
        return '0'
    raw = format(value, 'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def cents(value):
    with localcontext() as context:
        context.prec = 80
        return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise MeterError('json', 'Value is not canonical JSON') from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def loads(raw):
    def pairs(values):
        output = {}
        for name, value in values:
            if name in output:
                reject('duplicate_key', 'JSON key repeats')
            output[name] = value
        return output
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: reject('nonfinite', 'Nonfinite JSON values are forbidden'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MeterError('json_decode', 'Cannot decode JSON') from exc


def instant(value):
    if not isinstance(value, str):
        reject('time', 'Timestamp must be a string')
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise MeterError('time', 'Timestamp is malformed') from exc
    if parsed.tzinfo is None:
        reject('time', 'Timestamp must contain timezone')
    return parsed.astimezone(datetime.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def month(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
        reject('period', 'Period must use YYYY-MM')
    year, month_number = map(int, value.split('-'))
    if not 2000 <= year <= 2200 or not 1 <= month_number <= 12:
        reject('period', 'Unsupported settlement period')
    return value


def period_of(timestamp):
    return instant(timestamp)[:7]


def month_bounds(value):
    month(value)
    year, current = map(int, value.split('-'))
    next_year, next_month = (year + 1, 1) if current == 12 else (year, current + 1)
    return instant(f'{year:04}-{current:02}-01T00:00:00Z'), instant(f'{next_year:04}-{next_month:02}-01T00:00:00Z')


def exact(function):
    from functools import wraps
    @wraps(function)
    def invoke(*args, **kwargs):
        with localcontext() as context:
            context.prec = 80
            return function(*args, **kwargs)
    return invoke

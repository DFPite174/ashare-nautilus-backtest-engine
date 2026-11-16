"""Exact differential checks for native BacktestResult business fields."""
from decimal import Decimal
import json


_CONFIG = ('execution_model', 'execution_policy', 'initial_cash', 'participation_rate',
           'slippage_bps', 'fee_schedules', 'symbol_fee_policy')
_FILL = ('symbol', 'side', 'quantity', 'price', 'fee', 'time')
_REJECTION = ('symbol', 'reason', 'time')
_NAV = ('date', 'cash', 'positions_value', 'total_equity', 'receivable_dividends',
        'holdings', 'tradable_holdings')


def _serialized(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _serialized(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialized(child) for child in value]
    return value


def _require(mapping, fields, path):
    if not isinstance(mapping, dict):
        raise ValueError(f'{path} must be a dictionary')
    for field in fields:
        if field not in mapping:
            raise ValueError(f'Missing required field: {path}.{field}')


def _result(value):
    data = _serialized(value.to_dict() if hasattr(value, 'to_dict') else value)
    _require(data, ('manifest', 'execution_model', 'native_cash', 'ledger_cash',
                    'positions', 'fills', 'rejections', 'daily_nav'), 'result')
    _require(data['manifest'], ('input_sha256', 'config'), 'manifest')
    digest = data['manifest']['input_sha256']
    if not isinstance(digest, str) or not digest.strip():
        raise ValueError('manifest.input_sha256 must be nonempty')
    _require(data['manifest']['config'], _CONFIG, 'manifest.config')
    _require(data['positions'], (), 'positions')
    for name, fields in (('fills', _FILL), ('rejections', _REJECTION), ('daily_nav', _NAV)):
        if not isinstance(data[name], list):
            raise ValueError(f'{name} must be a list')
        for index, row in enumerate(data[name]):
            _require(row, fields, f'{name}[{index}]')
            if name == 'daily_nav':
                _require(row['holdings'], (), f'{name}[{index}].holdings')
                _require(row['tradable_holdings'], (), f'{name}[{index}].tradable_holdings')
    return data


def _rows(reference, candidate):
    first = None
    for index in range(max(len(reference), len(candidate))):
        left = reference[index] if index < len(reference) else None
        right = candidate[index] if index < len(candidate) else None
        if left != right:
            first = dict(index=index, reference=left, candidate=right)
            break
    return dict(reference_count=len(reference), candidate_count=len(candidate),
                count_difference=len(candidate)-len(reference), first_difference=first)


def compare_backtests(reference, candidate, *, allow_model_difference=False):
    """Compare native serialized results; equality does not prove correctness.

    Decimal values follow BacktestResult.to_dict (strings). Numeric strings are
    otherwise exact, so callers must use the same serialization convention.
    """
    left, right = _result(reference), _result(candidate)
    if left['manifest']['input_sha256'] != right['manifest']['input_sha256']:
        raise ValueError('manifest.input_sha256 must match')
    assumptions = {}
    optional = tuple(key for key in ('native_order_submit_rate',)
                     if key in left['manifest']['config'] or key in right['manifest']['config'])
    for path, a, b in [('execution_model', left['execution_model'], right['execution_model'])] + [
        (f'manifest.config.{key}', left['manifest']['config'][key], right['manifest']['config'][key])
        for key in _CONFIG
    ] + [(f'manifest.config.{key}', left['manifest']['config'].get(key), right['manifest']['config'].get(key))
         for key in optional]:
        if a != b:
            assumptions[path] = dict(reference=a, candidate=b)
    if assumptions and not allow_model_difference:
        raise ValueError(f'Execution/economic assumptions differ: {", ".join(assumptions)}')
    report = dict(comparable_model=not assumptions, assumption_differences=assumptions,
                  schema_difference=None)
    if left.get('schema_version') != right.get('schema_version'):
        report['schema_difference'] = dict(reference=left.get('schema_version'), candidate=right.get('schema_version'))
    for name, fields in (('fills', _FILL), ('rejections', _REJECTION)):
        # Native order IDs vary between runs; intent IDs carry business identity.
        def project(rows):
            return [{key: row[key] for key in (*fields, 'intent_id') if key in row} for row in rows]
        report[name] = _rows(project(left[name]), project(right[name]))
    report['daily_nav'] = _rows(left['daily_nav'], right['daily_nav'])
    report['final_cash_differences'] = {
        key: dict(reference=left[key], candidate=right[key])
        for key in ('native_cash', 'ledger_cash') if left[key] != right[key]}
    report['final_position_differences'] = {
        key: dict(reference=left['positions'].get(key), candidate=right['positions'].get(key))
        for key in sorted(left['positions'].keys() | right['positions'].keys())
        if key not in left['positions'] or key not in right['positions'] or left['positions'][key] != right['positions'][key]}
    report['equal_within_compared_fields'] = not (
        any(report[name]['first_difference'] is not None for name in ('fills', 'rejections', 'daily_nav'))
        or report['final_cash_differences'] or report['final_position_differences'])
    return report


def compare_complete_backtests(reference, candidate, *, allow_model_difference=False):
    """Add orders, pending intents and account events to the existing comparison.

    Orders/events compare as multisets, pending intents retain queue order.
    Native order IDs are ignored; quantities, status and intent IDs are retained.
    ``equal`` describes compared values; consult ``business.comparable_model``
    when explicitly allowing different models. This is not a correctness oracle.
    """
    left, right = _result(reference), _result(candidate)
    business = compare_backtests(left, right, allow_model_difference=allow_model_difference)
    report = dict(business=business)
    for name in ('orders', 'pending_intents', 'account_journal'):
        rows = []
        for value in (left, right):
            _require(value, (name,), 'result')
            if not isinstance(value[name], list) or any(not isinstance(r, dict) for r in value[name]):
                raise ValueError(f'{name} must be a list of dictionaries')
            selected = [{k: v for k, v in row.items() if name != 'orders' or k != 'id'}
                        for row in value[name]]
            if name != 'pending_intents':
                selected.sort(key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
            rows.append(selected)
        report[name] = _rows(*rows)
    report.update(orders_equal=report['orders']['first_difference'] is None,
                  pending_equal=report['pending_intents']['first_difference'] is None,
                  journal_equal=report['account_journal']['first_difference'] is None)
    report['equal'] = (business['equal_within_compared_fields'] and report['orders_equal']
                       and report['pending_equal'] and report['journal_equal'])
    return report

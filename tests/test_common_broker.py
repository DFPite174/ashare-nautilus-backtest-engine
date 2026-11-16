from copy import deepcopy

from examples.compare_common_broker import compare_accounts


def test_common_broker_comparison_allows_only_float_noise_and_reports_first_difference():
    original = dict(fills=[dict(date='2025-01-03', symbol='A', side='BUY',
        quantity=100, price='10.00', fee='5.01')],
        daily=[dict(date='2025-01-03', cash='8994.99', equity='9994.99', positions={'A': 100})])
    changed = deepcopy(original)
    changed['daily'][0]['cash'] = '8994.98999999999'
    assert compare_accounts(original, changed)['equal']
    changed['fills'][0]['fee'] = '5.01023'
    report = compare_accounts(original, changed)
    assert not report['equal'] and report['first_difference']['path'] == 'fills[0].fee'
    changed = deepcopy(original)
    changed['daily'][0]['positions'] = {}
    assert not compare_accounts(original, changed)['equal']

from copy import deepcopy
from decimal import Decimal

import pytest

from engine.comparison import compare_backtests


def result():
    return dict(schema_version=2, execution_model='bar-model',
        manifest=dict(input_sha256='same-input', config=dict(
            execution_model='bar', execution_policy='next-open', initial_cash='1000',
            participation_rate='0.01', slippage_bps='0', fee_schedules={}, symbol_fee_policy={})),
        native_cash='989', ledger_cash='989', positions={'000001.SZ': '1'},
        fills=[dict(symbol='000001.SZ', side='BUY', quantity='1', price='10', fee='1', time=10, order_id='native-1', intent_id='001')],
        rejections=[dict(symbol='000002.SZ', reason='001', time=11, intent_id='002')],
        daily_nav=[dict(date='2025-01-02', cash='989', positions_value='10', total_equity='999',
                        receivable_dividends='0', holdings={'000001.SZ': '1'}, tradable_holdings={})])


def test_equivalent_native_ids_and_decimal_serialization():
    left = result(); right = deepcopy(left)
    right['fills'][0]['order_id'] = 'other-native-id'
    right['fills'][0]['fee'] = Decimal('1')
    report = compare_backtests(left, right)
    assert report['comparable_model'] is True
    assert report['equal_within_compared_fields'] is True


def test_input_and_model_contract():
    left = result(); right = deepcopy(left)
    right['manifest']['input_sha256'] = 'different'
    with pytest.raises(ValueError, match='input_sha256'):
        compare_backtests(left, right, allow_model_difference=True)
    right = deepcopy(left); right['manifest']['config']['slippage_bps'] = '2'
    with pytest.raises(ValueError, match='assumptions'):
        compare_backtests(left, right)
    report = compare_backtests(left, right, allow_model_difference=True)
    assert not report['comparable_model']
    assert 'manifest.config.slippage_bps' in report['assumption_differences']


def test_first_daily_divergence_and_final_balances():
    left = result(); right = deepcopy(left)
    right['daily_nav'][0]['cash'] = '988'
    right['daily_nav'][0]['holdings']['000001.SZ'] = '2'
    right['ledger_cash'] = '988'; right['positions']['000001.SZ'] = '2'
    report = compare_backtests(left, right)
    diff = report['daily_nav']['first_difference']
    assert diff['index'] == 0
    assert diff['reference']['cash'] == '989'
    assert diff['candidate']['holdings']['000001.SZ'] == '2'
    assert report['final_cash_differences']['ledger_cash']['candidate'] == '988'
    assert report['final_position_differences']['000001.SZ']['candidate'] == '2'
    assert not report['equal_within_compared_fields']


def test_counts_schema_and_preserved_identifiers():
    left = result(); right = deepcopy(left)
    right['schema_version'] = 3
    right['rejections'][0]['reason'] = '1'
    right['fills'][0]['intent_id'] = '1'
    right['fills'].append(deepcopy(right['fills'][0]))
    report = compare_backtests(left, right)
    assert report['schema_difference'] == {'reference': 2, 'candidate': 3}
    assert report['fills']['count_difference'] == 1
    assert report['fills']['first_difference']['index'] == 0
    assert report['rejections']['first_difference']['index'] == 0
    del right['daily_nav'][0]['cash']
    with pytest.raises(ValueError, match='cash'):
        compare_backtests(left, right)


def test_result_object_to_dict():
    class Result:
        def to_dict(self):
            return result()
    assert compare_backtests(Result(), result())['equal_within_compared_fields']

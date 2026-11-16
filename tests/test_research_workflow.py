from decimal import Decimal as D
from dataclasses import replace
from copy import deepcopy
import pytest

from examples.research_workflow import target_weights, run_candidate
from engine.tushare_lake import LakeInputs
from contracts import Instrument, Venue
from test_regular_backtest import fixture


def test_target_uses_only_closed_history_and_equal_allocation():
    assert target_weights({'A': [D(10)] * 19}, True) == {}
    assert target_weights({'A': [D(9)] * 19 + [D(10)],
                           'B': [D(11)] * 19 + [D(10)]}, True) == {'A': 1.0}
    assert target_weights({'A': [], 'B': []}, False) == {'A': .5, 'B': .5}


def test_next_rebalance_uses_actual_partial_position_and_cash():
    bars, rules = fixture(days=8, symbol='600000.SH')
    bars[1] = replace(bars[1], volume=50)
    data = LakeInputs([Instrument(symbol='600000.SH', exchange=Venue.SSE)],
                      bars, rules, [b.end.date() for b in bars], [], {'snapshot_id': 'fixture'})
    result, decisions = run_candidate(data, False, D(10000), 1, 0)
    assert decisions[0]['holdings'] == {}
    # 10000 - 50 * 10.50 - 5.01; unfilled requested shares consume no cash.
    assert D(decisions[1]['cash']) == D('9469.99')
    assert D(decisions[1]['holdings']['600000.SH']) == 50
    assert D(result.fills[0]['quantity']) == 50
    assert result.fills[0]['time'] > int(bars[0].end.timestamp() * 1e9)
    assert all(row['cash_matches'] and row['positions_match'] for row in result.daily_nav)


def test_independent_cash_fee_dividend_and_next_session_oracle():
    from contracts import CorporateAction
    from engine import run_backtest, OrderIntent
    bars, rules = fixture(days=4, symbol='600000.SH')
    bars = [replace(b, open=D(10), high=D(10), low=D(10), close=D(10)) for b in bars]
    days = [b.end.date() for b in bars]
    def signal(bar, state):
        if bar == bars[0]: return [OrderIntent(bar.symbol, 'BUY', 100)]
        if bar == bars[1]:
            assert state.sellable[bar.symbol] == 0
            return [OrderIntent(bar.symbol, 'SELL', 100)]
        return []
    action = CorporateAction(symbol='600000.SH', record_date=days[1], ex_date=days[2],
                             pay_date=days[3], cash_div=D(1))
    result = run_backtest([Instrument(symbol='600000.SH', exchange=Venue.SSE)],
        bars, rules, signal, initial_cash=D(2000), participation_rate=1,
        corporate_actions=[action], trading_days=days)
    # Written independently: 2000 - 1000 - 5.01 + 1000 - 5.51 + 100 = 2089.48.
    assert [D(f['fee']) for f in result.fills] == [D('5.01'), D('5.51')]
    assert [f['side'] for f in result.fills] == ['BUY', 'SELL']
    assert result.native_cash == result.ledger_cash == D('2089.48')
    assert sum(D(r['amount']) for r in result.account_journal) == 100
    assert result.positions['600000.SH'] == 0


def test_outcomes_link_partial_rejected_and_end_of_data_without_inventing_reasons():
    from examples.research_workflow import execution_outcomes
    decisions = [dict(date='2025-01-02', decision_time='2025-01-02T15:00:00+08:00',
        execution_date='2025-01-03', intents=[dict(intent_id=s, symbol=s, side='BUY', quantity=100)
                                           for s in ('partial', 'rejected', 'pending')])]
    result = dict(fills=[dict(intent_id='partial', symbol='partial', side='BUY', quantity='50', fee='5')],
        rejections=[dict(intent_id='rejected', reason='INSUFFICIENT_CASH_WITH_FEES')],
        orders=[dict(intent_id='partial', status='CANCELED')],
        pending_intents=[dict(intent_id='pending', reason='END_OF_DATA')])
    rows = execution_outcomes(decisions, result)
    assert [r['outcome'] for r in rows] == ['PARTIALLY_FILLED', 'REJECTED', 'PENDING']
    assert rows[0]['unfilled_quantity'] == '50'
    assert rows[0]['rejection_reasons'] == []
    assert rows[1]['rejection_reasons'] == ['INSUFFICIENT_CASH_WITH_FEES']
    assert rows[2]['pending_reasons'] == ['END_OF_DATA']
    for change in ('unknown', 'overfill'):
        broken = deepcopy(result)
        broken['fills'][0]['intent_id' if change == 'unknown' else 'quantity'] = 'unknown' if change == 'unknown' else '101'
        with pytest.raises(ValueError):
            execution_outcomes(decisions, broken)

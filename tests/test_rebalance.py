from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest

from contracts.models import TargetWeights
from engine.market_rules import DailyMarketRule
from engine.rebalance import plan_rebalance


def plan(weights, **overrides):
    values = dict(cash=D('2005.02'), holdings={}, sellable={},
                  prices={s: D('10') for s in weights},
                  rules={s: DailyMarketRule('OPEN', 'UNBOUNDED') for s in weights},
                  valuation_at=datetime(2026, 11, 11, 7, tzinfo=timezone.utc),
                  decision_at=datetime(2026, 11, 11, 8, tzinfo=timezone.utc),
                  cash_reserve=D('0'))
    values.update(overrides)
    return plan_rebalance(TargetWeights(signal_date=date(2026, 11, 11),
                          execution_date=date(2026, 11, 12), weights=weights), **values)


def test_fee_inclusive_tight_cash_and_reserve():
    p = plan({'A': 1})
    assert [(i.side, i.quantity) for i in p.intents] == [('BUY', 200)]
    assert p.estimated_fees == D('5.02')
    assert p.estimated_cash == 0
    p = plan({'A': 1}, cash=D('2005.01'), cash_reserve=D('1'))
    assert p.intents[0].quantity == 100
    assert p.estimated_cash == D('1000.00')
    assert 'CASH_SHORTAGE' in p.allocations[0].reasons


def test_t1_lock_and_odd_lot_liquidation():
    p = plan({}, cash=D('0'), holdings={'A': 150}, sellable={'A': 50},
             prices={'A': D('10')}, rules={'A': DailyMarketRule('OPEN', 'UNBOUNDED')})
    assert p.intents == ()
    assert p.allocations[0].planned_quantity == 150
    assert set(p.allocations[0].reasons) == {'UNSELLABLE', 'UNITS'}
    p = plan({}, holdings={'A': 150}, sellable={'A': 150}, prices={'A': D('10')},
             rules={'A': DailyMarketRule('OPEN', 'UNBOUNDED')})
    assert p.intents[0].quantity == 150
    assert p.intents[0].side == 'SELL'


def test_deterministic_sell_before_buy_and_funding():
    p = plan({'B': .5, 'A': .5}, cash=D('0'), holdings={'Z': 1000}, sellable={'Z': 1000},
             prices={s: D('10') for s in 'ZBA'},
             rules={s: DailyMarketRule('OPEN', 'UNBOUNDED') for s in 'ZBA'})
    assert [(i.symbol, i.side, i.quantity) for i in p.intents] == [
        ('Z', 'SELL', 1000), ('A', 'BUY', 500), ('B', 'BUY', 400)]
    assert p.estimated_cash == D('979.81')
    assert p == plan({'A': .5, 'B': .5}, cash=D('0'), holdings={'Z': 1000}, sellable={'Z': 1000},
                    prices={s: D('10') for s in 'ABZ'},
                    rules={s: DailyMarketRule('OPEN', 'UNBOUNDED') for s in 'ABZ'})


def test_inactive_is_visible():
    p = plan({'A': 1}, rules={'A': DailyMarketRule('SUSPENDED', 'UNAVAILABLE')})
    assert not p.intents
    assert p.allocations[0].reasons == ('INACTIVE',)


@pytest.mark.parametrize('updates', [
    {'prices': {}}, {'prices': {'A': D('NaN')}}, {'cash': D('Infinity')},
    {'holdings': {'A': 1.5}}, {'holdings': {'A': 100}, 'sellable': {'A': 101}},
    {'rules': {}}, {'rules': {'A': DailyMarketRule('OPEN', 'UNBOUNDED', buy_step=0)}},
    {'valuation_at': datetime(2026, 11, 12, tzinfo=timezone.utc)},
    {'decision_at': datetime(2026, 11, 11)}, {'cash_reserve': D('-1')},
])
def test_invalid_inputs_fail_closed(updates):
    with pytest.raises(ValueError):
        plan({'A': 1}, **updates)


def test_missing_held_price_cannot_understate_equity():
    with pytest.raises(ValueError, match='price.*Z'):
        plan({'A': 1}, holdings={'Z': 100})


def test_execution_date_and_existing_intent_validator():
    from contracts.models import Instrument, Venue
    from engine.intents import validate_intents
    p = plan({'A': 1})
    earliest = p.intents[0].earliest_execution
    assert earliest.isoformat() == '2026-11-12T09:30:00+08:00'
    assert validate_intents(p.intents, {'A': Instrument(symbol='A', exchange=Venue.SSE)},
                            p.decision_at, set()) == list(p.intents)
    late = datetime(2026, 11, 12, 7, tzinfo=timezone.utc)
    assert plan({'A': 1}, decision_at=late).intents[0].earliest_execution == late


def test_configured_fee_model_is_used():
    from engine.fee_model import AShareFeeModel
    p = plan({'A': 1}, fee_model=AShareFeeModel(min_commission=10))
    assert p.intents[0].quantity == 100
    assert p.estimated_fees == D('10.01')


@pytest.mark.parametrize('lo,hi', [(D('NaN'), 11), (9, D('Infinity')), (11, 9), (0, 11)])
def test_invalid_bounded_rules(lo, hi):
    with pytest.raises(ValueError):
        plan({'A': 1}, rules={'A': DailyMarketRule('OPEN', 'BOUNDED', hi, lo)})


def test_invalid_target_is_clean_valueerror():
    with pytest.raises(ValueError):
        plan_rebalance(None, cash=0, holdings={}, sellable={}, prices={}, rules={},
                       valuation_at=datetime.now(timezone.utc),
                       decision_at=datetime.now(timezone.utc), cash_reserve=0)


def test_receivables_value_equity_without_funding_buys():
    p = plan({'A': 1}, cash=D('1005.01'), receivables=D('1000'))
    assert p.initial_equity == D('2005.01')
    assert p.receivables == D('1000')
    assert p.allocations[0].target_quantity == 200
    assert p.allocations[0].planned_quantity == 100
    assert p.allocations[0].estimated_weight == D('.5')
    assert 'CASH_SHORTAGE' in p.allocations[0].reasons
    assert p.estimated_fees == D('5.01')
    assert p.estimated_cash == 0


def test_zero_receivables_preserves_existing_position_plan():
    inputs = dict(cash=D('1005.01'), holdings={'A': 100}, sellable={'A': 100})
    assert plan({'A': 1}, **inputs) == plan({'A': 1}, receivables=D('0'), **inputs)


@pytest.mark.parametrize('value', [D('-1'), D('NaN'), D('Infinity')])
def test_invalid_receivables(value):
    with pytest.raises(ValueError, match='receivables'):
        plan({'A': 1}, receivables=value)

from dataclasses import FrozenInstanceError
from datetime import date, timedelta, tzinfo
from decimal import Decimal

import pytest

from contracts import Instrument, Venue, CorporateAction
from engine.backtest import BacktestBar, at
from engine.prepared import PreparedBacktest, prepare_backtest
from engine.reproducibility import digest


def market():
    day = date(2025, 5, 6)
    instruments = [Instrument(symbol='600000.SH', exchange=Venue.SSE)]
    bars = [BacktestBar('600000.SH', at(day, 9, 30), at(day, 15),
                        *(Decimal(10) for _ in range(4)), 10000)]
    rules = {('600000.SH', day): dict(trade_status='OPEN', limit_state='UNBOUNDED',
        buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000)}
    return instruments, bars, rules


def test_prepared_detaches_sources_and_freezes_every_container():
    instruments, bars, rules = market()
    rules[next(iter(rules))]['metadata'] = {'tags': ['source']}
    action = CorporateAction(symbol='600000.SH', ex_date=date(2025, 5, 7))
    actions = [action]
    prepared = prepare_backtest(instruments, bars, rules, corporate_actions=actions)
    expected = digest(dict(instruments=instruments, bars=bars, rules=rules,
                           actions=actions, calendar=prepared.sessions))
    assert prepared.input_sha256 == expected
    assert prepared.instruments[0] is not instruments[0]
    assert prepared.actions[0] is not action
    instruments.clear(); bars.clear(); actions.clear()
    rules[next(iter(rules))]['metadata']['tags'].append('changed')
    assert prepared.rules[next(iter(prepared.rules))]['metadata']['tags'] == ('source',)
    assert len(prepared.bars) == 1 and len(prepared.instruments) == 1
    assert prepared.input_sha256 == expected
    with pytest.raises(TypeError):
        prepared.rules[next(iter(prepared.rules))]['trade_status'] = 'SUSPENDED'
    with pytest.raises(TypeError):
        prepared.master['other'] = prepared.instruments[0]
    with pytest.raises(FrozenInstanceError):
        prepared.bars = ()
    prepared.assert_compatible()


def test_factory_only_and_unsupported_mutable_values():
    with pytest.raises(TypeError, match='prepare_backtest'):
        PreparedBacktest()
    instruments, bars, rules = market()
    rules[next(iter(rules))]['custom'] = set()
    with pytest.raises(TypeError, match='Unsupported'):
        prepare_backtest(instruments, bars, rules)


def test_invalid_calendar_and_missing_open_bar_rejected():
    instruments, bars, rules = market()
    with pytest.raises(ValueError, match='calendar'):
        prepare_backtest(instruments, bars, rules, trading_days=[date(2025, 5, 7)])
    rules[('600000.SH', date(2025, 5, 7))] = dict(next(iter(rules.values())))
    with pytest.raises(ValueError, match='Missing market bar'):
        prepare_backtest(instruments, bars, rules, trading_days=[date(2025, 5, 6), date(2025, 5, 7)])


def test_validator_replacement_requires_repreparation(monkeypatch):
    prepared = prepare_backtest(*market())
    def different_validator(*args):
        raise AssertionError('must not execute')
    monkeypatch.setattr('engine.backtest._validate', different_validator)
    with pytest.raises(ValueError, match='validator changed'):
        prepared.assert_compatible()


def test_mutable_timezone_rejected_before_it_can_invalidate_cached_inputs():
    class MutableZone(tzinfo):
        hours = 8
        def utcoffset(self, dt):
            return timedelta(hours=self.hours)
        def dst(self, dt):
            return timedelta(0)
    instruments, bars, rules = market()
    bar = bars[0]
    zone = MutableZone()
    bars[0] = BacktestBar(bar.symbol, bar.start.replace(tzinfo=zone), bar.end.replace(tzinfo=zone),
                          bar.open, bar.high, bar.low, bar.close, bar.volume)
    with pytest.raises(TypeError, match='immutable timezone'):
        prepare_backtest(instruments, bars, rules)


def test_prepared_runs_are_isolated_and_match_raw_inputs(monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from engine import OrderIntent, run_backtest, run_prepared_backtest
    instruments,bars,rules=market()
    bars.append(replace(bars[0],start=bars[0].start+timedelta(days=1),end=bars[0].end+timedelta(days=1)))
    rules[(bars[0].symbol,bars[1].end.date())]=dict(next(iter(rules.values())))
    def signal(b,state):return [OrderIntent(b.symbol,'BUY',100)] if b==bars[0] else []
    raw=run_backtest(instruments,bars,rules,signal)
    prepared=prepare_backtest(instruments,bars,rules)
    from engine.reproducibility import digest as actual_digest
    def no_input_rehash(value):
        if isinstance(value,dict) and set(value)=={'instruments','bars','rules','actions','calendar'}:
            raise AssertionError('prepared runs must reuse validated input hash')
        return actual_digest(value)
    monkeypatch.setattr('engine.reproducibility.digest',no_input_rehash)
    first=run_prepared_backtest(prepared,signal)
    second=run_prepared_backtest(prepared,signal)
    empty=run_prepared_backtest(prepared,lambda b,s: [])
    assert first.fills==second.fills==raw.fills
    assert first.daily_nav==second.daily_nav==raw.daily_nav
    assert first.manifest['input_sha256']==raw.manifest['input_sha256']==prepared.input_sha256
    assert not empty.fills and empty.native_cash==Decimal(100000)

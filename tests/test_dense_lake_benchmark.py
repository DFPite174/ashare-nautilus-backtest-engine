from datetime import timedelta

import pytest
from contracts import Instrument, Venue
from engine import prepare_backtest
from examples.benchmark_dense_lake import build_entries, compare_complete
from test_research_hardening import market


def prepared_market():
    bars, rules = market(('600000.SH', '688001.SH'))
    instruments = [Instrument(symbol='600000.SH', exchange=Venue.SSE),
                   Instrument(symbol='688001.SH', exchange=Venue.SSE, min_order_qty=200)]
    return prepare_backtest(instruments, bars, rules)


def test_dense_decisions_respect_lots_unique_ids_and_last_session():
    prepared = prepared_market()
    entries = build_entries(prepared, 2)
    assert len(entries) == 4
    assert len({entry.intent.intent_id for entry in entries}) == len(entries)
    assert {e.intent.quantity for e in entries if e.intent.symbol == '688001.SH'} == {200}
    assert {e.intent.quantity for e in entries if e.intent.symbol == '600000.SH'} == {100}
    assert [e.intent.side for e in entries] == ['BUY', 'BUY', 'SELL', 'SELL']
    assert max(e.decision_time.date() for e in entries) < prepared.sessions[-1]
    assert all(e.decision_time.hour == 15 and e.decision_time.minute == 0 for e in entries)


def test_dense_builder_rejects_unknown_width_and_unbounded_load():
    prepared = prepared_market()
    for width in (0, True, 3):
        with pytest.raises(ValueError):
            build_entries(prepared, width)
    sessions = tuple(prepared.sessions[0] + timedelta(days=n) for n in range(10002))
    class TooLong:
        master = prepared.master
    long = TooLong()
    long.sessions = sessions
    with pytest.raises(ValueError, match='20000'):
        build_entries(long, 2)


def test_complete_comparison_checks_orders_pending_and_equity_events():
    from copy import deepcopy
    from engine import prepare_order_tape, run_order_tape
    prepared = prepared_market()
    result = run_order_tape(prepared, prepare_order_tape(prepared, build_entries(prepared, 2)),
                            participation_rate=1)
    reference = result.to_dict()
    changed_ids = deepcopy(reference)
    for row in changed_ids['orders']:
        row['id'] = 'different-native-id'
    for row in changed_ids['fills']:
        row['order_id'] = 'different-native-id'
    assert compare_complete(reference, changed_ids)['equal']
    for name in ('orders', 'pending_intents', 'account_journal'):
        candidate = deepcopy(reference)
        candidate[name].append({'unexpected': True})
        assert not compare_complete(reference, candidate)['equal']


@pytest.mark.parametrize('mode', ['full', 'sparse', 'orders'])
def test_wide_open_burst_is_not_rejected_by_default_native_throttle(mode, monkeypatch):
    from engine import prepare_order_tape, run_order_tape, run_prepared_backtest
    import engine.backtest as module
    original = module.BacktestEngine
    configs = []
    def capture(*args, **kwargs):
        configs.append(kwargs['config'])
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'BacktestEngine', capture)
    symbols = tuple(f'600{n:03d}.SH' for n in range(128))
    bars, rules = market(symbols)
    prepared = prepare_backtest([Instrument(symbol=s, exchange=Venue.SSE) for s in symbols], bars, rules)
    entries = build_entries(prepared, 128)
    options = dict(initial_cash=100000000, participation_rate=1)
    if mode == 'orders':
        result = run_order_tape(prepared, prepare_order_tape(prepared, entries), **options)
    else:
        result = run_prepared_backtest(prepared, data_mode=mode,
            portfolio_signal=lambda closed, state: [e.intent for e in entries if e.decision_time == closed[0].end],
            **options)
    assert not result.rejections
    assert len(result.fills) == 256
    assert result.manifest['config']['native_order_submit_rate'] == '1000000/00:00:01'
    assert configs[0].risk_engine.bypass is False
    assert configs[0].risk_engine.max_order_submit_rate == result.manifest['config']['native_order_submit_rate']


def test_project_account_result_does_not_compute_unused_native_summary(monkeypatch):
    from nautilus_trader.analysis.analyzer import PortfolioAnalyzer
    from engine import prepare_order_tape, run_order_tape
    def unexpected(*args, **kwargs):
        raise AssertionError('Unused native performance summary was computed')
    monkeypatch.setattr(PortfolioAnalyzer, 'calculate_statistics', unexpected)
    prepared = prepared_market()
    result = run_order_tape(prepared, prepare_order_tape(prepared, build_entries(prepared, 2)), participation_rate=1)
    assert len(result.daily_nav) == 3 and len(result.fills) == 4
    assert result.native_cash == result.ledger_cash
    assert all(row['cash_matches'] and row['positions_match'] for row in result.daily_nav)

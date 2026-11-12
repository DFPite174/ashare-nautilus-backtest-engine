"""Public account regressions with hand-calculated costs and cash flows."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest
from contracts import Instrument, ConvertibleBondInstrument, Venue
from engine import BacktestBar, OrderIntent, run_backtest, AShareFeeModel

TZ = ZoneInfo('Asia/Shanghai')


def market(symbols=('600519.SH',), days=3, minute=False):
    bars, rules = [], {}
    for n in range(days):
        day = date(2025, 5, 6) + timedelta(days=n)
        for symbol in symbols:
            start = datetime.combine(day, datetime.min.time(), TZ).replace(hour=9, minute=30)
            end = start + timedelta(minutes=1) if minute else start.replace(hour=15, minute=0)
            bars.append(BacktestBar(symbol, start, end, D(10), D(11), D(9), D(10), 10000))
            rules[symbol, day] = dict(trade_status='OPEN', limit_state='BOUNDED',
                down_limit=9., up_limit=11., buy_min=100, buy_step=100,
                sell_min=100, sell_step=100, max_order=1000000)
    return bars, rules


def buy_first(bar, state):
    return [OrderIntent(bar.symbol, 'BUY', 100)] if bar.end.date() == date(2025, 5, 6) else []


def test_custom_fee_config_drives_native_oracle_and_cash_reservation():
    bars, rules = market()
    inst = [Instrument(symbol='600519.SH', exchange=Venue.SSE)]
    fee = AShareFeeModel(commission_rate=.001, min_commission=10)
    for _ in range(2):  # Reuse must reset the fee model's order accumulator.
        result = run_backtest(inst, bars, rules, buy_first, initial_cash=D('1010.01'),
            participation_rate=1, fee_model=fee)
        assert len(result.fills) == 1
        assert D(result.fills[0]['fee']) == D('10.01')
        assert result.native_cash == result.ledger_cash == D(0)
    result = run_backtest(inst, bars, rules, buy_first, initial_cash=D(1006),
        participation_rate=1, fee_model=AShareFeeModel(min_commission=10))
    assert not result.fills
    assert result.rejections[0]['reason'] == 'INSUFFICIENT_CASH_WITH_FEES'


def test_mixed_equity_cb_preserves_asset_specific_fees():
    bars, rules = market(('600519.SH', '110059.SH'))
    result = run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE),
        ConvertibleBondInstrument(symbol='110059.SH', exchange=Venue.SSE)],
        bars, rules, buy_first, participation_rate=1)
    fees = {f['symbol']: D(f['fee']) for f in result.fills}
    assert fees == {'600519.SH': D('5.01'), '110059.SH': D('1.01')}


def test_cb_can_sell_same_day_but_equity_stays_locked():
    bars, rules = market(('600519.SH', '110059.SH'), days=1, minute=True)
    for b in list(bars):
        bars.extend(replace(b, start=b.start+timedelta(minutes=n), end=b.end+timedelta(minutes=n)) for n in (1, 2))
    def signal(b, state):
        return [OrderIntent(b.symbol, 'BUY' if b.end.minute == 31 else 'SELL', 100)]
    result = run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE),
        ConvertibleBondInstrument(symbol='110059.SH', exchange=Venue.SSE)],
        bars, rules, signal, participation_rate=1)
    assert [(f['symbol'], f['side']) for f in result.fills].count(('110059.SH', 'SELL')) == 1
    assert result.positions['600519.SH'] == 100
    assert result.positions['110059.SH'] == 0
    assert any('T1_' in r['reason'] for r in result.rejections)


def test_same_open_sales_fund_buys_even_if_buyer_symbol_sorts_first():
    bars, rules = market(('000001.SZ', '600519.SH'))
    def signal(b, state):
        if b.symbol != '600519.SH': return []
        if b.end.date() == date(2025, 5, 6): return [OrderIntent(b.symbol, 'BUY', 100)]
        if b.end.date() == date(2025, 5, 7):
            return [OrderIntent('000001.SZ', 'BUY', 100), OrderIntent(b.symbol, 'SELL', 100)]
        return []
    result = run_backtest([Instrument(symbol='000001.SZ', exchange=Venue.SZSE),
        Instrument(symbol='600519.SH', exchange=Venue.SSE)], bars, rules, signal,
        initial_cash=D(1020), participation_rate=1)
    assert [(f['symbol'], f['side']) for f in result.fills] == [
        ('600519.SH', 'BUY'), ('600519.SH', 'SELL'), ('000001.SZ', 'BUY')]
    assert result.native_cash == D('4.47')


def test_saved_history_is_immutable_and_does_not_grow_with_future_bars():
    bars, rules = market()
    saved = []
    def signal(b, state):
        if not saved: saved.append(state.history[b.symbol])
        else:
            assert len(saved[0]) == 1
            assert list(saved[0]) == [bars[0]]
        with pytest.raises(TypeError): state.history[b.symbol][0] = b
        return []
    run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE)], bars, rules, signal)


def test_portfolio_callback_sees_complete_same_timestamp_snapshot():
    bars, rules = market(('000001.SZ', '600519.SH'))
    seen = []
    def portfolio_signal(closed, state):
        assert len(closed) == 2
        assert {b.end for b in closed} == {closed[0].end}
        assert all(state.history[b.symbol][-1] == b for b in closed)
        seen.append(closed[0].end)
        return [OrderIntent(b.symbol, 'BUY', 100) for b in closed] if len(seen) == 1 else []
    result = run_backtest([Instrument(symbol='000001.SZ', exchange=Venue.SZSE),
        Instrument(symbol='600519.SH', exchange=Venue.SSE)], bars, rules,
        portfolio_signal=portfolio_signal, participation_rate=1)
    assert len(seen) == 3
    assert len(result.fills) == 2


def test_adverse_slippage_reduces_cash_and_respects_explicit_limit():
    bars, rules = market()
    inst = [Instrument(symbol='600519.SH', exchange=Venue.SSE)]
    result = run_backtest(inst, bars, rules, buy_first, participation_rate=1, slippage_bps=10)
    assert D(result.fills[0]['price']) == D('10.01')
    assert result.native_cash == D('98993.99')
    def limit_signal(b, state):
        return [OrderIntent(b.symbol, 'BUY', 100, D(10))] if b.end.date() == date(2025,5,6) else []
    result = run_backtest(inst, bars, rules, limit_signal, participation_rate=1, slippage_bps=10)
    assert D(result.fills[0]['price']) <= D(10)


def test_manifest_replays_content_hash_and_changes_with_data_or_costs():
    bars, rules = market()
    inst = [Instrument(symbol='600519.SH', exchange=Venue.SSE)]
    a = run_backtest(inst, bars, rules, buy_first, strategy_id='buy-first', strategy_params={'quantity':100})
    b = run_backtest(inst, list(reversed(bars)), rules, buy_first, strategy_id='buy-first', strategy_params={'quantity':100})
    c = run_backtest(inst, bars, rules, buy_first, strategy_id='buy-first', slippage_bps=10)
    changed = [replace(bars[0], volume=9999)] + bars[1:]
    d = run_backtest(inst, changed, rules, buy_first, strategy_id='buy-first', strategy_params={'quantity':100})
    assert a.manifest['input_sha256'] == b.manifest['input_sha256']
    assert a.manifest['run_sha256'] != c.manifest['run_sha256']
    assert a.manifest['input_sha256'] != d.manifest['input_sha256']
    assert a.to_dict()['manifest']['strategy_id'] == 'buy-first'

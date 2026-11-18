"""Replay frozen A-share execution cases on a local Tushare lake."""
import argparse
from datetime import date, datetime
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import (
    OrderIntent, TimedOrderIntent, compare_complete_backtests,
    prepare_order_tape, run_order_tape, run_prepared_backtest,
)
from engine.backtest import at
from engine.tushare_lake import load_tushare_lake
from examples.validate_tushare_lake import check_cash


def replay(root, start, end, orders):
    symbols = {intent.symbol for intents in orders.values() for intent in intents}
    data = load_tushare_lake(root, symbols, start, end)
    prepared = data.prepare()
    schedule = {date.fromisoformat(day): intents for day, intents in orders.items()}
    tape = prepare_order_tape(prepared, [
        TimedOrderIntent(at(day, 15), intent) for day, intents in schedule.items() for intent in intents
    ])
    options = dict(initial_cash=D(100000), data_snapshot_id=data.snapshot['snapshot_id'])
    callback = run_prepared_backtest(prepared,
        portfolio_signal=lambda bars, state: schedule.get(state.as_of.date(), []), **options)
    result = run_order_tape(prepared, tape, **options)
    comparison = compare_complete_backtests(callback, result)
    assert comparison['equal'], comparison
    check_cash(result, 100000)
    assert not result.pending_intents
    return data, result


def stamp_duty(root):
    symbol = '600000.SH'
    data, result = replay(root, '2023-08-23', '2023-08-28', {
        '2023-08-23': [OrderIntent(symbol, 'BUY', 2000)],
        '2023-08-24': [OrderIntent(symbol, 'SELL', 1000)],
        '2023-08-25': [OrderIntent(symbol, 'SELL', 1000)],
    })
    assert not result.rejections
    assert [f['side'] for f in result.fills] == ['BUY', 'SELL', 'SELL']
    dates = [datetime.fromtimestamp(f['time'] / 1e9, ZoneInfo('Asia/Shanghai')).date().isoformat()
             for f in result.fills]
    assert dates == ['2023-08-24', '2023-08-25', '2023-08-28']
    assert [D(f['quantity']) for f in result.fills] == [2000, 1000, 1000]
    opening_prices = {str(bar.start.date()): bar.open for bar in data.bars}
    assert [D(f['price']) for f in result.fills] == [opening_prices[day] for day in dates]
    fees = []
    for fill, tax in zip(result.fills, (D(0), D('.001'), D('.0005')), strict=True):
        notional = D(fill['price']) * D(fill['quantity'])
        fee = (max(D(5), notional * D('.0003')) + notional * (D('.00001') + tax))
        fee = fee.quantize(D('.01'), rounding=ROUND_HALF_UP)
        assert D(fill['fee']) == fee
        fees.append(str(fee))
    assert result.positions[symbol] == 0
    print('stamp-duty fees:', fees, 'cash:', result.native_cash)


def limit_days(root):
    upper, lower = '600257.SH', '001234.SZ'
    data, result = replay(root, '2023-08-23', '2023-08-28', {
        '2023-08-23': [OrderIntent(lower, 'BUY', 100)],
        '2023-08-24': [OrderIntent(upper, 'BUY', 100), OrderIntent(lower, 'SELL', 100)],
        '2023-08-25': [OrderIntent(lower, 'SELL', 100)],
    })
    day = date(2023, 8, 25)
    for symbol, band in ((upper, 'up_limit'), (lower, 'down_limit')):
        bar = next(b for b in data.bars if b.symbol == symbol and b.end.date() == day)
        assert bar.open == bar.high == bar.low == bar.close == D(str(data.rules[symbol, day][band]))
    assert sorted((r['symbol'], r['reason']) for r in result.rejections) == [
        (lower, 'LIMIT_DOWN_CLOSE'), (upper, 'LIMIT_UP_CLOSE')
    ]
    assert [(f['symbol'], f['side']) for f in result.fills] == [(lower, 'BUY'), (lower, 'SELL')]
    assert not any(result.positions.values())
    print('limit-day rejections:', [(r['symbol'], r['reason']) for r in result.rejections])


def partial_suspension(root):
    symbol = '688065.SH'
    data, result = replay(root, '2023-06-12', '2023-06-15', {
        '2023-06-12': [OrderIntent(symbol, 'BUY', 200)],
        '2023-06-14': [OrderIntent(symbol, 'SELL', 200)],
    })
    day = date(2023, 6, 15)
    assert data.rules[symbol, day]['trade_status'] == 'DAY_BLOCKED'
    assert any(b.end.date() == day and b.volume > 0 for b in data.bars)
    assert [r['reason'] for r in result.rejections] == ['DAY_BLOCKED']
    assert len(result.fills) == 1 and result.positions[symbol] == 200
    assert D(result.daily_nav[-1]['tradable_holdings'][symbol]) == 200
    assert result.daily_nav[-1]['total_equity'] == result.daily_nav[-2]['total_equity']
    print('partial suspension: DAY_BLOCKED; retained shares:', result.positions[symbol])


def limit_open_then_reopens(root):
    symbol = '601059.SH'
    data, result = replay(root, '2023-08-25', '2023-08-28', {
        '2023-08-25': [OrderIntent(symbol, 'BUY', 100, intent_id='open-only')],
    })
    day = date(2023, 8, 28)
    bar = next(b for b in data.bars if b.end.date() == day)
    assert bar.open == D(str(data.rules[symbol, day]['up_limit']))
    assert bar.close < bar.open
    assert not result.fills
    assert [(r['intent_id'], r['reason']) for r in result.rejections] == [('open-only', 'LIMIT_UP_CLOSE')]
    print('limit open: IOC rejected despite subsequent trading below the band')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    root = parser.parse_args().root
    stamp_duty(root)
    limit_days(root)
    partial_suspension(root)
    limit_open_then_reopens(root)

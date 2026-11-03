"""
Institutional Quantitative Multi-Scenario Acceptance Test Suite for ashare-nautilus-backtest-engine.
Formally verifies:
1. Multi-asset cross-sectional alpha rebalancing, multi-venue cash competition & order reservation.
2. Corporate actions: dividend payout, stock split, delayed share listing date & cash reinvestment.
3. Extreme market microstructure: limit-up/down blocks, suspension, low-liquidity partial fills & cash-with-fees exhaustion.
4. Intraday continuous trading: intraday T+1 lock, resting limit orders, 15:00 order expiration & overnight T+1 carryover.
"""
from datetime import datetime, date, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

from contracts import Instrument, Venue, CorporateAction
from engine.backtest import BacktestBar, OrderIntent, run_backtest

TZ = ZoneInfo("Asia/Shanghai")


def dt_daily(day_str: str, hour: int = 9, minute: int = 30) -> datetime:
    d = date.fromisoformat(day_str)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ)


def make_daily_bar(symbol: str, day_str: str, o: float, h: float, l: float, c: float, v: int) -> BacktestBar:
    start = dt_daily(day_str, 9, 30)
    end = dt_daily(day_str, 15, 0)
    return BacktestBar(
        symbol=symbol,
        start=start,
        end=end,
        open=D(str(o)).quantize(D("0.01")),
        high=D(str(h)).quantize(D("0.01")),
        low=D(str(l)).quantize(D("0.01")),
        close=D(str(c)).quantize(D("0.01")),
        volume=v,
    )


def make_minute_bar(symbol: str, day_str: str, start_h: int, start_m: int, duration_min: int, o: float, h: float, l: float, c: float, v: int) -> BacktestBar:
    start = dt_daily(day_str, start_h, start_m)
    end = start + timedelta(minutes=duration_min)
    return BacktestBar(
        symbol=symbol,
        start=start,
        end=end,
        open=D(str(o)).quantize(D("0.01")),
        high=D(str(h)).quantize(D("0.01")),
        low=D(str(l)).quantize(D("0.01")),
        close=D(str(c)).quantize(D("0.01")),
        volume=v,
    )


def make_daily_rule(
    symbol: str,
    day_str: str,
    status: str = "OPEN",
    limit_state: str = "BOUNDED",
    up: float = 110.0,
    down: float = 90.0,
    buy_min: int = 100,
    buy_step: int = 100,
    sell_min: int = 100,
    sell_step: int = 100,
    max_order: int = 1000000,
) -> dict:
    return {
        "trade_status": status,
        "limit_state": limit_state,
        "up_limit": float(D(str(up)).quantize(D("0.01"))),
        "down_limit": float(D(str(down)).quantize(D("0.01"))),
        "buy_min": buy_min,
        "buy_step": buy_step,
        "sell_min": sell_min,
        "sell_step": sell_step,
        "max_order": max_order,
    }


def test_scenario_1_multi_asset_alpha_rebalancing_and_cash_competition():
    """Scenario 1: Cross-Sectional Alpha Rebalancing & Cash Competition across SSE & SZSE."""
    instruments = [
        Instrument(symbol="600519.SH", exchange=Venue.SSE, name="贵州茅台"),
        Instrument(symbol="000001.SZ", exchange=Venue.SZSE, name="平安银行"),
        Instrument(symbol="000858.SZ", exchange=Venue.SZSE, name="五粮液"),
    ]

    trading_days = [
        date(2025, 6, 2),  # Monday (Day 1)
        date(2025, 6, 3),  # Tuesday (Day 2)
        date(2025, 6, 4),  # Wednesday (Day 3)
        date(2025, 6, 5),  # Thursday (Day 4)
        date(2025, 6, 6),  # Friday (Day 5)
    ]

    bars = []
    rules = {}
    prices = {
        "600519.SH": [100.0, 102.0, 101.0, 104.0, 105.0],
        "000001.SZ": [12.0, 12.2, 12.1, 12.5, 12.4],
        "000858.SZ": [50.0, 51.0, 52.0, 51.5, 53.0],
    }

    for sym in ["600519.SH", "000001.SZ", "000858.SZ"]:
        prev = prices[sym][0]
        for idx, day in enumerate(trading_days):
            day_s = str(day)
            p = prices[sym][idx]
            bars.append(make_daily_bar(sym, day_s, p, p + 0.5, p - 0.5, p, 50000))
            up = round(prev * 1.10, 2)
            down = round(prev * 0.90, 2)
            rules[(sym, day)] = make_daily_rule(sym, day_s, up=up, down=down)
            prev = p

    def alpha_signal(bar, state):
        cur_d = bar.end.date()
        if cur_d == date(2025, 6, 2):
            if bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 200)]
            elif bar.symbol == "000001.SZ":
                return [OrderIntent("000001.SZ", "BUY", 2000)]
            elif bar.symbol == "000858.SZ":
                return [OrderIntent("000858.SZ", "BUY", 400)]
        elif cur_d == date(2025, 6, 4):
            if bar.symbol == "000001.SZ":
                return [OrderIntent("000001.SZ", "SELL", 2000)]
            elif bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 100)]
        return []

    res = run_backtest(
        instruments,
        bars,
        rules,
        alpha_signal,
        initial_cash=D("100000"),
        participation_rate=0.10,
        trading_days=trading_days,
    )

    assert res.native_cash == res.ledger_cash
    assert res.positions["000001.SZ"] == 0
    assert res.positions["600519.SH"] == 300
    assert res.positions["000858.SZ"] == 400
    assert len(res.fills) == 5
    assert all(x["cash_matches"] and x["positions_match"] for x in res.daily_nav)


def test_scenario_2_corporate_actions_and_reinvestment():
    """Scenario 2: Corporate Actions (Dividends, Splits, Delayed Listing & Reinvestment)."""
    symbol = "600519.SH"
    instruments = [Instrument(symbol=symbol, exchange=Venue.SSE, name="贵州茅台")]

    trading_days = [
        date(2025, 6, 2),  # Day 1: Buy initial shares
        date(2025, 6, 3),  # Day 2: Record date
        date(2025, 6, 4),  # Day 3: Ex-date, bonus shares arrive (listing delayed)
        date(2025, 6, 5),  # Day 4: Pay date (cash dividend arrived), reinvestment intent
        date(2025, 6, 6),  # Day 5: Reinvestment buys fill; Share Listing Date
        date(2025, 6, 9),  # Day 6: Sell listed shares (300 shares)
        date(2025, 6, 10), # Day 7: Sell remaining 100 reinvested shares
    ]

    daily_prices = [100.00, 100.00, 63.33, 63.33, 64.00, 65.00, 65.50]
    bars = []
    rules = {}
    prev = daily_prices[0]
    for idx, day in enumerate(trading_days):
        day_s = str(day)
        p = daily_prices[idx]
        bars.append(make_daily_bar(symbol, day_s, p, p + 0.5, p - 0.5, p, 50000))
        if day < date(2025, 6, 4):
            up, down = round(prev * 1.10, 2), round(prev * 0.90, 2)
        elif day == date(2025, 6, 4):
            up, down = round(63.33 * 1.10, 2), round(63.33 * 0.90, 2)
        else:
            up, down = round(prev * 1.10, 2), round(prev * 0.90, 2)
        rules[(symbol, day)] = make_daily_rule(symbol, day_s, up=up, down=down)
        prev = p

    ca = CorporateAction(
        symbol=symbol,
        record_date=date(2025, 6, 3),
        ex_date=date(2025, 6, 4),
        pay_date=date(2025, 6, 5),
        share_listing_date=date(2025, 6, 6),
        cash_div=D("5.00"),
        bonus_ratio=D("0.50"),
    )

    def ca_signal(bar, state):
        cur_d = bar.end.date()
        if cur_d == date(2025, 6, 2):
            return [OrderIntent(symbol, "BUY", 200)]
        elif cur_d == date(2025, 6, 4):
            return [OrderIntent(symbol, "SELL", 300)]
        elif cur_d == date(2025, 6, 5):
            assert state.cash > D("1000")
            return [OrderIntent(symbol, "BUY", 100)]
        elif cur_d == date(2025, 6, 6):
            sellable = state.sellable.get(symbol, 0)
            assert sellable == 300
            return [OrderIntent(symbol, "SELL", int(sellable))]
        elif cur_d == date(2025, 6, 9):
            sellable = state.sellable.get(symbol, 0)
            assert sellable == 100
            return [OrderIntent(symbol, "SELL", int(sellable))]
        return []

    res = run_backtest(
        instruments,
        bars,
        rules,
        ca_signal,
        corporate_actions=[ca],
        initial_cash=D("30000"),
        participation_rate=0.10,
        trading_days=trading_days,
    )

    assert any("T1_" in r["reason"] for r in res.rejections)
    div_entries = [j for j in res.account_journal if "dividend" in j["event_id"]]
    assert len(div_entries) == 1 and div_entries[0]["amount"] == "1000.00"
    assert res.daily_nav[2]["receivable_dividends"] == "1000.00"
    assert res.positions[symbol] == 0
    assert res.native_cash == res.ledger_cash
    assert all(x["cash_matches"] and x["positions_match"] for x in res.daily_nav)


def test_scenario_3_extreme_microstructure_and_risk_collars():
    """Scenario 3: Extreme Microstructure, Liquidity Stress & Risk Collars."""
    instruments = [
        Instrument(symbol="600519.SH", exchange=Venue.SSE, name="贵州茅台"),
        Instrument(symbol="000001.SZ", exchange=Venue.SZSE, name="平安银行"),
    ]

    trading_days = [
        date(2025, 7, 1),
        date(2025, 7, 2),
        date(2025, 7, 3),
        date(2025, 7, 4),
        date(2025, 7, 7),
        date(2025, 7, 8),
        date(2025, 7, 9),
    ]

    bars = []
    rules = {}
    for sym in ["600519.SH", "000001.SZ"]:
        for d in trading_days:
            day_s = str(d)
            if sym == "600519.SH":
                if d in (date(2025, 7, 1), date(2025, 7, 2)):
                    bars.append(make_daily_bar(sym, day_s, 100.0, 100.5, 99.5, 100.0, 50000))
                    rules[(sym, d)] = make_daily_rule(sym, day_s, up=110.0, down=90.0)
                elif d == date(2025, 7, 3):
                    bars.append(make_daily_bar(sym, day_s, 110.0, 110.0, 110.0, 110.0, 20000))
                    rules[(sym, d)] = make_daily_rule(sym, day_s, up=110.0, down=90.0)
                elif d == date(2025, 7, 4):
                    bars.append(make_daily_bar(sym, day_s, 110.0, 110.0, 110.0, 110.0, 0))
                    rules[(sym, d)] = make_daily_rule(sym, day_s, status="SUSPENDED", up=121.0, down=99.0)
                elif d == date(2025, 7, 7):
                    bars.append(make_daily_bar(sym, day_s, 99.0, 99.0, 99.0, 99.0, 5000))
                    rules[(sym, d)] = make_daily_rule(sym, day_s, up=121.0, down=99.0)
                elif d == date(2025, 7, 8):
                    bars.append(make_daily_bar(sym, day_s, 100.0, 101.0, 99.0, 100.0, 1000))
                    rules[(sym, d)] = make_daily_rule(sym, day_s, up=108.9, down=89.1)
                elif d == date(2025, 7, 9):
                    bars.append(make_daily_bar(sym, day_s, 100.0, 101.0, 99.0, 100.0, 50000))
                    rules[(sym, d)] = make_daily_rule(sym, day_s, up=110.0, down=90.0)
            else:
                bars.append(make_daily_bar(sym, day_s, 10.0, 10.1, 9.9, 10.0, 50000))
                rules[(sym, d)] = make_daily_rule(sym, day_s, up=11.0, down=9.0)

    def stress_signal(bar, state):
        cur_d = bar.end.date()
        if cur_d == date(2025, 7, 1):
            if bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 200)]
        elif cur_d == date(2025, 7, 2):
            if bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 100)]
        elif cur_d == date(2025, 7, 3):
            if bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 100)]
        elif cur_d == date(2025, 7, 4):
            if bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "SELL", 200)]
        elif cur_d == date(2025, 7, 7):
            if bar.symbol == "000001.SZ":
                return [OrderIntent("000001.SZ", "BUY", 5000)]
            elif bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 200)]
        elif cur_d == date(2025, 7, 8):
            intents = []
            for s in ["600519.SH", "000001.SZ"]:
                sellable = state.sellable.get(s, 0)
                if sellable > 0:
                    intents.append(OrderIntent(s, "SELL", int(sellable)))
            return intents
        return []

    res = run_backtest(
        instruments,
        bars,
        rules,
        stress_signal,
        initial_cash=D("50000"),
        participation_rate=0.10,
        trading_days=trading_days,
    )

    assert any("SUSPENDED" in r["reason"] for r in res.rejections)
    assert any("LIMIT_DOWN" in r["reason"] for r in res.rejections)
    assert any("LIMIT_UP" in r["reason"] for r in res.rejections)
    assert any("INSUFFICIENT_CASH" in r["reason"] for r in res.rejections)
    buy_fills = [f for f in res.fills if f["symbol"] == "600519.SH" and f["side"] == "BUY"]
    assert len(buy_fills) == 2
    assert buy_fills[0]["quantity"] == "200" and buy_fills[1]["quantity"] == "100"
    assert res.native_cash == res.ledger_cash
    assert all(x["cash_matches"] and x["positions_match"] for x in res.daily_nav)


def test_scenario_4_intraday_minute_continuous_and_resting_limits():
    """Scenario 4: Intraday Minute Continuous Trading, Resting Limits & T+1 Carryover."""
    symbol = "600519.SH"
    instruments = [Instrument(symbol=symbol, exchange=Venue.SSE, name="贵州茅台")]

    trading_days = [
        date(2025, 8, 1),
        date(2025, 8, 4),
    ]

    bars = [
        make_minute_bar(symbol, "2025-08-01", 9, 30, 1, 100.0, 100.2, 99.8, 100.0, 20000),
        make_minute_bar(symbol, "2025-08-01", 9, 31, 1, 100.0, 100.5, 99.3, 100.0, 20000),
        make_minute_bar(symbol, "2025-08-01", 10, 0, 1, 100.2, 100.4, 100.1, 100.2, 20000),
        make_minute_bar(symbol, "2025-08-01", 14, 55, 1, 100.5, 100.6, 100.4, 100.5, 20000),
        make_minute_bar(symbol, "2025-08-01", 14, 59, 1, 100.5, 100.6, 100.4, 100.5, 20000),
        make_minute_bar(symbol, "2025-08-04", 9, 30, 1, 101.0, 101.2, 100.8, 101.0, 20000),
        make_minute_bar(symbol, "2025-08-04", 9, 31, 1, 101.0, 101.2, 100.8, 101.0, 20000),
        make_minute_bar(symbol, "2025-08-04", 14, 59, 1, 101.5, 101.6, 101.4, 101.5, 20000),
    ]

    rules = {
        (symbol, date(2025, 8, 1)): make_daily_rule(symbol, "2025-08-01", up=110.0, down=90.0),
        (symbol, date(2025, 8, 4)): make_daily_rule(symbol, "2025-08-04", up=110.5, down=90.5),
    }

    def intraday_signal(bar, state):
        cur_dt = bar.end.astimezone(TZ)
        cur_d = cur_dt.date()
        cur_h = cur_dt.hour
        cur_m = cur_dt.minute

        if cur_d == date(2025, 8, 1):
            if cur_h == 9 and cur_m == 31:
                return [
                    OrderIntent(symbol, "BUY", 200),
                    OrderIntent(symbol, "BUY", 100, limit_price=D("99.50")),
                ]
            elif cur_h == 10 and cur_m == 1:
                return [OrderIntent(symbol, "SELL", 200)]
            elif cur_h == 14 and cur_m == 56:
                return [OrderIntent(symbol, "BUY", 100, limit_price=D("90.00"))]
        elif cur_d == date(2025, 8, 4):
            if cur_h == 9 and cur_m == 31:
                sellable = state.sellable.get(symbol, 0)
                assert sellable == 300
                return [OrderIntent(symbol, "SELL", int(sellable))]
        return []

    res = run_backtest(
        instruments,
        bars,
        rules,
        intraday_signal,
        initial_cash=D("50000"),
        participation_rate=0.10,
        trading_days=trading_days,
    )

    assert any("T1_" in r["reason"] for r in res.rejections)
    limit_fills = [f for f in res.fills if f["side"] == "BUY" and D(f["price"]) <= D("99.50")]
    assert len(limit_fills) == 1 and limit_fills[0]["quantity"] == "100"
    expired_or_canceled = [o for o in res.orders if o["status"] in ("CANCELED", "EXPIRED")]
    assert len(expired_or_canceled) >= 1
    assert res.positions[symbol] == 0
    assert res.native_cash == res.ledger_cash
    assert all(x["cash_matches"] and x["positions_match"] for x in res.daily_nav)

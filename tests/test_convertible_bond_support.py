"""
Unit Tests for Convertible Bond Extension in ashare-nautilus-backtest-engine.
Verifies ConvertibleBondInstrument, ConvertibleBondFeeModel (0% stamp duty), and lot sizing.
"""

from decimal import Decimal
import pytest
from contracts.models import AssetClass, Venue, ConvertibleBondInstrument
from engine.fee_model import (
    ConvertibleBondFeeModel,
    AShareFeeModel,
    calculate_expected_fee,
    get_cb_rates_for_date,
    get_ashare_rates_for_date,
)


def test_convertible_bond_instrument_contract():
    """验证可转债契约模型默认属性与不可变性"""
    cb = ConvertibleBondInstrument(
        symbol="110059.SH",
        exchange=Venue.SSE,
        name="浦发转债",
        underlying_symbol="600000.SH",
        conversion_price=Decimal("13.50"),
    )
    assert cb.asset_class == AssetClass.CONVERTIBLE_BOND
    assert cb.lot_size == 10  # 1手 = 10张
    assert cb.min_order_qty == 10
    assert cb.face_value == Decimal("100.0")
    assert cb.tick_size == Decimal("0.001")
    assert cb.underlying_symbol == "600000.SH"
    assert cb.conversion_price == Decimal("13.50")

    # 验证不可变设计
    with pytest.raises(Exception):
        cb.lot_size = 100


def test_convertible_bond_zero_stamp_duty():
    """
    核心业务测试: 验证可转债卖出时完全免征印花税，而普通股票必须征收印花税。
    """
    trade_val = Decimal("100000.0")  # 10万元成交额
    qty = Decimal("1000")             # 1000张
    price = Decimal("100.0")          # 100元/张

    # 1. 普通股票卖出费用 (包含印花税)
    stock_fee = calculate_expected_fee(
        side="SELL",
        qty=qty,
        price=price,
        trade_date="2023-09-01",
        is_convertible_bond=False,
    )
    # 股票卖出: 佣金 30 (万三) + 过户费 1 (十万分之一) + 印花税 50 (万分之五) = 81.00 元
    assert stock_fee == Decimal("81.00")

    # 2. 可转债卖出费用 (免征印花税)
    cb_fee = calculate_expected_fee(
        side="SELL",
        qty=qty,
        price=price,
        trade_date="2023-09-01",
        is_convertible_bond=True,
    )
    # 可转债卖出: 佣金 10 (万一) + 过户费 1 (十万分之一) + 印花税 0 (免征) = 11.00 元
    assert cb_fee == Decimal("11.00")
    assert cb_fee < stock_fee


def test_convertible_bond_fee_model_class():
    """测试 ConvertibleBondFeeModel 类的费率解析与参数重载"""
    cb_model = ConvertibleBondFeeModel(current_trade_date="2023-09-01")
    rates = get_cb_rates_for_date("2023-09-01")
    assert rates["stamp_duty_rate"] == Decimal("0.0")
    assert rates["commission_rate"] == Decimal("0.0001")
    assert rates["min_commission"] == Decimal("1.0")


def test_convertible_bond_batch_commission():
    """测试分批成交时的增量佣金计算 (最低1元保底)"""
    # 第一批小额成交: 1000元，万一为 0.1 元，受 1 元保底限制
    fee_part1 = calculate_expected_fee(
        side="BUY",
        qty=Decimal("10"),
        price=Decimal("100.0"),
        is_new_order=True,
        cum_trade_val_before=Decimal("0.0"),
        is_convertible_bond=True,
    )
    # 佣金保底 1 元 + 过户费 0.01 元 = 1.01 元
    assert fee_part1 == Decimal("1.01")


def test_convertible_bond_end_to_end_backtest():
    """
    全闭环测试：在原生 Nautilus 引擎中运行可转债真实委托撮合回测。
    验证：
    1. 1手=10张整手成交
    2. 卖出完全免征印花税 (0 stamp duty)
    3. 双账本（Nautilus native 与 GoldenLedger）每日日切对账 100% 精确一致
    """
    from datetime import datetime, date, timedelta
    from zoneinfo import ZoneInfo
    from engine.backtest import BacktestBar, OrderIntent, run_backtest

    TZ = ZoneInfo("Asia/Shanghai")
    symbol = "110059.SH"
    inst = ConvertibleBondInstrument(
        symbol=symbol,
        exchange=Venue.SSE,
        name="浦发转债",
        lot_size=10,
        min_order_qty=10,
        tick_size=Decimal("0.001"),
    )

    bars = []
    rules = {}
    day = date(2025, 5, 6)
    px = Decimal("110.000")
    for i in range(3):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        start = datetime.combine(day, datetime.min.time(), TZ).replace(hour=9, minute=30)
        end = start.replace(hour=15, minute=0)
        bars.append(BacktestBar(symbol, start, end, px, px + Decimal("1.0"), px - Decimal("1.0"), px, 50000))
        rules[(symbol, day)] = dict(
            trade_status="OPEN",
            limit_state="BOUNDED",
            up_limit=float((px * Decimal("1.2")).quantize(Decimal("0.01"))),
            down_limit=float((px * Decimal("0.8")).quantize(Decimal("0.01"))),
            buy_min=10,
            buy_step=10,
            sell_min=10,
            sell_step=10,
            max_order=1000000,
        )
        px += Decimal("2.000")
        day += timedelta(days=1)

    def signal(bar, state):
        if bar.end.date() == date(2025, 5, 6):
            return [OrderIntent(bar.symbol, "BUY", 100)]  # 买入 100 张 (10手)
        if bar.end.date() == date(2025, 5, 7):
            return [OrderIntent(bar.symbol, "SELL", 100)] # 卖出 100 张
        return []

    result = run_backtest(
        instruments=[inst],
        bars=bars,
        rules=rules,
        signal=signal,
        initial_cash=Decimal("100000.0"),
        participation_rate=1.0,
    )

    assert len(result.fills) == 2
    buy_fill = result.fills[0]
    sell_fill = result.fills[1]

    assert buy_fill["side"] == "BUY"
    assert buy_fill["quantity"] == "100"
    assert Decimal(buy_fill["price"]) == Decimal("112.00")

    assert sell_fill["side"] == "SELL"
    assert sell_fill["quantity"] == "100"
    assert Decimal(sell_fill["price"]) == Decimal("114.00")

    # 验证卖出时免印花税：卖出成交额 = 100 * 114 = 11400 元
    # 佣金 = 11400 * 0.0001 = 1.14 元，过户费 = 11400 * 0.00001 = 0.11 元，印花税 = 0 元
    # 总费用 = 1.25 元 (若为股票，将额外征收 5.70 元印花税)
    sell_fee = Decimal(sell_fill["fee"])
    assert sell_fee == Decimal("1.25")

    # 验证双轨账本 100% 对账成功
    assert result.native_cash == result.ledger_cash
    assert result.positions[symbol] == Decimal("0")
    assert len(result.daily_nav) == 3
    assert all(d["cash_matches"] and d["positions_match"] for d in result.daily_nav)


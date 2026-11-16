from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from contracts import CorporateAction
import engine.backtest as module


@pytest.mark.parametrize('held', [False, True])
def test_split_rebuilds_native_portfolio_only_after_position_change(monkeypatch, held):
    day = date(2025, 5, 7)
    action = CorporateAction(symbol='600000.SH', ex_date=day, bonus_ratio=D('.1'), share_listing_date=day)
    bridge = SimpleNamespace(apply_split=Mock())
    monkeypatch.setattr(module, 'load_bridge', lambda: bridge)
    position = object()
    portfolio = SimpleNamespace(initialize_positions=Mock())
    gateway = SimpleNamespace(on_trade_day_start=Mock(), apply_stock_split=Mock())
    ledger = SimpleNamespace(on_trade_day_start=Mock(), process_corporate_actions_for_date=Mock(), positions={})
    instrument = SimpleNamespace(id='600000.SH.ASHARE')
    state = SimpleNamespace(sessions=[day], gateway=gateway, ledger=ledger,
        fee_model=SimpleNamespace(set_trade_date=Mock()), pending=[], ex_actions={day: [action]},
        pay_actions={}, last_close={}, native={action.symbol: instrument}, portfolio=portfolio,
        cash_module=SimpleNamespace(entitlements={}),
        cache=SimpleNamespace(positions_open=Mock(return_value=[position] if held else [])),
        _native_quantity=lambda symbol: D(100) if held else D(0))
    event = SimpleNamespace(ts_event=module.nanos(module.at(day, 9)))
    module._ResearchStrategy._start_day(state, event)
    gateway.apply_stock_split.assert_called_once_with(action.symbol, D('1.1'), share_listing_date=day, trade_date=day)
    ledger.process_corporate_actions_for_date.assert_called_once_with(str(day))
    if held:
        bridge.apply_split.assert_called_once_with(position, D('1.1'), event.ts_event, instrument, fractional_policy='reject')
        portfolio.initialize_positions.assert_called_once_with()
    else:
        bridge.apply_split.assert_not_called()
        portfolio.initialize_positions.assert_not_called()

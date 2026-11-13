from dataclasses import replace
from decimal import Decimal

from contracts import Instrument, Venue
from engine import OrderIntent, TimedOrderIntent, prepare_backtest, prepare_order_tape, run_order_tape
from engine.backtest import _ResearchStrategy
from test_research_hardening import market


def test_session_close_cancels_only_instruments_with_tracked_orders(monkeypatch):
    symbols = ('600002.SH', '600001.SH', '600000.SH')
    bars, rules = market(symbols)
    bars = [replace(bar, low=Decimal('9.90')) for bar in bars]
    prepared = prepare_backtest([Instrument(symbol=s, exchange=Venue.SSE) for s in symbols], bars, rules)
    calls = []
    original = _ResearchStrategy.cancel_all_orders
    def counted(self, instrument_id, *args, **kwargs):
        calls.append((self.current_day, str(instrument_id)))
        return original(self, instrument_id, *args, **kwargs)
    monkeypatch.setattr(_ResearchStrategy, 'cancel_all_orders', counted)
    idle = run_order_tape(prepared, prepare_order_tape(prepared, []))
    assert not idle.fills and not calls
    entries = [TimedOrderIntent(bars[0].end,
                OrderIntent(symbols[0], 'BUY', 100, Decimal('9.85'), intent_id='unfilled')),
               TimedOrderIntent(bars[0].end,
                OrderIntent(symbols[2], 'BUY', 100, Decimal('9.85'), intent_id='unfilled-2')),
               TimedOrderIntent(bars[0].end,
                OrderIntent(symbols[1], 'BUY', 100, intent_id='filled'))]
    result = run_order_tape(prepared, prepare_order_tape(prepared, entries))
    assert calls == [(prepared.sessions[1], f'{s}.ASHARE') for s in (symbols[0], symbols[2])]
    assert len(result.fills) == 1 and result.fills[0]['intent_id'] == 'filled'
    assert {row.get('intent_id'): row['status'] for row in result.orders} == {
        'unfilled': 'CANCELED', 'unfilled-2': 'CANCELED', 'filled': 'FILLED'}


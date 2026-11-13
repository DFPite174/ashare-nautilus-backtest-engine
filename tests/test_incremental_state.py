from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from contracts import Instrument, Venue
from engine import run_backtest
from engine.order_gateway import AShareOrderGateway
from test_research_hardening import market


def test_no_order_bar_callbacks_do_not_rebuild_all_sellable_symbols(monkeypatch):
    symbols=tuple(f'60000{n}.SH' for n in range(6))
    bars,rules=market(symbols,days=1,minute=True)
    bars=[replace(b,start=b.start+timedelta(minutes=n),end=b.end+timedelta(minutes=n))
          for n in range(3) for b in bars]
    calls=[]
    original=AShareOrderGateway.get_sellable_qty
    def observed(self,symbol):
        calls.append(symbol)
        return original(self,symbol)
    monkeypatch.setattr(AShareOrderGateway,'get_sellable_qty',observed)
    snapshots=[]
    def signal(bar,state):
        snapshots.append(state)
        assert dict(state.sellable)==dict.fromkeys(symbols,D(0))
        return []
    run_backtest([Instrument(symbol=s,exchange=Venue.SSE) for s in symbols],bars,rules,signal)
    assert len(snapshots)==18
    assert len(calls)<=len(symbols)
    assert len(snapshots[0].history)==1
    assert len(snapshots[0].valuation_prices)==1
    assert len(snapshots[0].history[symbols[0]])==1
    assert len(snapshots[-1].history[symbols[0]])==3

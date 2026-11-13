from decimal import Decimal as D

from contracts import Instrument,Venue
import engine.backtest as module
from test_research_hardening import market


def test_market_validation_constructs_boundaries_once_per_session(monkeypatch):
    symbols=tuple(f'60000{n}.SH' for n in range(6))
    bars,rules=market(symbols,days=1,minute=True)
    calls=[];original=module.at
    def counted(*args,**kwargs):
        calls.append(args)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'at',counted)
    module._validate([Instrument(symbol=s,exchange=Venue.SSE) for s in symbols],
                     bars,rules,(),D(10000),1,[bars[0].end.date()])
    assert len(calls)<=4

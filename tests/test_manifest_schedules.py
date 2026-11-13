from decimal import Decimal as D
from contracts import Instrument, Venue
from engine.fee_model import AShareFeeModel
from engine.reproducibility import build_manifest
from test_research_hardening import market


def manifest(fee):
    symbols=tuple(f'60000{n}.SH' for n in range(6))
    bars,rules=market(symbols)
    instruments=[Instrument(symbol=s,exchange=Venue.SSE) for s in symbols]
    return build_manifest(instruments,bars,rules,[],sorted({b.end.date() for b in bars}),fee,
        lambda b,s:[],initial_cash=D(100000),participation_rate=1,slippage_bps=0,
        strategy_id='test',strategy_params={},data_snapshot_id=None)


def test_builtin_equity_schedule_is_computed_once_per_date(monkeypatch):
    import engine.fee_model as module
    fee=AShareFeeModel()
    original=module.get_ashare_rates_for_date
    calls=[]
    def counted(day):
        calls.append(day)
        return original(day)
    monkeypatch.setattr(module,'get_ashare_rates_for_date',counted)
    result=manifest(fee)
    assert len(calls)==3
    assert len(result['config']['symbol_fee_policy'])==6


def test_symbol_specific_fee_subclass_keeps_distinct_policies():
    class Fees(AShareFeeModel):
        def rates_for(self,symbol,day):
            rates=super().rates_for(symbol,day)
            rates['min_commission']=D(int(symbol[5])+1)
            return rates
    result=manifest(Fees())
    assert len(result['config']['fee_schedules'])==6

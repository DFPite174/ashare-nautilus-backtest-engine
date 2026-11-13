from dataclasses import replace
from decimal import Decimal as D

import pytest
from contracts import Instrument, Venue
from engine import OrderIntent, compare_backtests, run_backtest
from test_research_hardening import market


@pytest.mark.parametrize('mode',['session','sparse'])
@pytest.mark.parametrize('kind',['idle','roundtrip','partial_limit','halt','future'])
def test_delivery_matches_full_order_and_account_contract(mode,kind):
    bars,rules=market()
    if kind=='partial_limit':bars[1]=replace(bars[1],volume=150)
    if kind=='halt':
        rules[bars[1].symbol,bars[1].end.date()]['trade_status']='SUSPENDED'
        bars[1]=replace(bars[1],volume=0)
    def signal(bar,state):
        if kind=='idle':return []
        if bar==bars[0]:
            if kind=='partial_limit':return [OrderIntent(bar.symbol,'BUY',100,D(10))]*2
            return [OrderIntent(bar.symbol,'BUY',100,earliest_execution=bars[2].start if kind=='future' else None)]
        if bar==bars[1] and kind=='roundtrip':return [OrderIntent(bar.symbol,'SELL',100)]
        return []
    instruments=[Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)]
    full=run_backtest(instruments,bars,rules,signal,participation_rate=1)
    candidate=run_backtest(instruments,bars,rules,signal,participation_rate=1,data_mode=mode)
    assert compare_backtests(full,candidate)['equal_within_compared_fields']
    assert full.pending_intents==candidate.pending_intents
    if mode=='sparse' and kind=='idle':
        assert candidate.run_statistics['market_events']==2*len(bars)


def test_sparse_requires_daily_bars_but_session_supports_minutes():
    bars,rules=market(minute=True)
    instruments=[Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)]
    full=run_backtest(instruments,bars,rules,lambda b,s:[])
    session=run_backtest(instruments,bars,rules,lambda b,s:[],data_mode='session')
    assert compare_backtests(full,session)['equal_within_compared_fields']
    with pytest.raises(ValueError,match='daily'):
        run_backtest(instruments,bars,rules,lambda b,s:[],data_mode='sparse')


@pytest.mark.parametrize('mode,index',[(m,n) for m in ('session','sparse') for n in range(1,5) if m!='sparse' or n!=4])
def test_existing_institutional_acceptance_under_delivery_mode(monkeypatch,mode,index):
    import test_institutional_scenarios as scenarios
    names=sorted(name for name in vars(scenarios) if name.startswith(f'test_scenario_{index}_'))
    assert len(names)==1
    original=scenarios.run_backtest
    def delivered(*args,**kwargs):
        kwargs['data_mode']=mode
        return original(*args,**kwargs)
    monkeypatch.setattr(scenarios,'run_backtest',delivered)
    getattr(scenarios,names[0])()


@pytest.mark.parametrize('mode,name',[
    (m,n) for m in ('session','sparse') for n in (
        'test_mixed_equity_cb_preserves_asset_specific_fees',
        'test_custom_fee_config_drives_native_oracle_and_cash_reservation')]+[
    ('session','test_cb_can_sell_same_day_but_equity_stays_locked')])
def test_asset_and_custom_fee_contracts_under_delivery_mode(monkeypatch,mode,name):
    import test_research_hardening as scenarios
    original=scenarios.run_backtest
    def delivered(*args,**kwargs):return original(*args,**kwargs,data_mode=mode)
    monkeypatch.setattr(scenarios,'run_backtest',delivered)
    getattr(scenarios,name)()


@pytest.mark.parametrize('mode',['session','sparse'])
def test_trailing_suspended_sessions_without_bars_still_close(mode):
    bars,rules=market()
    days=[b.end.date() for b in bars]
    for b in bars[1:]:rules[b.symbol,b.end.date()]['trade_status']='SUSPENDED'
    result=run_backtest([Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)],
        bars[:1],rules,lambda b,s:[],trading_days=days,data_mode=mode)
    assert len(result.daily_nav)==3
    assert result.run_statistics['boundary_events']==3

from datetime import date, timedelta
from dataclasses import replace
from decimal import Decimal as D

import pytest
from contracts import CorporateAction, Instrument, Venue
from engine import OrderIntent, run_backtest
from test_research_hardening import market


def test_earliest_execution_and_id_reach_result():
    bars, rules = market()
    def signal(bar, state):
        if bar.end.date() == date(2025, 5, 6):
            return [OrderIntent(bar.symbol, 'BUY', 100, intent_id='monthly-1', earliest_execution=bars[2].start)]
        return []
    result = run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE)], bars, rules, signal)
    assert len(result.fills) == 1
    assert result.fills[0]['intent_id'] == 'monthly-1'
    assert result.fills[0]['time'] >= int(bars[2].start.timestamp()*1e9)
    assert result.orders[0]['intent_id'] == 'monthly-1'
    assert result.to_dict()['schema_version'] == 2


def test_duplicate_intent_id_fails_across_callbacks():
    bars, rules = market()
    with pytest.raises(ValueError, match='[Dd]uplicate'):
        run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE)], bars, rules,
            lambda b, s: [OrderIntent(b.symbol, 'BUY', 100, intent_id='same')])


def test_account_snapshot_exposes_funds_and_remains_frozen():
    bars, rules = market()
    snapshots = []
    def signal(bar, state):
        snapshots.append(state)
        return [OrderIntent(bar.symbol, 'BUY', 100)] if len(snapshots) == 1 else []
    run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE)], bars, rules, signal)
    first, second = snapshots[:2]
    assert first.as_of == bars[0].end
    assert first.available_cash == first.cash == D(100000)
    assert first.reserved_cash == first.receivables == 0
    assert second.available_cash == second.cash == D('98994.99')
    assert second.nav == D('99994.99')
    assert second.valuation_prices['600519.SH'] == D(10)
    assert first.positions == {}
    assert len(first.history['600519.SH']) == 1
    with pytest.raises(TypeError): second.valuation_prices['600519.SH'] = D(999)


def test_identified_cash_rejection_and_future_pending():
    bars, rules = market()
    def signal(bar, state):
        if bar.end.date() == date(2025, 5, 6):
            return [OrderIntent(bar.symbol, 'BUY', 100, intent_id='cash'),
                    OrderIntent(bar.symbol, 'BUY', 100, intent_id='future',
                                earliest_execution=bars[-1].end)]
        return []
    result=run_backtest([Instrument(symbol='600519.SH', exchange=Venue.SSE)],
                        bars,rules,signal,initial_cash=1000,participation_rate=1)
    assert result.rejections[0]['intent_id']=='cash'
    assert result.pending_intents[0]['intent_id']=='future'
    assert result.pending_intents[0]['earliest_execution']==bars[-1].end.isoformat()


def test_future_eligible_suspension_without_bar_rejects_at_session_close():
    bars,rules=market()
    first,blocked,last=bars
    rules[blocked.symbol,blocked.end.date()]['trade_status']='SUSPENDED'
    def signal(bar,state):
        return [OrderIntent(bar.symbol,'BUY',100,intent_id='halt',earliest_execution=blocked.start)] if bar==first else []
    result=run_backtest([Instrument(symbol='600519.SH',exchange=Venue.SSE)],
                       [first,last],rules,signal,trading_days=[b.end.date() for b in bars])
    assert not result.fills and not result.pending_intents
    assert result.rejections==[{'symbol':first.symbol,'reason':'SUSPENDED',
                               'intent_id':'halt','time':int(blocked.end.timestamp()*1e9)}]


def test_intent_decided_at_suspended_close_is_for_next_session():
    bars,rules=market()
    bars[1]=replace(bars[1],volume=0)
    rules[bars[1].symbol,bars[1].end.date()]['trade_status']='SUSPENDED'
    def signal(bar,state):
        return [OrderIntent(bar.symbol,'BUY',100,intent_id='after-halt')] if bar==bars[1] else []
    result=run_backtest([Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)],bars,rules,signal)
    assert not result.rejections
    assert result.fills[0]['intent_id']=='after-halt'
    assert result.fills[0]['time']>=int(bars[2].start.timestamp()*1e9)


def test_earliest_inside_bar_does_not_use_its_intrabar_path():
    bars,rules=market()
    def signal(bar,state):
        return [OrderIntent(bar.symbol,'BUY',100,D(10),earliest_execution=bars[1].start.replace(hour=12))] if bar==bars[0] else []
    result=run_backtest([Instrument(symbol='600519.SH',exchange=Venue.SSE)],bars,rules,signal)
    assert len(result.fills)==1
    assert result.fills[0]['time']>=int(bars[2].start.timestamp()*1e9)


def test_partial_day_order_exposes_reserved_cash_in_minute_snapshot():
    first,rules=market(days=1,minute=True)
    bar=replace(first[0],volume=20000)
    bars=[bar,replace(bar,start=bar.start+timedelta(minutes=1),end=bar.end+timedelta(minutes=1),volume=15000),
          replace(bar,start=bar.start+timedelta(minutes=2),end=bar.end+timedelta(minutes=2))]
    snapshots=[]
    def signal(b,state):
        snapshots.append(state)
        return [OrderIntent(b.symbol,'BUY',200,D(10))] if b==bar else []
    run_backtest([Instrument(symbol=bar.symbol,exchange=Venue.SSE)],bars,rules,signal)
    partial=snapshots[1]
    assert partial.positions[bar.symbol]==150
    assert partial.reserved_cash>0
    assert partial.available_cash==partial.cash-partial.reserved_cash
    assert snapshots[2].reserved_cash==0
    assert partial.reserved_cash>0  # later fill does not mutate a retained snapshot


def test_unpaid_dividend_is_equity_but_not_available_cash():
    bars,rules=market(days=4)
    snapshots=[]
    action=CorporateAction(symbol=bars[0].symbol,record_date=bars[1].end.date(),
        ex_date=bars[2].end.date(),pay_date=bars[3].end.date(),cash_div=D(1))
    def signal(b,state):
        snapshots.append(state)
        return [OrderIntent(b.symbol,'BUY',100)] if b==bars[0] else []
    run_backtest([Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)],bars,rules,signal,corporate_actions=[action])
    assert snapshots[2].receivables==100
    assert snapshots[2].cash==snapshots[1].cash
    assert snapshots[2].nav==snapshots[2].cash+100+100*D(10)
    assert snapshots[3].receivables==0
    assert snapshots[3].cash==snapshots[2].cash+100

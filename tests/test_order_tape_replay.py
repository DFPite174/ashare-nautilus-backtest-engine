from dataclasses import replace
from decimal import Decimal as D

import pytest
from contracts import Instrument, Venue, ConvertibleBondInstrument
from engine import OrderIntent, compare_backtests, prepare_backtest, run_prepared_backtest
from test_research_hardening import market


@pytest.mark.parametrize('volume', [10000, 150])
def test_wide_dense_tape_matches_full_callback_execution(volume):
    from engine import TimedOrderIntent, prepare_order_tape, run_order_tape
    symbols = tuple(f'600{n:03d}.SH' for n in range(32))
    bars, rules = market(symbols, days=16)
    bars = [replace(bar, volume=volume) for bar in bars if bar.end.weekday() < 5]
    rules = {key: row for key, row in rules.items() if key[1].weekday() < 5}
    prepared = prepare_backtest([Instrument(symbol=s, exchange=Venue.SSE) for s in symbols], bars, rules)
    times = sorted({bar.end for bar in bars})[:-1]
    entries = [TimedOrderIntent(timestamp, OrderIntent(symbol, 'BUY' if day % 2 == 0 else 'SELL',
                    100, D(10), intent_id=f'{day}:{symbol}:{part}'))
               for day, timestamp in enumerate(times) for symbol in symbols for part in range(3)]
    by_time = {}
    for entry in entries:
        by_time.setdefault(entry.decision_time, []).append(entry.intent)
    options = dict(initial_cash=D(1000000), participation_rate=1)
    reference = run_prepared_backtest(prepared,
        portfolio_signal=lambda closed, state: by_time.get(closed[0].end, ()), **options)
    result = run_order_tape(prepared, prepare_order_tape(prepared, entries), **options)
    assert compare_backtests(reference, result)['equal_within_compared_fields']
    assert reference.orders == result.orders
    assert reference.pending_intents == result.pending_intents
    assert len(result.daily_nav) == 12
    assert max(sum(D(quantity) > 0 for quantity in row['holdings'].values())
               for row in result.daily_nav) == len(symbols)
    assert result.run_statistics['scheduled_intents'] == 1056


@pytest.mark.parametrize('kind',['idle','roundtrip','partial','halt','future','cross-symbol'])
def test_frozen_tape_matches_callback_account_and_fills(kind):
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape
    symbols=('600000.SH','600001.SH')
    bars,rules=market(symbols)
    if kind=='partial':
        bars=[replace(b,volume=150) if b.end.date().day==7 else b for b in bars]
    if kind=='halt':
        for b in bars:
            if b.end.date().day==7:rules[b.symbol,b.end.date()]['trade_status']='SUSPENDED'
        bars=[b for b in bars if b.end.date().day!=7]
    prepared=prepare_backtest([Instrument(symbol=s,exchange=Venue.SSE) for s in symbols],bars,rules)
    entries=[]
    if kind!='idle':
        decision=next(b.end for b in bars if b.end.date().day==6)
        symbol=symbols[1] if kind=='cross-symbol' else symbols[0]
        for _ in range(2 if kind=='partial' else 1):
            entries.append(TimedOrderIntent(decision,OrderIntent(symbol,'BUY',100,
                D(10) if kind=='partial' else None,
                earliest_execution=next(b.start for b in bars if b.end.date().day==8) if kind=='future' else None)))
        if kind=='roundtrip':
            entries.append(TimedOrderIntent(next(b.end for b in bars if b.end.date().day==7),OrderIntent(symbol,'SELL',100)))
    def portfolio_signal(closed,state):return [e.intent for e in entries if e.decision_time==closed[0].end]
    reference=run_prepared_backtest(prepared,portfolio_signal=portfolio_signal,participation_rate=1)
    frozen=prepare_order_tape(prepared,entries)
    candidate=run_order_tape(prepared,frozen,participation_rate=1)
    assert compare_backtests(reference,candidate)['equal_within_compared_fields']
    assert reference.orders==candidate.orders
    assert reference.pending_intents==candidate.pending_intents
    assert reference.account_journal==candidate.account_journal
    assert candidate.manifest['order_tape_sha256']==frozen.sha256
    assert candidate.run_statistics['input_bars']==len(bars)
    if kind=='idle':assert candidate.run_statistics['market_events']==0


def test_tape_run_is_isolated_and_market_binding_is_checked():
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape
    bars,rules=market()
    instruments=[Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)]
    prepared=prepare_backtest(instruments,bars,rules)
    tape=prepare_order_tape(prepared,[TimedOrderIntent(bars[0].end,OrderIntent(bars[0].symbol,'BUY',100))])
    a=run_order_tape(prepared,tape)
    b=run_order_tape(prepared,tape)
    assert compare_backtests(a,b)['equal_within_compared_fields']
    changed=prepare_backtest(instruments,[replace(bars[0],volume=9000),*bars[1:]],rules)
    with pytest.raises(ValueError,match='[Ii]nput|market|Market'):
        run_order_tape(changed,tape)


def test_empty_tape_does_not_mark_unheld_stock_prices(monkeypatch):
    from engine import prepare_order_tape,run_order_tape
    from engine.backtest import _ResearchStrategy
    bars,rules=market(('600000.SH','600001.SH'))
    prepared=prepare_backtest([Instrument(symbol=s,exchange=Venue.SSE) for s in ('600000.SH','600001.SH')],bars,rules)
    seen=[];original=_ResearchStrategy._mark_price
    def counted(self,symbol,price,timestamp):
        seen.append(symbol)
        return original(self,symbol,price,timestamp)
    monkeypatch.setattr(_ResearchStrategy,'_mark_price',counted)
    result=run_order_tape(prepared,prepare_order_tape(prepared,[]))
    assert result.native_cash==100000 and len(result.daily_nav)==3
    assert not seen


def test_zero_volume_orders_with_late_gate_preserve_rejection_order():
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape
    symbols=('600000.SH','600001.SH')
    bars,rules=market(symbols)
    bars=[replace(b,volume=0) if b.end.date().day==7 else b for b in bars]
    prepared=prepare_backtest([Instrument(symbol=s,exchange=Venue.SSE) for s in symbols],bars,rules)
    entries=[TimedOrderIntent(bars[0].end,OrderIntent(s,'BUY',100,intent_id=s,
        earliest_execution=next(b.start for b in bars if b.end.date().day==7))) for s in reversed(symbols)]
    reference=run_prepared_backtest(prepared,portfolio_signal=lambda closed,state:[e.intent for e in entries if e.decision_time==closed[0].end])
    result=run_order_tape(prepared,prepare_order_tape(prepared,entries))
    assert compare_backtests(reference,result)['equal_within_compared_fields']
    assert len(result.rejections)==2


def test_last_session_tape_without_market_bar_remains_pending():
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape
    bars,rules=market()
    for b in bars[1:]:rules[b.symbol,b.end.date()]['trade_status']='SUSPENDED'
    prepared=prepare_backtest([Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)],
        bars[:1],rules,trading_days=[b.end.date() for b in bars])
    result=run_order_tape(prepared,prepare_order_tape(prepared,[
        TimedOrderIntent(bars[-1].end,OrderIntent(bars[0].symbol,'BUY',100,intent_id='last'))]))
    assert len(result.daily_nav)==3 and not result.fills
    assert result.pending_intents==[{'symbol':bars[0].symbol,'side':'BUY','quantity':100,
                                    'reason':'END_OF_DATA','intent_id':'last'}]


def test_mixed_daily_assets_preserve_fee_policies():
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape
    symbols=('600519.SH','110059.SH')
    bars,rules=market(symbols)
    prepared=prepare_backtest([Instrument(symbol=symbols[0],exchange=Venue.SSE),
        ConvertibleBondInstrument(symbol=symbols[1],exchange=Venue.SSE)],bars,rules)
    entries=[TimedOrderIntent(bars[0].end,OrderIntent(s,'BUY',100)) for s in symbols]
    reference=run_prepared_backtest(prepared,portfolio_signal=lambda closed,state:[e.intent for e in entries if e.decision_time==closed[0].end])
    result=run_order_tape(prepared,prepare_order_tape(prepared,entries))
    assert compare_backtests(reference,result)['equal_within_compared_fields']
    assert {f['symbol']:D(f['fee']) for f in result.fills}=={symbols[0]:D('5.01'),symbols[1]:D('1.01')}


def test_tape_custom_commission_cash_boundary():
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape,AShareFeeModel
    bars,rules=market()
    prepared=prepare_backtest([Instrument(symbol=bars[0].symbol,exchange=Venue.SSE)],bars,rules)
    tape=prepare_order_tape(prepared,[TimedOrderIntent(bars[0].end,OrderIntent(bars[0].symbol,'BUY',100))])
    result=run_order_tape(prepared,tape,initial_cash=D('1010.01'),fee_model=AShareFeeModel(min_commission=10))
    assert result.native_cash==result.ledger_cash==0
    assert len(result.fills)==1 and D(result.fills[0]['fee'])==D('10.01')


@pytest.mark.parametrize('index',[1,2,3])
def test_existing_daily_acceptance_freezes_reference_decisions(monkeypatch,index):
    from engine import TimedOrderIntent,prepare_order_tape,run_order_tape
    import test_institutional_scenarios as scenarios
    original=scenarios.run_backtest
    def replay_and_compare(instruments,bars,rules,signal=None,**options):
        entries=[]
        reference_options=dict(options)
        if signal is not None:
            def capture(bar,state):
                emitted=list(signal(bar,state) or [])
                entries.extend(TimedOrderIntent(bar.end,intent) for intent in emitted)
                return emitted
            reference_signal=capture
        else:
            callback=options['portfolio_signal']
            def capture(closed,state):
                emitted=list(callback(closed,state) or [])
                entries.extend(TimedOrderIntent(closed[0].end,intent) for intent in emitted)
                return emitted
            reference_options['portfolio_signal']=capture
            reference_signal=None
        reference=original(instruments,bars,rules,reference_signal,**reference_options)
        prepared=prepare_backtest(instruments,bars,rules,
            corporate_actions=options.get('corporate_actions',()),trading_days=options.get('trading_days'))
        tape_options={k:v for k,v in options.items() if k not in {'corporate_actions','trading_days','portfolio_signal','data_mode'}}
        candidate=run_order_tape(prepared,prepare_order_tape(prepared,entries),**tape_options)
        assert compare_backtests(reference,candidate)['equal_within_compared_fields']
        assert reference.pending_intents==candidate.pending_intents
        assert reference.account_journal==candidate.account_journal
        return candidate
    monkeypatch.setattr(scenarios,'run_backtest',replay_and_compare)
    names=[name for name in vars(scenarios) if name.startswith(f'test_scenario_{index}_')]
    getattr(scenarios,names[0])()

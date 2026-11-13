"""Cash-long A-share research backtests on the official NautilusTrader engine.

Opening quotes are explicit bar-level liquidity approximations, not an auction or
queue reconstruction. A closed bar can only create orders for a later bar open.
"""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.cache.config import CacheConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick
from nautilus_trader.model.enums import AccountType, AggregationSource, BarAggregation, BookType, OmsType, OrderSide, PriceType, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from contracts import CorporateAction, Fill, Instrument, OrderSide as LedgerSide, AssetClass, ConvertibleBondInstrument
from engine.accounting import AShareAccountModule, action_id
from engine.fee_model import AShareFeeModel, ConvertibleBondFeeModel, calculate_expected_fee
from engine.golden_ledger import GoldenLedger
from engine.order_gateway import AShareOrderGateway
from engine.split_guard import load_bridge
from engine.liquidity import BarLiquidity, LiquidityModule, synthetic_book
from engine.reproducibility import HistorySnapshot, build_manifest
from engine.intents import OrderIntent, validate_intents
from engine.snapshots import SnapshotMap
from engine.replay import BOUNDARY_CLIENT, build_events, session_boundary

TZ = ZoneInfo("Asia/Shanghai")
SIM = Venue("ASHARE")
D = Decimal


def nanos(t):
    return int(t.timestamp() * 1_000_000_000)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), TZ)


@dataclass(frozen=True)
class BacktestBar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class ResearchState:
    cash: Decimal
    positions: object
    sellable: object
    history: object
    as_of: datetime | None = None
    available_cash: Decimal = D(0)
    reserved_cash: Decimal = D(0)
    receivables: Decimal = D(0)
    nav: Decimal = D(0)
    valuation_prices: object = field(default_factory=dict)
    valuation_times: object = field(default_factory=dict)


@dataclass
class BacktestResult:
    native_cash: Decimal
    ledger_cash: Decimal
    positions: dict
    fills: list
    rejections: list
    daily_nav: list
    account_journal: list
    orders: list
    pending_intents: list
    manifest: dict = field(default_factory=dict)
    execution_model: str = "bar-synthetic-l2-v1: bar-derived synthetic L2 with native NT matching; next-bar-open IOC; DAY limits: open price improvement, resting limits at limit; O-H-L-C path; shared bar volume cap; inactive-session pending intents rejected; no observed L2/queue/auction reconstruction"
    schema_version: int = 2
    run_statistics: dict = field(default_factory=dict)

    def to_dict(self):
        from dataclasses import asdict
        def convert(x):
            if isinstance(x, Decimal): return str(x)
            if isinstance(x, dict): return {str(k): convert(v) for k,v in x.items()}
            if isinstance(x, (list,tuple)): return [convert(v) for v in x]
            return x
        return convert(asdict(self))


def _validate(instruments, bars, rules, actions, initial_cash, rate, trading_days):
    if not initial_cash.is_finite() or initial_cash <= 0 or not 0 < rate <= 1:
        raise ValueError("Invalid cash or participation_rate")
    master = {i.symbol:i for i in instruments}
    if not master or len(master) != len(instruments) or not bars:
        raise ValueError("Unique instruments and nonempty bars are required")
    for i in instruments:
        is_cb = getattr(i, 'asset_class', None) == AssetClass.CONVERTIBLE_BOND or isinstance(i, ConvertibleBondInstrument)
        if i.exchange.value not in {"SSE","SZSE"} or (i.asset_class.value not in {"EQUITY", "CONVERTIBLE_BOND"}):
            raise ValueError("Only Shanghai/Shenzhen cash equities and convertible bonds are supported")
        valid_ticks = {D('.001'), D('.01')} if is_cb else {D('.01')}
        if i.currency!='CNY' or i.tick_size not in valid_ticks or i.lot_size<=0:
            raise ValueError('CNY instruments with valid price tick and positive lot size required')
        if i.list_date and i.delist_date and i.list_date>i.delist_date:
            raise ValueError(f'Invalid security lifecycle: {i.symbol}')
    # Validate before astimezone or sorting can assume a timezone implicitly.
    for b in bars:
        if b.symbol not in master:
            raise ValueError(f'Unknown bar symbol: {b.symbol}')
        for field in ('start','end'):
            value=getattr(b,field)
            if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f'Explicit timezone required: {b.symbol} {field}')
        inst=master[b.symbol];day=b.end.astimezone(TZ).date()
        if (inst.list_date and day<inst.list_date) or (inst.delist_date and day>inst.delist_date):
            raise ValueError(f'Bar outside security lifecycle: {b.symbol} {day}')
    bar_days={b.end.astimezone(TZ).date() for b in bars}
    first,last=min(bar_days),max(bar_days)
    sessions = sorted(set(trading_days) if trading_days is not None else {d for s,d in rules if s in master and first<=d<=last} | bar_days)
    if not sessions or not bar_days.issubset(sessions):raise ValueError('Bars outside explicit trading calendar')
    session_set=set(sessions)
    for day in sessions:
        if day.weekday() >= 5: raise ValueError("Invalid exchange session date")
        for symbol, inst in master.items():
            r = rules.get((symbol,day))
            if (inst.list_date and day<inst.list_date) or (inst.delist_date and day>inst.delist_date):
                if isinstance(r,Mapping) and r.get('trade_status')=='OPEN':
                    raise ValueError(f'OPEN rule outside security lifecycle: {symbol} {day}')
                continue
            if not isinstance(r,Mapping): raise ValueError(f"Missing daily rule: {symbol} {day}")
            if r.get('trade_status') not in {'OPEN','SUSPENDED','DAY_BLOCKED'}:
                raise ValueError(f"Invalid daily rule status: {symbol} {day}")
            for key in ('buy_min','buy_step','sell_min','sell_step','max_order'):
                if not isinstance(r.get(key),int) or r[key] <= 0:
                    raise ValueError(f"Invalid daily rule {key}: {symbol} {day}")
            if r.get('limit_state')=='BOUNDED':
                lo,hi=D(str(r.get('down_limit'))),D(str(r.get('up_limit')))
                if not lo.is_finite() or not hi.is_finite() or not 0 < lo < hi:
                    raise ValueError("Invalid price band rule")
            elif r.get('limit_state')!='UNBOUNDED' and r.get('trade_status')=='OPEN':
                raise ValueError("Missing executable price band rule")
    previous={}; actual=set()
    for b in sorted(bars,key=lambda x:(x.start,x.symbol)):
        start,end=b.start.astimezone(TZ),b.end.astimezone(TZ)
        if start.date()!=end.date() or not at(start.date(),9,30)<=start<end<=at(start.date(),15):
            raise ValueError("Bar is outside the supported session")
        daily=start==at(start.date(),9,30) and end==at(start.date(),15)
        if not daily and not (end<=at(start.date(),11,30) or start>=at(start.date(),13)):
            raise ValueError("Intraday bar crosses the lunch break")
        if not daily and (end-start).total_seconds()%60:
            raise ValueError("Only daily or whole-minute bars are supported")
        if b.symbol in previous and start<previous[b.symbol]: raise ValueError("Overlapping bars")
        previous[b.symbol]=end; actual.add((b.symbol,end.date()))
        values=[D(str(v)) for v in (b.open,b.high,b.low,b.close)]
        if any(not v.is_finite() or v<=0 for v in values) or not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]:
            raise ValueError("Invalid OHLC")
        tick=master[b.symbol].tick_size
        if any(v%tick for v in values): raise ValueError("OHLC violates price tick")
        if not isinstance(b.volume,int) or b.volume<0: raise ValueError("Invalid share volume")
        r=rules[(b.symbol,end.date())]
        if r['trade_status']=='OPEN' and r['limit_state']=='BOUNDED' and not D(str(r['down_limit']))<=values[2]<=values[1]<=D(str(r['up_limit'])):
            raise ValueError('OHLC violates daily price band')
    for (symbol,day),r in rules.items():
        if day in session_set and symbol in master and r['trade_status']=='OPEN' and (symbol,day) not in actual:
            raise ValueError(f"Missing market bar for open instrument: {symbol} {day}")
    for a in actions:
        if a.symbol not in master or a.cash_div < 0 or a.bonus_ratio < 0 or a.reserve_ratio < 0:
            raise ValueError("Invalid corporate action")
        if a.record_date and a.record_date>=a.ex_date: raise ValueError("Invalid corporate action record date")
        if a.cash_div and not a.record_date:raise ValueError('Cash dividend requires record date')
        if a.pay_date and a.pay_date<a.ex_date: raise ValueError("Payment predates ex-date")
        if a.share_listing_date and a.share_listing_date<a.ex_date: raise ValueError("Listing predates ex-date")
        if a.fractional_policy not in {'floor','reject','cash_in_lieu'}: raise ValueError("Invalid fractional policy")
    keys=[action_id(a) for a in actions]
    if len(keys)!=len(set(keys)): raise ValueError("Duplicate/conflicting corporate action")
    return master,sessions


class _ResearchStrategy(Strategy):
    def __init__(self, bars, rules, master, sessions, actions, signal, initial_cash, rate, native, fee_model, cash_module, liquidity, portfolio_signal=None):
        super().__init__(StrategyConfig())
        self.bars=bars;self.rules=rules;self.master=master;self.sessions=sessions;self.actions=actions
        self.signal=signal;self.rate=rate;self.native=native;self.fee_model=fee_model;self.cash_module=cash_module
        self.liquidity=liquidity
        self.buy_reservations={}
        self.liquidity.affordable=self._affordable_quantity
        t0_symbols={s for s in master if self._is_cb(s)}
        self.ledger=GoldenLedger(initial_cash,t0_symbols);self.gateway=AShareOrderGateway(t0_symbols)
        self.portfolio_signal=portfolio_signal
        self.open_quotes={};self.closed_groups=defaultdict(list);self.group_sizes=defaultdict(int)
        for b in bars:self.group_sizes[nanos(b.end)]+=1
        self.history=defaultdict(list);self.pending=[];self.last_close={};self.current_day=None;self.closed=set()
        self.valuation_times={};self.seen_intent_ids=set();self.order_intent_ids={}
        self._state_positions=SnapshotMap();self._state_sellable=SnapshotMap()
        self._state_history=SnapshotMap();self._state_prices=SnapshotMap();self._state_times=SnapshotMap()
        self._marked_quantities={};self._market_value=D(0)
        for symbol in master:self._state_sellable.update(symbol,D(0))
        self.fills=[];self.rejections=[];self.nav=[];self.fee_values={};self.native_receivables={}
        self.lookup=defaultdict(dict)
        for b in bars:self.lookup[native[b.symbol].id][nanos(b.end)]=b
        self.by_id={str(i.id):s for s,i in native.items()}
        self.by_day=defaultdict(list)
        self.precisions={s: (3 if inst.tick_size < D('.01') else 2) for s, inst in master.items()}
        for b in bars:self.by_day[b.end.astimezone(TZ).date()].append(b)
        self.ex_actions=defaultdict(list);self.pay_actions=defaultdict(list);self.record_actions=defaultdict(list)
        for a in actions:
            self.ex_actions[a.ex_date].append(a)
            self.pay_actions[a.pay_date or a.ex_date].append(a)
            if a.record_date:self.record_actions[a.record_date].append(a)
            self.ledger.register_corporate_action(a)
            if a.record_date and a.record_date<sessions[0]:
                self.ledger.dividend_entitlements[f"{a.symbol}_{a.ex_date}"]=D(0)
                self.cash_module.record_entitlement(a,D(0))

    def _is_cb(self, symbol: str) -> bool:
        inst = self.master.get(symbol)
        if inst is None: return False
        return getattr(inst, 'asset_class', None) == AssetClass.CONVERTIBLE_BOND or isinstance(inst, ConvertibleBondInstrument)

    def on_start(self):
        for ts in sorted({nanos(b.start) for b in self.bars}):
            self.clock.set_time_alert_ns(f'execute_{ts}',ts+2,callback=self._execute_open)
            self.clock.set_time_alert_ns(f'buy_{ts}',ts+3,callback=self._execute_buys)
        subscribed=set()
        for b in self.bars:
            inst=self.native[b.symbol]
            daily=b.start.astimezone(TZ)==at(b.start.astimezone(TZ).date(),9,30) and b.end.astimezone(TZ)==at(b.end.astimezone(TZ).date(),15)
            duration='DAY' if daily else int((b.end-b.start).total_seconds()/60)
            if (b.symbol,duration) in subscribed:continue
            subscribed.add((b.symbol,duration))
            spec=BarSpecification(1,BarAggregation.DAY,PriceType.LAST) if daily else BarSpecification(duration,BarAggregation.MINUTE,PriceType.LAST)
            bt=BarType(inst.id,spec,AggregationSource.EXTERNAL)
            self.subscribe_bars(bt)
        for inst in self.native.values():self.subscribe_quote_ticks(inst.id)
        days=set(self.sessions)|{a.ex_date for a in self.actions}|{a.pay_date for a in self.actions if a.pay_date}
        for day in sorted(days):
            if self.sessions[0]<=day<=self.sessions[-1]:
                self.clock.set_time_alert_ns(f"start_{day}",nanos(at(day,9)),callback=self._start_day)
        for day in self.sessions:
            self.clock.set_time_alert_ns(f"close_{day}",nanos(at(day,15)),callback=self._close_day)
        for n,b in enumerate(self.bars):
            if b.volume==0 or self.rules[(b.symbol,b.end.astimezone(TZ).date())]['trade_status']!='OPEN':
                self.clock.set_time_alert_ns(f"inactive_{n}",nanos(b.end),callback=lambda e,b=b:self._inactive_bar(b))

    def _native_quantity(self,symbol):
        return D(str(self.portfolio.net_position(self.native[symbol].id)))

    def _reject_intent(self,intent,reason,timestamp):
        row={'symbol':intent.symbol,'reason':reason,'time':timestamp}
        if intent.intent_id is not None:row['intent_id']=intent.intent_id
        self.rejections.append(row)

    def _mark_price(self,symbol,price,timestamp):
        self._market_value+=self._marked_quantities.get(symbol,D(0))*(price-self.last_close.get(symbol,D(0)))
        self.last_close[symbol]=price;self.valuation_times[symbol]=timestamp
        self._state_prices.update(symbol,price);self._state_times.update(symbol,timestamp)

    def _sync_account_symbol(self,symbol):
        if symbol in self.ledger.positions:
            quantity=self.ledger.positions[symbol]
            self._market_value+=(quantity-self._marked_quantities.get(symbol,D(0)))*self.last_close.get(symbol,D(0))
            self._marked_quantities[symbol]=quantity;self._state_positions.update(symbol,quantity)
        self._state_sellable.update(symbol,self.gateway.get_sellable_qty(symbol))

    def _start_day(self,event):
        day=datetime.fromtimestamp(event.ts_event/1e9,TZ).date()
        if day in self.sessions:
            self.current_day=day;self.gateway.on_trade_day_start(str(day));self.ledger.on_trade_day_start(str(day))
            self.fee_model.set_trade_date(str(day))
            remaining=[]
            for created,intent in self.pending:
                status=self.rules.get((intent.symbol,day),{}).get('trade_status','DAY_BLOCKED')
                if created<event.ts_event and status!='OPEN':
                    self._reject_intent(intent,status,event.ts_event)
                else:remaining.append((created,intent))
            self.pending=remaining
        for a in self.ex_actions.get(day, ()):
            if a.ex_date==day:
                ratio=D(1)+a.bonus_ratio+a.reserve_ratio
                if ratio>1:
                    qty=self._native_quantity(a.symbol);fraction=qty*ratio-(qty*ratio).to_integral_value(rounding='ROUND_FLOOR')
                    if fraction and a.fractional_policy=='floor':
                        raise ValueError("Strict backtest requires reject or cash_in_lieu for fractional account entitlement")
                    for pos in self.cache.positions_open(instrument_id=self.native[a.symbol].id):
                        load_bridge().apply_split(pos,ratio,event.ts_event,self.native[a.symbol],fractional_policy='cash_in_lieu' if a.fractional_policy=='cash_in_lieu' else 'reject')
                    self.gateway.apply_stock_split(a.symbol,ratio,share_listing_date=a.share_listing_date,trade_date=day)
                    self.portfolio.initialize_positions()
                    if fraction:
                        px=a.fractional_cash_price
                        if px is None: px=self.last_close[a.symbol]/ratio
                        self.cash_module.adjust_cash(action_id(a)+'|fraction',fraction*px)
                qty=self.cash_module.entitlements.get(action_id(a))
                if a.cash_div and qty is None: raise ValueError("Missing native record snapshot")
                if a.cash_div:self.native_receivables[action_id(a)]=(qty*a.cash_div).quantize(D('.01'),rounding=ROUND_HALF_UP)
                if a.symbol in self.last_close:
                    adjusted=(self.last_close[a.symbol]-a.cash_div)/ratio
                    if adjusted<=0:raise ValueError('Invalid ex-date valuation reference')
                    self._mark_price(a.symbol,adjusted,datetime.fromtimestamp(event.ts_event/1e9,TZ))
        self.ledger.process_corporate_actions_for_date(str(day))
        for a in self.pay_actions.get(day, ()):
            if (a.pay_date or a.ex_date)==day and a.cash_div:
                self.cash_module.pay_dividend(a,str(day));self.native_receivables.pop(action_id(a),None)
        for symbol in self.ledger.positions:self._sync_account_symbol(symbol)

    def _state(self, as_of=None):
        reserved=sum((r['amount'] for r in self.buy_reservations.values()),D(0))
        receivables=sum(self.ledger.receivable_dividends.values(),D(0))
        nav=self.ledger.cash+receivables+self._market_value
        return ResearchState(self.ledger.cash,self._state_positions.snapshot(),
                             self._state_sellable.snapshot(),self._state_history.snapshot(),
                             as_of,self.ledger.cash-reserved,reserved,receivables,nav,
                             self._state_prices.snapshot(),self._state_times.snapshot())

    def _signal(self,b,update_price=True):
        if update_price:
            self._mark_price(b.symbol,D(str(b.close)),b.end)
        self.history[b.symbol].append(b)
        self._state_history.update(b.symbol,HistorySnapshot(self.history[b.symbol]))
        if self.portfolio_signal is not None:
            ts=nanos(b.end);self.closed_groups[ts].append(b)
            if len(self.closed_groups[ts]) < self.group_sizes[ts]:return
            closed=tuple(sorted(self.closed_groups.pop(ts),key=lambda item:item.symbol))
            intents=self.portfolio_signal(closed,self._state(b.end)) or []
        else:
            intents=self.signal(b,self._state(b.end)) or []
        self._queue_intents(intents,b.end)

    def _queue_intents(self,intents,decision_time):
        for i in validate_intents(intents,self.master,decision_time,self.seen_intent_ids):
            earliest=max(nanos(decision_time),nanos(i.earliest_execution)) if i.earliest_execution else nanos(decision_time)
            self.pending.append((earliest,i))

    def _inactive_bar(self,b):
        day=b.end.astimezone(TZ).date()
        reason='ZERO_VOLUME' if b.volume==0 and self.rules[(b.symbol,day)]['trade_status']=='OPEN' else self.rules[(b.symbol,day)]['trade_status']
        remaining=[]
        for created,intent in self.pending:
            if intent.symbol==b.symbol and created<=nanos(b.start):
                self._reject_intent(intent,reason,nanos(b.end))
            else:remaining.append((created,intent))
        self.pending=remaining
        if b not in self.history[b.symbol]:self._signal(b,update_price=self.rules[(b.symbol,day)]['trade_status']=='OPEN')

    def on_bar(self,bar):
        self._signal(self.lookup[bar.bar_type.instrument_id][bar.ts_event])

    def on_quote_tick(self,tick):
        self.open_quotes[str(tick.instrument_id)]=tick

    def _execute_open(self,event):
        if not self.pending:return
        ticks=sorted((tick for tick in self.open_quotes.values() if tick.ts_event==event.ts_event-2),key=lambda tick:str(tick.instrument_id))
        for tick in ticks:self._submit_open(tick,'SELL')

    def _execute_buys(self,event):
        if not self.pending:return
        ticks=sorted((tick for tick in self.open_quotes.values() if tick.ts_event==event.ts_event-3),key=lambda tick:str(tick.instrument_id))
        for tick in ticks:self._submit_open(tick,'BUY')

    def _expected_fee(self, side, qty, price, *, symbol, day, before=D(0)):
        return calculate_expected_fee(side,qty,price,is_new_order=before==0,trade_date=str(day),
            cum_trade_val_before=before,rates=self.fee_model.rates_for(symbol,str(day)))

    def _submit_open(self,tick,phase):
        if not self.pending:return
        symbol=self.by_id[str(tick.instrument_id)];day=datetime.fromtimestamp(tick.ts_event/1e9,TZ).date()
        previous_volume=next((b.volume for b in reversed(self.history[symbol]) if b.volume>0),0)
        budget_key=(symbol,tick.ts_event)
        if not hasattr(self,'submission_budget'):self.submission_budget={}
        available=self.submission_budget.setdefault(budget_key,int(D(previous_volume)*D(str(self.rate))))
        remaining=[]
        for created,intent in self.pending:
            if intent.symbol!=symbol or created>tick.ts_event or intent.side.upper()!=phase:
                remaining.append((created,intent));continue
            if intent.quantity>available:
                self._reject_intent(intent,'PARTICIPATION_LIMIT',tick.ts_init);continue
            side=OrderSide.BUY if intent.side.upper()=='BUY' else OrderSide.SELL
            # IOC at the observed open avoids NT market-order empty-book fallback.
            args=dict(instrument_id=tick.instrument_id,order_side=side,quantity=Quantity.from_int(intent.quantity),time_in_force=TimeInForce.IOC if intent.limit_price is None else TimeInForce.DAY)
            price=tick.ask_price if side==OrderSide.BUY else tick.bid_price
            if intent.limit_price is None:price=self.liquidity.execution_price(price,side,self.native[symbol])
            order=self.order_factory.limit(**args,price=price if intent.limit_price is None else Price.from_str(format(intent.limit_price,f".{self.precisions[symbol]}f")))
            if side==OrderSide.BUY:
                cost=D(str(order.price))*intent.quantity
                cost+=self._expected_fee('BUY',D(intent.quantity),D(str(order.price)),symbol=symbol,day=day)
                if cost>self.ledger.cash-sum((r['amount'] for r in self.buy_reservations.values()),D(0)):
                    self._reject_intent(intent,'INSUFFICIENT_CASH_WITH_FEES',tick.ts_init);continue
            r=self.gateway.validate_and_reserve(order,self.rules[(symbol,day)],{'close':tick.ask_price.as_double(),'volume':max(available,1),'execution_ts':tick.ts_init},tick.ask_price.as_double() if side==OrderSide.BUY else tick.bid_price.as_double())
            if r.is_valid:
                self._sync_account_symbol(symbol)
                if side==OrderSide.BUY:self.buy_reservations[str(order.client_order_id)]={'amount':cost,'quantity':D(intent.quantity),'price':D(str(order.price))}
                if intent.intent_id is not None:self.order_intent_ids[str(order.client_order_id)]=intent.intent_id
                self.submit_order(order);available-=intent.quantity
            else:self._reject_intent(intent,r.rejection_reason,tick.ts_init)
        self.pending=remaining
        self.submission_budget[budget_key]=available

    def _affordable_quantity(self,order,price,maximum):
        key=str(order.client_order_id);px=D(str(price))
        symbol=self.by_id[str(order.instrument_id)]
        cash=self.ledger.cash-sum((r['amount'] for k,r in self.buy_reservations.items() if k!=key),D(0))
        before=self.fee_values.get(key,D(0));lo,hi=0,maximum
        while lo<hi:
            qty=(lo+hi+1)//2
            fee=self._expected_fee('BUY',D(qty),px,symbol=symbol,day=self.current_day,before=before)
            if D(qty)*px+fee<=cash:lo=qty
            else:hi=qty-1
        return lo

    def on_order_filled(self,e):
        if str(e.trade_id) in self.gateway._processed_trade_ids:return
        self.liquidity.consume(e.instrument_id,e.last_qty)
        symbol=self.by_id[str(e.instrument_id)];day=self.gateway.on_order_filled(e)
        qty=D(str(e.last_qty));px=D(str(e.last_px));fee=D(str(e.commission.as_double()))
        valued_at=datetime.fromtimestamp(e.ts_event/1e9,TZ)
        if symbol not in self.valuation_times or self.valuation_times[symbol]<valued_at:
            self._mark_price(symbol,px,valued_at)
        key=str(e.client_order_id);before=self.fee_values.get(key,D(0))
        expected=self._expected_fee(e.order_side.name,qty,px,symbol=symbol,day=day,before=before)
        if fee!=expected:raise RuntimeError(f"Native fee mismatch: {fee} != {expected}")
        self.fee_values[key]=before+qty*px
        self.ledger.apply_fill(Fill(fill_id=str(e.trade_id),symbol=symbol,side=LedgerSide(e.order_side.name),quantity=qty,price=px,fee=fee,timestamp=datetime.fromtimestamp(e.ts_event/1e9,TZ)),day)
        self._sync_account_symbol(symbol)
        if key in self.buy_reservations:
            reserve=self.buy_reservations[key];reserve['quantity']-=qty
            if reserve['quantity']==0:self.buy_reservations.pop(key)
            else:
                reserve['amount']=reserve['quantity']*reserve['price']+self._expected_fee('BUY',reserve['quantity'],reserve['price'],symbol=symbol,day=day,before=self.fee_values[key])
        self.fills.append({'symbol':symbol,'side':e.order_side.name,'quantity':str(qty),'price':str(px),'fee':str(fee),'time':e.ts_event,'order_id':key})
        if key in self.order_intent_ids:self.fills[-1]['intent_id']=self.order_intent_ids[key]

    def on_order_canceled(self,e):
        self.gateway.on_order_canceled(str(e.client_order_id));self.buy_reservations.pop(str(e.client_order_id),None)
        self._sync_account_symbol(self.by_id[str(e.instrument_id)])
    def on_order_expired(self,e):
        self.gateway.on_order_expired(str(e.client_order_id));self.buy_reservations.pop(str(e.client_order_id),None)
        self._sync_account_symbol(self.by_id[str(e.instrument_id)])
    def on_order_rejected(self,e):
        self.buy_reservations.pop(str(e.client_order_id),None)
        self.gateway.on_order_rejected(str(e.client_order_id));self.rejections.append({'symbol':self.by_id[str(e.instrument_id)],'reason':str(e.reason),'time':e.ts_event})
        self._sync_account_symbol(self.by_id[str(e.instrument_id)])
        if str(e.client_order_id) in self.order_intent_ids:self.rejections[-1]['intent_id']=self.order_intent_ids[str(e.client_order_id)]
    def on_order_denied(self,e):
        self.buy_reservations.pop(str(e.client_order_id),None)
        self.gateway.on_order_denied(str(e.client_order_id));self.rejections.append({'symbol':self.by_id[str(e.instrument_id)],'reason':str(e.reason),'time':e.ts_event})
        self._sync_account_symbol(self.by_id[str(e.instrument_id)])
        if str(e.client_order_id) in self.order_intent_ids:self.rejections[-1]['intent_id']=self.order_intent_ids[str(e.client_order_id)]

    def _close_day(self,event):
        day=datetime.fromtimestamp(event.ts_event/1e9,TZ).date()
        if day in self.closed:return
        for inst in self.native.values():self.cancel_all_orders(inst.id)
        closing_symbols=set(self.gateway._order_symbol.values())
        self.gateway.on_session_close()
        for symbol in closing_symbols:self._sync_account_symbol(symbol)
        for b in self.by_day[day]:
            if (b.volume==0 or self.rules[(b.symbol,day)]['trade_status']!='OPEN') and b not in self.history[b.symbol]:self._inactive_bar(b)
        remaining=[]
        for eligible,intent in self.pending:
            status=self.rules.get((intent.symbol,day),{}).get('trade_status','DAY_BLOCKED')
            # A close-time decision belongs to a later bar, not this session.
            if eligible<event.ts_event and status!='OPEN':self._reject_intent(intent,status,event.ts_event)
            else:remaining.append((eligible,intent))
        self.pending=remaining
        prices={}
        for symbol,qty in self.ledger.positions.items():
            if qty:
                if symbol not in self.last_close:raise ValueError(f"Missing valuation price {symbol}")
                prices[symbol]=self.last_close[symbol]
        # Also snapshot known non-held symbols, needed for record-day and later split valuation.
        prices.update(self.last_close)
        nav=self.ledger.mark_to_market(str(day),prices)
        cached_positions=self._state_positions.snapshot();cached_sellable=self._state_sellable.snapshot()
        if abs(self._market_value-nav.positions_value)>D('.000001') or any(
                cached_positions.get(s,D(0))!=q or cached_sellable[s]!=self.gateway.get_sellable_qty(s)
                for s,q in self.ledger.positions.items()):
            raise RuntimeError(f'Incremental account snapshot mismatch on {day}')
        for a in self.record_actions.get(day, ()):
            if a.record_date==day:self.cash_module.record_entitlement(a,self._native_quantity(a.symbol))
        native_cash=D(str(self.portfolio.account(SIM).balance_total(CNY).as_double()))
        native_positions={s:self._native_quantity(s) for s in self.master}
        positions_match=all(native_positions[s]==self.ledger.positions.get(s,D(0))==self.gateway.get_total_position(s) for s in self.master)
        cash_matches=native_cash==self.ledger.cash
        native_equity=native_cash+sum(q*prices[s] for s,q in native_positions.items() if q)+sum(self.native_receivables.values(),D(0))
        if not positions_match or not cash_matches or abs(native_equity-nav.total_equity)>D('.000001'):
            raise RuntimeError(f"Daily reconciliation failed on {day}: cash={cash_matches}, positions={positions_match}")
        row=nav.model_dump(mode='json');row.update(native_cash=str(native_cash),native_equity=str(native_equity),cash_matches=cash_matches,positions_match=positions_match)
        self.nav.append(row);self.closed.add(day)


def _run_backtest(instruments, bars, rules, signal=None, *, corporate_actions=(), initial_cash=D(100000), participation_rate=.01, trading_days=None, fee_model=None, portfolio_signal=None, slippage_bps=0, strategy_id=None, strategy_params=None, data_snapshot_id=None, data_mode='full', _prepared=None, _tape=None):
    """Run one isolated, shared-cash portfolio. Rules keyed by (symbol, local date)."""
    initial_cash=D(str(initial_cash))
    if _prepared is None:
        instruments=list(instruments);bars=list(bars);actions=list(corporate_actions)
        master,sessions=_validate(instruments,bars,rules,actions,initial_cash,participation_rate,trading_days)
    else:
        if not initial_cash.is_finite() or initial_cash<=0 or not 0<participation_rate<=1:
            raise ValueError('Invalid cash or participation_rate')
        master,sessions=_prepared.master,_prepared.sessions
        actions=_prepared.actions
    if (signal is None)==(portfolio_signal is None):raise ValueError('Provide exactly one of signal or portfolio_signal')
    slippage_bps=D(str(slippage_bps))
    if not slippage_bps.is_finite() or not 0<=slippage_bps<10000:raise ValueError('Invalid slippage_bps')
    if data_mode not in {'full','session','sparse','orders'}:raise ValueError('Invalid data_mode')
    if data_mode=='orders' and (_prepared is None or _tape is None):raise ValueError('Use run_order_tape for orders delivery')
    if data_mode=='sparse' and any(b.start.astimezone(TZ)!=at(b.start.astimezone(TZ).date(),9,30) or b.end.astimezone(TZ)!=at(b.end.astimezone(TZ).date(),15) for b in bars):
        raise ValueError('Sparse delivery requires daily bars')
    cb_symbols = {s for s, i in master.items() if getattr(i, 'asset_class', None) == AssetClass.CONVERTIBLE_BOND or isinstance(i, ConvertibleBondInstrument)}
    cash_module=AShareAccountModule()
    if fee_model is None:
        if len(cb_symbols) == len(master) and len(cb_symbols) > 0:
            fee = ConvertibleBondFeeModel(scope_id='isolated_run')
        else:
            fee = AShareFeeModel(scope_id='isolated_run', cb_symbols=cb_symbols)
    else:
        fee = fee_model
        if hasattr(fee, "cb_symbols"):
            fee.cb_symbols=set(cb_symbols)
    if not isinstance(fee,AShareFeeModel):raise ValueError('fee_model must expose AShareFeeModel configuration for independent reconciliation')
    if isinstance(fee,ConvertibleBondFeeModel) and len(cb_symbols)!=len(master):raise ValueError('ConvertibleBondFeeModel requires a bond-only portfolio')
    fee.set_scope('isolated_run')
    manifest=build_manifest(instruments,bars,rules,actions,sessions,fee,portfolio_signal or signal,
        initial_cash=initial_cash,participation_rate=participation_rate,slippage_bps=slippage_bps,
        strategy_id=strategy_id,strategy_params=strategy_params,data_snapshot_id=data_snapshot_id,
        _input_sha256=_prepared.input_sha256 if _prepared is not None else None,_data_delivery=data_mode,
        _order_tape_sha256=_tape.sha256 if _tape is not None else None)
    liquidity=BarLiquidity(slippage_bps)
    # Complete research history lives in our immutable callback snapshots.
    eng=BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId('RESEARCH-001'),logging=LoggingConfig(log_level='ERROR'),cache=CacheConfig(bar_capacity=2,tick_capacity=2)))
    try:
        eng.add_venue(venue=SIM,oms_type=OmsType.NETTING,account_type=AccountType.CASH,base_currency=CNY,starting_balances=[Money(float(initial_cash),CNY)],fee_model=fee,fill_model=liquidity,book_type=BookType.L2_MBP,liquidity_consumption=True,modules=[cash_module,LiquidityModule(liquidity)])
        native={}
        for symbol,i in master.items():
            prec = 3 if i.tick_size < D('.01') else 2
            inc_str = '.001' if prec == 3 else '.01'
            native[symbol]=Equity(instrument_id=InstrumentId(Symbol(symbol),SIM),raw_symbol=Symbol(symbol.split('.')[0]),currency=CNY,price_precision=prec,price_increment=Price.from_str(inc_str),lot_size=Quantity.from_int(i.lot_size),ts_event=nanos(at(sessions[0],0)),ts_init=nanos(at(sessions[0],0)))
            eng.add_instrument(native[symbol])
        if _tape is None:
            strategy=_ResearchStrategy(bars,rules,master,sessions,actions,signal,initial_cash,participation_rate,native,fee,cash_module,liquidity,portfolio_signal)
        else:
            from engine.tape_replay import _TapeStrategy
            strategy=_TapeStrategy(_prepared,_tape,signal,initial_cash,participation_rate,native,fee,cash_module,liquidity)
        eng.add_strategy(strategy)
        bar_types={};statistics=dict(data_mode=data_mode,market_events=0,boundary_events=0,peak_batch_events=0)
        if data_mode=='full':
            data=build_events(bars,rules,native,participation_rate,liquidity,bar_types)
            statistics.update(market_events=len(data),peak_batch_events=len(data))
            if data:eng.add_data(data)
            eng.run(start=at(sessions[0],0),end=at(sessions[-1],15))
        else:
            for day in sessions:
                active={i.symbol for eligible,i in strategy.pending if eligible<=nanos(at(day,9,30))} if data_mode in {'sparse','orders'} else None
                day_bars=strategy.by_day[day]
                if data_mode=='orders':
                    selected=active|{s for s,q in strategy.ledger.positions.items() if q}
                    day_bars=[b for b in day_bars if b.symbol in selected]
                    strategy.current_bars={b.symbol:b for b in day_bars}
                    statistics['native_market_bars']=statistics.get('native_market_bars',0)+sum(b.volume>0 and rules[b.symbol,day]['trade_status']=='OPEN' for b in day_bars)
                data=build_events(day_bars,rules,native,participation_rate,liquidity,bar_types,active)
                statistics['market_events']+=len(data)
                statistics['peak_batch_events']=max(statistics['peak_batch_events'],len(data)+1)
                if data:eng.add_data(data)
                eng.add_data([session_boundary(nanos(at(day,15))+1)],client_id=BOUNDARY_CLIENT)
                statistics['boundary_events']+=1
                eng.run(start=at(sessions[0],0),end=at(sessions[-1],15)+timedelta(microseconds=1),streaming=True)
                eng.clear_data()
                liquidity.events.clear();liquidity.remaining.clear();liquidity.active.clear();liquidity.intrabar.clear()
            eng.end()
        if len(strategy.nav)!=len(sessions):raise RuntimeError("Missing daily reconciliation")
        native_cash=D(str(eng.portfolio.account(SIM).balance_total(CNY).as_double()))
        orders=[{'id':str(o.client_order_id),'status':o.status.name,'quantity':str(o.quantity),'filled_quantity':str(o.filled_qty),
                 **({'intent_id':strategy.order_intent_ids[str(o.client_order_id)]} if str(o.client_order_id) in strategy.order_intent_ids else {})} for o in eng.cache.orders()]
        pending=[{'symbol':i.symbol,'side':i.side,'quantity':i.quantity,'reason':'END_OF_DATA',
                  **({'intent_id':i.intent_id} if i.intent_id is not None else {}),
                  **({'earliest_execution':i.earliest_execution.isoformat()} if i.earliest_execution else {})} for _,i in strategy.pending]
        result=BacktestResult(native_cash,strategy.ledger.cash,{s:strategy.ledger.positions.get(s,D(0)) for s in master},strategy.fills,strategy.rejections,strategy.nav,list(cash_module.journal),orders,pending,manifest)
        result.run_statistics=statistics
        if _tape is not None:statistics.update(input_bars=len(bars),scheduled_intents=len(_tape.entries))
        return result
    finally:
        try:
            # Kernel disposal stops other engines, but not a still-running trader.
            # Do not end() a failed run: it flushes future timers and callbacks.
            if eng.trader.is_running:eng.trader.stop()
        finally:eng.dispose()


def run_backtest(instruments, bars, rules, signal=None, *, corporate_actions=(), initial_cash=D(100000), participation_rate=.01, trading_days=None, fee_model=None, portfolio_signal=None, slippage_bps=0, strategy_id=None, strategy_params=None, data_snapshot_id=None, data_mode='full'):
    """Validate raw inputs and run one isolated shared-cash portfolio."""
    return _run_backtest(instruments,bars,rules,signal,corporate_actions=corporate_actions,
        initial_cash=initial_cash,participation_rate=participation_rate,trading_days=trading_days,
        fee_model=fee_model,portfolio_signal=portfolio_signal,slippage_bps=slippage_bps,
        strategy_id=strategy_id,strategy_params=strategy_params,data_snapshot_id=data_snapshot_id,data_mode=data_mode)


def run_prepared_backtest(prepared, signal=None, **run_options):
    """Reuse validated market inputs, always constructing fresh runtime accounts."""
    from engine.prepared import PreparedBacktest
    if type(prepared) is not PreparedBacktest:raise TypeError('PreparedBacktest required')
    prepared.assert_compatible()
    return _run_backtest(prepared.instruments,prepared.bars,prepared.rules,signal,
        corporate_actions=prepared.actions,trading_days=prepared.sessions,_prepared=prepared,**run_options)

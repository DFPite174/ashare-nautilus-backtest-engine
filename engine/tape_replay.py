"""Daily execution-only replay using the shared NT account and order machinery."""
from datetime import datetime
from decimal import Decimal

from nautilus_trader.model.data import BarSpecification, BarType
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType

from engine.backtest import _ResearchStrategy, _run_backtest, TZ, at, nanos
from engine.prepared import PreparedBacktest


def _no_signals(bar,state):
    return []


class _TapeStrategy(_ResearchStrategy):
    def __init__(self,prepared,tape,signal,initial_cash,rate,native,fee,cash_module,liquidity):
        super().__init__((),prepared.rules,prepared.master,prepared.sessions,prepared.actions,
                         signal,initial_cash,rate,native,fee,cash_module,liquidity)
        self.by_day=prepared.bars_by_day
        self.tape=tape;self.current_bars={}

    def on_start(self):
        for day in self.sessions:
            timestamp=nanos(at(day,9,30))
            self.clock.set_time_alert_ns(f'execute_{timestamp}',timestamp+2,callback=self._execute_open)
            self.clock.set_time_alert_ns(f'buy_{timestamp}',timestamp+3,callback=self._execute_buys)
            self.clock.set_time_alert_ns(f'close_{day}',nanos(at(day,15)),callback=self._close_day)
        for inst in self.native.values():
            self.subscribe_bars(BarType(inst.id,BarSpecification(1,BarAggregation.DAY,PriceType.LAST),AggregationSource.EXTERNAL))
            self.subscribe_quote_ticks(inst.id)
        days=set(self.sessions)|{a.ex_date for a in self.actions}|{a.pay_date for a in self.actions if a.pay_date}
        for day in sorted(days):
            if self.sessions[0]<=day<=self.sessions[-1]:
                self.clock.set_time_alert_ns(f'start_{day}',nanos(at(day,9)),callback=self._start_day)

    def on_bar(self,bar):
        symbol=self.by_id[str(bar.bar_type.instrument_id)]
        raw=self.current_bars[symbol]
        self._mark_price(symbol,Decimal(str(raw.close)),raw.end)

    def _signal(self,bar,update_price=True):
        # Inactive-bar rejection is shared; a frozen tape has no signal callback.
        pass

    def _close_day(self,event):
        day=datetime.fromtimestamp(event.ts_event/1e9,TZ).date()
        if day in self.closed:return
        # Observe raw closes only at their declared close; never before execution.
        valuation_symbols={s for s,q in self.ledger.positions.items() if q}
        valuation_symbols.update(entry.intent.symbol for entry in self.tape.by_day.get(day,()))
        for bar in self.by_day[day]:
            if bar.symbol in valuation_symbols and self.rules[bar.symbol,day]['trade_status']=='OPEN':
                self._mark_price(bar.symbol,Decimal(str(bar.close)),bar.end)
        super()._close_day(event)
        for bar in self.by_day[day]:
            if bar.volume>0:self.history[bar.symbol][:]=[bar]
        self._queue_intents([entry.intent for entry in self.tape.by_day.get(day,())],at(day,15))


def run_order_tape(prepared,tape,**run_options):
    """Replay a prepared, daily-close order tape against full validated inputs."""
    from engine.order_tape import PreparedOrderTape, prepare_order_tape
    if type(prepared) is not PreparedBacktest:raise TypeError('PreparedBacktest required')
    prepared.assert_compatible()
    if not prepared.daily_only:raise ValueError('Order tape replay requires daily bars')
    if type(tape) is not PreparedOrderTape:tape=prepare_order_tape(prepared,tape)
    if tape.market_input_sha256!=prepared.input_sha256:raise ValueError('Order tape market input mismatch')
    run_options.setdefault('strategy_id','frozen-order-tape')
    return _run_backtest(prepared.instruments,prepared.bars,prepared.rules,_no_signals,
        corporate_actions=prepared.actions,trading_days=prepared.sessions,_prepared=prepared,
        _tape=tape,data_mode='orders',**run_options)

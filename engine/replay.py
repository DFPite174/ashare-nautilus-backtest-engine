"""Native event batches and price-free session clock boundaries."""
from decimal import Decimal
from nautilus_trader.core.data import Data
from nautilus_trader.model.data import Bar, BarSpecification, BarType, CustomData, DataType, QuoteTick, OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import AggregationSource, BarAggregation, PriceType, BookAction
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.objects import Price, Quantity
from engine.liquidity import synthetic_book

BOUNDARY_CLIENT=ClientId('REPLAY-BOUNDARY')


class _SessionBoundary(Data):
    def __init__(self,timestamp):self._timestamp=timestamp
    @property
    def ts_event(self):return self._timestamp
    @property
    def ts_init(self):return self._timestamp


def session_boundary(timestamp):
    return CustomData(DataType(_SessionBoundary),_SessionBoundary(timestamp))


def build_events(bars,rules,native,rate,liquidity,bar_types,active_symbols=None):
    from engine.backtest import TZ,at,nanos
    data=[];rate=Decimal(str(rate))
    for b in sorted(bars,key=lambda item:(item.start,item.symbol)):
        if b.volume==0 or rules[(b.symbol,b.end.astimezone(TZ).date())]['trade_status']!='OPEN':continue
        inst=native[b.symbol];start,end=nanos(b.start),nanos(b.end)
        cap=int(Decimal(b.volume)*rate);iid=str(inst.id);key=(b.symbol,end)
        precision=inst.price_precision;px_fmt=f'.{precision}f'
        detailed=active_symbols is None or b.symbol in active_symbols
        if detailed:
            for ts in (start,start+1):liquidity.events[(iid,ts)]=(key,cap,False)
            for ts in (end-3,end-2,end-1,end):liquidity.events[(iid,ts)]=(key,cap,True)
            price=Price.from_str(format(b.open,px_fmt))
            data.append(synthetic_book(inst,price,cap,start))
            data.append(QuoteTick(inst.id,price,price,Quantity.from_int(cap),Quantity.from_int(cap),start,start+1))
            for ts,value in zip((end-3,end-2,end-1),(b.high,b.low,b.close)):
                data.append(synthetic_book(inst,Price.from_str(format(value,px_fmt)),cap,ts))
        else:
            # NT's L2 input contract needs a book event, even for an idle symbol.
            liquidity.events[(iid,start)]=(key,cap,False)
            data.append(OrderBookDeltas(inst.id,[OrderBookDelta(inst.id,BookAction.CLEAR,None,0,0,start,start)]))
            liquidity.events[(iid,end)]=(key,cap,True)
        daily=b.start.astimezone(TZ)==at(b.start.astimezone(TZ).date(),9,30) and b.end.astimezone(TZ)==at(b.end.astimezone(TZ).date(),15)
        duration='DAY' if daily else int((b.end-b.start).total_seconds()/60)
        bt_key=(b.symbol,duration)
        if bt_key not in bar_types:
            spec=BarSpecification(1,BarAggregation.DAY,PriceType.LAST) if daily else BarSpecification(duration,BarAggregation.MINUTE,PriceType.LAST)
            bar_types[bt_key]=BarType(inst.id,spec,AggregationSource.EXTERNAL)
        data.append(Bar(bar_types[bt_key],*[Price.from_str(format(v,px_fmt)) for v in (b.open,b.high,b.low,b.close)],Quantity.from_int(cap),end,end))
    return data

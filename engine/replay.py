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


def build_events(bars,rules,native,rate,liquidity,bar_types,active_symbols=None,mode='synthetic_l2',prev_volumes=None):
    from engine.backtest import TZ,at,nanos
    data=[];rate=Decimal(str(rate))
    history_vol = dict(prev_volumes) if prev_volumes is not None else {}
    for b in sorted(bars,key=lambda item:(item.start,item.symbol)):
        if b.volume==0 or rules[(b.symbol,b.end.astimezone(TZ).date())]['trade_status']!='OPEN':
            if b.volume > 0: history_vol[b.symbol] = b.volume
            continue
        inst=native[b.symbol];start,end=nanos(b.start),nanos(b.end)
        day_cap=int(Decimal(b.volume)*rate);iid=str(inst.id);key=(b.symbol,end)
        daily=b.start.astimezone(TZ)==at(b.start.astimezone(TZ).date(),9,30) and b.end.astimezone(TZ)==at(b.end.astimezone(TZ).date(),15)
        if daily:
            open_vol = getattr(b, 'open_volume', None)
            if open_vol is not None:
                open_cap = int(Decimal(str(open_vol)) * rate)
            elif b.symbol in history_vol:
                open_cap = int(Decimal(str(history_vol[b.symbol])) * rate)
            else:
                open_cap = day_cap
            if b.volume > 0:
                history_vol[b.symbol] = b.volume
        else:
            open_cap = day_cap
        precision=inst.price_precision;px_fmt=f'.{precision}f'
        detailed=active_symbols is None or b.symbol in active_symbols
        if detailed:
            for ts in (start,start+1):liquidity.events[(iid,ts)]=(key,open_cap,False)
            price=Price.from_str(format(b.open,px_fmt))
            data.append(synthetic_book(inst,price,open_cap,start))
            data.append(QuoteTick(inst.id,price,price,Quantity.from_int(open_cap),Quantity.from_int(open_cap),start,start+1))
            if mode != 'auction_open':
                for ts in (end-3,end-2,end-1,end):liquidity.events[(iid,ts)]=(key,day_cap,True)
                for ts,value in zip((end-3,end-2,end-1),(b.high,b.low,b.close)):
                    data.append(synthetic_book(inst,Price.from_str(format(value,px_fmt)),day_cap,ts))
            else:
                liquidity.events[(iid,end)]=(key,day_cap,True)
        else:
            # NT's L2 input contract needs a book event, even for an idle symbol.
            liquidity.events[(iid,start)]=(key,open_cap,False)
            data.append(OrderBookDeltas(inst.id,[OrderBookDelta(inst.id,BookAction.CLEAR,None,0,0,start,start)]))
            liquidity.events[(iid,end)]=(key,day_cap,True)
        daily=b.start.astimezone(TZ)==at(b.start.astimezone(TZ).date(),9,30) and b.end.astimezone(TZ)==at(b.end.astimezone(TZ).date(),15)
        duration='DAY' if daily else int((b.end-b.start).total_seconds()/60)
        bt_key=(b.symbol,duration)
        if bt_key not in bar_types:
            spec=BarSpecification(1,BarAggregation.DAY,PriceType.LAST) if daily else BarSpecification(duration,BarAggregation.MINUTE,PriceType.LAST)
            bar_types[bt_key]=BarType(inst.id,spec,AggregationSource.EXTERNAL)
        data.append(Bar(bar_types[bt_key],*[Price.from_str(format(v,px_fmt)) for v in (b.open,b.high,b.low,b.close)],Quantity.from_int(day_cap),end,end))
    return data

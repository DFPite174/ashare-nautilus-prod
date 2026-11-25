"""One shared execution budget per raw bar, including opening and OHLC fills."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.enums import BookType, OrderSide, BookAction
from nautilus_trader.model.objects import Quantity, Price
from nautilus_trader.model.data import BookOrder, OrderBookDelta, OrderBookDeltas


def synthetic_book(instrument,price,quantity,ts):
    """Bar-derived depth, not observed L2: native L2 avoids L1 slip-fill fallback."""
    qty=Quantity.from_int(max(quantity,1))
    return OrderBookDeltas(instrument.id,[
        OrderBookDelta(instrument.id,BookAction.CLEAR,None,0,0,ts,ts),
        OrderBookDelta(instrument.id,BookAction.ADD,BookOrder(OrderSide.BUY,price,qty,1),0,0,ts,ts),
        OrderBookDelta(instrument.id,BookAction.ADD,BookOrder(OrderSide.SELL,price,qty,2),128,0,ts,ts),
    ])


class BarLiquidity(FillModel):
    def __init__(self, slippage_bps=0):
        super().__init__()
        self.events={};self.active={};self.remaining={};self.intrabar={}
        self.consumed={}
        self.affordable=None
        self.slippage_bps=Decimal(str(slippage_bps))

    def execution_price(self, price, side, instrument, limit=None, *, down_limit=None, up_limit=None):
        px=Decimal(str(price))
        tick=Decimal(str(instrument.price_increment))
        buy=side==OrderSide.BUY
        px=px*(1+(self.slippage_bps/10000 if buy else -self.slippage_bps/10000))
        px=(px/tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)*tick
        if buy and up_limit is not None:
            cap_px=(Decimal(str(up_limit))/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
            px=min(px,cap_px)
        elif not buy and down_limit is not None:
            floor_px=(Decimal(str(down_limit))/tick).to_integral_value(rounding=ROUND_CEILING)*tick
            px=max(px,floor_px)
        if limit is not None:
            px=min(px,Decimal(str(limit))) if buy else max(px,Decimal(str(limit)))
        return Price.from_str(format(px,f'.{instrument.price_precision}f'))

    def begin(self,data):
        iid=str(data.instrument_id if hasattr(data,'instrument_id') else data.bar_type.instrument_id)
        key,cap,intrabar=self.events[(iid,data.ts_init)]
        self.active[iid]=key
        self.intrabar[iid]=intrabar
        if key not in self.remaining:
            self.remaining[key]=cap
        elif intrabar:
            self.remaining[key]=max(0, cap - self.consumed.get(key, 0))

    def consume(self,iid,quantity):
        key=self.active[str(iid)]
        qty=int(Decimal(str(quantity)))
        self.consumed[key]=self.consumed.get(key, 0) + qty
        self.remaining[key]-=qty
        if self.remaining[key]<0:raise RuntimeError('Native fills exceeded bar participation budget')

    def get_orderbook_for_fill_simulation(self,instrument,order,best_bid,best_ask):
        book=OrderBook(instrument_id=instrument.id,book_type=BookType.L2_MBP)
        iid=str(instrument.id)
        qty=min(self.remaining.get(self.active.get(iid),0),int(Decimal(str(order.leaves_qty))))
        # Touch inside a bar fills at the submitted limit, never at the bar extreme.
        price=order.price if self.intrabar.get(iid) else (best_ask if order.side==OrderSide.BUY else best_bid)
        price=self.execution_price(price,order.side,instrument,order.price)
        if self.affordable is not None and order.side==OrderSide.BUY:
            qty=self.affordable(order,price,qty)
        if qty:
            side=OrderSide.SELL if order.side==OrderSide.BUY else OrderSide.BUY
            book.add(BookOrder(side,price,Quantity.from_int(qty),1),0,0)
        return book


class LiquidityModule(SimulationModule):
    def __init__(self,model):
        super().__init__(SimulationModuleConfig())
        self.model=model

    def pre_process(self,data):self.model.begin(data)
    def process(self,ts_now):pass
    def log_diagnostics(self,logger):pass
    def reset(self):
        self.model.active.clear();self.model.remaining.clear();self.model.intrabar.clear();self.model.consumed.clear()

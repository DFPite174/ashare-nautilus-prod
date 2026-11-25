"""Cash-long A-share research backtests on the official NautilusTrader engine.

Opening quotes are explicit bar-level liquidity approximations, not an auction or
queue reconstruction. A closed bar can only create orders for a later bar open.
"""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Tuple, Set, List, Any
from zoneinfo import ZoneInfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig, RiskEngineConfig
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
NATIVE_ORDER_SUBMIT_RATE = '1000000/00:00:01'


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
    open_volume: Optional[int] = None


@dataclass(frozen=True)
class TrimmedIntentRecord:
    intent_id: Optional[str]
    symbol: str
    planned_quantity: int
    submitted_quantity: int
    trimmed_quantity: int
    reference_price: Decimal
    timestamp: int


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
    fee_model: Optional[Any] = None
    unfilled_capital_amount: Decimal = Decimal('0.0')
    residual_holding_symbols: dict = field(default_factory=dict)
    uncompleted_sell_orders: dict = field(default_factory=dict)
    trimmed_intents: list = field(default_factory=list)

    def to_dict(self):
        from dataclasses import asdict
        def convert(x):
            if isinstance(x, Decimal): return str(x)
            if isinstance(x, dict): return {str(k): convert(v) for k,v in x.items()}
            if isinstance(x, (list,tuple)): return [convert(v) for v in x]
            if type(x) not in (int, float, str, bool, type(None)): return str(x)
            return x
        return convert(asdict(self))

    def frictional_summary(self) -> dict:
        from engine.fee_model import get_ashare_rates_for_date, get_cb_rates_for_date
        from zoneinfo import ZoneInfo
        sh_tz = ZoneInfo("Asia/Shanghai")

        total_turnover = Decimal('0.0')
        buy_turnover = Decimal('0.0')
        sell_turnover = Decimal('0.0')
        total_fee = Decimal('0.0')
        stamp_duty = Decimal('0.0')
        transfer_fee = Decimal('0.0')
        proportional_commission = Decimal('0.0')
        min_commission_drag = Decimal('0.0')

        order_raw_comm: Dict[str, Decimal] = {}
        order_charged_comm: Dict[str, Decimal] = {}

        for f in self.fills:
            sym = f.get('symbol', '')
            qty = Decimal(str(f['quantity']))
            px = Decimal(str(f['price']))
            val = qty * px
            fee = Decimal(str(f['fee']))
            side = str(f['side']).upper()
            total_turnover += val
            total_fee += fee

            time_ns = f.get('time')
            trade_date = None
            if time_ns:
                dt = datetime.fromtimestamp(time_ns / 1e9, sh_tz)
                trade_date = str(dt.date())

            if self.fee_model is not None:
                rates = self.fee_model.rates_for(sym, trade_date)
            else:
                rates = get_ashare_rates_for_date(trade_date)

            t_rate = Decimal(str(rates['transfer_fee_rate']))
            s_rate = Decimal(str(rates['stamp_duty_rate']))
            c_rate = Decimal(str(rates['commission_rate']))
            min_c = Decimal(str(rates['min_commission']))

            t_fee = (val * t_rate).quantize(Decimal('0.0001'))
            transfer_fee += t_fee

            s_duty = Decimal('0.0')
            if 'SELL' in side:
                sell_turnover += val
                s_duty = (val * s_rate).quantize(Decimal('0.0001'))
                stamp_duty += s_duty
            else:
                buy_turnover += val

            order_key = f.get('order_id', f.get('id', sym))
            raw_c = val * c_rate
            prev_raw = order_raw_comm.get(order_key, Decimal('0.0'))
            prev_charged = order_charged_comm.get(order_key, Decimal('0.0'))
            new_raw = prev_raw + raw_c
            order_raw_comm[order_key] = new_raw
            total_comm_due = max(new_raw, min_c)
            inc_comm = total_comm_due - prev_charged
            order_charged_comm[order_key] = prev_charged + inc_comm

            proportional_commission += raw_c
            drag = max(Decimal('0.0'), inc_comm - raw_c)
            min_commission_drag += drag

        rejections_by_reason = {}
        for r in self.rejections:
            reason = r.get('reason', 'UNKNOWN')
            rejections_by_reason[reason] = rejections_by_reason.get(reason, 0) + 1

        total_commission = proportional_commission + min_commission_drag
        friction_bps = (total_fee / total_turnover * Decimal('10000')).quantize(Decimal('0.01')) if total_turnover > 0 else Decimal('0.0')

        return {
            'total_turnover': total_turnover.quantize(Decimal('0.01')),
            'buy_turnover': buy_turnover.quantize(Decimal('0.01')),
            'sell_turnover': sell_turnover.quantize(Decimal('0.01')),
            'total_fee': total_fee.quantize(Decimal('0.01')),
            'total_commission': total_commission.quantize(Decimal('0.01')),
            'proportional_commission': proportional_commission.quantize(Decimal('0.01')),
            'min_commission_drag': min_commission_drag.quantize(Decimal('0.01')),
            'stamp_duty': stamp_duty.quantize(Decimal('0.01')),
            'transfer_fees': transfer_fee.quantize(Decimal('0.01')),
            'total_fills': len(self.fills),
            'total_rejections': len(self.rejections),
            'rejections_by_reason': rejections_by_reason,
            'friction_bps': friction_bps,
        }

    def attribution_report(self) -> str:
        s = self.frictional_summary()
        raw_initial_cash = (self.manifest or {}).get("config", {}).get("initial_cash")
        if raw_initial_cash is None and self.manifest:
            raw_initial_cash = self.manifest.get("initial_cash")
        initial_cash = Decimal(str(raw_initial_cash)) if raw_initial_cash is not None else None
        final_equity = Decimal(str(self.daily_nav[-1]['total_equity'])) if self.daily_nav else self.ledger_cash
        total_pnl = final_equity - initial_cash if initial_cash is not None else Decimal('0.0')
        rejections_str = "".join(f"     - {k:<24}: {v:>8d}\n" for k, v in s['rejections_by_reason'].items()) if s['rejections_by_reason'] else "     - None\n"
        initial_cash_str = f"{initial_cash:>15,.2f} CNY" if initial_cash is not None else "        UNKNOWN"
        pnl_str = f"{total_pnl:>15,.2f} CNY" if initial_cash is not None else "        UNKNOWN"
        return (
            "================================================================================\n"
            "                 ASHARE-NAUTILUS MICROSTRUCTURE FRICTION REPORT                 \n"
            "================================================================================\n"
            f" Total Turnover           : {s['total_turnover']:>15,.2f} CNY\n"
            f"   - Buy Turnover         : {s['buy_turnover']:>15,.2f} CNY\n"
            f"   - Sell Turnover        : {s['sell_turnover']:>15,.2f} CNY\n"
            "--------------------------------------------------------------------------------\n"
            f" Total Statutory Frictions: {s['total_fee']:>15,.2f} CNY ({s['friction_bps']} bps)\n"
            f"   * Pure Commission      : {s['proportional_commission']:>15,.2f} CNY\n"
            f"   * Min Commission Drag  : {s['min_commission_drag']:>15,.2f} CNY\n"
            f"   * Stamp Duty (印花税)  : {s['stamp_duty']:>15,.2f} CNY\n"
            f"   * Transfer Fee (过户费): {s['transfer_fees']:>15,.2f} CNY\n"
            "--------------------------------------------------------------------------------\n"
            f" Execution Summary        :\n"
            f"   * Total Fills          : {s['total_fills']:>15d}\n"
            f"   * Total Rejections     : {s['total_rejections']:>15d}\n"
            + rejections_str +
            "--------------------------------------------------------------------------------\n"
            f" Account Equity           :\n"
            f"   * Initial Cash         : {initial_cash_str}\n"
            f"   * Final Equity         : {final_equity:>15,.2f} CNY\n"
            f"   * Net Realized PnL     : {pnl_str}\n"
            "================================================================================"
        )


def _validate(instruments, bars, rules, actions, initial_cash, rate, trading_days, corporate_action_gate_policy='strict', anomalies=None):
    if corporate_action_gate_policy not in {"strict", "warn"}:
        raise ValueError(f"Invalid corporate_action_gate_policy: {corporate_action_gate_policy}")
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
    bar_days=set()
    for b in bars:
        if b.symbol not in master:
            raise ValueError(f'Unknown bar symbol: {b.symbol}')
        for field in ('start','end'):
            value=getattr(b,field)
            if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f'Explicit timezone required: {b.symbol} {field}')
        inst=master[b.symbol];day=b.end.astimezone(TZ).date()
        bar_days.add(day)
        if (inst.list_date and day<inst.list_date) or (inst.delist_date and day>inst.delist_date):
            raise ValueError(f'Bar outside security lifecycle: {b.symbol} {day}')
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
    bounds={day:(at(day,9,30),at(day,11,30),at(day,13),at(day,15)) for day in sessions}
    actions_by_symbol = defaultdict(set)
    for a in actions:
        actions_by_symbol[a.symbol].add(a.ex_date)
    previous={}; actual=set(); last_close_record={}
    for b in sorted(bars,key=lambda x:(x.start,x.symbol)):
        start,end=b.start.astimezone(TZ),b.end.astimezone(TZ)
        day=start.date();end_day=end.date()
        if day!=end_day:
            raise ValueError("Bar is outside the supported session")
        opening,morning_end,afternoon_start,closing=bounds[day]
        if not opening<=start<end<=closing:raise ValueError('Bar is outside the supported session')
        daily=start==opening and end==closing
        if not daily and not (end<=morning_end or start>=afternoon_start):
            raise ValueError("Intraday bar crosses the lunch break")
        if not daily and (end-start).total_seconds()%60:
            raise ValueError("Only daily or whole-minute bars are supported")
        if b.symbol in previous and start<previous[b.symbol]: raise ValueError("Overlapping bars")
        previous[b.symbol]=end; actual.add((b.symbol,end_day))
        values=[D(str(v)) for v in (b.open,b.high,b.low,b.close)]
        if any(not v.is_finite() or v<=0 for v in values) or not values[2]<=min(values[0],values[3])<=max(values[0],values[3])<=values[1]:
            raise ValueError("Invalid OHLC")
        tick=master[b.symbol].tick_size
        if any(v%tick for v in values): raise ValueError("OHLC violates price tick")
        if not isinstance(b.volume,int) or b.volume<0: raise ValueError("Invalid share volume")
        if b.open_volume is not None:
            if type(b.open_volume) is not int or b.open_volume < 0:
                raise ValueError("open_volume must be a non-negative integer in shares")
            if b.open_volume > b.volume:
                raise ValueError(f"open_volume ({b.open_volume}) cannot exceed total volume ({b.volume})")
        r=rules[(b.symbol,end_day)]
        if r['trade_status']=='OPEN' and r['limit_state']=='BOUNDED' and not D(str(r['down_limit']))<=values[2]<=values[1]<=D(str(r['up_limit'])):
            raise ValueError('OHLC violates daily price band')

        # Anti-Illusion Gate: 除权除息防御性拦截断言，杜绝共享漏配 actions 导致的对账虚假确定性
        if b.symbol in last_close_record:
            prev_close, prev_date = last_close_record[b.symbol]
            if end_day > prev_date:
                has_action = any(prev_date < ex_d <= end_day for ex_d in actions_by_symbol[b.symbol])
                rule_prev = r.get('prev_close') if r.get('prev_close') is not None else r.get('ex_ref_price')
                if rule_prev is not None:
                    diff = abs(D(str(rule_prev)) - prev_close)
                    if diff > D('0.02') and not has_action:
                        msg = (
                            f"CRITICAL_DATA_DEFECT: Symbol {b.symbol} on {end_day} exhibits ex-rights price gap "
                            f"(prev_close={prev_close}, rule_prev_close={rule_prev}, diff={diff}) but NO corporate action "
                            f"was supplied in 'actions'."
                        )
                        if corporate_action_gate_policy == 'strict':
                            raise ValueError(msg + " Aborting backtest to prevent False Confidence Illusion.")
                        else:
                            import warnings
                            warnings.warn(msg + " Continuing under warn policy.", UserWarning)
                            if anomalies is not None:
                                anomalies.append({'symbol': b.symbol, 'date': str(end_day), 'type': 'confirmed_ex_rights_gap', 'diff': str(diff)})
                else:
                    if master[b.symbol].asset_class.value == 'EQUITY' and r.get('limit_state') == 'BOUNDED':
                        sym_clean = b.symbol.split('.')[0]
                        if sym_clean.startswith(('60', '00')):
                            statutory_bound = D('0.88')
                        elif sym_clean.startswith(('30', '688', '689')):
                            statutory_bound = D('0.78')
                        else:
                            statutory_bound = D('0.68')
                        if values[0] < prev_close * statutory_bound:
                            if not has_action:
                                msg = (
                                    f"CRITICAL_DATA_DEFECT: Symbol {b.symbol} on {end_day} exhibits ex-rights price gap "
                                    f"(prev_close={prev_close}, curr_open={values[0]}) but NO corporate action "
                                    f"was supplied in 'actions'."
                                )
                                if corporate_action_gate_policy == 'strict':
                                    raise ValueError(msg + " Aborting backtest to prevent False Confidence Illusion.")
                                else:
                                    import warnings
                                    warnings.warn(msg + " Continuing under warn policy.", UserWarning)
                                    if anomalies is not None:
                                        anomalies.append({'symbol': b.symbol, 'date': str(end_day), 'type': 'suspected_ex_rights_gap', 'curr_open': str(values[0]), 'prev_close': str(prev_close)})
        last_close_record[b.symbol] = (values[3], end_day)
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
    def __init__(self, bars, rules, master, sessions, actions, signal, initial_cash, rate, native, fee_model, cash_module, liquidity, portfolio_signal=None, frozen_orders=None, cash_exhaustion_policy='reject', participation_policy='reject'):
        super().__init__(StrategyConfig())
        self.bars=bars;self.rules=rules;self.master=master;self.sessions=sessions;self.actions=actions
        self.signal=signal;self.rate=rate;self.native=native;self.fee_model=fee_model;self.cash_module=cash_module
        self.liquidity=liquidity
        self.cash_exhaustion_policy=cash_exhaustion_policy
        self.participation_policy=participation_policy
        self.buy_reservations={}
        self.trimmed_intents = []
        self.failed_sell_symbols = set()
        self.uncompleted_sell_leaves = defaultdict(Decimal)
        self.order_sides = {}
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
        self.frozen_orders_by_date = defaultdict(list)
        if frozen_orders:
            for fo in frozen_orders:
                sym = getattr(fo, 'symbol', None) or (fo['symbol'] if isinstance(fo, dict) else None)
                if sym not in self.master:
                    raise ValueError(f"Unknown instrument in frozen order: {sym}")
                t_date = getattr(fo, 'trade_date', None) or (fo['trade_date'] if isinstance(fo, dict) else None)
                if isinstance(t_date, str):
                    from datetime import date
                    t_date = date.fromisoformat(t_date)
                side_val = getattr(fo, 'side', None) or (fo['side'] if isinstance(fo, dict) else None)
                side_str = str(side_val).upper().replace('ORDERSIDE.', '')
                if side_str not in {'BUY', 'SELL'}:
                    raise ValueError(f"Invalid side in frozen order: {side_val}")
                qty = getattr(fo, 'quantity', None) if hasattr(fo, 'quantity') else fo['quantity']
                if not isinstance(qty, int) or qty <= 0:
                    raise ValueError(f"Invalid quantity in frozen order: {qty}")
                limit_px = getattr(fo, 'limit_price', None) if hasattr(fo, 'limit_price') else (fo.get('limit_price') if isinstance(fo, dict) else None)
                if limit_px is not None:
                    px = D(str(limit_px))
                    if not px.is_finite() or px <= 0 or px % self.master[sym].tick_size:
                        raise ValueError("Invalid limit price or price tick in frozen order")
                    limit_px = px
                if t_date not in self.sessions:
                    raise ValueError(f"Frozen order date is outside trading sessions: {t_date}")
                intent_id = (fo.get('intent_id') or fo.get('client_order_id')) if isinstance(fo, dict) else getattr(fo, 'client_order_id', None)
                intent = OrderIntent(sym, side_str, qty, limit_px, intent_id)
                validate_intents([intent], self.master, at(t_date, 9), self.seen_intent_ids)
                self.frozen_orders_by_date[t_date].append(intent)

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

    def _reject_intent(self,intent,reason,timestamp,price=None):
        if price is None:
            price = intent.limit_price or self.last_close.get(intent.symbol, D(0))
        row={'symbol':intent.symbol,'reason':reason,'time':timestamp}
        if intent.intent_id is not None:row['intent_id']=intent.intent_id
        self.rejections.append(row)
        if not hasattr(self, 'rejected_intents'):
            self.rejected_intents = []
        self.rejected_intents.append((intent, price, timestamp))
        if intent.side.upper() == 'SELL':
            self._record_unfilled_sell(intent.symbol, D(intent.quantity))

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
            frozen_map = getattr(self, 'frozen_orders_by_date', {})
            if day in frozen_map:
                fo_list = frozen_map[day]
                fo_sorted = sorted(fo_list, key=lambda i: 0 if i.side == 'SELL' else 1)
                for intent in fo_sorted:
                    self.pending.append((event.ts_event - 1, intent))
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
                    positions=self.cache.positions_open(instrument_id=self.native[a.symbol].id)
                    for pos in positions:
                        load_bridge().apply_split(pos,ratio,event.ts_event,self.native[a.symbol],fractional_policy='cash_in_lieu' if a.fractional_policy=='cash_in_lieu' else 'reject')
                    self.gateway.apply_stock_split(a.symbol,ratio,share_listing_date=a.share_listing_date,trade_date=day)
                    if positions:self.portfolio.initialize_positions()
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
        elif self.signal is not None:
            intents=self.signal(b,self._state(b.end)) or []
        else:
            return
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

    def _calculate_affordable_lots(self, symbol: str, px: Decimal, requested_qty: int, avail_cash: Decimal, step: int, min_lot: int, day: date) -> int:
        if avail_cash <= 0 or px <= 0 or requested_qty < min_lot:
            return 0
        max_lots = (requested_qty - min_lot) // step
        lo = 0
        hi = max_lots
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            test_qty = min_lot + mid * step
            fee = self._expected_fee('BUY', D(test_qty), px, symbol=symbol, day=day)
            total = D(test_qty) * px + fee
            if total <= avail_cash:
                best = test_qty
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _submit_open(self,tick,phase):
        if not self.pending:return
        symbol=self.by_id[str(tick.instrument_id)];day=datetime.fromtimestamp(tick.ts_event/1e9,TZ).date()
        previous_volume=next((b.volume for b in reversed(self.history[symbol]) if b.volume>0 and self.rules[(symbol,b.end.astimezone(TZ).date())]['trade_status']=='OPEN'),0)
        budget_key=(symbol,tick.ts_event)
        if not hasattr(self,'submission_budget'):self.submission_budget={}
        available=self.submission_budget.setdefault(budget_key,max(int(D(previous_volume)*D(str(self.rate))), int(D(str(tick.ask_size)))))
        remaining=[]
        for created,intent in self.pending:
            if intent.symbol!=symbol or created>tick.ts_event or intent.side.upper()!=phase:
                remaining.append((created,intent));continue
            rule=self.rules[(symbol,day)]
            side=OrderSide.BUY if intent.side.upper()=='BUY' else OrderSide.SELL
            price=tick.ask_price if side==OrderSide.BUY else tick.bid_price
            if intent.limit_price is None:
                dn,up=(rule.get('down_limit'),rule.get('up_limit')) if rule.get('limit_state')=='BOUNDED' else (None,None)
                price=self.liquidity.execution_price(price,side,self.native[symbol],down_limit=dn,up_limit=up)
            if intent.quantity>available:
                if self.participation_policy=='clip':
                    step=rule.get('buy_step',100) if side==OrderSide.BUY else rule.get('sell_step',100)
                    min_lot=rule.get('buy_min',100) if side==OrderSide.BUY else rule.get('sell_min',100)
                    clipped=min_lot+((available-min_lot)//step)*step if available>=min_lot else 0
                    if clipped<min_lot:
                        self._reject_intent(intent,'PARTICIPATION_LIMIT',tick.ts_init,price=price);continue
                    from engine.intents import OrderIntent
                    ref_px=D(str(price)) if intent.limit_price is None else D(str(intent.limit_price))
                    self.trimmed_intents.append(TrimmedIntentRecord(
                        intent_id=intent.intent_id,
                        symbol=symbol,
                        planned_quantity=intent.quantity,
                        submitted_quantity=clipped,
                        trimmed_quantity=intent.quantity-clipped,
                        reference_price=ref_px,
                        timestamp=tick.ts_init,
                    ))
                    intent=OrderIntent(symbol=intent.symbol,side=intent.side,quantity=clipped,limit_price=intent.limit_price,intent_id=intent.intent_id,earliest_execution=intent.earliest_execution)
                else:
                    self._reject_intent(intent,'PARTICIPATION_LIMIT',tick.ts_init,price=price);continue
            # IOC at the observed open avoids NT market-order empty-book fallback.
            args=dict(instrument_id=tick.instrument_id,order_side=side,quantity=Quantity.from_int(intent.quantity),time_in_force=TimeInForce.IOC if intent.limit_price is None else TimeInForce.DAY)
            order=self.order_factory.limit(**args,price=price if intent.limit_price is None else Price.from_str(format(intent.limit_price,f".{self.precisions[symbol]}f")))
            if side==OrderSide.BUY:
                cost=D(str(order.price))*intent.quantity
                cost+=self._expected_fee('BUY',D(intent.quantity),D(str(order.price)),symbol=symbol,day=day)
                avail_cash=self.ledger.cash-sum((r['amount'] for r in self.buy_reservations.values()),D(0))
                if cost>avail_cash:
                    if self.cash_exhaustion_policy=='auto_trim':
                        step=rule.get('buy_step',100)
                        min_lot=rule.get('buy_min',100)
                        affordable_qty=self._calculate_affordable_lots(symbol,D(str(order.price)),intent.quantity,avail_cash,step,min_lot,day)
                        if affordable_qty>=min_lot:
                            from engine.intents import OrderIntent
                            trimmed_qty = intent.quantity - affordable_qty
                            if trimmed_qty > 0:
                                self.trimmed_intents.append(TrimmedIntentRecord(
                                    intent_id=intent.intent_id,
                                    symbol=symbol,
                                    planned_quantity=intent.quantity,
                                    submitted_quantity=affordable_qty,
                                    trimmed_quantity=trimmed_qty,
                                    reference_price=D(str(order.price)),
                                    timestamp=tick.ts_init
                                ))
                            intent=OrderIntent(symbol=intent.symbol,side=intent.side,quantity=affordable_qty,limit_price=intent.limit_price,intent_id=intent.intent_id,earliest_execution=intent.earliest_execution)
                            args['quantity']=Quantity.from_int(intent.quantity)
                            order=self.order_factory.limit(**args,price=order.price)
                            cost=D(str(order.price))*intent.quantity
                            cost+=self._expected_fee('BUY',D(intent.quantity),D(str(order.price)),symbol=symbol,day=day)
                        else:
                            self._reject_intent(intent,'INSUFFICIENT_CASH_WITH_FEES',tick.ts_init,price=order.price);continue
                    else:
                        self._reject_intent(intent,'INSUFFICIENT_CASH_WITH_FEES',tick.ts_init,price=order.price);continue
            self.order_sides[str(order.client_order_id)] = side.name
            open_ns=nanos(at(day,9,30))
            is_preopen_order=created<open_ns and tick.ts_event==open_ns
            r=self.gateway.validate_and_reserve(order,rule,{'close':tick.ask_price.as_double(),'volume':max(available,1),'execution_ts':tick.ts_init},tick.ask_price.as_double() if side==OrderSide.BUY else tick.bid_price.as_double(),enforce_collar=False if is_preopen_order else None)
            if r.is_valid:
                self._sync_account_symbol(symbol)
                if side==OrderSide.BUY:self.buy_reservations[str(order.client_order_id)]={'amount':cost,'quantity':D(intent.quantity),'price':D(str(order.price))}
                if intent.intent_id is not None:self.order_intent_ids[str(order.client_order_id)]=intent.intent_id
                self.submit_order(order);available-=intent.quantity
            else:self._reject_intent(intent,r.rejection_reason,tick.ts_init,price=order.price)
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
        if e.order_side.name == 'SELL' and symbol in self.uncompleted_sell_leaves:
            self.uncompleted_sell_leaves[symbol] = max(D(0), self.uncompleted_sell_leaves[symbol] - qty)
            if not self.uncompleted_sell_leaves[symbol]:
                self.failed_sell_symbols.discard(symbol)
        elif e.order_side.name == 'BUY':
            self.failed_sell_symbols.discard(symbol)
        if key in self.buy_reservations:
            reserve=self.buy_reservations[key];reserve['quantity']-=qty
            if reserve['quantity']==0:self.buy_reservations.pop(key)
            else:
                reserve['amount']=reserve['quantity']*reserve['price']+self._expected_fee('BUY',reserve['quantity'],reserve['price'],symbol=symbol,day=day,before=self.fee_values[key])
        self.fills.append({'symbol':symbol,'side':e.order_side.name,'quantity':str(qty),'price':str(px),'fee':str(fee),'time':e.ts_event,'order_id':key})
        if key in self.order_intent_ids:self.fills[-1]['intent_id']=self.order_intent_ids[key]

    def _record_unfilled_sell(self, symbol, leaves):
        if leaves > 0:
            self.uncompleted_sell_leaves[symbol] += leaves
            position = self.ledger.positions.get(symbol, D(0))
            if position > 0 and leaves >= position:
                self.failed_sell_symbols.add(symbol)

    def _record_failed_sell_leaves(self, e, sym):
        if self.order_sides.get(str(e.client_order_id)) == 'SELL':
            order = self.cache.order(e.client_order_id)
            if order is not None:
                self._record_unfilled_sell(sym, D(str(order.quantity)) - D(str(order.filled_qty)))

    def on_order_canceled(self,e):
        self.gateway.on_order_canceled(str(e.client_order_id));self.buy_reservations.pop(str(e.client_order_id),None)
        sym = self.by_id[str(e.instrument_id)]
        self._sync_account_symbol(sym)
        self._record_failed_sell_leaves(e, sym)

    def on_order_expired(self,e):
        self.gateway.on_order_expired(str(e.client_order_id));self.buy_reservations.pop(str(e.client_order_id),None)
        sym = self.by_id[str(e.instrument_id)]
        self._sync_account_symbol(sym)
        self._record_failed_sell_leaves(e, sym)

    def on_order_rejected(self,e):
        self.buy_reservations.pop(str(e.client_order_id),None)
        self.gateway.on_order_rejected(str(e.client_order_id))
        sym = self.by_id[str(e.instrument_id)]
        row = {'symbol':sym,'reason':str(e.reason),'time':e.ts_event}
        if str(e.client_order_id) in self.order_intent_ids:row['intent_id']=self.order_intent_ids[str(e.client_order_id)]
        self.rejections.append(row)
        self._sync_account_symbol(sym)
        self._record_failed_sell_leaves(e, sym)

    def on_order_denied(self,e):
        self.buy_reservations.pop(str(e.client_order_id),None)
        self.gateway.on_order_denied(str(e.client_order_id))
        sym = self.by_id[str(e.instrument_id)]
        row = {'symbol':sym,'reason':str(e.reason),'time':e.ts_event}
        if str(e.client_order_id) in self.order_intent_ids:row['intent_id']=self.order_intent_ids[str(e.client_order_id)]
        self.rejections.append(row)
        self._sync_account_symbol(sym)
        self._record_failed_sell_leaves(e, sym)

    def _close_day(self,event):
        day=datetime.fromtimestamp(event.ts_event/1e9,TZ).date()
        if day in self.closed:return
        active_symbols=set(self.gateway._order_symbol.values())
        for symbol,inst in self.native.items():
            if symbol in active_symbols:self.cancel_all_orders(inst.id)
        closing_symbols=set(self.gateway._order_symbol.values())
        self.gateway.on_session_close()
        for symbol in closing_symbols:self._sync_account_symbol(symbol)
        for b in self.by_day[day]:
            history = self.history[b.symbol]
            if history and history[-1].end >= b.end:
                continue  # Validated bars do not overlap and callbacks append chronologically.
            if b.volume==0 or self.rules[(b.symbol,day)]['trade_status']!='OPEN':
                self._inactive_bar(b)
            else:
                history.append(b)
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
        native_cash=D(str(round(self.portfolio.account(SIM).balance_total(CNY).as_double(), 2)))
        native_positions={s:self._native_quantity(s) for s in self.master}
        positions_match=all(native_positions[s]==self.ledger.positions.get(s,D(0))==self.gateway.get_total_position(s) for s in self.master)
        cash_matches=abs(native_cash - self.ledger.cash) <= D('.01')
        native_equity=native_cash+sum(q*prices[s] for s,q in native_positions.items() if q)+sum(self.native_receivables.values(),D(0))
        if not positions_match or not cash_matches or abs(native_equity-nav.total_equity)>D('.01'):
            raise RuntimeError(f"Daily reconciliation failed on {day}: cash={cash_matches}, positions={positions_match}")
        row=nav.model_dump(mode='json');row.update(native_cash=str(native_cash),native_equity=str(native_equity),cash_matches=cash_matches,positions_match=positions_match)
        self.nav.append(row);self.closed.add(day)


def _run_backtest(instruments, bars, rules, signal=None, *, corporate_actions=(), actions=None, initial_cash=D(100000), participation_rate=.01, trading_days=None, fee_model=None, portfolio_signal=None, slippage_bps=0, strategy_id=None, strategy_params=None, data_snapshot_id=None, data_mode='full', frozen_orders=None, execution_mode='synthetic_l2', cash_exhaustion_policy='reject', participation_policy='reject', corporate_action_gate_policy=None, unresolved_entitlements=(), _prepared=None, _tape=None):
    """Run one isolated, shared-cash portfolio. Rules keyed by (symbol, local date)."""
    VALID_GATE_POLICIES = {"strict", "warn"}
    VALID_EXEC_MODES = {"synthetic_l2", "auction_open"}
    VALID_DATA_MODES = {"full", "session", "sparse", "orders"}
    VALID_CASH_POLICIES = {"reject", "auto_trim"}
    VALID_PARTICIPATION_POLICIES = {"reject", "clip"}

    if corporate_action_gate_policy is None:
        corporate_action_gate_policy = _prepared.corporate_action_gate_policy if _prepared is not None else 'strict'
    elif _prepared is not None and corporate_action_gate_policy != _prepared.corporate_action_gate_policy:
        raise ValueError("Requested corporate_action_gate_policy conflicts with prepared policy")
    if corporate_action_gate_policy not in VALID_GATE_POLICIES:
        raise ValueError(f"Invalid corporate_action_gate_policy: {corporate_action_gate_policy}")
    if execution_mode not in VALID_EXEC_MODES:
        raise ValueError(f"Invalid execution_mode: {execution_mode}")
    if data_mode not in VALID_DATA_MODES:
        raise ValueError(f"Invalid data_mode: {data_mode}")
    if cash_exhaustion_policy not in VALID_CASH_POLICIES:
        raise ValueError(f"Invalid cash_exhaustion_policy: {cash_exhaustion_policy}")
    if participation_policy not in VALID_PARTICIPATION_POLICIES:
        raise ValueError(f"Invalid participation_policy: {participation_policy}")

    if actions is not None and not corporate_actions:
        corporate_actions = actions
    initial_cash=D(str(initial_cash))
    anomalies = []
    if _prepared is None:
        instruments=list(instruments);bars=list(bars);actions=list(corporate_actions)
        master,sessions=_validate(instruments,bars,rules,actions,initial_cash,participation_rate,trading_days,corporate_action_gate_policy=corporate_action_gate_policy,anomalies=anomalies)
    else:
        if not initial_cash.is_finite() or initial_cash<=0 or not 0<participation_rate<=1:
            raise ValueError('Invalid cash or participation_rate')
        master,sessions=_prepared.master,_prepared.sessions
        actions=_prepared.actions
        anomalies=[dict(row) for row in _prepared.anomalies]
    if frozen_orders is None and _tape is None and (signal is None)==(portfolio_signal is None):
        raise ValueError('Provide exactly one of signal or portfolio_signal')
    slippage_bps=D(str(slippage_bps))
    if not slippage_bps.is_finite() or not 0<=slippage_bps<10000:raise ValueError('Invalid slippage_bps')
    if data_mode not in {'full','session','sparse','orders'}:raise ValueError('Invalid data_mode')
    if data_mode=='orders' and (_prepared is None or _tape is None) and frozen_orders is None:
        raise ValueError('Use run_order_tape or frozen_orders for orders delivery')
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
        _order_tape_sha256=_tape.sha256 if _tape is not None else None,
        _native_order_submit_rate=NATIVE_ORDER_SUBMIT_RATE,
        frozen_orders=frozen_orders,
        execution_mode=execution_mode,
        cash_exhaustion_policy=cash_exhaustion_policy,
        corporate_action_gate_policy=corporate_action_gate_policy)
    if anomalies:
        manifest['data_anomalies'] = anomalies
    if participation_policy != 'reject':
        manifest['participation_policy'] = participation_policy
    liquidity=BarLiquidity(slippage_bps)
    # Complete research history lives in our immutable callback snapshots.
    eng=BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId('RESEARCH-001'),logging=LoggingConfig(log_level='ERROR'),
        cache=CacheConfig(bar_capacity=2,tick_capacity=2),
        risk_engine=RiskEngineConfig(max_order_submit_rate=NATIVE_ORDER_SUBMIT_RATE),run_analysis=False))
    try:
        eng.add_venue(venue=SIM,oms_type=OmsType.NETTING,account_type=AccountType.CASH,base_currency=CNY,starting_balances=[Money(float(initial_cash),CNY)],fee_model=fee,fill_model=liquidity,book_type=BookType.L2_MBP,liquidity_consumption=True,modules=[cash_module,LiquidityModule(liquidity)])
        native={}
        for symbol,i in master.items():
            prec = 3 if i.tick_size < D('.01') else 2
            inc_str = '.001' if prec == 3 else '.01'
            native[symbol]=Equity(instrument_id=InstrumentId(Symbol(symbol),SIM),raw_symbol=Symbol(symbol.split('.')[0]),currency=CNY,price_precision=prec,price_increment=Price.from_str(inc_str),lot_size=Quantity.from_int(i.lot_size),ts_event=nanos(at(sessions[0],0)),ts_init=nanos(at(sessions[0],0)))
            eng.add_instrument(native[symbol])
        if _tape is None:
            strategy=_ResearchStrategy(bars,rules,master,sessions,actions,signal,initial_cash,participation_rate,native,fee,cash_module,liquidity,portfolio_signal,frozen_orders=frozen_orders,cash_exhaustion_policy=cash_exhaustion_policy,participation_policy=participation_policy)
        else:
            from engine.tape_replay import _TapeStrategy
            strategy=_TapeStrategy(_prepared,_tape,signal,initial_cash,participation_rate,native,fee,cash_module,liquidity, frozen_orders=frozen_orders, cash_exhaustion_policy=cash_exhaustion_policy, participation_policy=participation_policy)
        eng.add_strategy(strategy)
        bar_types={};statistics=dict(data_mode=data_mode,execution_mode=execution_mode,cash_exhaustion_policy=cash_exhaustion_policy,market_events=0,boundary_events=0,peak_batch_events=0)
        if data_mode=='full':
            data=build_events(bars,rules,native,participation_rate,liquidity,bar_types,mode=execution_mode)
            statistics.update(market_events=len(data),peak_batch_events=len(data))
            if data:eng.add_data(data)
            eng.run(start=at(sessions[0],0),end=at(sessions[-1],15))
        else:
            universe_prev_volumes = {}
            for day in sessions:
                raw_day_bars = strategy.by_day.get(day, ())
                active = {i.symbol for eligible, i in strategy.pending if eligible <= nanos(at(day, 9, 30))} if data_mode in {'sparse', 'orders'} else None
                if data_mode in {'sparse', 'orders'} and hasattr(strategy, 'frozen_orders_by_date') and day in strategy.frozen_orders_by_date:
                    active = (active or set()) | {i.symbol for i in strategy.frozen_orders_by_date[day]}
                if data_mode == 'orders':
                    selected = active | {s for s, q in strategy.ledger.positions.items() if q}
                    day_bars = [b for b in raw_day_bars if b.symbol in selected]
                    strategy.current_bars = {b.symbol: b for b in day_bars}
                    statistics['native_market_bars'] = statistics.get('native_market_bars', 0) + sum(b.volume > 0 and rules[b.symbol, day]['trade_status'] == 'OPEN' for b in day_bars)
                else:
                    day_bars = list(raw_day_bars)
                prev_vol_snapshot = dict(universe_prev_volumes)
                data = build_events(day_bars, rules, native, participation_rate, liquidity, bar_types, active, mode=execution_mode, prev_volumes=prev_vol_snapshot)
                for b in raw_day_bars:
                    if rules.get((b.symbol, day), {}).get('trade_status') == 'OPEN' and b.volume > 0:
                        universe_prev_volumes[b.symbol] = b.volume
                statistics['market_events'] += len(data)
                statistics['peak_batch_events'] = max(statistics['peak_batch_events'], len(data) + 1)
                if data: eng.add_data(data)
                eng.add_data([session_boundary(nanos(at(day, 15)) + 1)], client_id=BOUNDARY_CLIENT)
                statistics['boundary_events'] += 1
                eng.run(start=at(sessions[0], 0), end=at(sessions[-1], 15) + timedelta(microseconds=1), streaming=True)
                eng.clear_data()
                liquidity.events.clear(); liquidity.remaining.clear(); liquidity.active.clear(); liquidity.intrabar.clear(); liquidity.consumed.clear()
            eng.end()
        if len(strategy.nav)!=len(sessions):raise RuntimeError("Missing daily reconciliation")
        effective_unresolved = tuple(_prepared.unresolved_entitlements) if (_prepared is not None and _prepared.unresolved_entitlements) else tuple(unresolved_entitlements)
        if effective_unresolved:
            from engine.company_action_admission import verify_zero_entitlements
            verify_zero_entitlements(strategy.nav, effective_unresolved)
        native_cash=D(str(round(eng.portfolio.account(SIM).balance_total(CNY).as_double(), 2)))
        orders=[{'id':str(o.client_order_id),'status':o.status.name,'quantity':str(o.quantity),'filled_quantity':str(o.filled_qty),
                 **({'price': str(o.price)} if getattr(o, 'price', None) is not None else {}),
                 **({'intent_id':strategy.order_intent_ids[str(o.client_order_id)]} if str(o.client_order_id) in strategy.order_intent_ids else {})} for o in eng.cache.orders()]
        pending=[{'symbol':i.symbol,'side':i.side,'quantity':i.quantity,'reason':'END_OF_DATA',
                  **({'intent_id':i.intent_id} if i.intent_id is not None else {}),
                  **({'earliest_execution':i.earliest_execution.isoformat()} if i.earliest_execution else {})} for _,i in strategy.pending]
        unfilled_cap = Decimal('0.0')
        for intent, rej_px, _ in getattr(strategy, 'rejected_intents', []):
            rej_qty = Decimal(str(intent.quantity))
            rej_px_val = Decimal(str(rej_px))
            if rej_qty > 0 and rej_px_val > 0:
                unfilled_cap += rej_qty * rej_px_val
        for o in orders:
            if o['status'] in {'CANCELED', 'EXPIRED'}:
                qty_req = Decimal(str(o.get('quantity', 0)))
                qty_fill = Decimal(str(o.get('filled_quantity', 0)))
                unfilled_qty = qty_req - qty_fill
                if unfilled_qty > 0:
                    px_val = Decimal(str(o.get('price', 0))) if 'price' in o else Decimal('0.0')
                    unfilled_cap += unfilled_qty * px_val
        for t in getattr(strategy, 'trimmed_intents', []):
            unfilled_cap += Decimal(str(t.trimmed_quantity)) * t.reference_price

        uncompleted_sells = {}
        for s, leaves in getattr(strategy, 'uncompleted_sell_leaves', {}).items():
            if leaves > 0:
                uncompleted_sells[s] = str(int(leaves) if leaves == int(leaves) else leaves)

        residual_holdings = {
            s: str(pos.normalize()) if pos % 1 else str(int(pos))
            for s, pos in strategy.ledger.positions.items()
            if s in strategy.failed_sell_symbols and pos > 0
            and strategy.uncompleted_sell_leaves[s] >= pos
        }

        exec_model_desc = (
            "bar-synthetic-l2-v1: bar-derived synthetic L2 with native NT matching; next-bar-open IOC; DAY limits: open price improvement, resting limits at limit; O-H-L-C path; shared bar volume cap; inactive-session pending intents rejected; no observed L2/queue/auction reconstruction"
            if execution_mode == "synthetic_l2" else str(execution_mode)
        )
        result=BacktestResult(native_cash,strategy.ledger.cash,{s:strategy.ledger.positions.get(s,D(0)) for s in master},strategy.fills,strategy.rejections,strategy.nav,list(cash_module.journal),orders,pending,manifest,
                              execution_model=exec_model_desc,
                              schema_version=2,
                              run_statistics=statistics,
                              fee_model=fee,
                              unfilled_capital_amount=unfilled_cap.quantize(D('0.01')),
                              residual_holding_symbols=residual_holdings,
                              uncompleted_sell_orders=uncompleted_sells,
                              trimmed_intents=list(getattr(strategy, 'trimmed_intents', [])))
        result.run_statistics=statistics
        if _tape is not None:statistics.update(input_bars=len(bars),scheduled_intents=len(_tape.entries))
        return result
    finally:
        try:
            # Kernel disposal stops other engines, but not a still-running trader.
            # Do not end() a failed run: it flushes future timers and callbacks.
            if eng.trader.is_running:eng.trader.stop()
        finally:eng.dispose()


def run_backtest(instruments, bars, rules, signal=None, *, corporate_actions=(), actions=None, initial_cash=D(100000), participation_rate=.01, trading_days=None, fee_model=None, portfolio_signal=None, slippage_bps=0, strategy_id=None, strategy_params=None, data_snapshot_id=None, data_mode='full', execution_mode='synthetic_l2', cash_exhaustion_policy='reject', participation_policy='reject', corporate_action_gate_policy='strict', unresolved_entitlements=(), frozen_orders=None):
    """Validate raw inputs and run one isolated shared-cash portfolio."""
    if actions is not None and not corporate_actions:
        corporate_actions = actions
    return _run_backtest(instruments,bars,rules,signal,corporate_actions=corporate_actions,
        initial_cash=initial_cash,participation_rate=participation_rate,trading_days=trading_days,
        fee_model=fee_model,portfolio_signal=portfolio_signal,slippage_bps=slippage_bps,
        strategy_id=strategy_id,strategy_params=strategy_params,data_snapshot_id=data_snapshot_id,data_mode=data_mode,
        execution_mode=execution_mode,cash_exhaustion_policy=cash_exhaustion_policy,participation_policy=participation_policy,corporate_action_gate_policy=corporate_action_gate_policy,unresolved_entitlements=unresolved_entitlements,frozen_orders=frozen_orders)


def run_prepared_backtest(prepared, signal=None, **run_options):
    """Reuse validated market inputs, always constructing fresh runtime accounts."""
    from engine.prepared import PreparedBacktest
    if type(prepared) is not PreparedBacktest:raise TypeError('PreparedBacktest required')
    prepared.assert_compatible()
    if 'corporate_action_gate_policy' in run_options:
        req = run_options['corporate_action_gate_policy']
        if req != prepared.corporate_action_gate_policy:
            raise ValueError(f"Requested corporate_action_gate_policy '{req}' conflicts with prepared policy '{prepared.corporate_action_gate_policy}'. Prepare inputs again with the desired policy.")
    else:
        run_options['corporate_action_gate_policy'] = prepared.corporate_action_gate_policy
    return _run_backtest(prepared.instruments,prepared.bars,prepared.rules,signal,
        corporate_actions=prepared.actions,trading_days=prepared.sessions,_prepared=prepared,**run_options)


def run_frozen_orders_backtest(
    instruments, bars, rules, frozen_orders, *,
    corporate_actions=(), actions=None, initial_cash=D(100000),
    participation_rate=.01, trading_days=None, fee_model=None, data_mode='orders',
    execution_mode='synthetic_l2', cash_exhaustion_policy='reject', participation_policy='reject',
    corporate_action_gate_policy='strict', unresolved_entitlements=(), **kwargs
) -> BacktestResult:
    """Run backtest directly executing frozen orders from upstream (e.g. VectorBT)."""
    if actions is not None and not corporate_actions:
        corporate_actions = actions

    from engine.prepared import prepare_backtest, PreparedBacktest
    from engine.order_tape import prepare_order_tape
    from engine.tape_replay import run_order_tape

    _prep = kwargs.pop('_prepared', None) or kwargs.pop('prepared', None)
    if frozen_orders is not None:
        frozen_orders = tuple(frozen_orders)
    if data_mode == 'orders' and frozen_orders is not None:
        if _prep is None:
            _prep = prepare_backtest(instruments, bars, rules, corporate_actions=corporate_actions,
                trading_days=trading_days, unresolved_entitlements=unresolved_entitlements,
                corporate_action_gate_policy=corporate_action_gate_policy)
        if type(_prep) is not PreparedBacktest:
            raise TypeError('PreparedBacktest required')
        _prep.assert_compatible()
        if _prep.daily_only:
            # Executable-date orders are injected by _start_day, never a fabricated decision.
            return run_order_tape(_prep, prepare_order_tape(_prep, ()), frozen_orders=frozen_orders,
                initial_cash=initial_cash, participation_rate=participation_rate, fee_model=fee_model,
                execution_mode=execution_mode, cash_exhaustion_policy=cash_exhaustion_policy,
                participation_policy=participation_policy,
                corporate_action_gate_policy=corporate_action_gate_policy,
                unresolved_entitlements=unresolved_entitlements, **kwargs)
        instruments, bars, rules, trading_days = _prep.instruments, _prep.bars, _prep.rules, _prep.sessions
        data_mode = 'full'  # Minute orders can remain active across multiple bars.

    return _run_backtest(
        instruments, bars, rules, signal=None, corporate_actions=corporate_actions,
        initial_cash=initial_cash, participation_rate=participation_rate, trading_days=trading_days,
        fee_model=fee_model, data_mode=data_mode, frozen_orders=frozen_orders,
        execution_mode=execution_mode, cash_exhaustion_policy=cash_exhaustion_policy,
        participation_policy=participation_policy,
        corporate_action_gate_policy=corporate_action_gate_policy,
        unresolved_entitlements=unresolved_entitlements, _prepared=_prep, **kwargs
    )


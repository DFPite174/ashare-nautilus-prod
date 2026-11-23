"""Single-contract daily IF replay; Nautilus matches, this module settles in CNY."""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D, ROUND_HALF_UP
import re
from zoneinfo import ZoneInfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

TZ = ZoneInfo("Asia/Shanghai")
MULTIPLIER = D(300)
TICK = D(".2")


def _ns(day, hour, minute=0):
    dt = datetime.combine(day, time(hour, minute), TZ)
    return int(dt.timestamp()) * 10**9


def _money(amount):
    return amount.quantize(D(".01"), rounding=ROUND_HALF_UP)


def _positive(value):
    return isinstance(value, D) and value.is_finite() and value > 0


@dataclass(frozen=True)
class IFContract:
    symbol: str
    listed: date
    expiry: date


@dataclass(frozen=True)
class IFDay:
    day: date
    open: D
    settle: D
    lower: D
    upper: D
    open_capacity: int  # Available contracts under an explicit opening execution model.
    delivery: D | None = None


@dataclass(frozen=True)
class IFOrder:
    day: date
    side: str
    effect: str
    lots: int


@dataclass(frozen=True)
class IFPolicy:
    margin: D
    open_fee: D
    close_today_fee: D
    close_yesterday_fee: D
    delivery_fee: D


@dataclass(frozen=True)
class IFDaily:
    day: date
    cash: D
    position: int
    variation: D
    fees: D
    margin: D
    available: D
    delivered_position: int
    policy: IFPolicy


@dataclass(frozen=True)
class IFResult:
    fills: tuple
    rejections: tuple
    daily: tuple


class _IFReplay(Strategy):
    def __init__(self, instrument, contract, days, orders, cash, policies):
        super().__init__(StrategyConfig())
        self.instrument = instrument
        self.contract = contract
        self.days = days
        self.orders = orders
        self.cash = cash
        self.policies = policies
        self.yesterday = self.today = 0
        self.previous_settle = None
        self.fills, self.rejections, self.daily = [], [], []
        self.submitted = {}
        self.fill_offset = 0

    def on_start(self):
        self.subscribe_quote_ticks(self.instrument.id)
        for day in self.days.values():
            self.clock.set_time_alert_ns(f"settle-{day.day}", _ns(day.day, 15),
                                         callback=lambda event, day=day: self._settle(day))

    def on_quote_tick(self, tick):
        day = self.days[tick.ts_event]
        policy = self.policies[day.day]
        capacity = day.open_capacity
        q = self.yesterday
        projected = self.cash + (D(q) * (day.open - self.previous_settle) * MULTIPLIER if q else 0)
        today, yesterday = self.today, self.yesterday
        for index, order in self.orders[day.day]:
            sign = 1 if order.side == "BUY" else -1
            lots = sign * order.lots
            new_today, new_yesterday = today, yesterday
            reason = None
            if (sign > 0 and day.open == day.upper) or (sign < 0 and day.open == day.lower):
                reason = "LIMIT_LOCK"
            elif order.lots > capacity:
                reason = "OPEN_CAPACITY"
            elif order.effect == "OPEN":
                if q * sign < 0:
                    reason = "OPPOSITE_POSITION"
                new_today += lots
            else:
                bucket = today if order.effect == "CLOSE_TODAY" else yesterday
                if bucket * sign >= 0 or abs(bucket) < order.lots:
                    reason = "INSUFFICIENT_CLOSE_POSITION"
                if order.effect == "CLOSE_TODAY":
                    new_today += lots
                else:
                    new_yesterday += lots
            rate = {"OPEN": policy.open_fee, "CLOSE_TODAY": policy.close_today_fee,
                    "CLOSE_YESTERDAY": policy.close_yesterday_fee}[order.effect]
            fee = _money(day.open * MULTIPLIER * order.lots * rate)
            margin = _money(abs(q + lots) * day.open * MULTIPLIER * policy.margin)
            if not reason and order.effect == "OPEN" and projected - fee < margin:
                reason = "INSUFFICIENT_MARGIN"
            if reason:
                self.rejections.append((index, reason))
                continue
            native = self.order_factory.limit(
                instrument_id=self.instrument.id, order_side=OrderSide.BUY if sign > 0 else OrderSide.SELL,
                quantity=Quantity.from_int(order.lots), price=tick.ask_price, time_in_force=TimeInForce.IOC)
            self.submitted[str(native.client_order_id)] = (index, order, fee, day.open)
            self.submit_order(native)
            capacity -= order.lots
            q += lots
            today, yesterday = new_today, new_yesterday
            projected -= fee

    def on_order_filled(self, event):
        index, order, fee, price = self.submitted.pop(str(event.client_order_id))
        if D(str(event.last_px)) != price or int(event.last_qty) != order.lots:
            raise RuntimeError("Native fill differs from reserved opening execution")
        lots = order.lots if order.side == "BUY" else -order.lots
        if order.effect == "CLOSE_YESTERDAY":
            self.yesterday += lots
        else:
            self.today += lots
        self.fills.append((index, order, price, fee))

    def _settle(self, day):
        policy = self.policies[day.day]
        if self.submitted:
            raise RuntimeError("Reserved IF orders did not fill in NautilusTrader")
        position = self.yesterday + self.today
        if D(str(self.portfolio.net_position(self.instrument.id))) != position:
            raise RuntimeError("Native IF position disagrees with settlement book")
        delivered = position if day.day == self.contract.expiry else 0
        if delivered and day.delivery is None:
            raise ValueError("Missing delivery settlement price for open expiry position")
        settle = day.delivery if delivered else day.settle
        carry = self.daily[-1].position if self.daily else 0
        daily_fills = self.fills[self.fill_offset:]
        variation = (carry * (settle - self.previous_settle) if carry else D(0))
        variation += sum((settle-price)*order.lots*(1 if order.side == "BUY" else -1)
                         for _, order, price, _ in daily_fills)
        variation = _money(variation * MULTIPLIER)
        fees = sum((fee for _, _, _, fee in daily_fills), D(0))
        if delivered:
            fees += _money(abs(delivered) * settle * MULTIPLIER * policy.delivery_fee)
            position = 0
        self.cash += variation - fees
        margin = _money(abs(position) * settle * MULTIPLIER * policy.margin)
        self.daily.append(IFDaily(day.day, self.cash, position, variation, fees, margin,
                                 self.cash-margin, delivered, policy))
        self.previous_settle = settle
        self.yesterday, self.today = position, 0
        self.fill_offset = len(self.fills)


def _check_policy(policy):
    if not isinstance(policy, IFPolicy) or not _positive(policy.margin) or policy.margin > 1 or any(
        not isinstance(r, D) or not r.is_finite() or not 0 <= r <= 1
        for r in (policy.open_fee, policy.close_today_fee, policy.close_yesterday_fee, policy.delivery_fee)
    ):
        raise ValueError("Invalid IF margin or fee policy")


def _effective_policies(days, policy):
    if not isinstance(policy, Mapping):
        _check_policy(policy)
        return dict.fromkeys(days, policy)
    schedule = list(policy.items())
    if any(type(key) is not date for key, _ in schedule):
        raise ValueError("IF policy effective date must be a date")
    for _, item in schedule:
        _check_policy(item)
    schedule.sort()
    active = None
    index = 0
    policies = {}
    for day in days:
        while index < len(schedule) and schedule[index][0] <= day:
            active = schedule[index][1]
            index += 1
        if active is None:
            raise ValueError("Policy schedule does not cover the first IF session")
        policies[day] = active
    return policies


def replay_if(contract, days, orders, *, initial_cash, policy):
    """Replay pre-scheduled orders against opening quotes and supplied settlement prices."""
    days = tuple(days)
    if (not re.fullmatch(r"IF\d{4}", contract.symbol) or contract.listed > contract.expiry
            or not days or not _positive(initial_cash) or _money(initial_cash) != initial_cash):
        raise ValueError("Invalid IF contract, sessions or external cash")
    previous = None
    for d in days:
        if (not contract.listed <= d.day <= contract.expiry or (previous is not None and d.day <= previous)
                or not all(_positive(p) for p in (d.open, d.settle, d.lower, d.upper))
                or not d.lower <= d.open <= d.upper
                or any(p % TICK for p in (d.open, d.lower, d.upper))
                or type(d.open_capacity) is not int or d.open_capacity < 0):
            raise ValueError(f"Invalid or unordered IF session: {d.day}")
        if d.delivery is not None and (d.day != contract.expiry or not _positive(d.delivery)):
            raise ValueError("Invalid delivery settlement input")
        previous = d.day
    policies = _effective_policies(tuple(d.day for d in days), policy)
    sessions = policies.keys()
    by_day = defaultdict(list)
    for index, o in enumerate(orders):
        if (o.day not in sessions or o.side not in {"BUY", "SELL"} or type(o.lots) is not int or o.lots <= 0
                or o.effect not in {"OPEN", "CLOSE_TODAY", "CLOSE_YESTERDAY"}):
            raise ValueError(f"Invalid IF order at index {index}")
        by_day[o.day].append((index, o))

    venue = Venue("CFFEX")
    instrument = FuturesContract(
        InstrumentId(Symbol(contract.symbol), venue), Symbol(contract.symbol), AssetClass.INDEX,
        CNY, 1, Price.from_str("0.2"), Quantity.from_int(300), Quantity.from_int(1), "CSI300",
        _ns(contract.listed, 0), _ns(contract.expiry + timedelta(days=1), 0),
        0, 0, margin_init=policies[days[0].day].margin, margin_maint=policies[days[0].day].margin)
    by_ns = {_ns(d.day, 9, 30): d for d in days}
    ticks = []
    for ns, day in by_ns.items():
        price = Price.from_str(format(day.open, ".1f"))
        size = Quantity.from_int(max(day.open_capacity, 1))
        ticks.append(QuoteTick(instrument.id, price, price, size, size, ns, ns))
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR"), run_analysis=False))
    try:
        # Local IF settlement is authoritative; NT's generic margin account is not a CFFEX ledger.
        engine.add_venue(venue, OmsType.NETTING, AccountType.MARGIN, [Money(initial_cash, CNY)],
                         base_currency=CNY, frozen_account=True)
        engine.add_instrument(instrument)
        strategy = _IFReplay(instrument, contract, by_ns, by_day, initial_cash, policies)
        engine.add_strategy(strategy)
        engine.add_data(ticks)
        engine.run(end=_ns(days[-1].day, 15) + 1)
        if len(strategy.daily) != len(days):
            raise RuntimeError("Missing daily IF settlement")
        return IFResult(tuple(strategy.fills), tuple(strategy.rejections), tuple(strategy.daily))
    finally:
        engine.dispose()

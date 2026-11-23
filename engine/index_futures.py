"""Daily CFFEX equity index futures replay and CNY settlement."""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from datetime import date, datetime, time
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
PRODUCTS = {"IF": (300, "CSI300"), "IH": (300, "SSE50"),
            "IC": (200, "CSI500"), "IM": (200, "CSI1000")}
TICK = D(".2")


def _ns(day, hour, minute=0):
    dt = datetime.combine(day, time(hour, minute), TZ)
    return int(dt.timestamp()) * 10**9


def _money(amount):
    return amount.quantize(D(".01"), rounding=ROUND_HALF_UP)


def _positive(value):
    return isinstance(value, D) and value.is_finite() and value > 0


def _margin_requirement(notionals, policies, groups):
    gross = ungrouped = D(0)
    sides = {}
    for symbol, notional in notionals.items():
        if not notional:
            continue
        amount = _money(abs(notional) * policies[symbol].margin)
        gross += amount
        group = groups.get(symbol[:2])
        if group is None:
            ungrouped += amount
        else:
            side = sides.setdefault(group, [D(0), D(0)])
            side[notional < 0] += amount
    return gross, ungrouped + sum(max(side) for side in sides.values())


@dataclass(frozen=True)
class IFContract:
    symbol: str
    listed: date
    expiry: date

    @property
    def multiplier(self):
        return PRODUCTS[self.symbol[:2]][0]


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
    depends_on: int | None = None
    allow_partial: bool = False
    limit_price: D | None = None


@dataclass(frozen=True)
class IFPolicy:
    margin: D
    open_fee: D
    close_today_fee: D
    close_yesterday_fee: D
    delivery_fee: D
    submission_fee: D = D(0)
    max_order_lots: int | None = None
    max_position_lots: int | None = None
    max_daily_open_lots: int | None = None


@dataclass(frozen=True)
class IFDaily:
    day: date
    cash: D
    positions: Mapping[str, int]
    variation: D
    fees: D
    margin: D
    available: D
    deliveries: Mapping[str, int]
    policy: IFPolicy
    contract_policies: Mapping[str, IFPolicy]
    notionals: Mapping[str, D]
    gross_margin: D
    submission_fees: D
    cash_deposit: D

    @property
    def margin_shortfall(self):
        return max(D(0), -self.available)

    @property
    def position(self):
        return _single(self.positions)

    @property
    def delivered_position(self):
        return _single(self.deliveries)


def _single(values):
    if len(values) != 1:
        raise ValueError("Use the per-contract mapping for a multi-contract replay")
    return next(iter(values.values()))


@dataclass(frozen=True)
class IFResult:
    fills: tuple
    rejections: tuple
    daily: tuple
    order_symbols: tuple[str, ...]
    unfilled_lots: Mapping[int, int]
    evidence: Mapping | None = None


class _IFReplay(Strategy):
    def __init__(self, instruments, contracts, sessions, orders, order_symbols, cash, policies, overrides,
                 groups, deposits, confirm_before_next, audit):
        super().__init__(StrategyConfig())
        self.instruments, self.contracts, self.sessions = instruments, contracts, sessions
        self.orders, self.cash, self.policies = orders, cash, policies
        self.order_symbols = order_symbols
        self.overrides = overrides
        self.groups, self.deposits = groups, deposits
        self.confirm_before_next = confirm_before_next
        self.fill_times = {} if audit else None
        self.capacity_before = {} if audit else None
        self.multipliers = {s: c.multiplier for s, c in contracts.items()}
        self.carry = dict.fromkeys(contracts, 0)
        self.yesterday = self.carry.copy()
        self.today = self.yesterday.copy()
        self.previous_settle = {}
        self.fills, self.rejections, self.daily = [], [], []
        self.submitted = {}
        self.unfilled_lots = {}
        self.submission_fees = D(0)
        self.pending_delivery = {}
        self.fill_offset = 0
        self.symbols = {v.id: k for k, v in instruments.items()}
        self.quotes = {}

    def on_start(self):
        for instrument in self.instruments.values():
            self.subscribe_quote_ticks(instrument.id)
        for day, market in self.sessions.items():
            start = _ns(day, 9, 30)
            self.clock.set_time_alert_ns(f"open-{day}", start + 1,
                                         callback=lambda event, day=day, market=market: self._execute(day, market))
            self.clock.set_time_alert_ns(f"settle-{day}", _ns(day, 15),
                                         callback=lambda event, day=day: self._settle(day))

    def on_quote_tick(self, tick):
        self.quotes[self.symbols[tick.instrument_id]] = tick

    def _execute(self, day, market):
        policies = self.overrides[day]
        self.cash += self.deposits.get(day, D(0))
        capacity = {symbol: row.open_capacity for symbol, row in market.items()}
        opened = defaultdict(int)
        today, yesterday = self.today.copy(), self.yesterday.copy()
        completed = set()
        notionals = {s: row.open * self.multipliers[s] for s, row in market.items()}
        projected = self.cash + sum(
            q * (market[s].open - self.previous_settle[s]) * self.multipliers[s]
            for s, q in yesterday.items() if q)
        for index, symbol, order in self.orders[day]:
            row = market[symbol]
            policy = policies[symbol]
            tick = self.quotes[symbol]
            if tick.ts_event != _ns(day, 9, 30):
                raise RuntimeError("Missing current IF opening quote")
            q = yesterday[symbol] + today[symbol]
            sign = 1 if order.side == "BUY" else -1
            executable = min(order.lots, capacity[symbol]) if order.allow_partial else order.lots
            reason = None
            limit = order.limit_price
            if self.capacity_before is not None:
                self.capacity_before[index] = capacity[symbol]
            if order.depends_on is not None and order.depends_on not in completed:
                reason = "DEPENDENCY_UNFILLED"
            elif policy.max_order_lots is not None and order.lots > policy.max_order_lots:
                reason = "ORDER_LIMIT"
            elif limit is not None and not row.lower <= limit <= row.upper:
                reason = "PRICE_OUTSIDE_BAND"
            elif limit is not None and sign * (row.open - limit) > 0:
                reason = "NOT_MARKETABLE"
            elif (sign > 0 and row.open == row.upper) or (sign < 0 and row.open == row.lower):
                reason = "LIMIT_LOCK"
            elif executable == 0 or executable > capacity[symbol]:
                reason = "OPEN_CAPACITY"
            elif order.effect == "OPEN":
                if q * sign < 0:
                    reason = "OPPOSITE_POSITION"
                elif policy.max_position_lots is not None and abs(q) + executable > policy.max_position_lots:
                    reason = "POSITION_LIMIT"
                elif policy.max_daily_open_lots is not None and opened[symbol] + executable > policy.max_daily_open_lots:
                    reason = "DAILY_OPEN_LIMIT"
            else:
                holding = today[symbol] if order.effect == "CLOSE_TODAY" else yesterday[symbol]
                if holding * sign >= 0 or abs(holding) < order.lots:
                    reason = "INSUFFICIENT_CLOSE_POSITION"
            rate = {"OPEN": policy.open_fee, "CLOSE_TODAY": policy.close_today_fee,
                    "CLOSE_YESTERDAY": policy.close_yesterday_fee}[order.effect]
            fee = _money(notionals[symbol] * executable * rate)
            if not reason and order.effect == "OPEN":
                needed = {}
                for s, amount in notionals.items():
                    quantity = yesterday[s] + today[s] + (sign * executable if s == symbol else 0)
                    if quantity:
                        needed[s] = quantity * amount
                _, margin = _margin_requirement(needed, policies, self.groups)
                if projected - fee - policy.submission_fee < margin:
                    reason = "INSUFFICIENT_MARGIN"
            if reason:
                self.rejections.append((index, reason))
                continue
            native = self.order_factory.limit(
                instrument_id=tick.instrument_id, order_side=OrderSide.BUY if sign > 0 else OrderSide.SELL,
                quantity=Quantity.from_int(executable),
                price=tick.ask_price if limit is None else Price.from_str(format(limit, ".2f")),
                time_in_force=TimeInForce.IOC)
            native_id = str(native.client_order_id)
            executed_order = order if executable == order.lots else replace(order, lots=executable)
            self.submitted[native_id] = (index, symbol, executed_order, fee, row.open)
            self.submit_order(native)
            if self.confirm_before_next and native_id in self.submitted:
                raise RuntimeError("Reserved IF order did not fill synchronously in NautilusTrader")
            capacity[symbol] -= executable
            bucket = yesterday if order.effect == "CLOSE_YESTERDAY" else today
            bucket[symbol] += sign * executable
            if order.effect == "OPEN" and policy.max_daily_open_lots is not None:
                opened[symbol] += executable
            self.submission_fees += policy.submission_fee
            projected -= fee + policy.submission_fee
            if executable == order.lots:
                completed.add(index)
            else:
                self.unfilled_lots[index] = order.lots - executable

    def on_order_filled(self, event):
        order_id = str(event.client_order_id)
        if order_id.startswith("EXPIRATION-LEG-"):
            symbol = self.symbols[event.instrument_id]
            expected = self.pending_delivery.pop(symbol, None)
            side = 1 if event.order_side == OrderSide.BUY else -1
            actual = (event.ts_event, side * int(event.last_qty), D(str(event.last_px)))
            if actual != expected:
                raise RuntimeError("Native expiry differs from CFFEX cash delivery")
            return
        index, symbol, order, fee, price = self.submitted.pop(order_id)
        if D(str(event.last_px)) != price or int(event.last_qty) != order.lots:
            raise RuntimeError("Native fill differs from reserved opening execution")
        lots = order.lots if order.side == "BUY" else -order.lots
        bucket = self.yesterday if order.effect == "CLOSE_YESTERDAY" else self.today
        bucket[symbol] += lots
        self.fills.append((index, order, price, fee))
        if self.fill_times is not None:
            self.fill_times[index] = event.ts_event

    def _settle(self, day):
        policies = self.overrides[day]
        if self.submitted:
            raise RuntimeError("Reserved IF orders did not fill in NautilusTrader")
        positions = self.yesterday.copy()
        deliveries = dict.fromkeys(positions, 0)
        notionals = dict.fromkeys(positions, D(0))
        variation, fees = D(0), self.submission_fees
        fills = defaultdict(list)
        for index, order, price, fee in self.fills[self.fill_offset:]:
            fills[self.order_symbols[index]].append((order, price))
            fees += fee
        for symbol, row in self.sessions[day].items():
            policy = policies[symbol]
            multiplier = self.multipliers[symbol]
            position = self.yesterday[symbol] + self.today[symbol]
            if D(str(self.portfolio.net_position(self.instruments[symbol].id))) != position:
                raise RuntimeError("Native IF position disagrees with settlement book")
            delivered = position if day == self.contracts[symbol].expiry else 0
            if delivered and row.delivery is None:
                raise ValueError("Missing delivery settlement price for open expiry position")
            settle = row.delivery if delivered else row.settle
            carry = self.carry[symbol]
            pnl = carry * (settle - self.previous_settle[symbol]) if carry else D(0)
            pnl += sum((settle-price)*order.lots*(1 if order.side == "BUY" else -1)
                       for order, price in fills[symbol])
            variation += _money(pnl * multiplier)
            if delivered:
                self.pending_delivery[symbol] = (_ns(day, 15) + 1, -delivered, settle)
                fees += _money(abs(delivered) * settle * multiplier * policy.delivery_fee)
                position = 0
            positions[symbol] = position
            deliveries[symbol] = delivered
            notionals[symbol] = position * settle * multiplier
            self.previous_settle[symbol] = settle
        gross, margin = _margin_requirement({s: notionals[s] for s in policies}, policies, self.groups)
        self.cash += variation - fees
        self.daily.append(IFDaily(day, self.cash, MappingProxyType(positions), variation, fees, margin,
                                 self.cash-margin, MappingProxyType(deliveries), self.policies[day],
                                 MappingProxyType(policies), MappingProxyType(notionals), gross,
                                 self.submission_fees, self.deposits.get(day, D(0))))
        self.submission_fees = D(0)
        self.carry = positions
        self.yesterday, self.today = positions.copy(), dict.fromkeys(positions, 0)
        self.fill_offset = len(self.fills)


def _check_policy(policy):
    if not isinstance(policy, IFPolicy) or not _positive(policy.margin) or policy.margin > 1 or any(
        not isinstance(r, D) or not r.is_finite() or not 0 <= r <= 1
        for r in (policy.open_fee, policy.close_today_fee, policy.close_yesterday_fee, policy.delivery_fee)
    ) or (
        not isinstance(policy.submission_fee, D) or not policy.submission_fee.is_finite()
        or policy.submission_fee < 0 or _money(policy.submission_fee) != policy.submission_fee
    ):
        raise ValueError("Invalid IF margin or fee policy")
    for limit in (policy.max_order_lots, policy.max_position_lots, policy.max_daily_open_lots):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("IF order and position limits must be non-negative integer lots")


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


def _build_evidence(strategy, orders, initial_cash, snapshot, capacity_model):
    from hashlib import sha256
    from importlib.metadata import version
    from pathlib import Path

    from engine.reproducibility import digest

    fills = {index: (order, price, fee) for index, order, price, fee in strategy.fills}
    rejects = dict(strategy.rejections)
    trace = []
    for index, (symbol, order) in enumerate(orders):
        executed, price, fee = fills.get(index, (None, None, D(0)))
        filled = executed.lots if executed else 0
        market = strategy.sessions[order.day][symbol]
        trace.append(MappingProxyType({
            "index": index, "symbol": symbol, "day": order.day, "side": order.side, "effect": order.effect,
            "requested_lots": order.lots, "limit_price": order.limit_price, "depends_on": order.depends_on,
            "allow_partial": order.allow_partial, "filled_lots": filled, "unfilled_lots": order.lots - filled,
            "opening_price": market.open, "opening_band": (market.lower, market.upper),
            "available_opening_lots": strategy.capacity_before[index],
            "fill_price": price, "fill_ts_ns": strategy.fill_times.get(index), "trade_fee": fee,
            "submission_fee": strategy.overrides[order.day][symbol].submission_fee if filled else D(0),
            "reason": rejects.get(index),
        }))
    return MappingProxyType({
        "schema": "cffex-daily-audit-v1", "nautilus_version": version("nautilus-trader"),
        "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_snapshot_id": snapshot, "capacity_model": capacity_model,
        "input_sha256": digest({
            "contracts": strategy.contracts, "sessions": strategy.sessions, "orders": orders,
            "initial_cash": initial_cash, "policy": strategy.policies, "contract_policies": strategy.overrides,
            "margin_groups": strategy.groups, "cash_deposits": strategy.deposits,
            "data_snapshot_id": snapshot, "capacity_model": capacity_model,
        }),
        "result_sha256": digest({"orders": trace, "daily": strategy.daily}),
        "orders": tuple(trace),
    })


def replay_if(contract, days, orders, *, initial_cash, policy, cash_deposits=None, audit=False,
              data_snapshot_id=None, capacity_model=None):
    """Replay a single IF contract using the shared futures account."""
    return replay_if_portfolio({contract: days}, ((contract.symbol, order) for order in orders),
                               initial_cash=initial_cash, policy=policy, cash_deposits=cash_deposits,
                               audit=audit, data_snapshot_id=data_snapshot_id, capacity_model=capacity_model)


def replay_if_portfolio(contracts, orders, *, initial_cash, policy, contract_policies=None, trading_days=None,
                        margin_groups=(), cash_deposits=None, audit=False, data_snapshot_id=None,
                        capacity_model=None):
    """Replay (symbol, IFOrder) pairs in input order on each execution date."""
    if not contracts or not _positive(initial_cash) or _money(initial_cash) != initial_cash:
        raise ValueError("Invalid IF contracts or external cash")
    if audit and not all(isinstance(label, str) and label.strip() for label in (data_snapshot_id, capacity_model)):
        raise ValueError("Futures audit requires a data snapshot ID and an opening capacity model")
    by_symbol, market = {}, defaultdict(dict)
    for contract, days in contracts.items():
        if (not re.fullmatch(r"I[FHCM][0-9]{2}(0[1-9]|1[0-2])", contract.symbol)
                or contract.listed > contract.expiry
                or contract.symbol in by_symbol):
            raise ValueError("Invalid or duplicate IF contract")
        by_symbol[contract.symbol] = contract
        previous = None
        for d in days:
            if (not contract.listed <= d.day <= contract.expiry or (previous is not None and d.day <= previous)
                    or not all(_positive(p) for p in (d.open, d.settle, d.lower, d.upper))
                    or not d.lower <= d.open <= d.upper
                    or any(p % TICK for p in (d.open, d.lower, d.upper))
                    or type(d.open_capacity) is not int or d.open_capacity < 0):
                raise ValueError(f"Invalid or unordered IF session: {d.day}")
            if d.delivery is not None and (d.day != contract.expiry or not _positive(d.delivery)
                                           or _money(d.delivery) != d.delivery):
                raise ValueError("Invalid delivery settlement input")
            market[d.day][contract.symbol] = d
            previous = d.day
        if previous is None:
            raise ValueError(f"No sessions for {contract.symbol}")
    market = dict(sorted(market.items()))
    if trading_days is not None and tuple(trading_days) != tuple(market):
        raise ValueError("Supplied trading calendar differs from futures sessions")
    for day, rows in market.items():
        if any(c.listed <= day <= c.expiry and s not in rows for s, c in by_symbol.items()):
            raise ValueError(f"Missing IF session data on {day}")
    groups = {}
    for group_id, group in enumerate(margin_groups):
        if isinstance(group, str) or not group:
            raise ValueError("A margin group must contain one or more product codes")
        for product in group:
            if product not in PRODUCTS or product in groups:
                raise ValueError("Unknown or repeated product in margin groups")
            groups[product] = group_id
    deposits = dict(cash_deposits or {})
    if any(day not in market or not _positive(value) or _money(value) != value
           for day, value in deposits.items()):
        raise ValueError("A cash deposit requires a replay day and a positive cent amount")
    policies = _effective_policies(tuple(market), policy)
    contract_policies = {} if contract_policies is None else contract_policies
    if contract_policies.keys() - by_symbol.keys():
        raise ValueError("Policy override for unknown futures contract")
    schedules = {s: _effective_policies((day for day, rows in market.items() if s in rows), rule)
                 for s, rule in contract_policies.items()}
    overrides = {day: {s: schedules[s][day] if s in schedules else policies[day] for s in rows}
                 for day, rows in market.items()}
    by_day = defaultdict(list)
    order_symbols = {}
    order_days = []
    audited_orders = [] if audit else None
    confirm_before_next = False
    for index, (symbol, o) in enumerate(orders):
        if (symbol not in market.get(o.day, {}) or o.side not in {"BUY", "SELL"}
                or type(o.lots) is not int or o.lots <= 0
                or o.effect not in {"OPEN", "CLOSE_TODAY", "CLOSE_YESTERDAY"} or type(o.allow_partial) is not bool):
            raise ValueError(f"Invalid IF order at index {index}")
        if o.depends_on is not None and (type(o.depends_on) is not int or not 0 <= o.depends_on < index
                                         or order_days[o.depends_on] != o.day):
            raise ValueError(f"Order dependency must refer to an earlier order on the same day: {index}")
        if o.limit_price is not None and (not _positive(o.limit_price) or o.limit_price % TICK):
            raise ValueError(f"Invalid IF limit price at index {index}")
        confirm_before_next |= o.depends_on is not None
        by_day[o.day].append((index, symbol, o))
        order_symbols[index] = symbol
        order_days.append(o.day)
        if audit:
            audited_orders.append((symbol, o))
    venue = Venue("CFFEX")
    instruments = {
        s: FuturesContract(InstrumentId(Symbol(s), venue), Symbol(s), AssetClass.INDEX,
                           CNY, 2, Price.from_str("0.20"), Quantity.from_int(c.multiplier),
                           Quantity.from_int(1), PRODUCTS[s[:2]][1],
                           _ns(c.listed, 0), _ns(c.expiry, 15) + 1, 0, 0)
        for s, c in by_symbol.items()
    }
    ticks = []
    for day, rows in market.items():
        ns = _ns(day, 9, 30)
        for symbol, row in rows.items():
            price = Price.from_str(format(row.open, ".2f"))
            size = Quantity.from_int(max(row.open_capacity, 1))
            ticks.append(QuoteTick(instruments[symbol].id, price, price, size, size, ns, ns))
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(log_level="ERROR"), run_analysis=False))
    try:
        # Local IF settlement is authoritative; NT's generic margin account is not a CFFEX ledger.
        delivery_prices = {instruments[s].id: float(row.delivery) for rows in market.values()
                           for s, row in rows.items() if row.delivery is not None}
        engine.add_venue(venue, OmsType.NETTING, AccountType.MARGIN, [Money(initial_cash, CNY)],
                         base_currency=CNY, frozen_account=True, settlement_prices=delivery_prices,
                         use_message_queue=not confirm_before_next)
        for instrument in instruments.values():
            engine.add_instrument(instrument)
        strategy = _IFReplay(instruments, by_symbol, market, by_day, order_symbols, initial_cash,
                             policies, overrides, groups, deposits, confirm_before_next, audit)
        engine.add_strategy(strategy)
        engine.add_data(ticks)
        engine.run(end=_ns(max(market), 15) + 2)
        if len(strategy.daily) != len(market):
            raise RuntimeError("Missing daily IF settlement")
        if strategy.pending_delivery:
            raise RuntimeError("Missing NautilusTrader expiry fill")
        evidence = None
        if audit:
            evidence = _build_evidence(strategy, audited_orders, initial_cash, data_snapshot_id, capacity_model)
        return IFResult(tuple(strategy.fills), tuple(strategy.rejections), tuple(strategy.daily),
                        tuple(order_symbols.values()), MappingProxyType(strategy.unfilled_lots), evidence)
    finally:
        engine.dispose()

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


def _opening_time(day, rows):
    if next(iter(rows.values())).opening_quote is None:
        return _ns(day, 9, 30)
    return max(row.opening_quote.ts_ns for row in rows.values())


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
class IFOpeningQuote:
    ts_ns: int
    bid: D
    ask: D
    bid_lots: int
    ask_lots: int
    source: str


@dataclass(frozen=True)
class IFDay:
    day: date
    open: D
    settle: D
    lower: D
    upper: D
    open_capacity: int  # Available contracts under an explicit opening execution model.
    delivery: D | None = None
    opening_quote: IFOpeningQuote | None = None


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


@dataclass(frozen=True)
class IFReferenceFill:
    fill_id: str
    order_index: int
    symbol: str
    side: str
    ts_ns: int
    lots: int
    price: D


@dataclass(frozen=True)
class IFOpeningComparison:
    source: str
    reference_sha256: str
    replay_result_sha256: str
    timestamp_tolerance_ns: int
    differences: Mapping[int, tuple[str, ...]]

    @property
    def matches(self):
        return not self.differences


def compare_opening_fills(result, reference_fills, *, source, timestamp_tolerance_ns=0):
    """Compare an audited run with a complete, order-indexed external fill extract."""
    from engine.reproducibility import digest

    if result.evidence is None or not isinstance(source, str) or not source.strip():
        raise ValueError("Opening comparison requires an audited result and reference source")
    if type(timestamp_tolerance_ns) is not int or timestamp_tolerance_ns < 0:
        raise ValueError("Timing tolerance must be a non-negative integer in nanoseconds")
    orders = result.evidence["orders"]
    by_order, seen = defaultdict(list), set()
    for fill in reference_fills:
        if (not isinstance(fill, IFReferenceFill)
                or not isinstance(fill.fill_id, str) or not fill.fill_id.strip()
                or type(fill.order_index) is not int or not 0 <= fill.order_index < len(orders)
                or type(fill.lots) is not int or fill.lots <= 0 or not _positive(fill.price) or fill.price % TICK
                or type(fill.ts_ns) is not int or fill.ts_ns <= 0):
            raise ValueError("Invalid reference fill")
        if fill.fill_id in seen:
            raise ValueError(f"Reference contains duplicate fill ID: {fill.fill_id}")
        order = orders[fill.order_index]
        if (fill.symbol, fill.side) != (order["symbol"], order["side"]):
            raise ValueError(f"Reference fill does not identify the replay order: {fill.fill_id}")
        seen.add(fill.fill_id)
        by_order[fill.order_index].append(fill)
    differences = {}
    for order in orders:
        index = order["index"]
        recorded = by_order.get(index, ())
        lots = order["filled_lots"]
        if not lots and not recorded:
            continue
        if not recorded or not lots:
            differences[index] = ("MISSING_REFERENCE_FILL" if lots else "MISSING_REPLAY_FILL",)
            continue
        total = sum(fill.lots for fill in recorded)
        reasons = []
        if lots != total:
            reasons.append("QUANTITY")
        if order["fill_price"] * total != sum(fill.price * fill.lots for fill in recorded):
            reasons.append("PRICE")
        if any(abs(fill.ts_ns - order["fill_ts_ns"]) > timestamp_tolerance_ns for fill in recorded):
            reasons.append("TIME")
        if reasons:
            differences[index] = tuple(reasons)
    ordered = sorted((fill for values in by_order.values() for fill in values), key=lambda fill: fill.fill_id)
    return IFOpeningComparison(source, digest(ordered), result.evidence["result_sha256"],
                               timestamp_tolerance_ns, MappingProxyType(differences))


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
            start = _opening_time(day, market)
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
        observed = next(iter(market.values())).opening_quote is not None
        side_capacity = ({s: [q.opening_quote.ask_lots, q.opening_quote.bid_lots] for s, q in market.items()}
                         if observed else None)
        opened = defaultdict(int)
        today, yesterday = self.today.copy(), self.yesterday.copy()
        completed = set()
        marks = {s: ((row.opening_quote.ask + row.opening_quote.bid) / 2 if observed else row.open)
                 for s, row in market.items()}
        notionals = {s: mark * self.multipliers[s] for s, mark in marks.items()}
        projected = self.cash + sum(
            q * (marks[s] - self.previous_settle[s]) * self.multipliers[s]
            for s, q in yesterday.items() if q)
        opening_ts = _ns(day, 9, 30)
        for index, symbol, order in self.orders[day]:
            row = market[symbol]
            policy = policies[symbol]
            tick = self.quotes[symbol]
            if tick.ts_event != (row.opening_quote.ts_ns if observed else opening_ts):
                raise RuntimeError("Missing current IF opening quote")
            q = yesterday[symbol] + today[symbol]
            sign = 1 if order.side == "BUY" else -1
            sell = sign < 0
            left = side_capacity[symbol][sell] if observed else capacity[symbol]
            price = (row.opening_quote.bid if sell else row.opening_quote.ask) if observed else row.open
            executable = min(order.lots, left) if order.allow_partial else order.lots
            reason = None
            limit = order.limit_price
            if self.capacity_before is not None:
                self.capacity_before[index] = left
            if order.depends_on is not None and order.depends_on not in completed:
                reason = "DEPENDENCY_UNFILLED"
            elif policy.max_order_lots is not None and order.lots > policy.max_order_lots:
                reason = "ORDER_LIMIT"
            elif limit is not None and not row.lower <= limit <= row.upper:
                reason = "PRICE_OUTSIDE_BAND"
            elif limit is not None and sign * (price - limit) > 0:
                reason = "NOT_MARKETABLE"
            elif not observed and ((sign > 0 and price == row.upper) or (sign < 0 and price == row.lower)):
                reason = "LIMIT_LOCK"
            elif executable == 0 or executable > left:
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
            fee = _money(price * self.multipliers[symbol] * executable * rate)
            execution_cost = sign * executable * (price - marks[symbol]) * self.multipliers[symbol] if observed else D(0)
            if not reason and order.effect == "OPEN":
                needed = {}
                for s, amount in notionals.items():
                    quantity = yesterday[s] + today[s] + (sign * executable if s == symbol else 0)
                    if quantity:
                        needed[s] = quantity * amount
                _, margin = _margin_requirement(needed, policies, self.groups)
                if projected - fee - policy.submission_fee - execution_cost < margin:
                    reason = "INSUFFICIENT_MARGIN"
            if reason:
                self.rejections.append((index, reason))
                continue
            native = self.order_factory.limit(
                instrument_id=tick.instrument_id, order_side=OrderSide.BUY if sign > 0 else OrderSide.SELL,
                quantity=Quantity.from_int(executable),
                price=(tick.bid_price if sell else tick.ask_price) if limit is None else Price.from_str(format(limit, ".2f")),
                time_in_force=TimeInForce.IOC)
            native_id = str(native.client_order_id)
            executed_order = order if executable == order.lots else replace(order, lots=executable)
            self.submitted[native_id] = (index, symbol, executed_order, fee, price)
            self.submit_order(native)
            if self.confirm_before_next and native_id in self.submitted:
                raise RuntimeError("Reserved IF order did not fill synchronously in NautilusTrader")
            if observed:
                side_capacity[symbol][sell] -= executable
            else:
                capacity[symbol] -= executable
            bucket = yesterday if order.effect == "CLOSE_YESTERDAY" else today
            bucket[symbol] += sign * executable
            if order.effect == "OPEN" and policy.max_daily_open_lots is not None:
                opened[symbol] += executable
            self.submission_fees += policy.submission_fee
            projected -= fee + policy.submission_fee + execution_cost
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


def _build_evidence(strategy, orders, initial_cash, snapshot, capacity_model, max_open_quote_age_ns):
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
            "opening_quote_ts_ns": market.opening_quote.ts_ns if market.opening_quote else None,
            "opening_quote_source": market.opening_quote.source if market.opening_quote else None,
            "opening_bid": market.opening_quote.bid if market.opening_quote else None,
            "opening_ask": market.opening_quote.ask if market.opening_quote else None,
            "fill_price": price, "fill_ts_ns": strategy.fill_times.get(index), "trade_fee": fee,
            "submission_fee": strategy.overrides[order.day][symbol].submission_fee if filled else D(0),
            "reason": rejects.get(index),
        }))
    return MappingProxyType({
        "schema": "cffex-daily-audit-v1", "nautilus_version": version("nautilus-trader"),
        "implementation_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "data_snapshot_id": snapshot, "capacity_model": capacity_model,
        "max_open_quote_age_ns": max_open_quote_age_ns,
        "input_sha256": digest({
            "contracts": strategy.contracts, "sessions": strategy.sessions, "orders": orders,
            "initial_cash": initial_cash, "policy": strategy.policies, "contract_policies": strategy.overrides,
            "margin_groups": strategy.groups, "cash_deposits": strategy.deposits,
            "data_snapshot_id": snapshot, "capacity_model": capacity_model,
            "max_open_quote_age_ns": max_open_quote_age_ns,
        }),
        "result_sha256": digest({"orders": trace, "daily": strategy.daily}),
        "orders": tuple(trace),
    })


def replay_if(contract, days, orders, *, initial_cash, policy, cash_deposits=None, audit=False,
              data_snapshot_id=None, capacity_model=None, max_open_quote_age_ns=0):
    """Replay a single IF contract using the shared futures account."""
    return replay_if_portfolio({contract: days}, ((contract.symbol, order) for order in orders),
                               initial_cash=initial_cash, policy=policy, cash_deposits=cash_deposits,
                               audit=audit, data_snapshot_id=data_snapshot_id, capacity_model=capacity_model,
                               max_open_quote_age_ns=max_open_quote_age_ns)


def replay_if_portfolio(contracts, orders, *, initial_cash, policy, contract_policies=None, trading_days=None,
                        margin_groups=(), cash_deposits=None, audit=False, data_snapshot_id=None,
                        capacity_model=None, max_open_quote_age_ns=0):
    """Replay (symbol, IFOrder) pairs in input order on each execution date."""
    if not contracts or not _positive(initial_cash) or _money(initial_cash) != initial_cash:
        raise ValueError("Invalid IF contracts or external cash")
    if audit and not all(isinstance(label, str) and label.strip() for label in (data_snapshot_id, capacity_model)):
        raise ValueError("Futures audit requires a data snapshot ID and an opening capacity model")
    if type(max_open_quote_age_ns) is not int or max_open_quote_age_ns < 0:
        raise ValueError("Opening quote age limit must be a non-negative integer in nanoseconds")
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
            q = d.opening_quote
            if q is not None and (
                not isinstance(q, IFOpeningQuote) or type(q.ts_ns) is not int
                or not _ns(d.day, 9, 30) <= q.ts_ns < _ns(d.day, 9, 31)
                or not all(_positive(p) and not p % TICK for p in (q.bid, q.ask))
                or not d.lower <= q.bid <= q.ask <= d.upper
                or any(type(size) is not int or size < 0 for size in (q.bid_lots, q.ask_lots))
                or not isinstance(q.source, str) or not q.source.strip()
            ):
                raise ValueError(f"Invalid opening quote for {contract.symbol} on {d.day}")
            market[d.day][contract.symbol] = d
            previous = d.day
        if previous is None:
            raise ValueError(f"No sessions for {contract.symbol}")
    market = dict(sorted(market.items()))
    observed = any(row.opening_quote is not None for rows in market.values() for row in rows.values())
    if trading_days is not None and tuple(trading_days) != tuple(market):
        raise ValueError("Supplied trading calendar differs from futures sessions")
    for day, rows in market.items():
        if any(c.listed <= day <= c.expiry and s not in rows for s, c in by_symbol.items()):
            raise ValueError(f"Missing IF session data on {day}")
        if observed:
            if any(row.opening_quote is None for row in rows.values()):
                raise ValueError(f"Opening quotes must be complete on {day}")
            observed_times = [row.opening_quote.ts_ns for row in rows.values()]
            if max(observed_times) - min(observed_times) > max_open_quote_age_ns:
                raise ValueError(f"Opening quotes are not aligned within the age limit on {day}")
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
            quote = row.opening_quote
            if quote:
                bid, ask = (Price.from_str(format(px, ".2f")) for px in (quote.bid, quote.ask))
                bid_size, ask_size = (Quantity.from_int(max(lots, 1)) for lots in (quote.bid_lots, quote.ask_lots))
                ticks.append(QuoteTick(instruments[symbol].id, bid, ask, bid_size, ask_size, quote.ts_ns, quote.ts_ns))
            else:
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
            evidence = _build_evidence(strategy, audited_orders, initial_cash, data_snapshot_id, capacity_model,
                                       max_open_quote_age_ns)
        return IFResult(tuple(strategy.fills), tuple(strategy.rejections), tuple(strategy.daily),
                        tuple(order_symbols.values()), MappingProxyType(strategy.unfilled_lots), evidence)
    finally:
        engine.dispose()

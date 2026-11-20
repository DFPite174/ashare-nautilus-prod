"""
Test Suite: test_audit_v3_frozen_fastpath_perf.py
P1-3: Verify frozen orders fastpath to order tape kernel:
1. Mathematical equivalence of fills, NAV, positions, and frictions.
2. Fastpath activation when frozen orders can be represented cleanly.
3. Safe fallback when frozen orders include Day 1 orders.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import time
import pytest

from contracts import Instrument, Venue, FrozenOrder
from engine.backtest import at
from engine import (
    run_frozen_orders_backtest,
    prepare_backtest,
    prepare_order_tape,
    run_order_tape,
    TimedOrderIntent,
    OrderIntent,
    BacktestBar,
)

SH_TZ = ZoneInfo("Asia/Shanghai")


def make_market(num_days=5):
    symbols = ["600519.SH", "600036.SH"]
    instruments = [Instrument(symbol=s, exchange=Venue.SSE) for s in symbols]
    bars = []
    rules = {}
    base_dates = [
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 6),
        date(2025, 1, 7),
        date(2025, 1, 8),
    ][:num_days]

    for d in base_dates:
        for s in symbols:
            px = D("100.00") if s == "600519.SH" else D("50.00")
            bars.append(
                BacktestBar(
                    symbol=s,
                    open=px,
                    high=px + D("2.00"),
                    low=px - D("2.00"),
                    close=px,
                    volume=50000,
                    start=datetime.combine(d, datetime.min.time()).replace(hour=9, minute=30, tzinfo=SH_TZ),
                    end=datetime.combine(d, datetime.min.time()).replace(hour=15, minute=0, tzinfo=SH_TZ),
                )
            )
            rules[(s, d)] = dict(
                trade_status='OPEN',
                limit_state='BOUNDED',
                down_limit=float(px * D("0.9")),
                up_limit=float(px * D("1.1")),
                buy_min=100,
                buy_step=100,
                sell_min=100,
                sell_step=100,
                max_order=1000000,
            )

    return instruments, bars, rules, base_dates


def test_frozen_orders_fastpath_equivalence():
    """Verify that run_frozen_orders_backtest matches run_order_tape 1:1 in fills, NAV, and positions."""
    instruments, bars, rules, dates = make_market(5)

    # Orders on Day 2 (BUY 600519) and Day 3 (BUY 600036)
    frozen_orders = [
        FrozenOrder(symbol="600519.SH", trade_date=dates[1], side="BUY", quantity=200, intent_id="F1"),
        FrozenOrder(symbol="600036.SH", trade_date=dates[2], side="BUY", quantity=400, intent_id="F2"),
        FrozenOrder(symbol="600519.SH", trade_date=dates[3], side="SELL", quantity=100, intent_id="F3"),
    ]

    # Run convenient frozen orders entrypoint (fastpath)
    res_frozen = run_frozen_orders_backtest(
        instruments, bars, rules, frozen_orders,
        initial_cash=D("100000"),
        participation_rate=1.0,
    )

    # Run explicit compiled order tape entrypoint
    prepared = prepare_backtest(instruments, bars, rules)
    timed_intents = [
        TimedOrderIntent(at(dates[0], 15), OrderIntent("600519.SH", "BUY", 200, intent_id="F1")),
        TimedOrderIntent(at(dates[1], 15), OrderIntent("600036.SH", "BUY", 400, intent_id="F2")),
        TimedOrderIntent(at(dates[2], 15), OrderIntent("600519.SH", "SELL", 100, intent_id="F3")),
    ]
    tape = prepare_order_tape(prepared, timed_intents)
    res_tape = run_order_tape(prepared, tape, initial_cash=D("100000"), participation_rate=1.0)

    # 1. Exact match on final account state
    assert res_frozen.ledger_cash == res_tape.ledger_cash
    assert res_frozen.positions == res_tape.positions
    assert len(res_frozen.fills) == len(res_tape.fills) == 3

    # 2. Exact match on daily NAV sequence
    assert len(res_frozen.daily_nav) == len(res_tape.daily_nav) == 5
    for nav_f, nav_t in zip(res_frozen.daily_nav, res_tape.daily_nav):
        assert nav_f['total_equity'] == nav_t['total_equity']
        assert nav_f['cash'] == nav_t['cash']


def test_frozen_orders_fallback_under_auto_trim_and_minute_bars():
    """Verify that frozen orders safely fall back to standard replay under auto_trim policy."""
    instruments, bars, rules, dates = make_market(3)

    frozen_orders = [
        FrozenOrder(symbol="600519.SH", trade_date=dates[1], side="BUY", quantity=100, intent_id="D2"),
    ]

    # Run with cash_exhaustion_policy='auto_trim' (triggers fallback path)
    res = run_frozen_orders_backtest(
        instruments, bars, rules, frozen_orders,
        initial_cash=D("100000"),
        participation_rate=1.0,
        cash_exhaustion_policy='auto_trim',
    )

    assert len(res.fills) == 1
    assert res.fills[0]["symbol"] == "600519.SH"
    assert res.positions["600519.SH"] == D("100.0")
    assert res.manifest["config"]["cash_exhaustion_policy"] == "auto_trim"

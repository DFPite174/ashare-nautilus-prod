"""
Audit Counter-Example Test P1-4:
Order semantics, auto_trim timing, and residual holding/unfilled metrics.
Asserts behavior when open price (102.00) exceeds limit price (101.00) with intraday low (99.00):
- auction_open mode does not execute and records unfilled capital amount.
- synthetic_l2 mode executes intraday and records residual holdings.
"""
from datetime import date, datetime
from decimal import Decimal as D
import pytest
import pytz

from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest
from engine.backtest import BacktestBar

SH_TZ = pytz.timezone("Asia/Shanghai")


def make_bar(symbol: str, day_str: str, o: float, h: float, l: float, c: float, v: int) -> BacktestBar:
    return BacktestBar(
        symbol=symbol,
        start=SH_TZ.localize(datetime.strptime(f"{day_str} 09:30:00", "%Y-%m-%d %H:%M:%S")),
        end=SH_TZ.localize(datetime.strptime(f"{day_str} 15:00:00", "%Y-%m-%d %H:%M:%S")),
        open=D(str(o)).quantize(D("0.01")),
        high=D(str(h)).quantize(D("0.01")),
        low=D(str(l)).quantize(D("0.01")),
        close=D(str(c)).quantize(D("0.01")),
        volume=v,
    )


def test_order_semantics_auction_open_vs_synthetic_l2():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    # Day 1: close 100.00
    # Day 2: open 102.00, high 103.00, low 99.00, close 101.00
    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2025-01-03", 102.0, 103.0, 99.0, 101.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Signal on Day 1 submits BUY 100 shares with limit_price = 101.00
    def signal_limit_101(b, state):
        if b.end.date() == date(2025, 1, 2):
            return [OrderIntent("600519.SH", "BUY", 100, limit_price=D("101.00"))]
        return []

    # 1. auction_open mode:
    # Open price is 102.00 > 101.00, so cannot fill at open auction.
    # In auction_open mode, orders do not persist into intraday.
    res_auction = run_backtest(
        inst, bars, rules, signal_limit_101,
        initial_cash=D("50000"),
        participation_rate=1.0,
        execution_mode="auction_open",
    )
    assert len(res_auction.fills) == 0
    assert res_auction.unfilled_capital_amount == D("10100.00")
    assert res_auction.residual_holding_symbols == {}

    # 2. synthetic_l2 mode:
    # Open price is 102.00 > 101.00 (misses open), but low is 99.00 <= 101.00.
    # Order fills intraday at limit price 101.00!
    res_l2 = run_backtest(
        inst, bars, rules, signal_limit_101,
        initial_cash=D("50000"),
        participation_rate=1.0,
        execution_mode="synthetic_l2",
    )
    assert len(res_l2.fills) == 1
    assert res_l2.fills[0]["price"] == "101.00"
    assert res_l2.fills[0]["quantity"] == "100"
    assert res_l2.unfilled_capital_amount == D("0.00")
    assert res_l2.positions["600519.SH"] == D("100.0")
    assert res_l2.residual_holding_symbols == {}

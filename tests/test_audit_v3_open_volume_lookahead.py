"""
Test Suite: test_audit_v3_open_volume_lookahead.py
P0: Verify Day T volume==0 does not look ahead at 9:30 AM open quotes,
and verify data contradiction detection when open_volume > volume.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

from contracts import Instrument, Venue
from engine import run_backtest, BacktestBar, OrderIntent
from engine.replay import build_events
from engine.liquidity import BarLiquidity

SH_TZ = ZoneInfo("Asia/Shanghai")
INST = Instrument(symbol="600519.SH", exchange=Venue.SSE)


def make_day_bar(symbol, d, o, h, l, c, vol, open_vol=None):
    return BacktestBar(
        symbol=symbol,
        open=D(str(o)),
        high=D(str(h)),
        low=D(str(l)),
        close=D(str(c)),
        volume=int(vol),
        open_volume=open_vol,
        start=datetime.combine(d, datetime.min.time()).replace(hour=9, minute=30, tzinfo=SH_TZ),
        end=datetime.combine(d, datetime.min.time()).replace(hour=15, minute=0, tzinfo=SH_TZ),
    )


def test_open_quote_generated_without_looking_ahead_to_zero_volume():
    """Verify that Day T having volume==0 does NOT cause 9:30 QuoteTick to be skipped."""
    d1 = date(2025, 1, 2)
    d2 = date(2025, 1, 3)

    bars = [
        make_day_bar("600519.SH", d1, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d2, 100, 100, 100, 100, 0),  # Zero volume on Day 2
    ]
    rules = {
        ("600519.SH", d1): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d2): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    quote_ticks_received = []

    def signal(bar, state):
        # Trigger buy on Day 1 close for Day 2 open execution
        if bar.end.date() == d1:
            return [OrderIntent("600519.SH", "BUY", 100)]
        return []

    # Run backtest
    res = run_backtest([INST], bars, rules, signal, participation_rate=1.0)

    # Day 2 QuoteTick was generated at 9:30 AM, order was submitted, but because Day 2 total volume is 0,
    # book depth is 0 and no fill occurs.
    assert len(res.fills) == 0
    assert res.positions["600519.SH"] == D("0.0")
    # The order was submitted and canceled by NT due to lack of matching volume
    assert len(res.orders) == 1
    assert res.orders[0]["status"] == "CANCELED"
    assert res.orders[0]["quantity"] == "100"
    assert res.orders[0]["filled_quantity"] == "0"


def test_open_volume_exceeding_total_volume_raises_data_contradiction():
    """Verify that open_volume > volume is rejected as an impossible market data contradiction."""
    d = date(2025, 1, 2)
    bars = [
        make_day_bar("600519.SH", d, 100, 102, 99, 100, vol=500, open_vol=1000),
    ]
    rules = {
        ("600519.SH", d): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    with pytest.raises(ValueError, match="cannot exceed total volume"):
        run_backtest([INST], bars, rules, lambda b, s: [])


def test_open_decision_identical_regardless_of_afternoon_closing_volume():
    """Verify that morning open decision and order submission are identical regardless of whether Day T closes with 0 or 50,000 volume."""
    d1 = date(2025, 1, 2)
    d2 = date(2025, 1, 3)

    rules = {
        ("600519.SH", d1): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d2): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Case A: Day 2 has 50000 volume
    bars_a = [
        make_day_bar("600519.SH", d1, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d2, 100, 102, 99, 100, 50000),
    ]
    # Case B: Day 2 has 0 volume
    bars_b = [
        make_day_bar("600519.SH", d1, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d2, 100, 100, 100, 100, 0),
    ]

    def signal(bar, state):
        if bar.end.date() == d1:
            return [OrderIntent("600519.SH", "BUY", 100)]
        return []

    res_a = run_backtest([INST], bars_a, rules, signal, participation_rate=1.0)
    res_b = run_backtest([INST], bars_b, rules, signal, participation_rate=1.0)

    # In both runs, exactly 1 order was submitted at 9:30 AM with quantity 100
    assert len(res_a.orders) == 1 and res_a.orders[0]["quantity"] == "100"
    assert len(res_b.orders) == 1 and res_b.orders[0]["quantity"] == "100"
    # In run A (50k vol), it filled; in run B (0 vol), it canceled due to 0 liquidity
    assert len(res_a.fills) == 1 and res_a.fills[0]["quantity"] == "100"
    assert len(res_b.fills) == 0 and res_b.orders[0]["status"] == "CANCELED"

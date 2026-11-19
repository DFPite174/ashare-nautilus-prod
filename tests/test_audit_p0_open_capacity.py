"""
Audit Counter-Example Test P0-1:
Elimination of look-ahead bias in opening capacity.
Asserts that changing Day T's total volume from 5,000 to 50,000 does NOT alter the opening fill quantity at 09:30.
"""
from datetime import date, datetime
from decimal import Decimal as D
import pytz
import pytest

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


def test_open_capacity_eliminates_look_ahead_bias():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def buy_signal(b, state):
        if b.end.date() == date(2025, 1, 2):
            return [OrderIntent("600519.SH", "BUY", 1500)]
        return []

    # Day 1 volume is 20,000. At 10% participation rate, open capacity for Day 2 is 2,000 shares.
    # Scenario A: Day 2 total volume is 5,000 shares.
    bars_a = [
        make_bar("600519.SH", "2025-01-02", 100.0, 101.0, 99.0, 100.0, 20000),
        make_bar("600519.SH", "2025-01-03", 100.0, 101.0, 99.0, 100.0, 5000),
    ]

    # Scenario B: Day 2 total volume is 50,000 shares.
    bars_b = [
        make_bar("600519.SH", "2025-01-02", 100.0, 101.0, 99.0, 100.0, 20000),
        make_bar("600519.SH", "2025-01-03", 100.0, 101.0, 99.0, 100.0, 50000),
    ]

    res_a = run_backtest(
        inst, bars_a, rules, buy_signal,
        initial_cash=D("500000"),
        participation_rate=0.10,
    )

    res_b = run_backtest(
        inst, bars_b, rules, buy_signal,
        initial_cash=D("500000"),
        participation_rate=0.10,
    )

    # In both scenarios, the Day 2 opening match quantity MUST be identical (1,500 shares)
    # because open capacity derives strictly from Day 1's volume (20,000 * 0.10 = 2,000 >= 1,500),
    # NOT Day 2's total daily volume!
    assert len(res_a.fills) == 1
    assert len(res_b.fills) == 1
    assert res_a.fills[0]["quantity"] == "1500"
    assert res_b.fills[0]["quantity"] == "1500"
    assert res_a.fills[0]["quantity"] == res_b.fills[0]["quantity"]

"""
Unit tests for P1: Auction-Open Execution Mode Decoupling
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

from contracts import Instrument, Venue
from engine.backtest import BacktestBar, OrderIntent, run_backtest

TZ = ZoneInfo("Asia/Shanghai")


def dt_daily(day_str: str, hour: int = 9, minute: int = 30) -> datetime:
    d = date.fromisoformat(day_str)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ)


def make_bar(symbol: str, day_str: str, o: float, h: float, l: float, c: float, v: int) -> BacktestBar:
    return BacktestBar(
        symbol=symbol,
        start=dt_daily(day_str, 9, 30),
        end=dt_daily(day_str, 15, 0),
        open=D(str(o)).quantize(D("0.01")),
        high=D(str(h)).quantize(D("0.01")),
        low=D(str(l)).quantize(D("0.01")),
        close=D(str(c)).quantize(D("0.01")),
        volume=v,
    )


def test_auction_open_reduces_events_while_preserving_open_execution():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 105.0, 98.0, 103.0, 50000),
        make_bar("600519.SH", "2025-01-03", 102.0, 106.0, 101.0, 104.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def signal(b, state):
        if b.end.date() == date(2025, 1, 2):
            return [OrderIntent("600519.SH", "BUY", 100)]
        return []

    # Run in standard synthetic_l2 mode
    res_l2 = run_backtest(inst, bars, rules, signal, initial_cash=D("50000"), participation_rate=1.0, execution_mode="synthetic_l2")
    # Run in auction_open mode
    res_open = run_backtest(inst, bars, rules, signal, initial_cash=D("50000"), participation_rate=1.0, execution_mode="auction_open")

    # In auction_open mode, market events should be significantly fewer (O-H-L-C books skipped)
    assert res_open.run_statistics["market_events"] < res_l2.run_statistics["market_events"]
    # But fill execution, price, quantity, fee, and daily nav must be identical!
    assert len(res_open.fills) == len(res_l2.fills) == 1
    assert res_open.fills[0]["price"] == res_l2.fills[0]["price"] == "102.00"
    assert res_open.fills[0]["quantity"] == res_l2.fills[0]["quantity"] == "100"
    assert res_open.ledger_cash == res_l2.ledger_cash
    assert res_open.daily_nav[-1]["total_equity"] == res_l2.daily_nav[-1]["total_equity"]

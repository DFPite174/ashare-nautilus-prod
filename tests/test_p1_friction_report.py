"""
Unit tests for P1: Microstructure Friction Decomposition Report
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


def test_frictional_summary_and_attribution_report():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2023-01-05", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-01-06", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-01-09", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2023, 1, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 1, 6)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 1, 9)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def signal(b, state):
        cur_d = b.end.date()
        if cur_d == date(2023, 1, 5):
            return [OrderIntent("600519.SH", "BUY", 100)]
        elif cur_d == date(2023, 1, 6):
            return [OrderIntent("600519.SH", "SELL", 100)]
        return []

    res = run_backtest(inst, bars, rules, signal, initial_cash=D("50000"), participation_rate=1.0)
    assert len(res.fills) == 2

    s = res.frictional_summary()
    assert s["total_turnover"] == D("20000.00")
    assert s["buy_turnover"] == D("10000.00")
    assert s["sell_turnover"] == D("10000.00")
    assert s["total_fills"] == 2
    assert s["total_fee"] > 0
    # In Jan 2023:
    # Stamp duty on sell = 10000 * 0.001 = 10.00
    assert s["stamp_duty"] == D("10.00")
    # Transfer fee = 20000 * 0.00001 = 0.20
    assert s["transfer_fees"] == D("0.20")
    # Commission:
    # Buy 10000 * 0.0003 = 3.00 -> floor 5.00 -> drag = 2.00
    # Sell 10000 * 0.0003 = 3.00 -> floor 5.00 -> drag = 2.00
    assert s["proportional_commission"] == D("6.00")
    assert s["min_commission_drag"] == D("4.00")
    assert s["total_commission"] == D("10.00")

    report = res.attribution_report()
    assert "ASHARE-NAUTILUS MICROSTRUCTURE FRICTION REPORT" in report
    assert "Pure Commission" in report
    assert "Stamp Duty" in report
    assert "Transfer Fee" in report

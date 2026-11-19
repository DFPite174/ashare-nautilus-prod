"""
Unit tests for P0: Adaptive Down-sizing on Cash Exhaustion (Auto-trimming)
"""
from datetime import date, datetime, timedelta
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


def test_auto_trim_downsizes_on_cash_shortage():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2025-01-03", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def signal_buy_500(b, state):
        if b.end.date() == date(2025, 1, 2):
            return [OrderIntent("600519.SH", "BUY", 500)]
        return []

    # With 30,100 CNY cash, 500 shares * 100 = 50,000 + fee cannot be afforded.
    # 300 shares * 100 = 30,000 + fee ~ 9.30 = 30,009.30 <= 30,100.
    # Under 'auto_trim', order should be downsized to 300 shares!
    res_trim = run_backtest(
        inst, bars, rules, signal_buy_500,
        initial_cash=D("30100"),
        participation_rate=1.0,
        cash_exhaustion_policy='auto_trim',
    )
    assert len(res_trim.fills) == 1
    assert res_trim.fills[0]["quantity"] == "300"
    assert res_trim.native_cash == res_trim.ledger_cash >= 0
    assert len(res_trim.rejections) == 0

    # Under 'reject', the order should be rejected completely.
    res_reject = run_backtest(
        inst, bars, rules, signal_buy_500,
        initial_cash=D("30100"),
        participation_rate=1.0,
        cash_exhaustion_policy='reject',
    )
    assert len(res_reject.fills) == 0
    assert len(res_reject.rejections) == 1
    assert res_reject.rejections[0]["reason"] == "INSUFFICIENT_CASH_WITH_FEES"


def test_auto_trim_rejects_when_even_min_lot_unaffordable():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2025-01-03", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def signal_buy(b, state):
        if b.end.date() == date(2025, 1, 2):
            return [OrderIntent("600519.SH", "BUY", 200)]
        return []

    # 100 shares * 100 = 10,000 + 5 min fee = 10,005.
    # With only 9,000 CNY cash, even 1 lot cannot be bought.
    # Must reject with INSUFFICIENT_CASH_WITH_FEES even under 'auto_trim'.
    res = run_backtest(
        inst, bars, rules, signal_buy,
        initial_cash=D("9000"),
        participation_rate=1.0,
        cash_exhaustion_policy='auto_trim',
    )
    assert len(res.fills) == 0
    assert len(res.rejections) == 1
    assert res.rejections[0]["reason"] == "INSUFFICIENT_CASH_WITH_FEES"

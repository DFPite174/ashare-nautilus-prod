"""
Test Suite: test_audit_v3_residual_partial_sell.py
P1-1: Verify accurate accounting of residual holdings and uncompleted sell orders.
Distinguishes partial sell reduction failure vs explicit full liquidation failure.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

from contracts import Instrument, Venue
from engine import run_backtest, BacktestBar, OrderIntent

SH_TZ = ZoneInfo("Asia/Shanghai")
INST = Instrument(symbol="600519.SH", exchange=Venue.SSE)


def make_day_bar(symbol, d, o, h, l, c, vol):
    return BacktestBar(
        symbol=symbol,
        open=D(str(o)),
        high=D(str(h)),
        low=D(str(l)),
        close=D(str(c)),
        volume=int(vol),
        start=datetime.combine(d, datetime.min.time()).replace(hour=9, minute=30, tzinfo=SH_TZ),
        end=datetime.combine(d, datetime.min.time()).replace(hour=15, minute=0, tzinfo=SH_TZ),
    )


def test_partial_sell_failure_reports_uncompleted_sell_not_full_residual():
    """
    Scenario:
    - Day 1: Signal emits BUY 200 shares of 600519.SH.
    - Day 2: Order fills at 9:30 AM open. Shares are held, T+1 sellable on Day 3.
             At Day 2 close (15:00), strategy emits SELL 100 shares (partial reduction) for Day 3.
    - Day 3: Market opens with limit down (90.00). Sell limit price is 95.00, so intent fails/rejected.
             At Day 3 close, account holds 200 shares.
    - Verification:
      uncompleted_sell_orders['600519.SH'] == '100'
      residual_holding_symbols does NOT contain 600519.SH (as only 100 was intended to sell).
    """
    d1 = date(2025, 1, 2)
    d2 = date(2025, 1, 3)
    d3 = date(2025, 1, 6)

    bars = [
        make_day_bar("600519.SH", d1, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d2, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d3, 90, 90, 90, 90, 50000),  # Limit down on Day 3
    ]
    rules = {
        ("600519.SH", d1): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d2): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d3): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def signal(bar, state):
        if bar.end.date() == d1:
            # Buy 200 shares for Day 2 open
            return [OrderIntent("600519.SH", "BUY", 200)]
        elif bar.end.date() == d2:
            # Attempt partial reduction of 100 shares for Day 3 open
            return [OrderIntent("600519.SH", "SELL", 100, limit_price=D("95.00"))]
        return []

    res = run_backtest([INST], bars, rules, signal, initial_cash=D("100000"), participation_rate=1.0)

    assert res.positions["600519.SH"] == D("200.0")
    # Partial sell of 100 failed due to limit down, reported accurately
    assert res.uncompleted_sell_orders.get("600519.SH") == "100"
    # Not reported as a 200-share residual holding!
    assert "600519.SH" not in res.residual_holding_symbols


def test_full_liquidation_failure_reports_both_residual_and_uncompleted():
    """
    Scenario:
    - Day 1: Signal emits BUY 200 shares of 600519.SH.
    - Day 2: Order fills at 9:30 AM open. Shares are held.
             At Day 2 close, strategy emits SELL 200 shares (full liquidation) for Day 3.
    - Day 3: Market opens with limit down (90.00). Sell limit price is 95.00, so intent fails/rejected.
    - Verification:
      Account holds 200 shares.
      uncompleted_sell_orders['600519.SH'] == '200'
      residual_holding_symbols['600519.SH'] == '200' (full position trapped).
    """
    d1 = date(2025, 1, 2)
    d2 = date(2025, 1, 3)
    d3 = date(2025, 1, 6)

    bars = [
        make_day_bar("600519.SH", d1, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d2, 100, 102, 99, 100, 50000),
        make_day_bar("600519.SH", d3, 90, 90, 90, 90, 50000),
    ]
    rules = {
        ("600519.SH", d1): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d2): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d3): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def signal(bar, state):
        if bar.end.date() == d1:
            return [OrderIntent("600519.SH", "BUY", 200)]
        elif bar.end.date() == d2:
            return [OrderIntent("600519.SH", "SELL", 200, limit_price=D("95.00"))]
        return []

    res = run_backtest([INST], bars, rules, signal, initial_cash=D("100000"), participation_rate=1.0)

    assert res.positions["600519.SH"] == D("200.0")
    assert res.uncompleted_sell_orders.get("600519.SH") == "200"
    assert res.residual_holding_symbols.get("600519.SH") == "200"

def test_later_sell_retires_failed_reduction():
    days = [date(2025, 1, n) for n in (2, 3, 6, 7)]
    bars = [make_day_bar(INST.symbol, day, 100, 101, 99, 100, 50000) for day in days]
    rules = {(INST.symbol, day): dict(
        trade_status='DAY_BLOCKED' if day == days[2] else 'OPEN',
        limit_state='BOUNDED', down_limit=90, up_limit=110,
        buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000,
    ) for day in days}

    def signal(bar, state):
        if bar.end.date() == days[0]:
            return [OrderIntent(bar.symbol, 'BUY', 200)]
        if bar.end.date() in days[1:3]:
            return [OrderIntent(bar.symbol, 'SELL', 100)]
        return []

    result = run_backtest([INST], bars, rules, signal)
    assert result.positions[INST.symbol] == D(100)
    assert result.uncompleted_sell_orders == {}
    assert result.residual_holding_symbols == {}

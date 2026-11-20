"""
Test P1-2: Residual Holdings semantics.
Verifies that normal target portfolio holdings are NOT marked as residual holdings,
and ONLY unintended failed liquidations (e.g. limit-down/rejected sell intents) are isolated.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest
from engine.backtest import BacktestBar

SH_TZ = ZoneInfo("Asia/Shanghai")


def make_bar(symbol, dt_str, o, h, l, c, vol):
    return BacktestBar(
        symbol=symbol,
        open=float(o),
        high=float(h),
        low=float(l),
        close=float(c),
        volume=int(vol),
        start=datetime.strptime(f"{dt_str} 09:30:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
        end=datetime.strptime(f"{dt_str} 15:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
    )


def test_residual_holdings_excludes_normal_holdings_and_isolates_failed_sell():
    inst = [
        Instrument(symbol="600519.SH", exchange=Venue.SSE),
        Instrument(symbol="600036.SH", exchange=Venue.SSE),
    ]

    bars = [
        # Day 1: Signal to buy both stocks
        make_bar("600519.SH", "2025-01-02", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600036.SH", "2025-01-02", 50.0, 51.0, 49.0, 50.0, 50000),
        # Day 2: Both buy orders fill at 9:30. At close, signal to sell 600036.
        make_bar("600519.SH", "2025-01-03", 100.0, 102.0, 99.0, 101.0, 50000),
        make_bar("600036.SH", "2025-01-03", 50.0, 51.0, 49.0, 50.0, 50000),
        # Day 3: 600036 is suspended (DAY_BLOCKED), 600519 trades normally
        make_bar("600519.SH", "2025-01-06", 101.0, 102.0, 100.0, 101.5, 50000),
        make_bar("600036.SH", "2025-01-06", 50.0, 50.0, 50.0, 50.0, 0),
    ]

    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600036.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600036.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 6)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600036.SH", date(2025, 1, 6)): dict(trade_status='DAY_BLOCKED', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Signal:
    # On Day 1 close: Buy 100 shares of 600519.SH and 100 shares of 600036.SH
    # On Day 2 close: Sell 100 shares of 600036.SH (intended liquidation), but keep 600519.SH!
    # Because 600036 is DAY_BLOCKED on Day 3, the SELL intent will be rejected!
    def signal(bar, state):
        if bar.end.date() == date(2025, 1, 2):
            if bar.symbol == "600519.SH":
                return [OrderIntent("600519.SH", "BUY", 100)]
            elif bar.symbol == "600036.SH":
                return [OrderIntent("600036.SH", "BUY", 100)]
        elif bar.end.date() == date(2025, 1, 3):
            if bar.symbol == "600036.SH":
                return [OrderIntent("600036.SH", "SELL", 100)]
        return []

    res = run_backtest(
        inst, bars, rules, signal,
        initial_cash=D("100000"),
        participation_rate=1.0,
    )

    # Both stocks are held at end of Day 2
    assert res.positions["600519.SH"] == D("100.0")
    assert res.positions["600036.SH"] == D("100.0")

    # 600519.SH was intentionally held -> NOT in residual_holding_symbols!
    # 600036.SH sell failed (rejected due to suspension/DAY_BLOCKED) -> IS in residual_holding_symbols!
    assert "600519.SH" not in res.residual_holding_symbols
    assert res.residual_holding_symbols == {"600036.SH": "100"}

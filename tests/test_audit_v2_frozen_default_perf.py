"""
Test P1-4: Frozen orders default mode and throughput release.
Verifies that run_frozen_orders_backtest defaults to data_mode='orders',
executing frozen orders accurately without full-universe market data inflation.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from contracts import Instrument, Venue, FrozenOrder
from engine import run_frozen_orders_backtest
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


def test_frozen_orders_default_mode_and_execution():
    # 2 instruments in universe: 600519 and 600036
    inst = [
        Instrument(symbol="600519.SH", exchange=Venue.SSE),
        Instrument(symbol="600036.SH", exchange=Venue.SSE),
    ]

    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600036.SH", "2025-01-02", 50.0, 51.0, 49.0, 50.0, 50000),
        make_bar("600519.SH", "2025-01-03", 100.0, 102.0, 99.0, 101.0, 50000),
        make_bar("600036.SH", "2025-01-03", 50.0, 51.0, 49.0, 50.0, 50000),
    ]

    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600036.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600036.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Only 600519 has a frozen order on 2025-01-03 (Day 2)
    frozen_orders = [
        FrozenOrder(symbol="600519.SH", trade_date=date(2025, 1, 3), side="BUY", quantity=100),
    ]

    # Call run_frozen_orders_backtest without explicitly specifying data_mode
    res = run_frozen_orders_backtest(
        inst, bars, rules, frozen_orders,
        initial_cash=D("100000"),
        participation_rate=1.0,
    )

    # 1. Default data_mode is confirmed to be 'orders'
    assert res.run_statistics["data_mode"] == "orders"
    assert res.manifest["config"]["data_delivery"] == "orders"

    # 2. Execution succeeded and filled 100 shares of 600519.SH
    assert len(res.fills) == 1
    assert res.fills[0]["symbol"] == "600519.SH"
    assert res.fills[0]["quantity"] == "100"
    assert res.positions["600519.SH"] == D("100.0")
    assert res.positions["600036.SH"] == D("0.0")

    # 3. Market events processed in 'orders' mode are strictly sparse (only active/held symbols)
    # Day 1: neither was active/held (0 bars processed)
    # Day 2: 600519 was active (1 symbol * 1 bar = 1 bar, 600036 was omitted)
    assert res.run_statistics.get("native_market_bars", 0) == 1

"""
Test P1-1: Auto-Trim intent trimming lifecycle and unfilled capital amount closure.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest
from engine.backtest import BacktestBar

SH_TZ = ZoneInfo("Asia/Shanghai")


def test_auto_trim_intent_lifecycle():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        BacktestBar(
            symbol="600519.SH",
            open=100.0,
            high=105.0,
            low=99.0,
            close=102.0,
            volume=100000,
            start=datetime.strptime("2025-05-08 09:30:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
            end=datetime.strptime("2025-05-08 15:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
        ),
        BacktestBar(
            symbol="600519.SH",
            open=102.0,
            high=103.0,
            low=101.0,
            close=102.5,
            volume=100000,
            start=datetime.strptime("2025-05-09 09:30:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
            end=datetime.strptime("2025-05-09 15:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
        ),
    ]
    rules = {
        ("600519.SH", date(2025, 5, 8)): {
            "trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 110.0, "down_limit": 90.0,
            "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000,
        },
        ("600519.SH", date(2025, 5, 9)): {
            "trade_status": "OPEN", "limit_state": "BOUNDED", "up_limit": 112.2, "down_limit": 91.8,
            "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000,
        },
    }

    # Signal submits a 500-share BUY order at Day 1 close for Day 2 open.
    # Price is ~102.00, so 500 shares would require ~51,000 + commission.
    # Initial cash is 35,000 CNY -> affordable quantity is 300 shares!
    def signal_trim(bar, state):
        if bar.end.date() == date(2025, 5, 8):
            return [OrderIntent(symbol="600519.SH", side="BUY", quantity=500, intent_id="INTENT_BUY_500")]
        return []

    res = run_backtest(
        inst, bars, rules, signal_trim,
        initial_cash=D("35000"),
        participation_rate=1.0,
        cash_exhaustion_policy="auto_trim",
    )

    # 1. 300 shares submitted and filled
    assert len(res.fills) == 1
    assert res.fills[0]["quantity"] == "300"
    assert res.positions["600519.SH"] == D("300.0")

    # 2. TrimmedIntentRecord correctly captured
    assert len(res.trimmed_intents) == 1
    t = res.trimmed_intents[0]
    assert t.intent_id == "INTENT_BUY_500"
    assert t.symbol == "600519.SH"
    assert t.planned_quantity == 500
    assert t.submitted_quantity == 300
    assert t.trimmed_quantity == 200
    assert t.reference_price == D("102.00")

    # 3. Unfilled capital tracks exactly 200 * 102.00 = 20,400.00 CNY
    assert res.unfilled_capital_amount == D("20400.00")
    # Not an abnormal residual holding
    assert res.residual_holding_symbols == {}

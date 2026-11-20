"""
Test Suite: test_audit_v3_open_volume_validation.py
P0: Verify strict type and range validation of BacktestBar.open_volume.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

from contracts import Instrument, Venue
from engine import run_backtest, BacktestBar

SH_TZ = ZoneInfo("Asia/Shanghai")
INST = Instrument(symbol="600519.SH", exchange=Venue.SSE)


def make_bar_with_open_vol(open_vol):
    d = date(2025, 1, 2)
    return [
        BacktestBar(
            symbol="600519.SH",
            open=D("100.00"),
            high=D("102.00"),
            low=D("99.00"),
            close=D("101.00"),
            volume=50000,
            open_volume=open_vol,
            start=datetime(2025, 1, 2, 9, 30, tzinfo=SH_TZ),
            end=datetime(2025, 1, 2, 15, 0, tzinfo=SH_TZ),
        )
    ]


def rules_for_day():
    d = date(2025, 1, 2)
    return {
        ("600519.SH", d): dict(
            trade_status='OPEN',
            limit_state='BOUNDED',
            down_limit=90.0,
            up_limit=110.0,
            buy_min=100,
            buy_step=100,
            sell_min=100,
            sell_step=100,
            max_order=1000000,
        )
    }


def test_open_volume_negative_integer_rejected():
    bars = make_bar_with_open_vol(-1)
    with pytest.raises(ValueError, match="open_volume must be a non-negative integer"):
        run_backtest([INST], bars, rules_for_day(), lambda b, s: [])


def test_open_volume_float_rejected():
    bars = make_bar_with_open_vol(12.5)
    with pytest.raises(ValueError, match="open_volume must be a non-negative integer"):
        run_backtest([INST], bars, rules_for_day(), lambda b, s: [])


def test_open_volume_boolean_rejected():
    bars = make_bar_with_open_vol(True)
    with pytest.raises(ValueError, match="open_volume must be a non-negative integer"):
        run_backtest([INST], bars, rules_for_day(), lambda b, s: [])


def test_open_volume_string_rejected():
    bars = make_bar_with_open_vol("5000")
    with pytest.raises(ValueError, match="open_volume must be a non-negative integer"):
        run_backtest([INST], bars, rules_for_day(), lambda b, s: [])


def test_open_volume_valid_zero_and_positive_accepted():
    for valid_val in [0, 100, 5000]:
        bars = make_bar_with_open_vol(valid_val)
        res = run_backtest([INST], bars, rules_for_day(), lambda b, s: [])
        assert res.ledger_cash == D("100000")

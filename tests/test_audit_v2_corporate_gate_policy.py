"""
Test P1-5: Corporate Action Gate Policy (strict vs warn).
Verifies that:
- Under 'strict' policy (default), unannounced ex-rights price drops raise ValueError.
- Under 'warn' policy, warnings are emitted, data_anomalies recorded in manifest, and backtest proceeds.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

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


def test_corporate_action_gate_strict_policy_raises():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    # Day 1 close: 100.00
    # Day 2 open: 50.00 (50% drop, exceeding mainboard 12% statutory bound without corporate actions)
    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2025-01-03", 50.0, 52.0, 49.0, 50.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Under strict policy: must raise ValueError containing CRITICAL_DATA_DEFECT
    with pytest.raises(ValueError, match="CRITICAL_DATA_DEFECT"):
        run_backtest(
            inst, bars, rules, lambda b, s: [],
            initial_cash=D("100000"),
            corporate_action_gate_policy="strict",
        )


def test_corporate_action_gate_warn_policy_records_anomalies():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2025-01-02", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2025-01-03", 50.0, 52.0, 49.0, 50.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=45.0, up_limit=55.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Under warn policy: warning emitted, does not abort, manifest captures anomaly
    with pytest.warns(UserWarning, match="CRITICAL_DATA_DEFECT"):
        res = run_backtest(
            inst, bars, rules, lambda b, s: [],
            initial_cash=D("100000"),
            corporate_action_gate_policy="warn",
        )

    assert "data_anomalies" in res.manifest
    assert len(res.manifest["data_anomalies"]) >= 1
    anomaly = res.manifest["data_anomalies"][0]
    assert anomaly["symbol"] == "600519.SH"
    assert anomaly["date"] == "2025-01-03"
    assert anomaly["type"] == "suspected_ex_rights_gap"

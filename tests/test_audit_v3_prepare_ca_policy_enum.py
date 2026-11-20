"""
Test Suite: test_audit_v3_prepare_ca_policy_enum.py
P1-2: Verify prepare_backtest corporate_action_gate_policy passthrough,
anomalies recording, policy conflict detection, and strict enum validation across all parameters.
"""
from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo
import pytest

from contracts import Instrument, Venue
from engine import (
    run_backtest,
    prepare_backtest,
    run_prepared_backtest,
    BacktestBar,
)

SH_TZ = ZoneInfo("Asia/Shanghai")
INST = Instrument(symbol="600519.SH", exchange=Venue.SSE)


def make_suspected_gap_market():
    d1 = date(2025, 1, 2)
    d2 = date(2025, 1, 3)

    # 600519 drops from 100 to 80 (exceeds statutory bound 0.88 for main board, suspected ex-rights gap)
    bars = [
        BacktestBar(
            symbol="600519.SH",
            open=D("100.00"),
            high=D("102.00"),
            low=D("99.00"),
            close=D("100.00"),
            volume=50000,
            start=datetime(2025, 1, 2, 9, 30, tzinfo=SH_TZ),
            end=datetime(2025, 1, 2, 15, 0, tzinfo=SH_TZ),
        ),
        BacktestBar(
            symbol="600519.SH",
            open=D("80.00"),
            high=D("82.00"),
            low=D("79.00"),
            close=D("81.00"),
            volume=50000,
            start=datetime(2025, 1, 3, 9, 30, tzinfo=SH_TZ),
            end=datetime(2025, 1, 3, 15, 0, tzinfo=SH_TZ),
        ),
    ]

    rules = {
        ("600519.SH", d1): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", d2): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=70.0, up_limit=95.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    return bars, rules


def test_prepare_backtest_with_warn_policy_records_anomalies_and_runs():
    bars, rules = make_suspected_gap_market()

    # Under strict policy, it must abort
    with pytest.raises(ValueError, match="exhibits ex-rights price gap"):
        prepare_backtest([INST], bars, rules, corporate_action_gate_policy='strict')

    # Under warn policy, it prepares successfully and records anomaly
    prepared = prepare_backtest([INST], bars, rules, corporate_action_gate_policy='warn')
    assert prepared.corporate_action_gate_policy == 'warn'
    assert len(prepared.anomalies) > 0
    assert prepared.anomalies[0]['type'] == 'suspected_ex_rights_gap'

    # Run prepared backtest under inherited warn policy
    res = run_prepared_backtest(prepared, lambda b, s: [])
    assert res.manifest['config']['corporate_action_gate_policy'] == 'warn'
    assert len(res.manifest.get('data_anomalies', [])) > 0


def test_prepare_backtest_invalid_ca_policy_rejected():
    bars, rules = make_suspected_gap_market()
    with pytest.raises(ValueError, match="Invalid corporate_action_gate_policy"):
        prepare_backtest([INST], bars, rules, corporate_action_gate_policy='invalid_policy')


def test_replay_policy_conflict_rejected():
    bars, rules = make_suspected_gap_market()
    prepared = prepare_backtest([INST], bars, rules, corporate_action_gate_policy='warn')

    with pytest.raises(ValueError, match="conflicts with prepared policy"):
        run_prepared_backtest(prepared, lambda b, s: [], corporate_action_gate_policy='strict')


def test_run_backtest_enum_validations():
    bars, rules = make_suspected_gap_market()

    # Invalid corporate_action_gate_policy
    with pytest.raises(ValueError, match="Invalid corporate_action_gate_policy"):
        run_backtest([INST], bars, rules, lambda b, s: [], corporate_action_gate_policy='foo')

    # Invalid execution_mode
    with pytest.raises(ValueError, match="Invalid execution_mode"):
        run_backtest([INST], bars, rules, lambda b, s: [], corporate_action_gate_policy='warn', execution_mode='foo')

    # Invalid data_mode
    with pytest.raises(ValueError, match="Invalid data_mode"):
        run_backtest([INST], bars, rules, lambda b, s: [], corporate_action_gate_policy='warn', data_mode='foo')

    # Invalid cash_exhaustion_policy
    with pytest.raises(ValueError, match="Invalid cash_exhaustion_policy"):
        run_backtest([INST], bars, rules, lambda b, s: [], corporate_action_gate_policy='warn', cash_exhaustion_policy='foo')

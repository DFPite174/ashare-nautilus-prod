"""
Audit Counter-Example Test P1-5:
Anti-Illusion Corporate Action Gate.
Asserts that ex-rights price drops without corporate actions are strictly blocked even when prev_close is omitted.
"""
from datetime import date, datetime
from decimal import Decimal as D
import pytest
import pytz

from contracts import Instrument, Venue, CorporateAction
from engine import OrderIntent, run_backtest
from engine.backtest import BacktestBar

SH_TZ = pytz.timezone("Asia/Shanghai")


def make_bar(symbol: str, day_str: str, o: float, h: float, l: float, c: float, v: int) -> BacktestBar:
    return BacktestBar(
        symbol=symbol,
        start=SH_TZ.localize(datetime.strptime(f"{day_str} 09:30:00", "%Y-%m-%d %H:%M:%S")),
        end=SH_TZ.localize(datetime.strptime(f"{day_str} 15:00:00", "%Y-%m-%d %H:%M:%S")),
        open=D(str(o)).quantize(D("0.01")),
        high=D(str(h)).quantize(D("0.01")),
        low=D(str(l)).quantize(D("0.01")),
        close=D(str(c)).quantize(D("0.01")),
        volume=v,
    )


def test_anti_illusion_gate_blocks_20pct_drop_without_action_when_rule_prev_omitted():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    # Day 1 close: 100.00
    # Day 2 open: 80.00 (20% drop without action, rule_prev omitted)
    bars = [
        make_bar("600519.SH", "2023-05-04", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-05-05", 80.0, 81.0, 79.0, 80.0, 50000),
    ]
    # rules has NO 'prev_close' or 'ex_ref_price' specified
    rules = {
        ("600519.SH", date(2023, 5, 4)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 5, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=72.0, up_limit=88.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Must raise CRITICAL_DATA_DEFECT
    with pytest.raises(ValueError, match="CRITICAL_DATA_DEFECT"):
        run_backtest(inst, bars, rules, lambda b, s: [], initial_cash=D("50000"))


def test_anti_illusion_gate_passes_normal_9pct_drop():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    # Day 1 close: 100.00
    # Day 2 open: 91.00 (9% drop, within statutory 10% limit)
    bars = [
        make_bar("600519.SH", "2023-05-04", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-05-05", 91.0, 92.0, 90.0, 91.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2023, 5, 4)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 5, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=81.9, up_limit=100.1, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Normal drop within limit state passes cleanly
    res = run_backtest(inst, bars, rules, lambda b, s: [], initial_cash=D("50000"))
    assert res.ledger_cash == D("50000")


def test_anti_illusion_gate_passes_20pct_drop_with_valid_corporate_action():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    # Day 1 close: 100.00
    # Day 2: 10送2.5除权，除权价 80.00
    bars = [
        make_bar("600519.SH", "2023-05-04", 100.0, 102.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-05-05", 80.0, 81.0, 79.0, 80.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2023, 5, 4)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 5, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=72.0, up_limit=88.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }
    action = CorporateAction(
        symbol="600519.SH",
        ex_date=date(2023, 5, 5),
        bonus_ratio=D("0.25"),
        fractional_policy="cash_in_lieu",
    )

    # Valid corporate action supplied: passes without error!
    res = run_backtest(inst, bars, rules, lambda b, s: [], initial_cash=D("50000"), corporate_actions=[action])
    assert res.ledger_cash == D("50000")

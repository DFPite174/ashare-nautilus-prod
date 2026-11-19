"""
Audit Counter-Example Test P1-6:
Run Fingerprint (manifest['run_sha256']) full-element binding.
Asserts that frozen_orders, execution_mode, and cash_exhaustion_policy are cryptographically bound to run_sha256.
"""
from datetime import date, datetime
from decimal import Decimal as D
import pytest
import pytz

from contracts import Instrument, Venue, FrozenOrder
from engine import run_backtest
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


def test_frozen_orders_change_run_sha256():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2023-01-05", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-01-06", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2023, 1, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 1, 6)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    fo_100 = [FrozenOrder(symbol="600519.SH", trade_date=date(2023, 1, 6), side="BUY", quantity=100)]
    fo_200 = [FrozenOrder(symbol="600519.SH", trade_date=date(2023, 1, 6), side="BUY", quantity=200)]

    res_100 = run_backtest(inst, bars, rules, initial_cash=D("50000"), frozen_orders=fo_100)
    res_200 = run_backtest(inst, bars, rules, initial_cash=D("50000"), frozen_orders=fo_200)

    # In legacy code, frozen_orders was omitted from manifest, causing run_sha256 collision.
    # Refactored code binds frozen_orders into manifest['frozen_orders_sha256'] and run_sha256.
    assert "frozen_orders_sha256" in res_100.manifest
    assert "frozen_orders_sha256" in res_200.manifest
    assert res_100.manifest["frozen_orders_sha256"] != res_200.manifest["frozen_orders_sha256"]
    assert res_100.manifest["run_sha256"] != res_200.manifest["run_sha256"]


def test_execution_mode_and_cash_exhaustion_policy_change_run_sha256():
    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2023-01-05", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-01-06", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2023, 1, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 1, 6)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    res_l2 = run_backtest(inst, bars, rules, lambda b, s: [], initial_cash=D("50000"), execution_mode="synthetic_l2", cash_exhaustion_policy="reject")
    res_auction = run_backtest(inst, bars, rules, lambda b, s: [], initial_cash=D("50000"), execution_mode="auction_open", cash_exhaustion_policy="reject")
    res_trim = run_backtest(inst, bars, rules, lambda b, s: [], initial_cash=D("50000"), execution_mode="synthetic_l2", cash_exhaustion_policy="auto_trim")

    assert res_l2.manifest["config"]["execution_model"] == "synthetic_l2"
    assert res_auction.manifest["config"]["execution_model"] == "auction_open"
    assert res_trim.manifest["config"]["cash_exhaustion_policy"] == "auto_trim"

    assert res_l2.manifest["run_sha256"] != res_auction.manifest["run_sha256"]
    assert res_l2.manifest["run_sha256"] != res_trim.manifest["run_sha256"]

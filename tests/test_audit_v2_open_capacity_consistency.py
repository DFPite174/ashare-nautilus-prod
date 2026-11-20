"""
Audit Counter-Example Test V2 P0-1:
Open Capacity Consistency across all data delivery modes (full, sparse, orders/tape).
Asserts that:
1. Streaming / sparse / orders delivery maintains complete universe history volume,
   preventing divergence between full, sparse and orders replay under idle-symbol first-buy scenarios.
2. Modifying Day T's volume (5,000 vs 50,000) strictly produces identical 09:30 fills
   across full, sparse, and orders delivery modes.
"""
from datetime import date, datetime
from decimal import Decimal as D
import pytest
from zoneinfo import ZoneInfo

from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest, prepare_backtest
from engine.backtest import BacktestBar
from engine.tape_replay import run_order_tape

SH_TZ = ZoneInfo("Asia/Shanghai")


def make_bar(symbol: str, day_str: str, o: float, h: float, l: float, c: float, v: int) -> BacktestBar:
    return BacktestBar(
        symbol=symbol,
        start=datetime.strptime(f"{day_str} 09:30:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
        end=datetime.strptime(f"{day_str} 15:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH_TZ),
        open=D(str(o)).quantize(D("0.01")),
        high=D(str(h)).quantize(D("0.01")),
        low=D(str(l)).quantize(D("0.01")),
        close=D(str(c)).quantize(D("0.01")),
        volume=v,
    )


def test_open_capacity_consistency_full_vs_sparse_and_tape():
    # Two stocks: 600519.SH (active on Day 1), 600000.SH (idle on Day 1, bought on Day 2)
    insts = [
        Instrument(symbol="600519.SH", exchange=Venue.SSE),
        Instrument(symbol="600000.SH", exchange=Venue.SSE),
    ]
    rules = {
        ("600519.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600000.SH", date(2025, 1, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=9.0, up_limit=11.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600000.SH", date(2025, 1, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=9.0, up_limit=11.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    # Signal on Day 1 buys 600000.SH (which was idle on Day 1)
    def buy_idle_symbol_on_day2(b, state):
        if b.symbol == "600519.SH" and b.end.date() == date(2025, 1, 2):
            return [OrderIntent("600000.SH", "BUY", 100)]
        return []

    # Scenario A: Day 2 volume of 600000.SH is 5,000 (Day 1 volume was 10,000 -> 1% part_rate is 100 shares cap)
    bars_a = [
        make_bar("600519.SH", "2025-01-02", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600000.SH", "2025-01-02", 10.0, 10.1, 9.9, 10.0, 10000),
        make_bar("600519.SH", "2025-01-03", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600000.SH", "2025-01-03", 10.0, 10.1, 9.9, 10.0, 5000),
    ]

    # Scenario B: Day 2 volume of 600000.SH is 50,000 (Day 1 volume is still 10,000)
    bars_b = [
        make_bar("600519.SH", "2025-01-02", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600000.SH", "2025-01-02", 10.0, 10.1, 9.9, 10.0, 10000),
        make_bar("600519.SH", "2025-01-03", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600000.SH", "2025-01-03", 10.0, 10.1, 9.9, 10.0, 50000),
    ]

    # 1. Compare full and sparse modes under dynamic signals
    res_full_a = run_backtest(insts, bars_a, rules, buy_idle_symbol_on_day2, initial_cash=D("100000"), participation_rate=0.01, data_mode='full')
    res_sparse_a = run_backtest(insts, bars_a, rules, buy_idle_symbol_on_day2, initial_cash=D("100000"), participation_rate=0.01, data_mode='sparse')

    res_full_b = run_backtest(insts, bars_b, rules, buy_idle_symbol_on_day2, initial_cash=D("100000"), participation_rate=0.01, data_mode='full')
    res_sparse_b = run_backtest(insts, bars_b, rules, buy_idle_symbol_on_day2, initial_cash=D("100000"), participation_rate=0.01, data_mode='sparse')

    # All runs must fill EXACTLY 100 shares based on T-1 volume (10,000 * 0.01 = 100)
    assert len(res_full_a.fills) == 1
    assert res_full_a.fills[0]['quantity'] == '100'

    assert len(res_sparse_a.fills) == 1
    assert res_sparse_a.fills[0]['quantity'] == '100'

    assert len(res_full_b.fills) == 1
    assert res_full_b.fills[0]['quantity'] == '100'

    assert len(res_sparse_b.fills) == 1
    assert res_sparse_b.fills[0]['quantity'] == '100'

    assert res_full_a.ledger_cash == res_sparse_a.ledger_cash == res_full_b.ledger_cash == res_sparse_b.ledger_cash

    # 2. Compare with Order Tape replay (orders mode)
    from engine import TimedOrderIntent, prepare_order_tape
    prep_a = prepare_backtest(insts, bars_a, rules)
    prep_b = prepare_backtest(insts, bars_b, rules)

    tape_entries = [
        TimedOrderIntent(bars_a[0].end, OrderIntent("600000.SH", "BUY", 100))
    ]
    res_tape_a = run_order_tape(prep_a, tape_entries, initial_cash=D("100000"), participation_rate=0.01)
    res_tape_b = run_order_tape(prep_b, tape_entries, initial_cash=D("100000"), participation_rate=0.01)

    assert len(res_tape_a.fills) == 1
    assert res_tape_a.fills[0]['quantity'] == '100'

    assert len(res_tape_b.fills) == 1
    assert res_tape_b.fills[0]['quantity'] == '100'

    assert res_tape_a.ledger_cash == res_tape_b.ledger_cash == res_full_a.ledger_cash

"""
Audit Counter-Example Test P0-2:
Single Source of Truth for fee overrides.
Asserts that explicitly specifying stamp_duty_rate=0.0005 in 2021 matches native and ledger fee without mismatch error.
"""
from datetime import date, datetime
from decimal import Decimal as D
import pytz

from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest
from engine.fee_model import AShareFeeModel
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


def test_explicit_stamp_duty_override_in_2021():
    # In 2021, standard statutory stamp duty was 0.0010 (1‰).
    # Here, user explicitly configures stamp_duty_rate=0.0005 (0.5‰) and transfer_fee_rate=0.00001 (0.01‰).
    custom_fee = AShareFeeModel(
        stamp_duty_rate=0.0005,
        transfer_fee_rate=0.00001,
        commission_rate=0.0003,
        min_commission=5.0,
    )

    # Verify rates_for exports the exact explicit override, not resetting to 0.0010
    rates_2021 = custom_fee.rates_for("600519.SH", "2021-06-01")
    assert rates_2021["stamp_duty_rate"] == D("0.0005")
    assert rates_2021["transfer_fee_rate"] == D("0.00001")

    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2021-06-01", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2021-06-02", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2021-06-03", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2021, 6, 1)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2021, 6, 2)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2021, 6, 3)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def roundtrip_signal(b, state):
        cur_d = b.end.date()
        if cur_d == date(2021, 6, 1):
            return [OrderIntent("600519.SH", "BUY", 100)]
        elif cur_d == date(2021, 6, 2):
            return [OrderIntent("600519.SH", "SELL", 100)]
        return []

    res = run_backtest(
        inst, bars, rules, roundtrip_signal,
        initial_cash=D("50000"),
        participation_rate=1.0,
        fee_model=custom_fee,
    )

    # In legacy code, Day 2 SELL caused Native fee mismatch (10.10 != 15.10) because
    # rates_for reset stamp_duty_rate to 0.0010 while native model used 0.0005.
    # With unified single source of truth, reconciliation passes cleanly.
    assert res.native_cash == res.ledger_cash
    assert len(res.fills) == 2

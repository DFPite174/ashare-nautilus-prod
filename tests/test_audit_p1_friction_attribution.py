"""
Audit Counter-Example Test P1-1:
Friction attribution report and accounting closure.
Asserts that initial cash is accurately reflected (not 0.00 CNY), custom commission (0.0010)
is properly attributed, and itemized friction components equal total fees.
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


def test_friction_attribution_closure_and_custom_rate():
    # User configures custom commission rate = 0.0010 (千一)
    custom_fee = AShareFeeModel(commission_rate=0.0010, min_commission=5.0)

    inst = [Instrument(symbol="600519.SH", exchange=Venue.SSE)]
    bars = [
        make_bar("600519.SH", "2023-01-05", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-01-06", 100.0, 101.0, 99.0, 100.0, 50000),
        make_bar("600519.SH", "2023-01-09", 100.0, 101.0, 99.0, 100.0, 50000),
    ]
    rules = {
        ("600519.SH", date(2023, 1, 5)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 1, 6)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
        ("600519.SH", date(2023, 1, 9)): dict(trade_status='OPEN', limit_state='BOUNDED', down_limit=90.0, up_limit=110.0, buy_min=100, buy_step=100, sell_min=100, sell_step=100, max_order=1000000),
    }

    def roundtrip_signal(b, state):
        cur_d = b.end.date()
        if cur_d == date(2023, 1, 5):
            return [OrderIntent("600519.SH", "BUY", 100)]
        elif cur_d == date(2023, 1, 6):
            return [OrderIntent("600519.SH", "SELL", 100)]
        return []

    initial_cash = D("50000.00")
    res = run_backtest(
        inst, bars, rules, roundtrip_signal,
        initial_cash=initial_cash,
        participation_rate=1.0,
        fee_model=custom_fee,
    )

    s = res.frictional_summary()
    assert s["total_turnover"] == D("20000.00")
    # Buy 10,000 * 0.0010 = 10.00 (>= min_commission 5.0)
    # Sell 10,000 * 0.0010 = 10.00 (>= min_commission 5.0)
    # Total proportional commission = 20.00 CNY, min drag = 0.00 CNY
    assert s["proportional_commission"] == D("20.00")
    assert s["min_commission_drag"] == D("0.00")
    assert s["total_commission"] == D("20.00")

    # Itemized components sum mathematically equals total fees
    assert s["total_fee"] == s["total_commission"] + s["stamp_duty"] + s["transfer_fees"]

    # Attribution report assertions
    report = res.attribution_report()
    assert "Initial Cash         :       50,000.00 CNY" in report
    assert "Initial Cash         :                 0.00 CNY" not in report
    assert "20.00 CNY" in report

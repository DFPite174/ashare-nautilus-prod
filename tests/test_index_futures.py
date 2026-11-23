from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from engine.index_futures import IFContract, IFDay, IFOrder, IFPolicy, replay_if, replay_if_portfolio

START = date(2023, 6, 14)
DAYS = [START + timedelta(days=i) for i in range(3)]
CONTRACT = IFContract("IF2306", date(2022, 10, 24), DAYS[-1])
POLICY = IFPolicy(D(".12"), D(0), D(0), D(0), D(0))


def market(delivery=None):
    return [IFDay(d, D(o), D(s), D(3900), D(4100), 100, delivery if i == 2 else None)
            for i, (d, o, s) in enumerate(zip(DAYS, (4000, 4005, 4000), (4010, 4000, 4000)))]


@pytest.mark.parametrize("side,closing,sign", [("BUY", "SELL", 1), ("SELL", "BUY", -1)])
def test_variation_margin_is_not_booked_twice(side, closing, sign):
    orders = [IFOrder(DAYS[0], side, "OPEN", 1), IFOrder(DAYS[1], closing, "CLOSE_YESTERDAY", 1)]
    r = replay_if(CONTRACT, market()[:2], orders, initial_cash=D(200000), policy=POLICY)
    assert [row.variation for row in r.daily] == [D(3000)*sign, D(-1500)*sign]
    assert r.daily[0].margin == D("144360")
    assert r.daily[-1].cash == D(200000) + D(1500)*sign
    assert r.daily[-1].margin == 0 and r.daily[-1].position == 0
    assert len(r.fills) == 2


def test_cash_delivery_and_missing_delivery():
    orders = [IFOrder(DAYS[0], "SELL", "OPEN", 1)]
    with pytest.raises(ValueError, match="delivery"):
        replay_if(CONTRACT, market(), orders, initial_cash=D(200000), policy=POLICY)
    r = replay_if(CONTRACT, market(D("3999.99")), orders, initial_cash=D(200000), policy=POLICY)
    assert r.daily[-1].cash == D("200003.00")
    assert r.daily[-1].position == 0 and r.daily[-1].margin == 0
    assert r.daily[-1].delivered_position == -1


def test_margin_and_zero_capacity_reject():
    order = IFOrder(DAYS[0], "BUY", "OPEN", 1)
    r = replay_if(CONTRACT, market()[:1], [order], initial_cash=D(10000), policy=POLICY)
    assert not r.fills and r.rejections[0][1] == "INSUFFICIENT_MARGIN"

def test_open_close_today_charges_distinct_fees_and_shares_capacity():
    from dataclasses import replace

    policy = IFPolicy(D(".12"), D(".0001"), D(".0002"), D(".0003"), D(0))
    day = replace(market()[0], open_capacity=2)
    orders = [IFOrder(day.day, "BUY", "OPEN", 1),
              IFOrder(day.day, "SELL", "CLOSE_YESTERDAY", 1),
              IFOrder(day.day, "SELL", "CLOSE_TODAY", 1),
              IFOrder(day.day, "SELL", "OPEN", 1)]
    result = replay_if(CONTRACT, [day], orders, initial_cash=D(200000), policy=policy)
    assert result.rejections == ((1, "INSUFFICIENT_CLOSE_POSITION"), (3, "OPEN_CAPACITY"))
    assert result.daily[0].fees == D("360.00")
    assert result.daily[0].cash == D("199640.00")
    assert result.daily[0].position == 0


def test_reports_margin_deficit_and_allows_reducing_risk():
    from dataclasses import replace

    data = market()[:2]
    data[0] = replace(data[0], settle=D(3950))
    data[1] = replace(data[1], open=D(3950), settle=D(3950))
    orders = [IFOrder(DAYS[0], "BUY", "OPEN", 1), IFOrder(DAYS[1], "BUY", "OPEN", 1),
              IFOrder(DAYS[1], "SELL", "CLOSE_YESTERDAY", 1)]
    result = replay_if(CONTRACT, data, orders, initial_cash=D(145000), policy=POLICY)
    assert result.daily[0].available == D(-12200)
    assert result.rejections == ((1, "INSUFFICIENT_MARGIN"),)
    assert result.daily[-1].position == 0
    assert result.daily[-1].cash == D(130000)

def test_effective_policy_rebases_margin_and_charges_new_close_fee():
    old = IFPolicy(D(".10"), D(".0001"), D(0), D(0), D(0))
    new = IFPolicy(D(".20"), D(0), D(0), D(".0003"), D(0))
    orders = [IFOrder(DAYS[0], "BUY", "OPEN", 1), IFOrder(DAYS[1], "BUY", "OPEN", 1),
              IFOrder(DAYS[1], "SELL", "CLOSE_YESTERDAY", 1)]
    result = replay_if(CONTRACT, market()[:2], orders, initial_cash=D(200000),
                       policy={DAYS[1]: new, DAYS[0]: old})
    assert result.rejections == ((1, "INSUFFICIENT_MARGIN"),)
    assert [d.fees for d in result.daily] == [D("120.00"), D("360.45")]
    assert result.daily[-1].cash == D("201019.55")
    held = replay_if(CONTRACT, market()[:2], orders[:1], initial_cash=D(200000),
                     policy={DAYS[0]: old, DAYS[1]: new})
    assert [d.margin for d in held.daily] == [D("120300.00"), D("240000.00")]
    assert held.daily[-1].available == D("-40120.00")


def test_policy_schedule_requires_coverage_at_first_session():
    with pytest.raises(ValueError, match="first IF session"):
        replay_if(CONTRACT, market()[:2], [], initial_cash=D(200000), policy={DAYS[1]: POLICY})

@pytest.mark.parametrize("cash,lock,expected_positions,expected_rejection", [
    (200000, False, {"IF2306": 0, "IF2307": 1}, ()),
    (200000, True, {"IF2306": 1, "IF2307": 0}, ((1, "LIMIT_LOCK"), (2, "INSUFFICIENT_MARGIN"))),
])
def test_roll_has_shared_margin_and_preserves_failed_leg(cash, lock, expected_positions, expected_rejection):
    from dataclasses import replace

    old = market()[:2]
    if lock:
        old[1] = replace(old[1], lower=old[1].open)
    new = IFContract("IF2307", CONTRACT.listed, date(2023, 7, 21))
    orders = [(CONTRACT.symbol, IFOrder(DAYS[0], "BUY", "OPEN", 1)),
              (CONTRACT.symbol, IFOrder(DAYS[1], "SELL", "CLOSE_YESTERDAY", 1)),
              (new.symbol, IFOrder(DAYS[1], "BUY", "OPEN", 1))]
    result = replay_if_portfolio({CONTRACT: old, new: market()[:2]}, orders,
                                 initial_cash=D(cash), policy=POLICY)
    assert result.daily[-1].positions == expected_positions
    assert result.daily[-1].cash == D(200000)
    assert result.daily[-1].margin == D(144000)
    assert result.rejections == expected_rejection


def test_roll_open_first_cannot_spend_margin_from_future_close():
    new = IFContract("IF2307", CONTRACT.listed, date(2023, 7, 21))
    m = market()[:2]
    orders = [(CONTRACT.symbol, IFOrder(DAYS[0], "BUY", "OPEN", 1)),
              (new.symbol, IFOrder(DAYS[1], "BUY", "OPEN", 1)),
              (CONTRACT.symbol, IFOrder(DAYS[1], "SELL", "CLOSE_YESTERDAY", 1))]
    r = replay_if_portfolio({CONTRACT: m, new: m}, orders, initial_cash=D(200000), policy=POLICY)
    assert r.rejections == ((1, "INSUFFICIENT_MARGIN"),)
    assert r.daily[-1].positions == {"IF2306": 0, "IF2307": 0}
    assert r.daily[-1].cash == D("201500.00")
    assert r.daily[0].positions["IF2306"] == 1


def test_missing_overlap_data_is_not_treated_as_zero_exposure():
    new = IFContract("IF2307", CONTRACT.listed, date(2023, 7, 21))
    with pytest.raises(ValueError, match="Missing IF session"):
        replay_if_portfolio({CONTRACT: market()[:2], new: market()[1:2]}, [],
                            initial_cash=D(200000), policy=POLICY)


def test_calendar_detects_session_missing_for_all_contracts():
    rows = market()
    with pytest.raises(ValueError, match="trading calendar"):
        replay_if_portfolio({CONTRACT: [rows[0], rows[-1]]}, [], initial_cash=D(200000),
                            policy=POLICY, trading_days=DAYS)


def test_cross_product_gap_funding_and_effective_contract_policy():
    ic = IFContract("IC2306", CONTRACT.listed, CONTRACT.expiry)
    market_days = market()[:2]
    high_margin = IFPolicy(D(".25"), D(".0001"), D(0), D(0), D(0))
    orders = [(ic.symbol, IFOrder(DAYS[0], "SELL", "OPEN", 1)),
              (CONTRACT.symbol, IFOrder(DAYS[1], "BUY", "OPEN", 1))]
    result = replay_if_portfolio({CONTRACT: market_days, ic: market_days}, orders, initial_cash=D(340000),
                                 policy=POLICY, contract_policies={ic.symbol: {DAYS[0]: high_margin}})
    assert result.order_symbols == ("IC2306", "IF2306")
    assert result.rejections == ((1, "INSUFFICIENT_MARGIN"),)
    assert result.daily[0].cash == D("337920.00")
    assert result.daily[1].cash == D("339920.00")
    assert result.daily[1].margin == D("200000.00")
    assert result.daily[1].notionals == {"IF2306": D(0), "IC2306": D(-800000)}
    assert result.daily[1].contract_policies == {"IF2306": POLICY, "IC2306": high_margin}

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from engine.index_futures import IFContract, IFDay, IFOrder, IFPolicy, replay_if

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

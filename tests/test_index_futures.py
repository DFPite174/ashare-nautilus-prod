from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
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


@pytest.mark.parametrize("opening,sign", [("BUY", 1), ("SELL", -1)])
def test_expiry_uses_official_price_once_when_another_contract_keeps_trading(opening, sign):
    following = DAYS[-1] + timedelta(days=3)
    next_month = IFContract("IF2307", CONTRACT.listed, date(2023, 7, 21))
    next_day = IFDay(following, D(4000), D(4002), D(3900), D(4100), 100)
    orders = [(CONTRACT.symbol, IFOrder(DAYS[0], opening, "OPEN", 1)),
              (next_month.symbol, IFOrder(following, "BUY", "OPEN", 1))]
    result = replay_if_portfolio({CONTRACT: market(D("3999.99")), next_month: market()+[next_day]},
                                 orders, initial_cash=D(250000), policy=POLICY)
    assert not result.rejections and len(result.fills) == 2
    expiry, afterward = result.daily[-2:]
    assert expiry.cash == D(250000) - 3*sign
    assert expiry.deliveries[CONTRACT.symbol] == sign and expiry.margin == 0
    assert afterward.cash == expiry.cash + 600
    assert afterward.positions == {CONTRACT.symbol: 0, next_month.symbol: 1}
    assert afterward.deliveries == {CONTRACT.symbol: 0, next_month.symbol: 0}


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


@pytest.mark.parametrize("capacity,expected", [(0, 1), (1, 1), (2, 0)])
def test_linked_roll_requires_complete_confirmed_close(capacity, expected):
    new = IFContract("IF2307", CONTRACT.listed, date(2023, 7, 21))
    a, b = market()[:2]
    orders = [(CONTRACT.symbol, IFOrder(a.day, "BUY", "OPEN", 2)),
              (CONTRACT.symbol, IFOrder(b.day, "SELL", "CLOSE_YESTERDAY", 2, allow_partial=True)),
              (new.symbol, IFOrder(b.day, "BUY", "OPEN", 2, depends_on=1))]
    r = replay_if_portfolio({CONTRACT: [a, replace(b, open_capacity=capacity)], new: [a, b]},
                            orders, initial_cash=D(900000), policy=POLICY)
    assert r.daily[-1].positions == {CONTRACT.symbol: 2-capacity, new.symbol: 2*(1-expected)}
    assert any(reason == "DEPENDENCY_UNFILLED" for _, reason in r.rejections) == bool(expected)
    assert r.unfilled_lots.get(1, 0) == (1 if capacity == 1 else 0)


def test_offset_uses_larger_aggregate_side():
    ic = IFContract("IC2306", CONTRACT.listed, CONTRACT.expiry)
    a, b = market()[:2]
    policy = replace(POLICY, margin=D(".20"))
    # The opposite leg offsets both IF lots; the larger side sets the margin.
    orders = [(ic.symbol, IFOrder(a.day, "SELL", "OPEN", 2)),
              (CONTRACT.symbol, IFOrder(a.day, "BUY", "OPEN", 2)),
              (ic.symbol, IFOrder(b.day, "BUY", "CLOSE_YESTERDAY", 2))]
    rows = {CONTRACT: [a, replace(b, open=D(4100), settle=D(4100), upper=D(4120))], ic: [a, b]}
    r = replay_if_portfolio(rows, orders, initial_cash=D(500000), policy=policy,
                            margin_groups=[("IF", "IC")])
    assert r.daily[0].margin == D(481200)
    assert r.daily[0].gross_margin == D(802000)
    assert not r.rejections
    gross = replay_if_portfolio(rows, orders, initial_cash=D(500000), policy=policy)
    assert gross.rejections == ((1, "INSUFFICIENT_MARGIN"),)


def test_submission_fee_partial_fill_and_explicit_cash_deposit():
    a, b = market()[:2]
    orders = [(CONTRACT.symbol, IFOrder(a.day, "BUY", "OPEN", 2, allow_partial=True)),
              (CONTRACT.symbol, IFOrder(a.day, "BUY", "OPEN", 1)),
              (CONTRACT.symbol, IFOrder(b.day, "SELL", "CLOSE_YESTERDAY", 1))]
    r = replay_if_portfolio({CONTRACT: [replace(a, open_capacity=1), b]}, orders,
                            initial_cash=D(350000), policy=replace(POLICY, submission_fee=D(1)),
                            cash_deposits={b.day: D(250)})
    assert [fill[1].lots for fill in r.fills] == [1, 1]
    assert r.rejections == ((1, "OPEN_CAPACITY"),)
    assert r.daily[0].submission_fees == 1
    assert r.daily[1].cash_deposit == 250
    assert r.daily[1].cash == D(351748)


def test_previous_close_shortfall_can_be_funded_before_next_open():
    a, b = market()[:2]
    rows = {CONTRACT: [replace(a, settle=D(3950)), replace(b, open=D(3950), settle=D(3950))]}
    orders = [(CONTRACT.symbol, IFOrder(day, "BUY", "OPEN", 1)) for day in (a.day, b.day)]
    r = replay_if_portfolio(rows, orders, initial_cash=D(145000), policy=POLICY,
                            cash_deposits={b.day: D(160000)})
    assert r.daily[0].margin_shortfall == D(12200)
    assert r.daily[1].cash_deposit == D(160000)
    assert r.daily[1].available == D(5600)
    assert r.daily[1].margin_shortfall == 0 and r.daily[1].positions[CONTRACT.symbol] == 2


def test_dependency_must_be_an_earlier_order_on_the_same_day():
    a, b = market()[:2]
    orders = [IFOrder(a.day, "BUY", "OPEN", 1), IFOrder(b.day, "SELL", "CLOSE_YESTERDAY", 1, depends_on=0)]
    with pytest.raises(ValueError, match="same day"):
        replay_if(CONTRACT, [a, b], orders, initial_cash=D(350000), policy=POLICY)


def test_order_position_and_daily_open_limits_bind_independently():
    a, b = market()[:2]
    policy = replace(POLICY, max_order_lots=2, max_position_lots=2, max_daily_open_lots=3)
    orders = [IFOrder(a.day, side, effect, lots) for side, effect, lots in (
        ("BUY", "OPEN", 3), ("BUY", "OPEN", 2), ("BUY", "OPEN", 1),
        ("SELL", "CLOSE_TODAY", 2), ("BUY", "OPEN", 2), ("BUY", "OPEN", 1),
    )]
    orders.append(IFOrder(b.day, "BUY", "OPEN", 1))
    r = replay_if(CONTRACT, [a, b], orders, initial_cash=D(1000000), policy=policy)
    assert r.rejections == ((0, "ORDER_LIMIT"), (2, "POSITION_LIMIT"), (4, "DAILY_OPEN_LIMIT"))
    assert [row.position for row in r.daily] == [1, 2]
    assert [f[0] for f in r.fills] == [1, 3, 5, 6]


def test_replay_evidence_reconciles_input_orders_and_detects_data_changes():
    row = replace(market()[0], open_capacity=1)
    orders = [IFOrder(row.day, "BUY", "OPEN", 2, allow_partial=True, limit_price=D(4000)),
              IFOrder(row.day, "BUY", "OPEN", 1, limit_price=D(3999))]
    options = dict(initial_cash=D(500000), policy=POLICY, audit=True, data_snapshot_id="local-test",
                   capacity_model="opening-volume-scenario")
    r = replay_if(CONTRACT, [row], orders, **options)
    again = replay_if(CONTRACT, [row], iter(orders), **options)
    changed = replay_if(CONTRACT, [replace(row, settle=D(4020))], orders, **options)
    assert r.evidence == again.evidence
    assert r.evidence["input_sha256"] != changed.evidence["input_sha256"]
    assert r.evidence["result_sha256"] != changed.evidence["result_sha256"]
    first, second = r.evidence["orders"]
    assert (first["requested_lots"], first["filled_lots"], first["unfilled_lots"]) == (2, 1, 1)
    assert first["fill_ts_ns"] is not None
    assert (first["available_opening_lots"], second["available_opening_lots"]) == (1, 0)
    assert (second["reason"], second["unfilled_lots"]) == ("NOT_MARKETABLE", 1)


def test_limit_prices_book_the_native_open_for_both_sides():
    a, b = market()[:2]
    orders = [IFOrder(a.day, "BUY", "OPEN", 1, limit_price=D(4010)),
              IFOrder(b.day, "SELL", "CLOSE_YESTERDAY", 1, limit_price=D(4000)),
              IFOrder(b.day, "SELL", "OPEN", 1, limit_price=D(3850))]
    r = replay_if(CONTRACT, [a, b], orders, initial_cash=D(350000), policy=POLICY)
    assert [f[2] for f in r.fills] == [a.open, b.open]
    assert r.rejections == ((2, "PRICE_OUTSIDE_BAND"),)


def test_observed_opening_quotes_use_independent_sides_and_execution_prices():
    from engine.index_futures import IFOpeningQuote

    a = market()[0]
    timestamp = int(datetime(2023, 6, 14, 9, 30, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())*10**9
    quote = IFOpeningQuote(timestamp, D("4000.0"), D("4000.2"), 2, 1, "controlled-fixture")
    row = replace(a, opening_quote=quote)
    orders = [IFOrder(a.day, "BUY", "OPEN", 2, allow_partial=True),
              IFOrder(a.day, "BUY", "OPEN", 1),
              IFOrder(a.day, "SELL", "CLOSE_TODAY", 1),
              IFOrder(a.day, "SELL", "OPEN", 1)]
    r = replay_if(CONTRACT, [row], orders, initial_cash=D(500000), policy=POLICY,
                  audit=True, data_snapshot_id="fixture", capacity_model="observed-bbo")
    assert [f[2] for f in r.fills] == [D("4000.2"), D(4000), D(4000)]
    assert r.rejections == ((1, "OPEN_CAPACITY"),)
    assert r.unfilled_lots == {0: 1}
    assert r.daily[0].position == -1
    assert r.daily[0].cash == D(496940)
    assert [o["available_opening_lots"] for o in r.evidence["orders"]] == [1, 0, 2, 1]
    assert r.evidence["orders"][0]["opening_quote_ts_ns"] == timestamp
    assert r.evidence["orders"][0]["fill_ts_ns"] == timestamp + 1
    changed = replay_if(CONTRACT, [replace(row, open=a.open+D(10))], orders,
                        initial_cash=D(500000), policy=POLICY)
    assert changed.fills == r.fills and changed.daily == r.daily


def test_observed_opening_rejects_stale_and_incomplete_quotes():
    from engine.index_futures import IFOpeningQuote

    a, b = market()[:2]
    start = int(datetime(2023, 6, 14, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())*10**9
    q = IFOpeningQuote(start-1, D(4000), D(4000), 1, 1, "controlled-fixture")
    with pytest.raises(ValueError, match="opening quote"):
        replay_if(CONTRACT, [replace(a, opening_quote=q)], [], initial_cash=D(500000), policy=POLICY)
    with pytest.raises(ValueError, match="complete|aligned"):
        replay_if(CONTRACT, [replace(a, opening_quote=replace(q, ts_ns=start)), b], [],
                  initial_cash=D(500000), policy=POLICY)


def test_cross_contract_quotes_submit_after_last_quote_with_explicit_age_budget():
    from engine.index_futures import IFOpeningQuote

    a = market()[0]
    start = int(datetime(2023, 6, 14, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())*10**9
    next_month = IFContract("IF2307", CONTRACT.listed, date(2023, 7, 21))
    q = IFOpeningQuote(start, D(4000), D(4000), 1, 1, "controlled-fixture")
    rows = {CONTRACT: [replace(a, opening_quote=q)],
            next_month: [replace(a, opening_quote=replace(q, ts_ns=start+10))]}
    orders = [(CONTRACT.symbol, IFOrder(a.day, "BUY", "OPEN", 1))]
    kwargs = dict(initial_cash=D(500000), policy=POLICY, audit=True,
                  data_snapshot_id="fixture", capacity_model="observed-bbo")
    with pytest.raises(ValueError, match="age limit"):
        replay_if_portfolio(rows, orders, **kwargs)
    r = replay_if_portfolio(rows, orders, max_open_quote_age_ns=10, **kwargs)
    assert r.evidence["orders"][0]["fill_ts_ns"] == start+11


def test_reconcile_external_split_fills_rejects_duplicates_and_surfaces_deltas():
    from engine.index_futures import IFReferenceFill, compare_opening_fills

    row = market()[0]
    orders = [IFOrder(row.day, "BUY", "OPEN", 2), IFOrder(row.day, "SELL", "CLOSE_TODAY", 2)]
    result = replay_if(CONTRACT, [row], orders, initial_cash=D(500000), policy=POLICY, audit=True,
                       data_snapshot_id="fixture", capacity_model="daily")
    ts = result.evidence["orders"][0]["fill_ts_ns"]
    first = IFReferenceFill("one", 0, CONTRACT.symbol, "BUY", ts, 1, row.open)
    split = replace(first, fill_id="two", ts_ns=ts+2)
    exit_fill = IFReferenceFill("three", 1, CONTRACT.symbol, "SELL", ts, 2, row.open)
    accepted = compare_opening_fills(result, [first, split, exit_fill], source="controlled-reference",
                                     timestamp_tolerance_ns=2)
    assert accepted.matches and not accepted.differences
    failed = compare_opening_fills(result, [replace(first, price=row.open+D(".2")), split],
                                   source="controlled-reference")
    assert failed.differences == {0: ("PRICE", "TIME"), 1: ("MISSING_REFERENCE_FILL",)}
    with pytest.raises(ValueError, match="duplicate"):
        compare_opening_fills(result, [first, first], source="controlled-reference")


def test_strict_opening_comparison_detects_offsetting_prices_and_missing_zero_fill_order():
    from engine.index_futures import IFReferenceFill, IFReferenceOrder, compare_opening_fills

    row = market()[0]
    orders = [IFOrder(row.day, "BUY", "OPEN", 2),
              IFOrder(row.day, "BUY", "OPEN", 1, limit_price=row.open-D(".2"))]
    result = replay_if(CONTRACT, [row], orders, initial_cash=D(500000), policy=POLICY, audit=True,
                       data_snapshot_id="fixture", capacity_model="daily")
    ts = result.evidence["orders"][0]["fill_ts_ns"]
    one = IFReferenceFill("fill-1", 0, CONTRACT.symbol, "BUY", ts, 1, row.open, broker_order_id="order-1")
    two = replace(one, fill_id="fill-2")
    first = IFReferenceOrder(0, "order-1", row.day, CONTRACT.symbol, "BUY", "OPEN", 2, 2, "FILLED")
    second = IFReferenceOrder(1, "order-2", row.day, CONTRACT.symbol, "BUY", "OPEN", 1, 0, "EXPIRED",
                              limit_price=orders[1].limit_price)
    diagnostic = compare_opening_fills(result, [one, two], source="fixture")
    assert diagnostic.matches and not diagnostic.accepted
    incomplete = compare_opening_fills(result, [one, two], source="fixture", reference_orders=[first])
    assert incomplete.differences == {1: ("UNCOVERED_ORDER",)} and not incomplete.accepted
    strict = compare_opening_fills(result, [one, two], source="fixture", reference_orders=[first, second])
    assert strict.accepted and strict.reference_orders_covered == 2
    offset = compare_opening_fills(
        result, [replace(one, price=row.open-D(".2")), replace(two, price=row.open+D(".2"))],
        source="fixture", reference_orders=[first, second],
    )
    assert offset.differences == {0: ("PRICE_DISTRIBUTION",)} and not offset.accepted
    assert offset.reference_sha256 != strict.reference_sha256
    with pytest.raises(ValueError, match="terminal quantity"):
        compare_opening_fills(result, [one], source="fixture", reference_orders=[first, second])
    with pytest.raises(ValueError, match="broker order"):
        compare_opening_fills(result, [replace(one, broker_order_id="wrong"), two], source="fixture",
                              reference_orders=[first, second])
    wrong_fee = compare_opening_fills(result, [one, two], source="fixture",
                                     reference_orders=[first, replace(second, submission_fee=D(1))])
    assert wrong_fee.differences == {1: ("SUBMISSION_FEE",)}
    with pytest.raises(ValueError, match="does not identify"):
        compare_opening_fills(result, [one, two], source="fixture",
                              reference_orders=[replace(first, effect="CLOSE_TODAY"), second])
    with pytest.raises(ValueError, match="terminal status"):
        compare_opening_fills(result, [one, two], source="fixture",
                              reference_orders=[first, replace(second, status="WORKING")])

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from engine.futures_reconciliation import IFSettlementStatement, compare_daily_settlement
from engine.index_futures import IFContract, IFDay, IFOrder, IFPolicy, replay_if


@pytest.fixture
def expiry_run():
    first, second = date(2023, 6, 15), date(2023, 6, 16)
    contract = IFContract("IF2306", first, second)
    days = [IFDay(first, D(4000), D(4005), D(3900), D(4100), 1),
            IFDay(second, D(4002), D(4000), D(3900), D(4100), 1, D("4001.01"))]
    options = dict(initial_cash=D(180000), policy=IFPolicy(D(".1"), D(".0001"), D(0), D(0), D(".0001")),
                   cash_deposits={second: D(500)}, audit=True, data_snapshot_id="expiry-case",
                   capacity_model="fixture")
    result = replay_if(contract, days, [IFOrder(first, "BUY", "OPEN", 1)], **options)
    expected = [
        IFSettlementStatement(first, D(181380), D(1500), D(120), D(120150), D(61230), D(0),
                              {"IF2306": 1}, {}),
        IFSettlementStatement(second, D("180562.97"), D(-1197), D("120.03"), D(0), D("180562.97"), D(500),
                              {}, {"IF2306": 1}),
    ]
    return result, expected


def compare(result, statements, opening_cash=D(180000)):
    return compare_daily_settlement(result, statements, opening_cash=opening_cash, source="fixture")


def test_statement_reconciliation_covers_cash_bridge_position_and_delivery(expiry_run):
    result, statements = expiry_run
    report = compare(result, reversed(statements))
    assert report.accepted and report.covered_days == 2
    assert not report.differences
    assert report.reference_sha256 == compare(result, statements).reference_sha256
    assert report.replay_result_sha256 == result.evidence["result_sha256"]


def test_statement_reconciliation_localizes_lost_rows_and_wrong_amounts(expiry_run):
    result, (first, second) = expiry_run
    wrong_first = replace(first, cash=first.cash + D(1), positions={})
    wrong_second = replace(second, deliveries={})
    report = compare(result, [wrong_first, wrong_second])
    assert not report.accepted
    assert {(d.day, d.field) for d in report.differences} == {
        (first.day, "cash"), (first.day, "positions.IF2306"), (first.day, "reference_available_identity"),
        (first.day, "reference_cash_bridge"), (second.day, "deliveries.IF2306"),
        (second.day, "reference_cash_bridge"),
    }
    missing = compare(result, [second])
    assert not missing.accepted and missing.covered_days == 1
    assert [(d.day, d.field) for d in missing.differences] == [(first.day, "missing_statement")]


def test_duplicate_invalid_and_foreign_statement_rows(expiry_run):
    result, (first, second) = expiry_run
    with pytest.raises(ValueError, match="Duplicate"):
        compare(result, [first, first])
    with pytest.raises(ValueError, match="positions"):
        compare(result, [replace(first, positions={"IF2306": True}), second])
    extra = replace(second, day=date(2023, 6, 19))
    report = compare(result, [first, second, extra])
    assert not report.accepted and report.differences[0].field == "unexpected_statement"
    tampered = replace(result, daily=(replace(result.daily[0], cash=D(1)), result.daily[1]))
    with pytest.raises(ValueError, match="audit fingerprint"):
        compare(tampered, [first, second])

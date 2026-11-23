"""Independent clearing-statement reconciliation for audited CFFEX replay."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from engine.index_futures import IFResult
from engine.reproducibility import digest

_CENT = Decimal(".01")
_AMOUNTS = ("cash", "variation", "fees", "margin", "available", "cash_deposit")


@dataclass(frozen=True)
class IFSettlementStatement:
    day: date
    cash: Decimal
    variation: Decimal
    fees: Decimal
    margin: Decimal
    available: Decimal
    cash_deposit: Decimal
    positions: Mapping[str, int]
    deliveries: Mapping[str, int]
    submission_fees: Decimal | None = None


@dataclass(frozen=True)
class IFSettlementDifference:
    day: date
    field: str
    expected: Decimal | int | None
    received: Decimal | int | None


@dataclass(frozen=True)
class IFSettlementComparison:
    source: str
    reference_sha256: str
    replay_result_sha256: str
    replay_days: int
    covered_days: int
    differences: tuple[IFSettlementDifference, ...]

    @property
    def accepted(self):
        return self.covered_days == self.replay_days and not self.differences


def _is_money(value):
    return isinstance(value, Decimal) and value.is_finite() and value % _CENT == 0


def _statement_rows(statements):
    by_day = {}
    for row in statements:
        if not isinstance(row, IFSettlementStatement) or type(row.day) is not date:
            raise ValueError("Invalid settlement statement date")
        if row.day in by_day:
            raise ValueError(f"Duplicate settlement statement for {row.day}")
        for field in _AMOUNTS:
            value = getattr(row, field)
            if not _is_money(value) or (field in ("fees", "margin", "cash_deposit") and value < 0):
                raise ValueError(f"Invalid settlement {field} on {row.day}")
        if row.submission_fees is not None and (
            not _is_money(row.submission_fees) or not 0 <= row.submission_fees <= row.fees
        ):
            raise ValueError(f"Invalid settlement submission_fees on {row.day}")
        for field in ("positions", "deliveries"):
            lots = getattr(row, field)
            if not isinstance(lots, Mapping) or any(
                not isinstance(symbol, str) or not symbol.strip() or type(quantity) is not int
                for symbol, quantity in lots.items()
            ):
                raise ValueError(f"Invalid settlement {field} on {row.day}")
        by_day[row.day] = replace(row, positions=dict(row.positions), deliveries=dict(row.deliveries))
    return by_day


def compare_daily_settlement(result: IFResult, statements, *, opening_cash, source):
    """Compare complete CNY statements on the replay's trading calendar, exactly to the cent."""
    if result.evidence is None or not result.daily or not isinstance(source, str) or not source.strip():
        raise ValueError("Settlement comparison requires an audited replay and reference source")
    if not _is_money(opening_cash):
        raise ValueError("Reference opening cash must be a finite cent amount")
    replay_digest = result.evidence["result_sha256"]
    if digest({"orders": result.evidence["orders"], "daily": result.daily}) != replay_digest:
        raise ValueError("Replay settlement rows disagree with the audit fingerprint")

    references = _statement_rows(statements)
    replay_dates = {row.day for row in result.daily}
    first = result.daily[0]
    differences = []

    def compare(day, field, expected, received):
        if expected != received:
            differences.append(IFSettlementDifference(day, field, expected, received))

    compare(first.day, "opening_cash", first.cash - first.cash_deposit - first.variation + first.fees,
            opening_cash)
    previous_cash = opening_cash
    for actual in result.daily:
        ref = references.get(actual.day)
        if ref is None:
            differences.append(IFSettlementDifference(actual.day, "missing_statement", None, None))
            previous_cash = None
            continue
        for field in _AMOUNTS:
            compare(actual.day, field, getattr(actual, field), getattr(ref, field))
        if ref.submission_fees is not None:
            compare(actual.day, "submission_fees", actual.submission_fees, ref.submission_fees)
        for field in ("positions", "deliveries"):
            expected_lots, ref_lots = getattr(actual, field), getattr(ref, field)
            for symbol in sorted(expected_lots.keys() | ref_lots.keys()):
                compare(actual.day, f"{field}.{symbol}", expected_lots.get(symbol, 0), ref_lots.get(symbol, 0))
        compare(actual.day, "reference_available_identity", ref.cash - ref.margin, ref.available)
        if previous_cash is not None:
            compare(actual.day, "reference_cash_bridge",
                    previous_cash + ref.cash_deposit + ref.variation - ref.fees, ref.cash)
        previous_cash = ref.cash
    for day in sorted(references.keys() - replay_dates):
        differences.append(IFSettlementDifference(day, "unexpected_statement", None, None))
    reference_digest = digest({"opening_cash": opening_cash, "statements": references})
    return IFSettlementComparison(source, reference_digest, replay_digest, len(replay_dates),
                                  len(references.keys() & replay_dates), tuple(differences))

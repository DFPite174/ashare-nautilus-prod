"""Validate an IF2306 short through expiry against the local Tushare lake."""
import argparse
from datetime import date
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path
import sys

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.index_futures import IFContract, IFDay, IFOrder, IFPolicy, replay_if


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Tushare_DB directory")
    lake = parser.parse_args().root / "lake"
    code = "IF2306.CFX"
    master, = pq.read_table(lake / "meta/fut_basic.parquet", filters=[("ts_code", "=", code)]).to_pylist()
    assert master["exchange"] == "CFFEX" and D(str(master["multiplier"])) == 300
    expiry = date.fromisoformat(master["last_ddate"])
    assert expiry == date(2023, 6, 16)
    contract = IFContract("IF2306", date.fromisoformat(master["list_date"]), expiry)
    columns = ["trade_date", "pre_settle", "open", "settle", "vol", "oi"]
    rows = pq.read_table(lake / "futures/daily/year=2023/fut_daily_2023.parquet", columns=columns,
                         filters=[("ts_code", "=", code), ("trade_date", ">=", "20230612"),
                                  ("trade_date", "<=", "20230616")]).to_pylist()
    rows.sort(key=lambda row: row["trade_date"])
    assert [row["trade_date"] for row in rows] == [f"2023061{i}" for i in range(2, 7)]
    assert rows[-1]["oi"] == 0
    days = []
    tick = D(".2")
    for prev, row in zip(rows, rows[1:]):
        day = date.fromisoformat(row["trade_date"])
        assert D(str(row["pre_settle"])) == D(str(prev["settle"]))
        settle, opening = D(str(row["settle"])), D(str(row["open"]))
        # Explicit scenario bands for this check; not a historical rule feed.
        ref = D(str(row["pre_settle"]))
        lower, upper = (((ref * m) / tick).quantize(D(1), rounding=ROUND_HALF_UP) * tick
                        for m in (D(".8"), D("1.2")))
        assert lower < opening < upper
        days.append(IFDay(day, opening, settle, lower, upper, int(D(str(prev["vol"])) * D(".01")),
                          settle if day == expiry else None))
    initial = D(1000000)
    # Scenario margin and fees, not reconstructed brokerage terms.
    policy = IFPolicy(D(".12"), D(".0001"), D(".0002"), D(".0001"), D(".0001"))
    short = IFOrder(days[0].day, "SELL", "OPEN", 1)
    result = replay_if(contract, days, [short], initial_cash=initial, policy=policy)
    cents = lambda v: v.quantize(D(".01"), rounding=ROUND_HALF_UP)
    cash = initial
    prior = days[0].open
    for i, (day, account) in enumerate(zip(days, result.daily, strict=True)):
        delivered = day.day == expiry
        mark = day.delivery if delivered else day.settle
        variation = cents((prior - mark) * 300)
        fees = (cents(day.open * 300 * policy.open_fee) if i == 0 else D(0))
        if delivered:
            fees += cents(mark * 300 * policy.delivery_fee)
        cash += variation - fees
        assert (account.cash, account.variation, account.fees, account.margin, account.position) == (
            cash, variation, fees, D(0) if delivered else cents(mark * 300 * policy.margin),
            0 if delivered else -1)
        prior = mark
        print(day.day, f"variation={variation} fees={fees} cash={cash} margin={account.margin}")
    assert len(result.fills) == 1 and not result.rejections
    assert result.daily[-1].delivered_position == -1


if __name__ == "__main__":
    main()

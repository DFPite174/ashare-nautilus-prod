"""Verify opening, rolls and expiry against the local Tushare lake and a CFFEX notice."""
import argparse
from datetime import date
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path
import sys

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.index_futures import IFContract, IFDay, IFOrder, IFPolicy, replay_if, replay_if_portfolio

# CFFEX notice 中金所发〔2023〕39号, cross-checked against local expiry settlements.
DELIVERY = {"IF2306": D("3957.67"), "IH2306": D("2596.87"),
            "IC2306": D("6144.74"), "IM2306": D("6665.69")}
MULTIPLIERS = {"IF": 300, "IH": 300, "IC": 200, "IM": 200}


def load_sample(lake, code):
    master, = pq.read_table(lake / "meta/fut_basic.parquet", filters=[("ts_code", "=", code)]).to_pylist()
    assert master["exchange"] == "CFFEX" and D(str(master["multiplier"])) == MULTIPLIERS[code[:2]]
    expiry = date.fromisoformat(master["last_ddate"])
    contract = IFContract(code.split(".")[0], date.fromisoformat(master["list_date"]), expiry)
    columns = ["trade_date", "pre_settle", "open", "settle", "vol", "oi"]
    rows = pq.read_table(lake / "futures/daily/year=2023/fut_daily_2023.parquet", columns=columns,
                         filters=[("ts_code", "=", code), ("trade_date", ">=", "20230612"),
                                  ("trade_date", "<=", "20230616")]).to_pylist()
    rows.sort(key=lambda row: row["trade_date"])
    assert [row["trade_date"] for row in rows] == [f"2023061{i}" for i in range(2, 7)]
    if date.fromisoformat(rows[-1]["trade_date"]) == expiry:
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
        delivery = DELIVERY[contract.symbol] if day == expiry else None
        if delivery is not None:
            assert settle == delivery
        days.append(IFDay(day, opening, settle, lower, upper, int(D(str(prev["vol"])) * D(".01")),
                          delivery))
    return contract, days


def validate_cross_product(lake, trading_days):
    contracts = dict(load_sample(lake, f"{symbol}.CFX") for symbol in DELIVERY)
    initial_cash = D(1500000)
    policies = {contract.symbol: IFPolicy(D(i + 10) / 100, D(i + 1) / 10000, D(0), D(0), D(".0002"))
                for i, contract in enumerate(contracts)}
    orders = [(contract.symbol, IFOrder(trading_days[0], "SELL" if i % 2 else "BUY", "OPEN", 1))
              for i, contract in enumerate(contracts)]
    sample = replay_if_portfolio(contracts, orders, initial_cash=initial_cash, policy=policies["IF2306"],
                                 contract_policies=policies, trading_days=trading_days)
    assert not sample.rejections and len(sample.fills) == 4
    cents = lambda v: v.quantize(D(".01"), rounding=ROUND_HALF_UP)
    opening_fees = sum(cents(rows[0].open * MULTIPLIERS[c.symbol[:2]] * policies[c.symbol].open_fee)
                       for c, rows in contracts.items())
    for i, account in enumerate(sample.daily):
        pnl, margin = D(0), D(0)
        fees = opening_fees
        for j, (c, rows) in enumerate(contracts.items()):
            point_value = MULTIPLIERS[c.symbol[:2]]
            marked_value = rows[i].settle * point_value
            side = -1 if j % 2 else 1
            settled = account.day == c.expiry
            pnl += (marked_value - rows[0].open * point_value) * side
            if settled:
                fees += cents(marked_value * policies[c.symbol].delivery_fee)
            else:
                margin += cents(marked_value * policies[c.symbol].margin)
            assert account.positions[c.symbol] == (0 if settled else side)
            assert account.deliveries[c.symbol] == (side if settled else 0)
            assert account.notionals[c.symbol] == account.positions[c.symbol] * marked_value
        assert (account.cash, account.margin, account.available) == (initial_cash + pnl - fees, margin,
                                                                    initial_cash + pnl - fees - margin)
    print("IF/IH/IC/IM", sample.daily[-1].cash, dict(sample.daily[-1].deliveries), sample.daily[-1].margin)


def validate_equity_overlay(root, contract, days, policy):
    from engine import run_frozen_orders_backtest
    from engine.tushare_lake import load_tushare_lake

    symbol = "600519.SH"
    stocks = load_tushare_lake(root, [symbol], "2023-06-09", str(days[-1].day))
    stock_cash, futures_cash = D(2000000), D(300000)
    stock = run_frozen_orders_backtest(
        stocks.instruments, stocks.bars, stocks.rules,
        [dict(symbol=symbol, side="BUY", quantity=900, trade_date=date(2023, 6, 12))],
        initial_cash=stock_cash, corporate_actions=stocks.corporate_actions,
        trading_days=stocks.trading_days, data_snapshot_id=stocks.snapshot["snapshot_id"],
        participation_rate=.01,
    )
    assert len(stock.fills) == 1 and not stock.rejections
    nav = {date.fromisoformat(row["date"]): row for row in stock.daily_nav}
    prior = nav[date(2023, 6, 12)]
    stock_exposure = D(prior["total_equity"]) - D(prior["cash"])
    # Unit beta for the demonstration. Size with the prior settlement, never tomorrow's open.
    previous_settle, = pq.read_table(
        root / "lake/futures/daily/year=2023/fut_daily_2023.parquet", columns=["settle"],
        filters=[("ts_code", "=", "IF2306.CFX"), ("trade_date", "=", "20230612")],
    )["settle"].to_pylist()
    lots = int(stock_exposure // (D(str(previous_settle)) * 300))
    assert lots == 1
    futures = replay_if(contract, days, [IFOrder(days[0].day, "SELL", "OPEN", lots)],
                        initial_cash=futures_cash, policy=policy)
    assert len(futures.fills) == 1 and not futures.rejections
    for day, row in zip(days, futures.daily, strict=True):
        stock_row = nav[day.day]
        assert stock_row["cash_matches"] and stock_row["positions_match"]
        equity = D(stock_row["total_equity"])
        close = next(b.close for b in stocks.bars if b.end.date() == day.day)
        fill = stock.fills[0]
        stock_pnl = 900 * (close - D(fill["price"])) - D(fill["fee"])
        assert equity - stock_cash == stock_pnl
        combined = equity + row.cash
        net_notional = 900 * close + row.notionals[contract.symbol]
        print("hedge", day.day, f"stock_pnl={stock_pnl} futures_pnl={row.cash-futures_cash} "
              f"combined_nav={combined} net_notional={net_notional}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Tushare_DB directory")
    root = parser.parse_args().root
    lake = root / "lake"
    calendar = pq.read_table(lake / "meta/trade_cal.parquet", columns=["cal_date"], filters=[
        ("exchange", "=", "SSE"), ("is_open", "=", 1), ("cal_date", ">=", "20230613"),
        ("cal_date", "<=", "20230616"),
    ])
    trading_days = tuple(sorted(date.fromisoformat(d) for d in calendar["cal_date"].to_pylist()))
    code = "IF2306.CFX"
    contract, days = load_sample(lake, code)
    initial = D(1000000)
    # Scenario margin and fees, not reconstructed brokerage terms.
    policy = IFPolicy(D(".12"), D(".0001"), D(".0002"), D(".0001"), D(".0001"))
    policies = {days[0].day: policy, days[2].day: IFPolicy(
        D(".15"), D(".0001"), D(".0002"), D(".0001"), D(".0002"))}
    short = IFOrder(days[0].day, "SELL", "OPEN", 1)
    result = replay_if(contract, days, [short], initial_cash=initial, policy=policies)
    cents = lambda v: v.quantize(D(".01"), rounding=ROUND_HALF_UP)
    cash = initial
    prior = days[0].open
    for i, (day, account) in enumerate(zip(days, result.daily, strict=True)):
        active = policies[days[0].day if i < 2 else days[2].day]
        assert account.policy == active
        delivered = day.day == contract.expiry
        mark = day.delivery if delivered else day.settle
        variation = cents((prior - mark) * 300)
        fees = (cents(day.open * 300 * active.open_fee) if i == 0 else D(0))
        if delivered:
            fees += cents(mark * 300 * active.delivery_fee)
        cash += variation - fees
        assert (account.cash, account.variation, account.fees, account.margin, account.position) == (
            cash, variation, fees, D(0) if delivered else cents(mark * 300 * active.margin),
            0 if delivered else -1)
        prior = mark
        print(day.day, f"variation={variation} fees={fees} cash={cash} margin={account.margin}")
    assert len(result.fills) == 1 and not result.rejections
    assert result.daily[-1].delivered_position == -1

    new_contract, new_days = load_sample(lake, "IF2307.CFX")
    roll_day = days[2].day
    exit_old = IFOrder(roll_day, "BUY", "CLOSE_YESTERDAY", 1)
    enter_new = IFOrder(roll_day, "SELL", "OPEN", 1)
    orders = [(contract.symbol, short), (contract.symbol, exit_old), (new_contract.symbol, enter_new)]
    portfolio = replay_if_portfolio({contract: days, new_contract: new_days}, orders,
                                    initial_cash=initial, policy=policies, trading_days=trading_days)
    old_open, old_exit, new_open = days[0].open, days[2].open, new_days[2].open
    assert [f[0] for f in portfolio.fills] == [0, 1, 2] and not portfolio.rejections
    old_fees = cents(old_open*300*policy.open_fee) + cents(old_exit*300*policies[roll_day].close_yesterday_fee)
    new_fee = cents(new_open*300*policies[roll_day].open_fee)
    for i, row in enumerate(portfolio.daily):
        old_mark = days[i].settle if i < 2 else old_exit
        total_pnl = (old_open-old_mark)*300
        if i >= 2:
            total_pnl += (new_open-new_days[i].settle)*300
        total_fee = cents(old_open*300*policy.open_fee) if i < 2 else old_fees + new_fee
        assert row.cash == initial + total_pnl - total_fee
        assert row.positions == {contract.symbol: -int(i < 2), new_contract.symbol: -int(i >= 2)}
        margin_mark = days[i].settle if i < 2 else new_days[i].settle
        assert row.margin == cents(margin_mark*300*row.policy.margin)
    print("IF2306 -> IF2307", portfolio.daily[-1].cash, dict(portfolio.daily[-1].positions),
          portfolio.daily[-1].margin)
    validate_cross_product(lake, trading_days)
    validate_equity_overlay(root, contract, days, policies)


if __name__ == "__main__":
    main()

"""Custom local Tushare Parquet lake demo: cross-board weekly MA5/MA20 rebalance."""
import argparse
from collections import Counter
from dataclasses import replace
from decimal import Decimal as D
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.models import TargetWeights
from engine import (
    AShareFeeModel,
    DailyMarketRule,
    plan_rebalance,
    run_prepared_backtest,
)
from engine.tushare_lake import load_tushare_lake

DEFAULT_SYMBOLS = [
    "600519.SH",  # SSE main board
    "600036.SH",  # SSE main board
    "000001.SZ",  # SZSE main board
    "002594.SZ",  # SZSE main board
    "688981.SH",  # STAR market (200-share min order, 1-share step)
]
START_DATE = "2024-01-02"
END_DATE = "2024-12-31"


def compute_ma_weights(history, eligible_symbols, max_gross=0.95):
    """Equal-weight symbols whose close and MA5 are above MA20."""
    selected = []
    for symbol in sorted(eligible_symbols):
        bars = history.get(symbol, ())
        if len(bars) < 20:
            continue
        closes = [D(str(b.close)) for b in bars[-20:]]
        ma5 = sum(closes[-5:], D(0)) / 5
        ma20 = sum(closes, D(0)) / 20
        if closes[-1] > ma20 and ma5 > ma20:
            selected.append(symbol)
    if not selected:
        return {}
    weight = round(max_gross / len(selected), 6)
    return {s: weight for s in selected}


def calc_metrics(daily_nav, initial_cash):
    """Compute total return and maximum drawdown from reconciled daily NAV."""
    equities = [D(row["total_equity"]) for row in daily_nav]
    peak = initial_cash
    max_dd = D(0)
    for eq in equities:
        peak = max(peak, eq)
        dd = eq / peak - 1
        if dd < max_dd:
            max_dd = dd
    total_ret = equities[-1] / initial_cash - 1
    return {
        "final_equity": str(equities[-1]),
        "total_return_pct": f"{total_ret * 100:.2f}%",
        "max_drawdown_pct": f"{max_dd * 100:.2f}%",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ["TUSHARE_DB_ROOT"]) if os.environ.get("TUSHARE_DB_ROOT") else None,
        help="Path to local Tushare_DB root directory containing lake/",
    )
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--start", default=START_DATE)
    parser.add_argument("--end", default=END_DATE)
    parser.add_argument("--cash", type=D, default=D("1000000"))
    parser.add_argument("--output", type=Path, default=Path("output/my_tushare_demo.json"))
    args = parser.parse_args()
    if args.root is None:
        parser.error("Provide --root /path/to/Tushare_DB or set TUSHARE_DB_ROOT")

    data = load_tushare_lake(args.root, args.symbols, args.start, args.end)
    prepared = data.prepare()
    days = data.trading_days
    day_index = {d: i for i, d in enumerate(days)}

    fee_model = AShareFeeModel(commission_rate=0.00025, min_commission=5)
    decisions = []

    def portfolio_signal(closed_bars, state):
        day = state.as_of.date()
        idx = day_index[day]
        if idx % 5 != 0 or idx + 1 >= len(days):
            return []

        tradable_universe = {
            inst.symbol
            for inst in data.instruments
            if (inst.symbol, day) in data.rules and inst.symbol in state.valuation_prices
        }
        weights = compute_ma_weights(state.history, tradable_universe)
        target = TargetWeights(
            signal_date=day,
            execution_date=days[idx + 1],
            weights=weights,
            strategy_id="demo-weekly-ma5-ma20",
        )
        symbols = set(weights) | set(state.positions)
        rules = {
            s: data.rules[(s, day)]
            if isinstance(data.rules[(s, day)], DailyMarketRule)
            else DailyMarketRule(**data.rules[(s, day)])
            for s in symbols
        }
        prices = {s: state.valuation_prices[s] for s in symbols}

        plan = plan_rebalance(
            target,
            cash=state.available_cash,
            holdings=state.positions,
            sellable=state.sellable,
            prices=prices,
            rules=rules,
            valuation_at=state.as_of,
            decision_at=state.as_of,
            cash_reserve=D("1000"),
            fee_model=fee_model,
            receivables=state.receivables,
        )
        intents = [
            replace(intent, intent_id=f"{day}:{intent.symbol}:{intent.side}")
            for intent in plan.intents
        ]
        decisions.append({
            "signal_date": str(day),
            "execution_date": str(days[idx + 1]),
            "weights": weights,
            "intent_count": len(intents),
        })
        return intents

    result = run_prepared_backtest(
        prepared,
        portfolio_signal=portfolio_signal,
        initial_cash=args.cash,
        participation_rate=0.05,
        slippage_bps=5,
        fee_model=fee_model,
        strategy_id="demo-weekly-ma5-ma20",
        strategy_params={"rebalance_days": 5, "fast": 5, "slow": 20, "max_gross": 0.95},
        data_snapshot_id=data.snapshot["snapshot_id"],
    )

    assert result.native_cash == result.ledger_cash
    assert all(row["cash_matches"] and row["positions_match"] for row in result.daily_nav)

    summary = calc_metrics(result.daily_nav, args.cash)
    rejection_counts = dict(Counter(r["reason"] for r in result.rejections))
    dividend_events = [r for r in result.account_journal if D(r["amount"]) != 0]

    print("=== Custom Tushare Demo Completed ===")
    print(f"Range: {days[0]} ~ {days[-1]} ({len(days)} sessions) | Bars: {len(data.bars)}")
    print(f"Rebalances: {len(decisions)} | Fills: {len(result.fills)} | Rejections: {len(result.rejections)}")
    if rejection_counts:
        print(f"Rejection reasons: {rejection_counts}")
    print(f"Corporate action cash events: {len(dividend_events)}")
    print(f"Final cash (reconciled): {result.native_cash} | Final equity: {summary['final_equity']}")
    print(f"Total return: {summary['total_return_pct']} | Max drawdown: {summary['max_drawdown_pct']}")
    print(f"Final holdings: { {k: str(v) for k, v in result.positions.items() if D(v) != 0} }")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "summary": summary,
        "decisions": decisions,
        "rejection_counts": rejection_counts,
        "result": result.to_dict(),
    }
    args.output.write_text(json.dumps(payload, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Output saved to: {args.output}")


if __name__ == "__main__":
    main()

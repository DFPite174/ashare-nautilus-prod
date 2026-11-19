"""
Contracts & Adapters SDK for Mainstream Quantitative Frameworks (VectorBT / Qlib / Pandas).
Transforms external signal outputs into deterministic FrozenOrder contracts for high-speed replay.
"""

from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union
import pandas as pd

from contracts.models import FrozenOrder


def _parse_date(val: Any) -> date:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date()
    if isinstance(val, str):
        # Handle 'YYYYMMDD' or 'YYYY-MM-DD'
        s = val.strip().replace('/', '-').replace('.', '-')
        if len(s) == 8 and s.isdigit():
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        return date.fromisoformat(s[:10])
    raise ValueError(f"Unable to parse trade_date: {val}")


def from_dataframe(
    df: pd.DataFrame,
    date_col: str = "trade_date",
    symbol_col: str = "symbol",
    side_col: str = "side",
    qty_col: str = "quantity",
    price_col: Optional[str] = "limit_price",
    client_id_col: Optional[str] = "client_order_id",
) -> List[FrozenOrder]:
    """
    Ingest a tabular DataFrame of trade intents and convert to validated FrozenOrders.
    Handles flexible column naming and standardizes dates and quantities.
    """
    if df.empty:
        return []

    # Map possible fallback column names
    col_map = {col.lower(): col for col in df.columns}
    def resolve_col(preferred: Optional[str], fallbacks: list[str]) -> Optional[str]:
        if preferred and preferred in df.columns:
            return preferred
        if preferred and preferred.lower() in col_map:
            return col_map[preferred.lower()]
        for fb in fallbacks:
            if fb in df.columns:
                return fb
            if fb.lower() in col_map:
                return col_map[fb.lower()]
        return None

    actual_date = resolve_col(date_col, ["date", "datetime", "trade_dt", "time"])
    actual_sym = resolve_col(symbol_col, ["code", "ticker", "instrument", "asset", "sec_code"])
    actual_side = resolve_col(side_col, ["direction", "action", "order_side", "type"])
    actual_qty = resolve_col(qty_col, ["qty", "shares", "vol", "volume", "size"])
    actual_px = resolve_col(price_col, ["price", "px", "limit_px", "target_price"])
    actual_cid = resolve_col(client_id_col, ["order_id", "client_id", "id"])

    if not actual_date or not actual_sym or not actual_side or not actual_qty:
        raise ValueError(f"Missing required columns in DataFrame: date={actual_date}, symbol={actual_sym}, side={actual_side}, qty={actual_qty}")

    orders: List[FrozenOrder] = []
    for idx, row in df.iterrows():
        t_date = _parse_date(row[actual_date])
        sym = str(row[actual_sym]).strip()
        side_val = str(row[actual_side]).strip().upper()
        if side_val in {"BUY", "1", "LONG", "B"}:
            side = "BUY"
        elif side_val in {"SELL", "-1", "SHORT", "S"}:
            side = "SELL"
        else:
            raise ValueError(f"Invalid side value at row {idx}: {row[actual_side]}")

        qty = int(abs(row[actual_qty]))
        if qty <= 0:
            continue

        limit_px = None
        if actual_px and pd.notna(row[actual_px]):
            px_val = Decimal(str(row[actual_px]))
            if px_val > 0:
                limit_px = px_val

        cid = str(row[actual_cid]) if actual_cid and pd.notna(row[actual_cid]) else None

        orders.append(FrozenOrder(
            symbol=sym,
            trade_date=t_date,
            side=side,
            quantity=qty,
            limit_price=limit_px,
            client_order_id=cid,
        ))

    return orders


def from_target_weights_df(
    weights_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    initial_cash: Union[Decimal, float] = 1000000,
    lot_size: int = 100,
    cash_buffer_ratio: float = 0.005,
) -> List[FrozenOrder]:
    """
    Construct executable FrozenOrders from a matrix of target portfolio weights (date x symbol).
    - Executes rebalancing sequentially per trade day.
    - Sells overweight assets first to release liquidity.
    - Rounds buys down to lot_size (100 shares for A-shares).
    - Reserves a conservative cash buffer (default 0.5%) for transaction frictions and overnight gap.
    """
    initial_cash_dec = Decimal(str(initial_cash))
    cash = initial_cash_dec
    holdings: Dict[str, int] = {col: 0 for col in weights_df.columns}
    orders: List[FrozenOrder] = []

    # Sort index by date
    dates = sorted(weights_df.index)

    for dt in dates:
        t_date = _parse_date(dt)
        row_w = weights_df.loc[dt]
        if dt not in prices_df.index:
            continue
        row_p = prices_df.loc[dt]

        # Calculate current portfolio total valuation
        total_val = cash + sum(Decimal(holdings[s]) * Decimal(str(row_p[s])) for s in holdings if s in row_p and pd.notna(row_p[s]))
        available_budget = total_val * Decimal(str(1.0 - cash_buffer_ratio))

        sell_orders: List[FrozenOrder] = []
        buy_orders: List[FrozenOrder] = []

        for sym, weight in row_w.items():
            if pd.isna(weight) or sym not in row_p or pd.isna(row_p[sym]) or row_p[sym] <= 0:
                continue
            px = Decimal(str(row_p[sym]))
            target_val = available_budget * Decimal(str(weight))
            target_shares = int(target_val / px) // lot_size * lot_size
            curr_shares = holdings.get(sym, 0)
            diff = target_shares - curr_shares

            if diff < 0:
                sell_qty = abs(diff)
                sell_orders.append(FrozenOrder(
                    symbol=sym,
                    trade_date=t_date,
                    side="SELL",
                    quantity=sell_qty,
                    limit_price=None,
                ))
                cash += Decimal(sell_qty) * px
                holdings[sym] -= sell_qty
            elif diff > 0:
                buy_orders.append(FrozenOrder(
                    symbol=sym,
                    trade_date=t_date,
                    side="BUY",
                    quantity=diff,
                    limit_price=None,
                ))

        # Check cash feasibility for buys and down-trim if needed
        for bo in buy_orders:
            px = Decimal(str(row_p[bo.symbol]))
            cost = Decimal(bo.quantity) * px
            if cost > cash:
                affordable = int(cash / px) // lot_size * lot_size
                if affordable >= lot_size:
                    bo = FrozenOrder(
                        symbol=bo.symbol,
                        trade_date=bo.trade_date,
                        side="BUY",
                        quantity=affordable,
                        limit_price=None,
                    )
                    cash -= Decimal(affordable) * px
                    holdings[bo.symbol] = holdings.get(bo.symbol, 0) + affordable
                    orders.append(bo)
            else:
                cash -= cost
                holdings[bo.symbol] = holdings.get(bo.symbol, 0) + bo.quantity
                orders.append(bo)

        orders.extend(sell_orders)

    # Sort orders: SELLs first on each date, then by symbol
    orders.sort(key=lambda o: (o.trade_date, 0 if o.side == "SELL" else 1, o.symbol))
    return orders


def from_vectorbt_portfolio(vbt_pf: Any) -> List[FrozenOrder]:
    """
    Ingest orders from a VectorBT Portfolio object.
    Supports `vbt_pf.orders.records_readable` or standard VectorBT dataframe dumps.
    """
    if hasattr(vbt_pf, "orders") and hasattr(vbt_pf.orders, "records_readable"):
        df = vbt_pf.orders.records_readable
    elif isinstance(vbt_pf, pd.DataFrame):
        df = vbt_pf
    else:
        raise TypeError("Expected VectorBT Portfolio with .orders.records_readable or DataFrame")

    return from_dataframe(
        df,
        date_col="Date",
        symbol_col="Column",
        side_col="Side",
        qty_col="Size",
        price_col="Price",
    )


def from_qlib_predictions(
    pred_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    top_k: int = 5,
    initial_cash: Union[Decimal, float] = 1000000,
    lot_size: int = 100,
) -> List[FrozenOrder]:
    """
    Convert Qlib model prediction scores (DataFrame with MultiIndex: [datetime, instrument] or columns)
    into equal-weighted top-k rebalancing FrozenOrders.
    """
    # Normalize index
    df = pred_df.copy()
    if isinstance(df.index, pd.MultiIndex):
        dt_level = 0 if "date" in str(df.index.names[0]).lower() or "time" in str(df.index.names[0]).lower() else 1
        sym_level = 1 - dt_level
        score_col = df.columns[0]
        unstacked = df[score_col].unstack(level=sym_level)
    else:
        unstacked = df

    # Rank and assign 1/top_k weight to top_k stocks
    weights = pd.DataFrame(0.0, index=unstacked.index, columns=unstacked.columns)
    weight_each = 1.0 / top_k
    for dt, row in unstacked.iterrows():
        valid_row = row.dropna()
        if len(valid_row) > 0:
            top_stocks = valid_row.nlargest(top_k).index
            weights.loc[dt, top_stocks] = weight_each

    return from_target_weights_df(weights, prices_df, initial_cash=initial_cash, lot_size=lot_size)

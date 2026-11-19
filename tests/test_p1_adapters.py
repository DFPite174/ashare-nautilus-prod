"""
Unit tests for P1: Mainstream Framework Ingest Adapters (VectorBT / Qlib / DataFrame)
"""
from datetime import date
from decimal import Decimal as D
import pandas as pd
import pytest

from contracts.adapters import (
    from_dataframe,
    from_target_weights_df,
    from_vectorbt_portfolio,
    from_qlib_predictions,
)
from contracts.models import FrozenOrder


def test_from_dataframe_ingest():
    df = pd.DataFrame([
        {"trade_date": "2023-01-05", "symbol": "600519.SH", "side": "BUY", "quantity": 100, "limit_price": 100.5},
        {"trade_date": "20230106", "symbol": "000001.SZ", "side": "SELL", "quantity": 200, "limit_price": None},
        {"trade_date": "2023/01/09", "symbol": "600519.SH", "side": "BUY", "quantity": 300},
    ])
    orders = from_dataframe(df)
    assert len(orders) == 3
    assert orders[0].trade_date == date(2023, 1, 5)
    assert orders[0].symbol == "600519.SH"
    assert orders[0].side == "BUY"
    assert orders[0].quantity == 100
    assert orders[0].limit_price == D("100.5")

    assert orders[1].trade_date == date(2023, 1, 6)
    assert orders[1].symbol == "000001.SZ"
    assert orders[1].side == "SELL"
    assert orders[1].quantity == 200
    assert orders[1].limit_price is None

    assert orders[2].trade_date == date(2023, 1, 9)
    assert orders[2].quantity == 300


def test_from_target_weights_df():
    dates = pd.to_datetime(["2023-01-05", "2023-01-06"])
    weights = pd.DataFrame(
        [[0.5, 0.5], [0.8, 0.2]],
        index=dates,
        columns=["600519.SH", "000001.SZ"]
    )
    prices = pd.DataFrame(
        [[100.0, 10.0], [105.0, 10.5]],
        index=dates,
        columns=["600519.SH", "000001.SZ"]
    )

    orders = from_target_weights_df(weights, prices, initial_cash=100000)
    assert len(orders) > 0
    # All quantities should be multiples of 100
    assert all(o.quantity % 100 == 0 for o in orders)
    # Check that trade_dates are converted to date objects
    assert all(isinstance(o.trade_date, date) for o in orders)


def test_from_qlib_predictions():
    # MultiIndex [datetime, instrument]
    idx = pd.MultiIndex.from_tuples([
        (pd.Timestamp("2023-01-05"), "600519.SH"),
        (pd.Timestamp("2023-01-05"), "000001.SZ"),
        (pd.Timestamp("2023-01-05"), "000002.SZ"),
    ], names=["datetime", "instrument"])
    pred_df = pd.DataFrame({"score": [0.85, 0.95, 0.40]}, index=idx)

    prices = pd.DataFrame(
        [[100.0, 10.0, 20.0]],
        index=pd.to_datetime(["2023-01-05"]),
        columns=["600519.SH", "000001.SZ", "000002.SZ"]
    )

    orders = from_qlib_predictions(pred_df, prices, top_k=2, initial_cash=100000)
    assert len(orders) == 2
    # The top 2 scores are 000001.SZ (0.95) and 600519.SH (0.85)
    symbols = {o.symbol for o in orders}
    assert symbols == {"000001.SZ", "600519.SH"}

from datetime import date, datetime, timedelta
from decimal import Decimal
import pytest
from zoneinfo import ZoneInfo

from contracts import Instrument, Venue, AssetClass, FrozenOrder
from engine.backtest import BacktestBar, OrderIntent, run_backtest, run_frozen_orders_backtest

TZ = ZoneInfo("Asia/Shanghai")
D = Decimal


def _create_market_data(days=4, symbol="000001"):
    inst = Instrument(
        symbol=symbol,
        exchange=Venue.SZSE,
        asset_class=AssetClass.EQUITY,
        currency="CNY",
        lot_size=100,
        tick_size=D("0.01"),
    )
    start_date = date(2023, 6, 1)
    current = start_date
    trade_days = []
    while len(trade_days) < days:
        if current.weekday() < 5:
            trade_days.append(current)
        current += timedelta(days=1)

    bars = []
    rules = {}
    prev_px = D("10.00")
    for i, d in enumerate(trade_days):
        open_px = prev_px + D("0.10") * i
        high_px = open_px + D("0.20")
        low_px = open_px - D("0.10")
        close_px = open_px + D("0.05")

        b = BacktestBar(
            symbol=symbol,
            start=datetime.combine(d, datetime.min.time(), TZ).replace(hour=9, minute=30),
            end=datetime.combine(d, datetime.min.time(), TZ).replace(hour=15, minute=0),
            open=open_px,
            high=high_px,
            low=low_px,
            close=close_px,
            volume=50000,
        )
        bars.append(b)
        rules[(symbol, d)] = {
            "trade_status": "OPEN",
            "limit_state": "BOUNDED",
            "down_limit": float(prev_px * D("0.9")),
            "up_limit": float(prev_px * D("1.1")),
            "buy_min": 100,
            "buy_step": 100,
            "sell_min": 1,
            "sell_step": 1,
            "max_order": 1000000,
            "prev_close": float(prev_px),
        }
        prev_px = close_px

    return [inst], bars, rules, trade_days


def test_frozen_orders_parity_with_dynamic_backtest():
    """
    P2 高性能预固化订单回放验证：
    使用相同标的与交易行为，验证 run_frozen_orders_backtest 与原生 run_backtest 结果 100% 账务对齐。
    - Day 1: 策略发出买入信号 -> Day 2 开盘撮合成交 200 股
    - Day 2: 持仓观望
    - Day 3: 策略发出卖出信号 -> Day 4 开盘撮合成交 200 股
    """
    instruments, bars, rules, trade_days = _create_market_data(days=4)
    initial_cash = D("100000")

    # 1. 动态信号回测 run_backtest
    def dynamic_signal(bar, state):
        cur_date = bar.end.date()
        if cur_date == trade_days[0]:
            return [OrderIntent(symbol="000001", side="BUY", quantity=200)]
        elif cur_date == trade_days[2]:
            return [OrderIntent(symbol="000001", side="SELL", quantity=200)]
        return []

    res_dynamic = run_backtest(
        instruments=instruments,
        bars=bars,
        rules=rules,
        signal=dynamic_signal,
        initial_cash=initial_cash,
    )

    # 2. 静态预固化订单回测 run_frozen_orders_backtest
    # 拟在 Day 2 (trade_days[1]) 开盘买入，在 Day 4 (trade_days[3]) 开盘卖出
    frozen_orders = [
        FrozenOrder(symbol="000001", trade_date=trade_days[1], side="BUY", quantity=200),
        FrozenOrder(symbol="000001", trade_date=trade_days[3], side="SELL", quantity=200),
    ]

    res_frozen = run_frozen_orders_backtest(
        instruments=instruments,
        bars=bars,
        rules=rules,
        frozen_orders=frozen_orders,
        initial_cash=initial_cash,
    )

    # 3. 严格核对账务与成交对等性
    assert res_frozen.native_cash == res_dynamic.native_cash
    assert res_frozen.ledger_cash == res_dynamic.ledger_cash
    assert res_frozen.positions == res_dynamic.positions
    assert len(res_frozen.fills) == len(res_dynamic.fills) == 2

    for f_dyn, f_frz in zip(res_dynamic.fills, res_frozen.fills):
        assert f_dyn["symbol"] == f_frz["symbol"]
        assert f_dyn["side"] == f_frz["side"]
        assert f_dyn["quantity"] == f_frz["quantity"]
        assert f_dyn["price"] == f_frz["price"]
        assert f_dyn["fee"] == f_frz["fee"]

    # 4. 每日净值矩阵逐日精确一致
    assert len(res_frozen.daily_nav) == len(res_dynamic.daily_nav) == 4
    for nav_dyn, nav_frz in zip(res_dynamic.daily_nav, res_frozen.daily_nav):
        assert nav_dyn["date"] == nav_frz["date"]
        assert nav_dyn["total_equity"] == nav_frz["total_equity"]
        assert nav_dyn["cash"] == nav_frz["cash"]
        assert nav_dyn["cash_matches"] is True
        assert nav_frz["cash_matches"] is True


def test_frozen_orders_suspended_day_rejection():
    """
    P2 预固化订单异常行情校验：
    若订单指定在某日执行，但该日股票停牌 (SUSPENDED)，
    订单必须被正确拒绝并记录在 rejections 中，绝不可静默撮合。
    """
    instruments, bars, rules, trade_days = _create_market_data(days=3)
    target_day = trade_days[1]
    rules[("000001", target_day)]["trade_status"] = "SUSPENDED"

    frozen_orders = [
        FrozenOrder(symbol="000001", trade_date=target_day, side="BUY", quantity=100),
    ]

    res = run_frozen_orders_backtest(
        instruments=instruments,
        bars=bars,
        rules=rules,
        frozen_orders=frozen_orders,
        initial_cash=D("100000"),
    )

    assert len(res.fills) == 0
    assert len(res.rejections) == 1
    assert res.rejections[0]["symbol"] == "000001"
    assert res.rejections[0]["reason"] == "SUSPENDED"


def test_frozen_orders_same_day_rebalance_sell_before_buy():
    """
    P2 预固化订单同日调仓现金释放验证：
    - Day 1: 市场初始运行，提供历史成交量基准
    - Day 2: 买入 100 股 000001 (占用约 1005 元现金，剩余约 1495 元)
    - Day 3: 调仓：卖出 100 股 000001 (回流约 994 元)，同时买入 100 股 000002 (需 2005 元)。
      剩余现金 1495 元原本不足以支付 000002，但通过同日卖出优先撮合回流现金至 2489 元，买入成功执行！
    """
    inst1 = Instrument(symbol="000001", exchange=Venue.SZSE, asset_class=AssetClass.EQUITY, currency="CNY", lot_size=100, tick_size=D("0.01"))
    inst2 = Instrument(symbol="000002", exchange=Venue.SZSE, asset_class=AssetClass.EQUITY, currency="CNY", lot_size=100, tick_size=D("0.01"))
    d1 = date(2023, 6, 1)
    d2 = date(2023, 6, 2)
    d3 = date(2023, 6, 5)

    bars = [
        BacktestBar("000001", datetime(2023, 6, 1, 9, 30, tzinfo=TZ), datetime(2023, 6, 1, 15, 0, tzinfo=TZ), D("10.00"), D("10.00"), D("10.00"), D("10.00"), 50000),
        BacktestBar("000002", datetime(2023, 6, 1, 9, 30, tzinfo=TZ), datetime(2023, 6, 1, 15, 0, tzinfo=TZ), D("20.00"), D("20.00"), D("20.00"), D("20.00"), 50000),
        BacktestBar("000001", datetime(2023, 6, 2, 9, 30, tzinfo=TZ), datetime(2023, 6, 2, 15, 0, tzinfo=TZ), D("10.00"), D("10.00"), D("10.00"), D("10.00"), 50000),
        BacktestBar("000002", datetime(2023, 6, 2, 9, 30, tzinfo=TZ), datetime(2023, 6, 2, 15, 0, tzinfo=TZ), D("20.00"), D("20.00"), D("20.00"), D("20.00"), 50000),
        BacktestBar("000001", datetime(2023, 6, 5, 9, 30, tzinfo=TZ), datetime(2023, 6, 5, 15, 0, tzinfo=TZ), D("10.00"), D("10.00"), D("10.00"), D("10.00"), 50000),
        BacktestBar("000002", datetime(2023, 6, 5, 9, 30, tzinfo=TZ), datetime(2023, 6, 5, 15, 0, tzinfo=TZ), D("20.00"), D("20.00"), D("20.00"), D("20.00"), 50000),
    ]
    rules = {
        ("000001", d1): {"trade_status": "OPEN", "limit_state": "BOUNDED", "down_limit": 9.0, "up_limit": 11.0, "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000, "prev_close": 10.0},
        ("000002", d1): {"trade_status": "OPEN", "limit_state": "BOUNDED", "down_limit": 18.0, "up_limit": 22.0, "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000, "prev_close": 20.0},
        ("000001", d2): {"trade_status": "OPEN", "limit_state": "BOUNDED", "down_limit": 9.0, "up_limit": 11.0, "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000, "prev_close": 10.0},
        ("000002", d2): {"trade_status": "OPEN", "limit_state": "BOUNDED", "down_limit": 18.0, "up_limit": 22.0, "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000, "prev_close": 20.0},
        ("000001", d3): {"trade_status": "OPEN", "limit_state": "BOUNDED", "down_limit": 9.0, "up_limit": 11.0, "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000, "prev_close": 10.0},
        ("000002", d3): {"trade_status": "OPEN", "limit_state": "BOUNDED", "down_limit": 18.0, "up_limit": 22.0, "buy_min": 100, "buy_step": 100, "sell_min": 1, "sell_step": 1, "max_order": 1000000, "prev_close": 20.0},
    }

    frozen_orders = [
        FrozenOrder(symbol="000001", trade_date=d2, side="BUY", quantity=100),
        # 故意将 BUY 写在前面，SELL 写在后面，检验内部是否自动优化调仓顺序
        FrozenOrder(symbol="000002", trade_date=d3, side="BUY", quantity=100),
        FrozenOrder(symbol="000001", trade_date=d3, side="SELL", quantity=100),
    ]

    res = run_frozen_orders_backtest(
        instruments=[inst1, inst2],
        bars=bars,
        rules=rules,
        frozen_orders=frozen_orders,
        initial_cash=D("2500"),
    )

    # 应该有 3 笔成交：Day 2 买 000001，Day 3 卖 000001，Day 3 买 000002
    assert len(res.fills) == 3
    assert res.fills[0]["symbol"] == "000001" and res.fills[0]["side"] == "BUY"
    assert res.fills[1]["symbol"] == "000001" and res.fills[1]["side"] == "SELL"
    assert res.fills[2]["symbol"] == "000002" and res.fills[2]["side"] == "BUY"
    assert res.positions["000001"] == D(0)
    assert res.positions["000002"] == D(100)

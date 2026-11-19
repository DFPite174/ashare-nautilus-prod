from datetime import date, datetime
from decimal import Decimal
import pytest
from zoneinfo import ZoneInfo

from nautilus_trader.model.currencies import CNY
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.objects import Price, Quantity
from contracts import Instrument, Venue, AssetClass, CorporateAction
from engine.backtest import BacktestBar, run_backtest
from engine.fee_model import AShareFeeModel, calculate_expected_fee
from tests.test_fee_model import create_dummy_order

TZ = ZoneInfo("Asia/Shanghai")
D = Decimal


def test_custom_stamp_duty_not_hijacked_before_2023():
    """
    P1 费率覆盖验证：
    显式传入 stamp_duty_rate=0.0005 时，在 2023-08-28 之前的历史交易日绝不能被劫持为 0.0010。
    """
    # 显式指定 0.0005
    model_custom = AShareFeeModel(commission_rate=0.0003, min_commission=5.0, stamp_duty_rate=0.0005)
    model_custom.set_trade_date("2021-05-10")

    order = create_dummy_order(OrderSide.SELL)
    # 1000 股 @ 100 元 = 100,000 元
    # 佣金 = 30 元
    # 印花税 = 100000 * 0.0005 = 50 元 (若被劫持则为 100 元)
    # 过户费 = 100000 * 0.00002 = 2 元 (2021年过户费为十万分之二)
    # 总费用 = 30 + 50 + 2 = 82 元
    fee = model_custom.get_fee(order, Price.from_str("100.0"), Quantity.from_int(1000))
    assert fee.as_double() == 82.00, f"Expected 82.00 but got {fee.as_double()}"

    # 默认不传 stamp_duty_rate：自动使用历史法定税率 (2021年为 0.0010)
    model_default = AShareFeeModel(commission_rate=0.0003, min_commission=5.0)
    model_default.set_trade_date("2021-05-10")
    # 总费用 = 30 + 100 + 2 = 132 元
    fee_default = model_default.get_fee(order, Price.from_str("100.0"), Quantity.from_int(1000))
    assert fee_default.as_double() == 132.00, f"Expected 132.00 but got {fee_default.as_double()}"


def test_custom_transfer_fee_not_hijacked_before_2022():
    """
    P1 费率覆盖验证：
    显式传入 transfer_fee_rate=0.00001 时，在 2022-04-29 之前的历史交易日绝不能被劫持为 0.00002。
    """
    model_custom = AShareFeeModel(commission_rate=0.0003, min_commission=5.0, stamp_duty_rate=0.0005, transfer_fee_rate=0.00001)
    model_custom.set_trade_date("2021-05-10")

    order = create_dummy_order(OrderSide.SELL)
    # 100,000 元卖出: 佣金 30 + 印花税 50 + 自定义过户费 1 = 81 元
    fee = model_custom.get_fee(order, Price.from_str("100.0"), Quantity.from_int(1000))
    assert fee.as_double() == 81.00


def _build_test_env(ex_rights_without_action=False, ex_rights_rule_mismatch=False, normal_drop=False):
    inst = Instrument(
        symbol="000001",
        exchange=Venue.SZSE,
        asset_class=AssetClass.EQUITY,
        currency="CNY",
        lot_size=100,
        tick_size=D("0.01"),
    )
    d1 = date(2023, 5, 4)
    d2 = date(2023, 5, 5)

    # Day 1: close at 100.00
    b1 = BacktestBar(
        symbol="000001",
        start=datetime(2023, 5, 4, 9, 30, tzinfo=TZ),
        end=datetime(2023, 5, 4, 15, 0, tzinfo=TZ),
        open=D("100.00"),
        high=D("102.00"),
        low=D("99.00"),
        close=D("100.00"),
        volume=100000,
    )

    # Day 2:
    if ex_rights_without_action:
        # 10送10除权，价格腰斩至 50 元
        o2, h2, l2, c2 = D("50.00"), D("51.00"), D("49.00"), D("50.00")
        rule_down, rule_up = 45.0, 55.0
        rule_prev = 50.0
    elif ex_rights_rule_mismatch:
        # 价格正常跌停 10%，但规则除权参考价被改为了 50.00
        o2, h2, l2, c2 = D("90.00"), D("91.00"), D("90.00"), D("90.00")
        rule_down, rule_up = 89.0, 100.0
        rule_prev = 50.0
    elif normal_drop:
        # 创业板或正常大跌 20%
        o2, h2, l2, c2 = D("80.00"), D("82.00"), D("79.00"), D("80.00")
        rule_down, rule_up = 78.0, 110.0
        rule_prev = 100.0
    else:
        # 正常微跌
        o2, h2, l2, c2 = D("99.00"), D("101.00"), D("98.00"), D("99.00")
        rule_down, rule_up = 90.0, 110.0
        rule_prev = 100.0

    b2 = BacktestBar(
        symbol="000001",
        start=datetime(2023, 5, 5, 9, 30, tzinfo=TZ),
        end=datetime(2023, 5, 5, 15, 0, tzinfo=TZ),
        open=o2,
        high=h2,
        low=l2,
        close=c2,
        volume=100000,
    )

    rules = {
        ("000001", d1): {
            "trade_status": "OPEN",
            "limit_state": "BOUNDED",
            "down_limit": 90.0,
            "up_limit": 110.0,
            "buy_min": 100,
            "buy_step": 100,
            "sell_min": 1,
            "sell_step": 1,
            "max_order": 1000000,
            "prev_close": 100.0,
        },
        ("000001", d2): {
            "trade_status": "OPEN",
            "limit_state": "BOUNDED",
            "down_limit": rule_down,
            "up_limit": rule_up,
            "buy_min": 100,
            "buy_step": 100,
            "sell_min": 1,
            "sell_step": 1,
            "max_order": 1000000,
            "prev_close": rule_prev,
        },
    }
    return [inst], [b1, b2], rules


def test_anti_illusion_gate_detects_missing_corporate_action_on_price_drop():
    """
    P1 虚假确定性防御门禁验证：
    未复权行情单日暴跌 50%（除权腰斩），但 actions 未传除权记录，
    必须抛出 CRITICAL_DATA_DEFECT 明确拦截，杜绝虚假对账通过。
    """
    instruments, bars, rules = _build_test_env(ex_rights_without_action=True)

    with pytest.raises(ValueError, match="CRITICAL_DATA_DEFECT.*exhibits ex-rights price gap"):
        run_backtest(
            instruments=instruments,
            bars=bars,
            rules=rules,
            actions=[],  # 故意漏传 corporate action
            signal=lambda b, s: [],
            initial_cash=1000000,
        )


def test_anti_illusion_gate_detects_rule_prev_close_mismatch():
    """
    P1 虚假确定性防御门禁验证：
    行情价格未腰斩，但交易所日规则中 prev_close 出现除权跳空（50 vs 100），且无 actions，
    必须抛出 CRITICAL_DATA_DEFECT 明确拦截。
    """
    instruments, bars, rules = _build_test_env(ex_rights_rule_mismatch=True)

    with pytest.raises(ValueError, match="CRITICAL_DATA_DEFECT.*exhibits ex-rights price gap"):
        run_backtest(
            instruments=instruments,
            bars=bars,
            rules=rules,
            actions=[],
            signal=lambda b, s: [],
            initial_cash=1000000,
        )


def test_anti_illusion_gate_allows_valid_corporate_action():
    """
    P1 虚假确定性防御门禁验证：
    行情腰斩 50%，但正确配置了对应的除权送股 CorporateAction，
    门禁正常放行，回测正常执行。
    """
    instruments, bars, rules = _build_test_env(ex_rights_without_action=True)
    action = CorporateAction(
        symbol="000001",
        ex_date=date(2023, 5, 5),
        bonus_ratio=D("1.0"),  # 10 送 10
        reserve_ratio=D("0.0"),
        cash_div=D("0.0"),
        record_date=date(2023, 5, 4),
    )

    result = run_backtest(
        instruments=instruments,
        bars=bars,
        rules=rules,
        actions=[action],
        signal=lambda b, s: [],
        initial_cash=1000000,
    )
    assert result.native_cash == result.ledger_cash
    assert len(result.daily_nav) == 2


def test_anti_illusion_gate_allows_normal_market_drop():
    """
    P1 虚假确定性防御门禁验证：
    正常市场波动（例如 -20% 跌幅），不应被误判为除权缺口。
    """
    instruments, bars, rules = _build_test_env(normal_drop=True)

    result = run_backtest(
        instruments=instruments,
        bars=bars,
        rules=rules,
        actions=[],
        signal=lambda b, s: [],
        initial_cash=1000000,
    )
    assert result.native_cash == result.ledger_cash
    assert len(result.daily_nav) == 2

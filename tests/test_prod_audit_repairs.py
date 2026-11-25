"""Regression tests for P0 execution/liquidity fixes and P1 Tushare lake usability improvements."""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal as D
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from contracts import AssetClass, CorporateAction, FrozenOrder, Instrument, Venue
from engine import (
    OrderIntent,
    prepare_backtest,
    run_backtest,
    run_frozen_orders_backtest,
    run_prepared_backtest,
)
from engine.liquidity import BarLiquidity
from engine.order_gateway import AShareOrderGateway
from engine.reproducibility import digest
from engine.tushare_lake import load_tushare_lake
from test_regular_backtest import fixture


INST_SH = Instrument(symbol='600000.SH', exchange=Venue.SSE)


def _market(days=3):
    bars, rules = fixture(days=days, symbol=INST_SH.symbol)
    bars = [replace(b, open=D('10.00'), high=D('10.60'), low=D('9.80'), close=D('10.00')) for b in bars]
    for r in rules.values():
        r.update(up_limit=11.0, down_limit=9.0, prev_close=10.0)
    return bars, rules


def test_p0_1_intrabar_limit_budget_not_stuck_at_open_cap_when_open_unfilled():
    bars, rules = _market(days=3)
    # Open is 10.20 (above limit 9.90, so 0 shares fill at open), open_cap=100, day_cap=1000.
    bars[1] = replace(
        bars[1],
        open=D('10.20'),
        high=D('10.30'),
        low=D('9.80'),
        close=D('10.00'),
        volume=1000,
        open_volume=100,
    )

    def signal(b, s):
        return [OrderIntent(b.symbol, 'BUY', 200, D('9.90'))] if b == bars[0] else []

    r = run_backtest([INST_SH], bars, rules, signal, participation_rate=1)
    assert [(D(f['quantity']), D(f['price'])) for f in r.fills] == [(D(200), D('9.90'))]
    assert all(x['cash_matches'] and x['positions_match'] for x in r.daily_nav)


def test_p0_2_slippage_clamped_to_daily_price_band_at_limit_open():
    liq = BarLiquidity(slippage_bps=10)
    from engine.backtest import at, nanos
    from nautilus_trader.model.currencies import CNY
    from nautilus_trader.model.enums import OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue as NTVenue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Price, Quantity

    nt_inst = Equity(
        instrument_id=InstrumentId(Symbol('600000.SH'), NTVenue('SIM')),
        raw_symbol=Symbol('600000'),
        currency=CNY,
        price_precision=2,
        price_increment=Price.from_str('0.01'),
        lot_size=Quantity.from_int(100),
        ts_event=nanos(at(date(2025, 1, 2), 0)),
        ts_init=nanos(at(date(2025, 1, 2), 0)),
    )
    assert str(liq.execution_price(Price.from_str('11.00'), OrderSide.BUY, nt_inst, up_limit=11.0)) == '11.00'
    assert str(liq.execution_price(Price.from_str('9.00'), OrderSide.SELL, nt_inst, down_limit=9.0)) == '9.00'

    bars, rules = _market(days=4)
    bars[1] = replace(bars[1], open=D('10.99'), high=D('11.00'), low=D('10.90'), close=D('11.00'))
    bars[2] = replace(bars[2], open=D('9.91'), high=D('10.00'), low=D('9.90'), close=D('9.95'))
    rules[(INST_SH.symbol, bars[2].end.date())].update(down_limit=9.90, up_limit=12.10, prev_close=11.0)
    rules[(INST_SH.symbol, bars[3].end.date())].update(prev_close=9.95)

    def signal(b, s):
        if b == bars[0]:
            return [OrderIntent(b.symbol, 'BUY', 100)]
        if b == bars[1]:
            return [OrderIntent(b.symbol, 'SELL', 100)]
        return []

    r = run_backtest([INST_SH], bars, rules, signal, participation_rate=1, slippage_bps=20)
    assert len(r.fills) == 2
    assert r.rejections == []
    assert D(r.fills[0]['price']) == D('11.00')
    assert D(r.fills[1]['price']) == D('9.90')


def test_p0_3_proxy_open_capacity_capped_by_bar_total_volume():
    bars, rules = _market(days=3)
    # Day 1 volume is 100,000 -> 1% proxy open cap = 1,000, but Day 2 shrinks to 100 shares total.
    bars[0] = replace(bars[0], volume=100_000)
    bars[1] = replace(bars[1], volume=100, open_volume=None)

    def signal(b, s):
        return [OrderIntent(b.symbol, 'BUY', 200)] if b == bars[0] else []

    r = run_backtest([INST_SH], bars, rules, signal, participation_rate=0.01)
    assert sum(D(f['quantity']) for f in r.fills) <= 100
    assert all(x['cash_matches'] and x['positions_match'] for x in r.daily_nav)


def test_p0_4_preopen_limit_order_up_to_limit_band_not_blocked_by_continuous_collar():
    bars, rules = _market(days=3)
    # Open=10.00, up_limit=11.00. Overnight limit order capped at up_limit (11.00) must not fail 2% collar.
    def signal(b, s):
        return [OrderIntent(b.symbol, 'BUY', 100, D('11.00'))] if b == bars[0] else []

    r = run_backtest([INST_SH], bars, rules, signal, participation_rate=1)
    assert len(r.fills) == 1
    assert D(r.fills[0]['price']) == D('10.00')
    assert r.rejections == []

    # Exceeding up_limit must still be blocked by PRICE_OUTSIDE_BAND.
    def bad_signal(b, s):
        return [OrderIntent(b.symbol, 'BUY', 100, D('11.50'))] if b == bars[0] else []

    r_bad = run_backtest([INST_SH], bars, rules, bad_signal, participation_rate=1)
    assert r_bad.fills == []
    assert r_bad.rejections and 'PRICE_OUTSIDE_BAND' in r_bad.rejections[0]['reason']


@pytest.mark.parametrize('data_mode', ['orders', 'full'])
def test_p0_5_run_frozen_orders_backtest_preserves_unresolved_entitlements(data_mode):
    bars, rules = _market(days=3)
    bars[0] = replace(bars[0], open_volume=1000)
    d_record = bars[1].end.date()
    d_buy = bars[0].end.date()
    frozen_orders = [FrozenOrder(symbol=INST_SH.symbol, trade_date=d_buy, side='BUY', quantity=100)]
    events = [{'symbol': INST_SH.symbol, 'record_date': d_record.strftime('%Y%m%d'), 'kind': 'unresolved_test'}]

    with pytest.raises(ValueError, match=r'Unsupported entitlement: 600000\.SH'):
        run_frozen_orders_backtest(
            [INST_SH],
            bars,
            rules,
            frozen_orders,
            data_mode=data_mode,
            participation_rate=1,
            unresolved_entitlements=events,
        )


def test_p1_participation_policy_clip_respects_lot_step_and_conservation():
    bars, rules = _market(days=3)
    bars[0] = replace(bars[0], volume=25_600)
    bars[1] = replace(bars[1], volume=50_000, open_volume=None)

    def signal(b, s):
        return [OrderIntent(b.symbol, 'BUY', 500)] if b == bars[0] else []

    r_reject = run_backtest([INST_SH], bars, rules, signal, participation_rate=0.01, participation_policy='reject')
    assert r_reject.fills == []
    assert [x['reason'] for x in r_reject.rejections] == ['PARTICIPATION_LIMIT']

    r_clip = run_backtest([INST_SH], bars, rules, signal, participation_rate=0.01, participation_policy='clip')
    assert [(D(f['quantity']), D(f['price'])) for f in r_clip.fills] == [(D(200), D('10.00'))]
    assert r_clip.unfilled_capital_amount == D('3000.00')
    assert len(r_clip.trimmed_intents) == 1
    assert r_clip.trimmed_intents[0].trimmed_quantity == 300
    assert all(x['cash_matches'] and x['positions_match'] for x in r_clip.daily_nav)


def _write_pq(root, rel, rows):
    target = root / 'lake' / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), target)


def test_p1_star_market_detection_by_market_field_fixes_689009_and_sell_error_msg(tmp_path):
    _write_pq(
        tmp_path,
        'meta/stock_basic.parquet',
        [
            dict(ts_code='689009.SH', list_date='20201029', market='科创板', name='九号公司'),
            dict(ts_code='600000.SH', list_date='19991110', market='主板', name='浦发银行'),
        ],
    )
    _write_pq(
        tmp_path,
        'meta/trade_cal.parquet',
        [dict(exchange='SSE', cal_date=f'2025050{n}', is_open=1) for n in (6, 7)],
    )
    daily_rows = []
    limit_rows = []
    for sym in ('689009.SH', '600000.SH'):
        for n in (6, 7):
            daily_rows.append(dict(ts_code=sym, trade_date=f'2025050{n}', open=50.0, high=51.0, low=49.0, close=50.0, vol=100.0, volume_shares=10000))
            limit_rows.append(dict(ts_code=sym, trade_date=f'2025050{n}', up_limit=60.0 if sym == '689009.SH' else 55.0, down_limit=40.0 if sym == '689009.SH' else 45.0))
    _write_pq(tmp_path, 'market/daily/year=2025/daily_2025.parquet', daily_rows)
    _write_pq(tmp_path, 'market/stk_limit/year=2025/stk_limit_2025.parquet', limit_rows)
    _write_pq(tmp_path, 'market/suspend_d/year=2025/suspend_d_2025.parquet', [dict(ts_code='000001.SZ', trade_date='20250506', suspend_type='S', suspend_timing=None)])
    _write_pq(
        tmp_path,
        'actions/dividend.parquet',
        [dict(ts_code='600000.SH', div_proc='实施', ann_date='20250430', imp_ann_date='20250501', record_date='20250506', ex_date='20250507', pay_date='20250507', div_listdate=None, cash_div_tax=0.1, stk_div=0.0, stk_bo_rate=0.0, stk_co_rate=0.0)],
    )

    data = load_tushare_lake(tmp_path, ['689009.SH', '600000.SH'], '2025-05-06', '2025-05-07')
    inst_map = {i.symbol: i for i in data.instruments}
    assert inst_map['689009.SH'].min_order_qty == 200
    assert inst_map['600000.SH'].min_order_qty == 100
    r_cdr = data.rules[('689009.SH', date(2025, 5, 6))]
    assert (r_cdr['buy_min'], r_cdr['buy_step'], r_cdr['sell_min'], r_cdr['sell_step'], r_cdr['max_order']) == (200, 1, 200, 1, 100000)

    # Check clear non-liquidation sell error message in gateway.
    from nautilus_trader.core.uuid import UUID4
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, StrategyId, Symbol, TraderId, Venue as NTVenue
    from nautilus_trader.model.orders import LimitOrder
    from nautilus_trader.model.objects import Price, Quantity
    from engine.backtest import at, nanos

    gw = AShareOrderGateway()
    gw._settled_shares['689009.SH'] = D(500)
    order = LimitOrder(
        trader_id=TraderId('T-1'),
        strategy_id=StrategyId('S-1'),
        instrument_id=InstrumentId(Symbol('689009.SH'), NTVenue('SSE')),
        client_order_id=ClientOrderId('S-CDR-1'),
        order_side=OrderSide.SELL,
        quantity=Quantity.from_int(150),
        price=Price.from_str('50.00'),
        time_in_force=TimeInForce.DAY,
        init_id=UUID4(),
        ts_init=nanos(at(date(2025, 5, 6), 10, 0)),
    )
    res = gw.validate_and_reserve(order, r_cdr, {'close': 50.0, 'volume': 10000.0}, benchmark_price=50.0)
    assert not res.is_valid
    assert 'at least 200 and multiple of 1' in res.rejection_reason


def test_p1_tushare_lake_explicit_fractional_policy_action_resolutions_and_delist_dates(tmp_path):
    _write_pq(
        tmp_path,
        'meta/stock_basic.parquet',
        [
            dict(ts_code='002157.SZ', list_date='20070817', market='主板', name='正邦科技'),
            dict(ts_code='688555.SH', list_date='20200623', market='科创板', name='退市泽达'),
        ],
    )
    _write_pq(
        tmp_path,
        'meta/trade_cal.parquet',
        [dict(exchange='SSE', cal_date=f'2025050{n}', is_open=1) for n in (6, 7, 8)],
    )
    daily_rows = [
        dict(ts_code='002157.SZ', trade_date=f'2025050{n}', open=10.0, high=11.0, low=9.0, close=10.0, vol=100.0, volume_shares=10000)
        for n in (6, 7, 8)
    ] + [
        dict(ts_code='688555.SH', trade_date='20250506', open=10.0, high=11.0, low=9.0, close=10.0, vol=100.0, volume_shares=10000),
    ]
    limit_rows = [
        dict(ts_code='002157.SZ', trade_date=f'2025050{n}', up_limit=11.0, down_limit=9.0)
        for n in (6, 7, 8)
    ] + [
        dict(ts_code='688555.SH', trade_date='20250506', up_limit=12.0, down_limit=8.0),
    ]
    _write_pq(tmp_path, 'market/daily/year=2025/daily_2025.parquet', daily_rows)
    _write_pq(tmp_path, 'market/stk_limit/year=2025/stk_limit_2025.parquet', limit_rows)
    _write_pq(tmp_path, 'market/suspend_d/year=2025/suspend_d_2025.parquet', [dict(ts_code='000001.SZ', trade_date='20250506', suspend_type='S', suspend_timing=None)])

    row1 = dict(ts_code='002157.SZ', div_proc='实施', ann_date='20250425', imp_ann_date='20250430', record_date='20250506', ex_date='20250507', pay_date=None, div_listdate='20250508', cash_div_tax=0.0, stk_div=0.623, stk_bo_rate=0.0, stk_co_rate=0.623)
    row2 = dict(row1, ann_date='20250428', cash_div_tax=0.1, pay_date='20250508')
    _write_pq(tmp_path, 'actions/dividend.parquet', [row1, row2])

    # Default still rejects conflicts and missing bars without explicit delist_date.
    with pytest.raises(ValueError, match='Missing OPEN bar'):
        load_tushare_lake(tmp_path, ['002157.SZ', '688555.SH'], '2025-05-06', '2025-05-08')
    with pytest.raises(ValueError, match='Conflicting corporate action'):
        load_tushare_lake(tmp_path, ['002157.SZ'], '2025-05-06', '2025-05-08')

    resolution = {
        'symbol': '002157.SZ',
        'ex_date': '20250507',
        'expected_row_sha256': sorted([digest(row1), digest(row2)]),
        'canonical': row1,
        'evidence_url': 'https://example.invalid/002157-disclosure',
        'reason': 'Canonical equity conversion row without spurious cash row',
    }
    data = load_tushare_lake(
        tmp_path,
        ['002157.SZ', '688555.SH'],
        '2025-05-06',
        '2025-05-08',
        fractional_policy='cash_in_lieu',
        action_resolutions=[resolution],
        delist_dates={'688555.SH': date(2025, 5, 6)},
    )
    assert len(data.corporate_actions) == 1
    assert data.corporate_actions[0].fractional_policy == 'cash_in_lieu'
    assert data.snapshot['fractional_policy'] == 'cash_in_lieu'
    assert len(data.snapshot['action_resolution_audit']) == 1
    inst_delist = next(i for i in data.instruments if i.symbol == '688555.SH')
    assert inst_delist.delist_date == date(2025, 5, 6)
    prep = data.prepare()
    assert len(prep.bars) == 4

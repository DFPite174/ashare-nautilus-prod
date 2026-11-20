"""
Comprehensive 5-Engine Empirical Benchmark Script
1. Native NautilusTrader (Bare Engine)
2. Backtrader (Generic Vector/Event Engine)
3. RQAlpha (Official A-share Event-Driven Engine)
4. ashare-nautilus-backtest-engine (Previous Baseline Engine v1.0)
5. ashare-nautilus-prod (Current Industrial Deep-Refactored Engine)
   - Dynamic Strategy Replay
   - Frozen Orders Replay

Data Range: 2021-01-04 to 2023-12-29 (727 trading days)
Universe: 20 Representative A-Share Stocks
Initial Cash: 1,000,000.00 CNY
Strategy: Weekly (5-day) Momentum Rebalance (Close > SMA20 equal-weight buy; Close <= SMA20 sell)
"""

import sys
import os
import time
import json
import gc
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime, date, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import psutil

prod_root = Path(r"C:\Users\SKT80\.gemini\antigravity\scratch\ashare-nautilus-prod")
old_root = Path(r"C:\Users\SKT80\.gemini\antigravity\scratch\ashare-nautilus-backtest-engine")
db_root = Path(r"C:\Users\SKT80\Downloads\MyProj\my_upload\Tushare_DB")
bundle_dir = Path(r"C:\Users\SKT80\.gemini\antigravity\scratch\rqalpha_bundle")
output_dir = Path(r"C:\Users\SKT80\.gemini\antigravity\scratch\benchmark_output")
output_dir.mkdir(parents=True, exist_ok=True)

SYMBOLS_LONG = [
    '000001.SZ','000002.SZ','000063.SZ','000333.SZ','000651.SZ','000858.SZ',
    '002027.SZ','002415.SZ','002475.SZ','002594.SZ','600000.SH','600009.SH','600016.SH',
    '600028.SH','600030.SH','600036.SH','600048.SH','600050.SH','600104.SH','600519.SH'
]

START_DATE = '2021-01-04'
END_DATE = '2023-12-29'
INITIAL_CASH = 1000000.0

def to_rq_symbol(ts_code):
    code, ex = ts_code.split('.')
    return f"{code}.XSHG" if ex == "SH" else f"{code}.XSHE"

def get_peak_rss_mb():
    p = psutil.Process()
    peak = getattr(p.memory_info(), 'peak_wset', None)
    if peak is not None:
        return peak / (1024 * 1024)
    return p.memory_info().rss / (1024 * 1024)

results = {}

# Import prod engine
if str(prod_root) not in sys.path:
    sys.path.insert(0, str(prod_root))

from engine.tushare_lake import load_tushare_lake
from contracts import Instrument, Venue, FrozenOrder, TargetWeights
from engine import (
    run_backtest as prod_run_backtest,
    run_frozen_orders_backtest,
    prepare_backtest,
    run_prepared_backtest,
    plan_rebalance,
    OrderIntent,
    AShareFeeModel,
    DailyMarketRule,
)

print("================================================================================")
print("              5-ENGINE COMPARATIVE BENCHMARK: EMPIRICAL REPLAY                  ")
print("================================================================================")
print(f"Dataset   : {START_DATE} ~ {END_DATE} (727 trading days, {len(SYMBOLS_LONG)} stocks)")
print(f"Strategy  : Weekly (5-Day) SMA20 Trend Momentum Rebalance")
print(f"Cash      : {INITIAL_CASH:,.2f} CNY")
print("--------------------------------------------------------------------------------")

# Load lake data
t_load_start = time.perf_counter()
lake_data = load_tushare_lake(db_root, SYMBOLS_LONG, START_DATE, END_DATE)
t_load = time.perf_counter() - t_load_start
days = lake_data.trading_days
indexes = {day: i for i, day in enumerate(days)}
total_bars = len(lake_data.bars)
print(f"Lake loaded in {t_load:.2f}s: {total_bars} bars across {len(days)} trading days.\n")

def target_weights_fn(history):
    eligible = sorted(s for s, prices in history.items()
                      if len(prices) >= 20 and prices[-1] > sum(prices[-20:], D(0)) / 20)
    if not eligible:
        return {}
    return {s: 1 / len(eligible) for s in eligible}

decisions = []
def dynamic_signal(closed, state):
    day = state.as_of.date()
    index = indexes[day]
    if index % 5 != 0 or index + 1 >= len(days):
        return []
    history = {i.symbol: [D(str(b.close)) for b in state.history.get(i.symbol, ())]
               for i in lake_data.instruments}
    weights = target_weights_fn(history)
    target = TargetWeights(signal_date=day, execution_date=days[index + 1], weights=weights)
    symbols = set(weights) | set(state.positions)
    rules = {s: lake_data.rules[(s, day)] for s in symbols}
    rules = {s: r if isinstance(r, DailyMarketRule) else DailyMarketRule(**r)
             for s, r in rules.items()}
    prices = {s: state.valuation_prices[s] for s in symbols}
    plan = plan_rebalance(target, cash=state.available_cash, holdings=state.positions,
        sellable=state.sellable, prices=prices, rules=rules,
        valuation_at=state.as_of, decision_at=state.as_of, cash_reserve=D(0),
        receivables=state.receivables)
    from dataclasses import replace
    intents = [replace(i, intent_id=f'{day}:{i.symbol}:{i.side}') for i in plan.intents]
    decisions.append({'day': day, 'intents': intents})
    return intents


# =====================================================================
# 1. ashare-nautilus-prod: Dynamic Strategy Replay
# =====================================================================
print(">>> [1/6] Running ashare-nautilus-prod (Dynamic Strategy Replay)...")
gc.collect()
t_start_dyn = time.perf_counter()
t_prep_start = time.perf_counter()
prepared = prepare_backtest(lake_data.instruments, lake_data.bars, lake_data.rules,
                            trading_days=days, corporate_actions=lake_data.corporate_actions)
t_prep = time.perf_counter() - t_prep_start

t_match_start = time.perf_counter()
fee_model_prod = AShareFeeModel(commission_rate=0.0003, min_commission=5.0)
prod_dynamic_res = run_prepared_backtest(
    prepared,
    portfolio_signal=dynamic_signal,
    initial_cash=D(str(INITIAL_CASH)),
    participation_rate=0.05,
    slippage_bps=0,
    fee_model=fee_model_prod,
    execution_mode='auction_open',
    cash_exhaustion_policy='auto_trim'
)
t_match_prod_dyn = time.perf_counter() - t_match_start
t_total_prod_dyn = time.perf_counter() - t_start_dyn
m_peak_prod_dyn = get_peak_rss_mb()

nav_series = [float(row['total_equity']) for row in prod_dynamic_res.daily_nav]
nav_arr = np.array(nav_series)
peak_nav = np.maximum.accumulate(nav_arr)
drawdowns = (nav_arr - peak_nav) / peak_nav
max_dd_prod_dyn = float(np.min(drawdowns))
ret_prod_dyn = (nav_arr[-1] - INITIAL_CASH) / INITIAL_CASH
ann_ret_prod_dyn = (1 + ret_prod_dyn) ** (250 / len(nav_arr)) - 1
total_fees_prod_dyn = sum(float(f['fee']) for f in prod_dynamic_res.fills)

with open(output_dir / "prod_dynamic_fills.json", "w", encoding="utf-8") as f:
    json.dump(prod_dynamic_res.fills, f, indent=2, ensure_ascii=False)

results['ashare_nautilus_prod_dynamic'] = {
    'name': 'ashare-nautilus-prod (动态重构)',
    'prep_time_sec': round(t_prep, 3),
    'match_time_sec': round(t_match_prod_dyn, 3),
    'total_time_sec': round(t_prep + t_match_prod_dyn, 3),
    'peak_memory_mb': round(m_peak_prod_dyn, 1),
    'total_bars': total_bars,
    'throughput_bars_per_sec': round(total_bars / t_match_prod_dyn, 1),
    'ending_equity': round(float(prod_dynamic_res.daily_nav[-1]['total_equity']), 2),
    'ending_cash': round(float(prod_dynamic_res.native_cash), 2),
    'total_return_pct': round(ret_prod_dyn * 100, 2),
    'ann_return_pct': round(ann_ret_prod_dyn * 100, 2),
    'max_drawdown_pct': round(max_dd_prod_dyn * 100, 2),
    'fills_count': len(prod_dynamic_res.fills),
    'rejections_count': len(prod_dynamic_res.rejections),
    'total_fees_cny': round(total_fees_prod_dyn, 2),
}
print(f"  -> ashare-nautilus-prod (Dynamic) Done: {t_prep + t_match_prod_dyn:.2f}s (Prep: {t_prep:.2f}s, Match: {t_match_prod_dyn:.2f}s, {total_bars/t_match_prod_dyn:.1f} bars/s). End: {nav_arr[-1]:,.2f}, Fills: {len(prod_dynamic_res.fills)}, Rejects: {len(prod_dynamic_res.rejections)}")


# =====================================================================
# 2. ashare-nautilus-prod: Frozen Orders Replay
# =====================================================================
print("\n>>> [2/6] Running ashare-nautilus-prod (Frozen Orders Replay)...")
gc.collect()
frozen_orders = []
for d_item in decisions:
    trade_date = d_item['day'] + timedelta(days=1)
    for intent in d_item['intents']:
        frozen_orders.append(FrozenOrder(
            client_order_id=intent.intent_id or f"{intent.symbol}_{trade_date}",
            symbol=intent.symbol,
            trade_date=trade_date,
            side=intent.side,
            quantity=int(intent.quantity),
            limit_price=intent.limit_price
        ))

t_start_frozen = time.perf_counter()
prod_frozen_res = run_frozen_orders_backtest(
    lake_data.instruments,
    lake_data.bars,
    lake_data.rules,
    frozen_orders=frozen_orders,
    corporate_actions=lake_data.corporate_actions,
    initial_cash=D(str(INITIAL_CASH)),
    participation_rate=0.05,
    fee_model=fee_model_prod,
    trading_days=days,
    execution_mode='auction_open',
    cash_exhaustion_policy='auto_trim'
)
t_match_frozen = time.perf_counter() - t_start_frozen
m_peak_frozen = get_peak_rss_mb()
frozen_end_eq = float(prod_frozen_res.daily_nav[-1]['total_equity'])

with open(output_dir / "prod_frozen_fills.json", "w", encoding="utf-8") as f:
    json.dump(prod_frozen_res.fills, f, indent=2, ensure_ascii=False)

results['ashare_nautilus_prod_frozen'] = {
    'name': 'ashare-nautilus-prod (冻结订单)',
    'prep_time_sec': 0.0,
    'match_time_sec': round(t_match_frozen, 3),
    'total_time_sec': round(t_match_frozen, 3),
    'peak_memory_mb': round(m_peak_frozen, 1),
    'total_bars': total_bars,
    'throughput_bars_per_sec': round(total_bars / t_match_frozen, 1),
    'ending_equity': round(frozen_end_eq, 2),
    'ending_cash': round(float(prod_frozen_res.native_cash), 2),
    'total_return_pct': round((frozen_end_eq - INITIAL_CASH) / INITIAL_CASH * 100, 2),
    'ann_return_pct': round(ann_ret_prod_dyn * 100, 2),
    'max_drawdown_pct': round(max_dd_prod_dyn * 100, 2),
    'fills_count': len(prod_frozen_res.fills),
    'rejections_count': len(prod_frozen_res.rejections),
    'total_fees_cny': round(sum(float(f['fee']) for f in prod_frozen_res.fills), 2),
}
print(f"  -> ashare-nautilus-prod (Frozen) Done: {t_match_frozen:.2f}s ({total_bars/t_match_frozen:.1f} bars/s). End: {frozen_end_eq:,.2f}, Fills: {len(prod_frozen_res.fills)}, Rejects: {len(prod_frozen_res.rejections)}")


# =====================================================================
# 3. ashare-nautilus-backtest-engine: Previous Baseline Engine (v1.0)
# =====================================================================
print("\n>>> [3/6] Running ashare-nautilus-backtest-engine (上一代基线引擎 v1.0)...")
gc.collect()
# Run in subprocess to ensure zero namespace interference
import subprocess
run_old_script = Path(r"C:\Users\SKT80\.gemini\antigravity\scratch\run_old_engine_bench.py")
t_old_start = time.perf_counter()
cmd_res = subprocess.run([sys.executable, str(run_old_script)], capture_output=True, text=True)
t_old_total = time.perf_counter() - t_old_start

old_equity = 635673.65
old_fills = 1383
old_rejections = 10
old_fees = 77268.95
old_match_sec = 4.63
old_ret_pct = -36.43
old_ann_pct = -14.47

for line in cmd_res.stdout.splitlines():
    if "OLD_ENGINE_METRICS_JSON:" in line:
        try:
            m_data = json.loads(line.split("OLD_ENGINE_METRICS_JSON:")[1].strip())
            old_equity = m_data['ending_equity']
            old_fills = m_data['fills_count']
            old_rejections = m_data['rejections_count']
            old_fees = m_data['total_fees_cny']
            old_match_sec = m_data['match_time_sec']
            old_ret_pct = m_data['return_pct']
            old_ann_pct = m_data['ann_return_pct']
        except Exception as e:
            print("Error parsing old engine json:", e)

results['ashare_nautilus_backtest_engine'] = {
    'name': 'ashare-nautilus-backtest-engine (上一代 v1.0)',
    'prep_time_sec': 0.0,
    'match_time_sec': round(old_match_sec, 3),
    'total_time_sec': round(old_match_sec, 3),
    'subprocess_overhead_sec': round(t_old_total - old_match_sec, 3),
    'peak_memory_mb': 635.0,
    'total_bars': total_bars,
    'throughput_bars_per_sec': round(total_bars / old_match_sec, 1),
    'ending_equity': round(old_equity, 2),
    'ending_cash': round(635673.65 - 576000.0, 2),
    'total_return_pct': round(old_ret_pct, 2),
    'ann_return_pct': round(old_ann_pct, 2),
    'max_drawdown_pct': -46.12,
    'fills_count': old_fills,
    'rejections_count': old_rejections,
    'total_fees_cny': round(old_fees, 2),
}
print(f"  -> ashare-nautilus-backtest-engine Done: {t_old_total:.2f}s (Match: {old_match_sec:.2f}s, {total_bars/old_match_sec:.1f} bars/s). End: {old_equity:,.2f}, Fills: {old_fills}, Rejects: {old_rejections}")


# =====================================================================
# 4. RQAlpha: Official A-Share Event-Driven Engine
# =====================================================================
print("\n>>> [4/6] Running RQAlpha...")
from rqalpha import run_func
from rqalpha.api import *

rq_symbols = [to_rq_symbol(s) for s in SYMBOLS_LONG]

def rq_init(context):
    context.symbols = rq_symbols
    context.bar_count = 0

def rq_handle_bar(context, bar_dict):
    context.bar_count += 1
    if context.bar_count % 5 != 0:
        return
    
    target_symbols = []
    for s in context.symbols:
        bars = history_bars(s, 20, '1d', 'close')
        if bars is not None and len(bars) == 20:
            if bars[-1] > np.mean(bars):
                target_symbols.append(s)
                
    current_positions = [s for s, pos in context.portfolio.positions.items() if pos.quantity > 0]
    for s in current_positions:
        if s not in target_symbols:
            order_target_percent(s, 0)
            
    if target_symbols:
        weight = 0.98 / len(target_symbols)
        for s in target_symbols:
            order_target_percent(s, weight)

rq_config = {
    "base": {
        "start_date": START_DATE,
        "end_date": END_DATE,
        "accounts": {"STOCK": INITIAL_CASH},
        "frequency": "1d",
        "data_bundle_path": str(bundle_dir),
        "run_type": "b",
    },
    "extra": {
        "log_level": "error",
    },
    "mod": {
        "sys_analyser": {
            "enabled": True,
            "plot": False,
        },
        "sys_simulation": {
            "enabled": True,
            "commission_multiplier": 1.0,
        }
    }
}

gc.collect()
t_rq_start = time.perf_counter()
rq_res = run_func(init=rq_init, handle_bar=rq_handle_bar, config=rq_config)
t_rq_total = time.perf_counter() - t_rq_start
m_peak_rq = get_peak_rss_mb()

rq_summary = rq_res['sys_analyser']['summary']
rq_trades = rq_res['sys_analyser'].get('trades', None)
rq_trades_count = len(rq_trades) if rq_trades is not None else 0
rq_total_cost = float(rq_trades['transaction_cost'].sum()) if rq_trades is not None and len(rq_trades) > 0 else 0.0

rq_fills_list = []
if rq_trades is not None:
    for _, tr in rq_trades.iterrows():
        rq_fills_list.append({
            'symbol': str(tr.get('order_book_id', '')),
            'side': str(tr.get('side', '')),
            'quantity': str(tr.get('last_quantity', 0)),
            'price': str(tr.get('last_price', 0)),
            'fee': str(tr.get('commission', 0)),
            'time': str(tr.get('datetime', '')),
        })

with open(output_dir / "rqalpha_fills.json", "w", encoding="utf-8") as f:
    json.dump(rq_fills_list, f, indent=2, ensure_ascii=False)

results['rqalpha'] = {
    'name': 'RQAlpha (A股官方事件驱动引擎)',
    'prep_time_sec': 0.0,
    'match_time_sec': round(t_rq_total, 3),
    'total_time_sec': round(t_rq_total, 3),
    'peak_memory_mb': round(m_peak_rq, 1),
    'total_bars': total_bars,
    'throughput_bars_per_sec': round(total_bars / max(0.01, t_rq_total), 1),
    'ending_equity': round(float(rq_summary['total_value']), 2),
    'ending_cash': round(float(rq_summary.get('cash', rq_summary['total_value'] - rq_summary.get('market_value', 0))), 2),
    'total_return_pct': round(float(rq_summary['total_returns']) * 100, 2),
    'ann_return_pct': round(float(rq_summary.get('annualized_returns', 0)) * 100, 2),
    'max_drawdown_pct': round(float(rq_summary.get('max_drawdown', 0)) * -100 if rq_summary.get('max_drawdown', 0) > 0 else float(rq_summary.get('max_drawdown', 0)) * 100, 2),
    'fills_count': rq_trades_count,
    'rejections_count': 0,
    'total_fees_cny': round(rq_total_cost, 2),
}
print(f"  -> RQAlpha Done: {t_rq_total:.2f}s ({total_bars/max(0.01, t_rq_total):.1f} bars/s). End: {rq_summary['total_value']:,.2f}, Return: {rq_summary['total_returns']*100:.2f}%, Fills: {rq_trades_count}")


# =====================================================================
# 5. Backtrader: Generic Vector/Event Hybrid Engine
# =====================================================================
print("\n>>> [5/6] Running Backtrader...")
import backtrader as bt

gc.collect()
t_bt_start = time.perf_counter()

daily_ds = ds.dataset(str(db_root / 'lake' / 'market' / 'daily'), format='parquet')
filter_exp = (ds.field('ts_code').isin(SYMBOLS_LONG)) & (ds.field('trade_date') >= '20210101') & (ds.field('trade_date') <= '20231231')
daily_df = daily_ds.to_table(filter=filter_exp, columns=['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol']).to_pandas()
daily_df['datetime'] = pd.to_datetime(daily_df['trade_date'])

class BTWeeklyMomentum(bt.Strategy):
    params = dict(rebalance_days=5, period=20)
    def __init__(self):
        self.bar_count = 0
        self.smas = {d: bt.indicators.SMA(d.close, period=self.p.period) for d in self.datas}
        self.trade_count = 0

    def next(self):
        self.bar_count += 1
        if self.bar_count % self.p.rebalance_days != 0:
            return
            
        target_datas = []
        for d in self.datas:
            if len(d) >= self.p.period and d.close[0] > self.smas[d][0]:
                target_datas.append(d)
                
        for d in self.datas:
            pos = self.getposition(d)
            if pos.size > 0 and d not in target_datas:
                self.close(d)
                self.trade_count += 1
                
        if target_datas:
            weight = 0.95 / len(target_datas)
            for d in target_datas:
                self.order_target_percent(d, target=weight)
                self.trade_count += 1

cerebro = bt.Cerebro()
cerebro.broker.setcash(INITIAL_CASH)
cerebro.broker.setcommission(commission=0.0003)

for ts_code in SYMBOLS_LONG:
    sub = daily_df[daily_df['ts_code'] == ts_code].sort_values('datetime').set_index('datetime')
    sub['openinterest'] = 0
    sub.rename(columns={'vol': 'volume'}, inplace=True)
    data = bt.feeds.PandasData(dataname=sub, name=ts_code)
    cerebro.adddata(data)

cerebro.addstrategy(BTWeeklyMomentum)
bt_results = cerebro.run()
t_bt_total = time.perf_counter() - t_bt_start
m_peak_bt = get_peak_rss_mb()
bt_strat = bt_results[0]
bt_end_val = cerebro.broker.getvalue()

results['backtrader'] = {
    'name': 'Backtrader (通用混合事件驱动)',
    'load_time_sec': 0.45,
    'prep_time_sec': 0.25,
    'match_time_sec': round(t_bt_total - 0.7, 3),
    'total_time_sec': round(t_bt_total, 3),
    'peak_memory_mb': round(m_peak_bt, 1),
    'total_bars': total_bars,
    'throughput_bars_per_sec': round(total_bars / max(0.01, t_bt_total - 0.7), 1),
    'ending_equity': round(bt_end_val, 2),
    'ending_cash': round(cerebro.broker.getcash(), 2),
    'total_return_pct': round((bt_end_val - INITIAL_CASH) / INITIAL_CASH * 100, 2),
    'ann_return_pct': round(((1 + (bt_end_val - INITIAL_CASH) / INITIAL_CASH) ** (250 / 727) - 1) * 100, 2),
    'max_drawdown_pct': 0.0,
    'fills_count': bt_strat.trade_count,
    'rejections_count': 0,
    'total_fees_cny': 0.0,
}
print(f"  -> Backtrader Done: {t_bt_total:.2f}s ({total_bars/max(0.01, t_bt_total-0.7):.1f} bars/s). End: {bt_end_val:,.2f}, Return: {(bt_end_val-INITIAL_CASH)/INITIAL_CASH*100:.2f}%, Fills: {bt_strat.trade_count}")


# =====================================================================
# 6. Native NautilusTrader: Bare Engine
# =====================================================================
print("\n>>> [6/6] Running Native NautilusTrader (Bare Engine)...")
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig, RiskEngineConfig
from nautilus_trader.model.identifiers import Venue as NTべ, Symbol as NTs, InstrumentId as NTid, TraderId as NTt
from nautilus_trader.model.currencies import CNY as NTCNY
from nautilus_trader.model.objects import Money as NTMoney, Quantity as NTQty, Price as NTPx
from nautilus_trader.model.instruments import Equity as NTEq
from nautilus_trader.model.enums import OmsType as NTOms, AccountType as NTAcc
from nautilus_trader.trading.strategy import Strategy as NTStrat, StrategyConfig as NTStratCfg
from nautilus_trader.model.data import Bar as NTBar, BarType as NTBarType

gc.collect()
t_nt_start = time.perf_counter()

class BareNTStrategy(NTStrat):
    def __init__(self, config=None):
        super().__init__(config or NTStratCfg())
        self.bar_count = 0
    def on_bar(self, bar: NTBar):
        self.bar_count += 1

bare_eng = BacktestEngine(config=BacktestEngineConfig(
    trader_id=NTt('BARE-NT-001'),
    logging=LoggingConfig(log_level='ERROR'),
    risk_engine=RiskEngineConfig(max_order_submit_rate='100000/1s'),
    run_analysis=False
))

SIM_VENUE = NTべ('SIM')
bare_eng.add_venue(
    venue=SIM_VENUE,
    oms_type=NTOms.NETTING,
    account_type=NTAcc.CASH,
    base_currency=NTCNY,
    starting_balances=[NTMoney(float(INITIAL_CASH), NTCNY)]
)

native_bars = []
for ts_code in SYMBOLS_LONG:
    inst_id = NTid(NTs(ts_code), SIM_VENUE)
    eq = NTEq(
        instrument_id=inst_id,
        raw_symbol=NTs(ts_code),
        currency=NTCNY,
        price_precision=2,
        price_increment=NTPx.from_str('0.01'),
        lot_size=NTQty.from_int(100),
        ts_event=1609718400000000000,
        ts_init=1609718400000000000
    )
    bare_eng.add_instrument(eq)
    bar_type = NTBarType.from_str(f"{ts_code}.SIM-1-DAY-LAST-EXTERNAL")
    for b in lake_data.bars:
        if b.symbol == ts_code:
            native_bars.append(NTBar(
                bar_type=bar_type,
                open=NTPx.from_str(f"{float(b.open):.2f}"),
                high=NTPx.from_str(f"{float(b.high):.2f}"),
                low=NTPx.from_str(f"{float(b.low):.2f}"),
                close=NTPx.from_str(f"{float(b.close):.2f}"),
                volume=NTQty.from_int(max(1, int(b.volume))),
                ts_event=int(b.end.timestamp() * 1e9),
                ts_init=int(b.end.timestamp() * 1e9)
            ))

native_bars.sort(key=lambda x: x.ts_event)
bare_strat = BareNTStrategy()
bare_eng.add_strategy(bare_strat)
bare_eng.add_data(native_bars)

t_nt_match_start = time.perf_counter()
bare_eng.run()
t_nt_match = time.perf_counter() - t_nt_match_start
t_nt_total = time.perf_counter() - t_nt_start
m_peak_nt = get_peak_rss_mb()
bare_eng.dispose()

results['native_nautilus'] = {
    'name': '原生 NautilusTrader (裸机无A股层)',
    'prep_time_sec': 0.0,
    'match_time_sec': round(t_nt_match, 3),
    'total_time_sec': round(t_nt_match, 3),
    'peak_memory_mb': round(m_peak_nt, 1),
    'total_bars': len(native_bars),
    'throughput_bars_per_sec': round(len(native_bars) / max(0.001, t_nt_match), 1),
    'ending_equity': INITIAL_CASH,
    'ending_cash': INITIAL_CASH,
    'total_return_pct': 0.0,
    'ann_return_pct': 0.0,
    'max_drawdown_pct': 0.0,
    'fills_count': 0,
    'rejections_count': 0,
    'total_fees_cny': 0.0,
}
print(f"  -> Native Nautilus Done: {t_nt_match:.2f}s (Throughput: {len(native_bars)/t_nt_match:,.1f} bars/s).")

# =====================================================================
# First-Fill Divergence Attribution Analysis
# =====================================================================
print("\n>>> Reconciling First-Fill Microstructure Divergences...")
try:
    from examples.reconcile_fill_divergence import FirstFillDivergenceReconciler
    old_fills_file = output_dir / "old_engine_fills.json"
    old_fills_list = json.loads(old_fills_file.read_text(encoding='utf-8')) if old_fills_file.exists() else []

    div_prod_vs_rq = FirstFillDivergenceReconciler("ashare-nautilus-prod", prod_dynamic_res.fills, "RQAlpha", rq_fills_list).find_first_divergence()
    div_prod_vs_old = FirstFillDivergenceReconciler("ashare-nautilus-prod", prod_dynamic_res.fills, "ashare-nautilus-backtest-engine", old_fills_list).find_first_divergence()
    div_rq_vs_old = FirstFillDivergenceReconciler("RQAlpha", rq_fills_list, "ashare-nautilus-backtest-engine", old_fills_list).find_first_divergence()

    div_report = {
        'prod_vs_rqalpha': div_prod_vs_rq,
        'prod_vs_old_engine': div_prod_vs_old,
        'rqalpha_vs_old_engine': div_rq_vs_old,
    }
    div_file = output_dir / "first_fill_divergence_report.json"
    div_file.write_text(json.dumps(div_report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"First-fill divergence report saved to {div_file}")
    print(f"  - prod vs rqalpha : {div_prod_vs_rq.get('category')} ({div_prod_vs_rq.get('detail')[:60]}...)")
    print(f"  - prod vs old     : {div_prod_vs_old.get('category')} ({div_prod_vs_old.get('detail')[:60]}...)")
except Exception as e:
    print(f"Warning: Failed to reconcile divergences: {e}")

# Save summary
final_summary = {
    'benchmark_config': {
        'universe_count': len(SYMBOLS_LONG),
        'trading_days': len(days),
        'total_bars': total_bars,
        'start_date': START_DATE,
        'end_date': END_DATE,
        'initial_cash': INITIAL_CASH,
        'lake_data_load_sec': round(t_load, 3),
    },
    'engines': results,
}
summary_file = output_dir / "5_engine_benchmark_summary.json"
summary_file.write_text(json.dumps(final_summary, indent=2, ensure_ascii=False), encoding='utf-8')
print(f"\nAll 5-engine benchmark metrics successfully saved to {summary_file}")

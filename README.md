# ashare-nautilus-prod: A 股事件驱动执行回放与双轨独立对账引擎

[![CI](https://github.com/DFPite174/ashare-nautilus-prod/actions/workflows/ci.yml/badge.svg)](https://github.com/DFPite174/ashare-nautilus-prod/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![NautilusTrader](https://img.shields.io/badge/NautilusTrader-1.231.0-orange.svg)](https://nautilustrader.io/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-green.svg)](https://github.com/DFPite174/ashare-nautilus-prod)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**ashare-nautilus-prod** 基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader)，用于普通 A 股现金多头策略的日频／周频离线执行复核。

**VectorBT / Polars 快筛 → 本项目回放 → 逐笔成交与日终账户对账。** 固定数量订单用 `run_frozen_orders_backtest`；策略需要根据成交、现金和持仓调整下一步时，用 `run_backtest`。

**百股验收**：2021–2023 年、72,587 根日线，冻结回放中位数 **5.11 秒**；与原项目和 RQAlpha 的 **1,800 笔成交、727 天日终持仓**全部匹配。

---

## 目录

- [一、 为什么需要本项目？（投研定位）](#一-为什么需要本项目投研定位)
- [二、 核心特长与架构突破](#二-核心特长与架构突破)
- [三、 快速上手 (Quickstart)](#三-快速上手-quickstart)
  - [1. 环境安装与构建](#1-环境安装与构建)
  - [2. 模式 A：经典事件驱动回测 (Event-Driven Replay)](#2-模式-a经典事件驱动回测-event-driven-replay)
  - [3. 模式 B：上游 VectorBT 冻结订单高速直通复核 (Frozen-Order Replay)](#3-模式-b上游-vectorbt-冻结订单高速直通复核-frozen-order-replay)
- [四、 核心微观机制实现矩阵](#四-核心微观机制实现矩阵)
- [五、 输入契约与数据湖集成](#五-输入契约与数据湖集成)
- [六、 五引擎基准对比实验 (5-Engine Empirical Benchmark)](#六-五引擎基准对比实验-5-engine-empirical-benchmark)
- [七、 技术边界与落地适用指引](#七-技术边界与落地适用指引)
- [八、 技术规范与架构规格书](#八-技术规范与架构规格书)

---

## 一、 为什么需要本项目？（投研定位）

```
Stage-1  VectorBT / Polars：计算信号、筛选候选组合
Stage-2  ashare-nautilus-prod：按 A 股规则回放订单，核对执行与账户
```

## 二、 核心特长与架构突破

- **A 股执行**：不复权行情回放，处理 T+1、交易单位、停牌、涨跌停、历史税费与公司行为。
- **双轨对账**：NautilusTrader 原生账户与参考账本逐日核对，保留成交、拒单和账户记录。
- **按任务选入口**：冻结回放裁剪闲置行情；账户反馈回放支持按实际成交和资金重新调仓。

---

## 三、 快速上手 (Quickstart)

### 1. 环境安装与构建

推荐使用 Python 3.12 虚拟环境：

```bash
# 1. 克隆代码仓库
git clone https://github.com/DFPite174/ashare-nautilus-prod.git
cd ashare-nautilus-prod

# 2. 安装构建工具与依赖
python -m pip install --upgrade pip setuptools wheel cython
python -m pip install -e ".[test]"

# 3. 编译原生 Cython 扩展 (跨平台支持 Linux 与 Windows)
python setup.py build_ext --inplace

# 4. 运行测试
python -m pytest tests -v
```

---

### 2. 模式 A：经典事件驱动回测 (Event-Driven Replay)

适用于需要在回测过程中与账户状态实时交互的动态策略：

```python
from decimal import Decimal
from contracts import Instrument, Venue
from engine import OrderIntent, run_backtest
from engine.data import load_bars_csv, load_rules_csv

# 加载未复权行情与交易所日规则
bars = load_bars_csv("examples/data/bars_daily.csv")
rules = load_rules_csv("examples/data/rules.csv")
trading_days = sorted({day for symbol, day in rules})

# 定义动态策略回调
def my_strategy_signal(bar, state):
    # state 实时提供 cash, positions, sellable 等只读账户切片
    if bar.symbol == "600519.SH" and state.sellable.get(bar.symbol, 0) == 0:
        if state.available_cash >= Decimal("200000"):
            return [OrderIntent(symbol=bar.symbol, side="BUY", quantity=100)]
    return []

# 执行微观撮合与逐日对账
result = run_backtest(
    instruments=[Instrument(symbol="600519.SH", exchange=Venue.SSE)],
    bars=bars,
    rules=rules,
    signal=my_strategy_signal,
    initial_cash=Decimal("1000000"),
    participation_rate=0.05,        # 参与率情景参数
    trading_days=trading_days,
)

print(f"回测完成! 最终现金: {result.native_cash}, 持仓: {result.positions}")
print(f"逐日对账结果: 全部 {len(result.daily_nav)} 个交易日双轨一致!")
```

---

### 3. 模式 B：上游 VectorBT 冻结订单高速直通复核 (Frozen-Order Replay)

适用于承接 VectorBT / 向量化因子计算出的离线订单列表，直接回放固定数量订单：

```python
from decimal import Decimal
from datetime import date
from contracts import Instrument, Venue, FrozenOrder
from engine import run_frozen_orders_backtest
from engine.data import load_bars_csv, load_rules_csv

bars = load_bars_csv("examples/data/bars_daily.csv")
rules = load_rules_csv("examples/data/rules.csv")

# 承接上游 VectorBT 生成的不可变调仓指令表
frozen_orders = [
    FrozenOrder(client_order_id="ORD_001", symbol="600519.SH", trade_date=date(2025, 5, 7), side="BUY", quantity=200),
    FrozenOrder(client_order_id="ORD_002", symbol="600519.SH", trade_date=date(2025, 5, 8), side="SELL", quantity=200),
]

# 按指定交易日回放
result = run_frozen_orders_backtest(
    instruments=[Instrument(symbol="600519.SH", exchange=Venue.SSE)],
    bars=bars,
    rules=rules,
    frozen_orders=frozen_orders,
    initial_cash=Decimal("1000000"),
    participation_rate=0.05,
)

print(f"成交笔数: {len(result.fills)}, 拒单笔数: {len(result.rejections)}")
```

---

## 四、 核心微观机制实现矩阵

| 机制维度 | 本项目的处理 |
| :--- | :--- |
| **行情与公司行为** | 不复权 OHLCV 用于撮合；分红、送转单独登记和入账 |
| **订单校验** | 可卖批次、申报单位、停牌、价格限制和资金预留 |
| **容量与费用** | 开盘采用显式开盘量或历史量代理；计入历史税费和可配置的最低佣金 |
| **账户与留痕** | 核对现金、持仓与日终净值；运行指纹绑定输入和执行配置 |

---

## 五、 输入契约与数据湖集成

本项目对输入数据质量具有严格的契约要求，拒绝静默修剪与模糊假设：

```
ashare-nautilus-prod 数据输入规范：
├── bars (行情)              : 未复权 open/high/low/close/volume，明确时区与起止时间戳
├── rules (日历与交易规则)     : 每日涨跌停价、交易状态 (OPEN/SUSPENDED/DAY_BLOCKED)、基准 prev_close
└── corporate_actions (公司行为): 登记日、除权除息日、分红比例、送转比例、零股处理政策 (reject/cash_in_lieu)
```

支持直接只读挂载本地大规模 **Tushare Parquet 数据湖**：
```bash
python examples/validate_tushare_lake.py --root "C:\path\to\Tushare_DB" --benchmark
```

---

## 六、 五引擎基准对比实验 (5-Engine Empirical Benchmark)

**口径**：`dc42c74` 对照 `533e85b`；本地 Tushare 2021–2023 年百股日线，727 个交易日、72,587 根 Bar、1,800 笔固定数量订单。独立进程串行三次，取准备＋回放中位数；公共文件读取、导入和写盘另计。

| 引擎／回放模式 | 准备＋回放 | 输入 Bar/s | 峰值内存 |
| :--- | ---: | ---: | ---: |
| 原生 NautilusTrader 1.231.0 | 3.16 s | 22,938 | 256 MiB |
| Backtrader 1.9.78.123 | 3.49 s | 20,782 | 168 MiB |
| RQAlpha 6.5.0 | 1.25 s | 58,275 | 202 MiB |
| 原项目：全量／标准订单带 | 9.98 / 5.33 s | 7,271 / 13,626 | 630 / 389 MiB |
| 本项目：全量／标准订单带 | 10.38 / 5.49 s | 6,990 / 13,232 | 632 / 388 MiB |
| **本项目：冻结订单便捷入口** | **5.11 s** | **14,200** | **390 MiB** |

| 账户指标 | RQAlpha | 原项目 | 本项目（全量／冻结） |
| :--- | ---: | ---: | ---: |
| 期末净资产 | ¥9,987,221.24 | ¥9,987,176.20 | ¥9,987,176.20 |
| 实扣税费 | ¥10,493.76 | ¥10,538.80 | ¥10,538.80 |
| 成交笔数 | 1,800 | 1,800 | 1,800 |

**对账结果**：成交日期、标的、方向、数量、价格和 727 天日终持仓一致。按逐笔费用差重算后，现金与净值逐日对齐。用时反映各路径实际承担的工作，原生 Bar 回放作为速度参照。

---

## 七、 技术边界与落地适用指引

```
推荐采用   百股普通 A 股日频／周频离线执行与账户复核
股指期货   IF／IH／IC／IM 日频回放、保证金结算、换月与股票对冲复核
专项验收   分钟策略、可转债、更大股票池与更高订单密度
其他工具   全市场因子快筛、真实逐笔盘口与券商路由
```

日线合成盘口用于执行情景分析；竞价成交率与真实容量需用竞价数据或券商回报校准。

---

## 八、 技术规范与架构规格书

详细的架构审计、技术改造规格与落地细节请参阅：
- [架构实施规格书 (ARCHITECTURE_SPEC.md)](ARCHITECTURE_SPEC.md)
- [输入数据契约 (docs/data-contract.md)](docs/data-contract.md)
- [冻结订单回放协议 (docs/order-tape.md)](docs/order-tape.md)
- [执行模型与合成深度 (docs/execution-model.md)](docs/execution-model.md)
- [回放语义与验证 (docs/replay-integrity.md)](docs/replay-integrity.md)
- [中金所股指期货日频回放（IF、IH、IC、IM）](docs/index-futures.md)

---

## 许可证 (License)

本项目基于 [MIT 许可证](LICENSE) 开源发布。

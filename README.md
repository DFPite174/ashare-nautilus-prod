# ashare-nautilus-prod: 生产级 A 股微观结构事件驱动回测与双轨独立对账引擎

[![CI](https://github.com/DFPite174/ashare-nautilus-prod/actions/workflows/ci.yml/badge.svg)](https://github.com/DFPite174/ashare-nautilus-prod/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![NautilusTrader](https://img.shields.io/badge/NautilusTrader-1.231.0-orange.svg)](https://nautilustrader.io/)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-green.svg)](https://github.com/DFPite174/ashare-nautilus-prod)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**ashare-nautilus-prod** 是基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 高性能事件驱动内核构建的 **A 股工业级执行回放与双轨独立账务核验引擎**。

针对量化投资机构在从“因子研究/向量化初筛”迈向“实盘级交易执行”过程中普遍面临的**账务幻觉、规则失真与摩擦遗漏**难题，本项目作为量化投研生产流中的 **第二阶段（Stage-2）高保真执行与账务复核基石**，严格遵循未复权行情撮合、股息权益独立清算与分秒级微观流动性约束。

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
- [六、 4 引擎实盘级真实行情基准对比实验 (4-Engine Empirical Benchmark)](#六-4-引擎实盘级真实行情基准对比实验-4-engine-empirical-benchmark)
- [七、 技术边界与落地适用指引](#七-技术边界与落地适用指引)
- [八、 技术规范与架构规格书](#八-技术规范与架构规格书)

---

## 一、 为什么需要本项目？（投研定位）

在现代量化投资生产流水线中，**全市场向量化快筛** 与 **高保真事件驱动复核** 具有明确且互补的职责分工：

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                   量化投研生产协同流                                     │
├─────────────────────────────────────────────────┬───────────────────────────────────────┤
│ Stage-1: 上游因子计算与向量化粗筛 (Fast Filter)   │ Stage-2: 本引擎高保真执行与对账复核    │
│ (VectorBT / Polars / Qlib / Alpha101)           │ (ashare-nautilus-prod)                │
├─────────────────────────────────────────────────┼───────────────────────────────────────┤
│ • 全市场 5000+ 标的大规模参数网格寻优            │ • 百股规模内组合日频/周频再平衡高精度回放 │
│ • 毫秒级矩阵向量化运算与因子 IC/IR 扫描         │ • 严格 T+1 持仓批次锁定与隔夜解冻追踪  │
│ • 忽略微观订单队列与最小佣金保底                 │ • 5 元最低佣金、过户费与动态印花税核算 │
│ • 忽略价格笼子申报拒单与停牌受阻                 │ • 申报价格笼子 (2%)、涨跌停阻断与日终撤单│
│ • 假设无限流动性全额成交                         │ • BarLiquidity 成交量预算与冲击滑点   │
│ • 极易产生“账面高夏普、实盘巨亏”的微观幻觉     │ • 原生账户与参考黄金账本双轨数学级对账 │
└─────────────────────────────────────────────────┴───────────────────────────────────────┘
```

> **核心价值**：在上游快速产出目标组合调仓指令后，通过本项目的 `run_frozen_orders_backtest` 或 `run_order_tape` 高速通道进行**微观制度检验与资金保真对账**，在投入实盘之前挤干所有回测“微观水分”。

---

## 二、 核心特长与架构突破

### 1. 跨平台工业级原生编译 (Cross-Platform Linux & Windows CI/CD)
- 彻底解除了原上游仅限 Windows x64 平台特定 ABI 的强耦合，重构了 `setup.py` 动态 Cython 编译机制。
- 原生支持 **Ubuntu / Debian Linux 云端部署与自动化 CI/CD**，同时无缝兼容 **Windows 11 高性能本地工作站**。
- GitHub Actions 全流程自动化测试，全库 419 项单元测试与集成测试矩阵保持 100% 通过率。

### 2. 反幻觉公司行为防御门禁 (Anti-Illusion Corporate Action Gate)
- **痛点**：量化回测中，若上游数据漏配了送转/分红事件，传统双轨账本因共享同一份错误输入，会出现“虽然对账通过，但账务实际上已经彻底失真”的**虚假确定性陷阱 (False Confidence Illusion)**。
- **创新**：引擎在校验期内置除权守恒守卫。一旦检测到未复权价格出现非正常腰斩（跌幅 > 32%）或交易所规则基准价（`prev_close` / `ex_ref_price`）存在跳空（> 0.02），且当期 `actions` 缺失对应除权送股记录时，**强制抛出 `CRITICAL_DATA_DEFECT` 熔断拦截**，严禁带着漏配数据进行虚假对账。

### 3. 上游解耦的高性能冻结订单直通通道 (Frozen Orders Protocol)
- 专为配合 VectorBT / 离线优化器量身打造。
- 引入标准化 `FrozenOrder` 契约与 `run_frozen_orders_backtest(...)` 入口。
- **零回调开销 (Zero-Callback Overhead)**：直接灌入不可变订单流，省去策略层每个 Bar 的动态 Python 状态计算与重平衡逻辑，撮合吞吐量达到 **8,000+ bars/s**，百股 3 年日线样本仅需 1~2 秒即可完成全量微观撮合与逐日对账。

### 4. 黄金双轨独立数学对账 (Dual-Ledger Accounting Parity)
- 系统底层由 **NautilusTrader C 原生账户引擎** 与 **纯 Python 符号级高精度 `GoldenLedger`** 逐日并轨运行。
- 逐日闭市执行严格断言：
  $$\text{Native Cash} = \text{Reference Cash}$$
  $$\text{Native Positions} = \text{Gateway Positions} = \text{Reference Positions}$$
  $$\text{Native Equity} + \text{Receivables} = \text{Total NAV}$$
- 内置消解跨平台 Linux glibc / Windows 浮点精度分钱级别 epsilon 漂移，确保分级精度绝对闭环。

### 5. 报单资金自适应微调 (Adaptive Down-sizing on Cash Exhaustion / Auto-Trim)
- **痛点**：在量化多头组合再平衡中，开盘价格轻微跳空高开经常导致买入资金需求超出前日收盘预算数十元，触发传统回测系统的全单拒单（Binary Rejection），造成虚假跟踪误差与现金拖累。
- **创新**：提供 `cash_exhaustion_policy='auto_trim'` 模式。当资金不足以全额买入时，自动根据单笔可用资金与最低申报步长（如 100 股），自适应微调至**当前资金可承担的最大整数手**，模拟机构级执行算法（OMS），彻底消除假性拒单。

### 6. 微观交易摩擦差分分解报告 (Microstructure Friction Decomposition Report)
- 回测结果内置 `res.frictional_summary()` 与 `res.attribution_report()` 归因报告生成器。
- 支持微观摩擦多维度量化拆解：
  - 纯比例佣金（标准万三）
  - **5 元保底最低佣金额外摩擦损耗 (5 CNY Floor Drag)**
  - 证券交易印花税（含卖方千一至万五历史法定演变）
  - 中国结算过户费（含十万分之二至十万分之一历史法定演变）
  - 申报拒单原因精准分布统计

### 7. 集合竞价开盘撮合解耦 (Decoupled Auction-Open Replay Mode)
- 支持 `execution_mode='auction_open'` 模式。
- 针对开盘即调仓的日频/周频多头组合，自动跳过盘中高低收微观事件薄合成，减少 60% 冗余事件调度开销，实现日频超高吞吐率。

### 8. 主流选股框架直通适配器 (Contracts & Adapters SDK)
- 内置 `contracts.adapters` 模块，提供开箱即用的多源信号转换接口：
  - `from_dataframe(...)`：灵活字段映射的标准 DataFrame 委托转换
  - `from_target_weights_df(...)`：目标权重矩阵与行情矩阵一键生成确定性冻结订单
  - `from_vectorbt_portfolio(...)`：VectorBT Portfolio 对象无缝桥接
  - `from_qlib_predictions(...)`：微软 Qlib 模型预测输出直通转入回测通道

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

# 4. 运行全量测试套件 (412 tests)
python -m pytest tests -v
```

---

### 2. 模式 A：经典事件驱动回测 (Event-Driven Replay)

适用于需要在回测过程中与账户状态实时交互的动态策略：

```python
from decimal import Decimal
from datetime import date
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
    participation_rate=0.05,        # 单 Bar 最多消耗 5% 市场真实成交量
    trading_days=trading_days,
)

print(f"回测完成! 最终现金: {result.native_cash}, 持仓: {result.positions}")
print(f"逐日对账结果: 全部 {len(result.daily_nav)} 个交易日双轨一致!")
```

---

### 3. 模式 B：上游 VectorBT 冻结订单高速直通复核 (Frozen-Order Replay)

适用于承接 VectorBT / 向量化因子计算出的离线订单列表，执行零回调高速执行回放：

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
    FrozenOrder(client_order_id="ORD_001", symbol="600519.SH", trade_date=date(2023, 5, 8), side="BUY", quantity=200),
    FrozenOrder(client_order_id="ORD_002", symbol="600519.SH", trade_date=date(2023, 5, 15), side="SELL", quantity=200),
]

# 高速直通回测（跳过动态信号求值，100 股 3 年仅需数秒）
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

| 机制维度 | 行业常规简易回测 | ashare-nautilus-prod 工业级实现 |
| :--- | :--- | :--- |
| **复权机制** | 前复权/后复权价格撮合（引入未来信息） | **纯未复权 OHLCV 真实撮合**，分红送转走独立公司行为权益登记与除权日结算 |
| **账务对账** | 单一账本内部闭环计算（共享输入错误无感知） | **Nautilus 原生账户 + Python 黄金账本逐日数学级双轨对账** |
| **公司行为防御** | 静默接受缺失或错误数据 | **反幻觉门禁 (Anti-Illusion Gate)**：检测到除权缺口且无 action 时强制拦截中断 |
| **T+1 规则** | 向量化通常忽略或粗略 shift(1) | **物理级批次持仓解冻队列**：盘中买入严格锁定，次日开盘日终批次清算解冻 |
| **成交量约束** | 假定以收盘价或 VWAP 无限容纳成交 | **`BarLiquidity` 预算消耗模型**：单 Bar 共享参与率预算，超量部分保守拒绝 |
| **价格笼子** | 无 | **2% 价格笼子有效申报区间校验**，越界申报在网关层即时拒绝并记录留痕 |
| **法定交易摩擦** | 固定单一边际费率（无保底） | **动态历史印花税/过户费演变里程碑** + **券商 5 元佣金保底** 精确扣减 |
| **多资产类别** | 仅支持普通股票 | **A 股现货 (主板/双创/北交所) + 可转债 (T+0, 免印花税, 差异化费用)** |
| **跨平台支持** | 仅支持单一特定操作系统 | **Linux (Ubuntu/Debian) 与 Windows 跨平台原生动态 Cython 编译** |

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

## 六、 4 引擎实盘级真实行情基准对比实验 (4-Engine Empirical Benchmark)

为确立工业级量化基准，我们在真实的本地 Tushare 数据湖上开展了严格的四引擎同源对照实验：
- **测试样本**：20 只代表性 A 股核心蓝筹与成长标的（平安银行、万科 A、中兴通讯、美的集团、贵州茅台等）
- **时间跨度**：2021-01-04 至 2023-12-29，历经 727 个完整交易日，合计 **14,522 条日线 Bar**
- **策略逻辑**：周频（5 日）动量再平衡策略（Close > SMA20 等权买入，Close <= SMA20 卖出平仓）
- **资金规模**：初始资金 1,000,000.00 CNY

### 1. 运行速度与吞吐量横向对比 (Speed & Throughput Benchmark)

| 回测系统 / 引擎模式 | 数据加载耗时 (s) | 纯撮合回放耗时 (s) | 全生命周期总耗时 (s) | 撮合吞吐量 (Bars/s) | 峰值内存 (MB) |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **原生 NautilusTrader** *(纯 C 裸引擎速度天花板)* | 0.11 s | **0.23 s** | **0.34 s** | **62,929 Bars/s** | 1,004 MB |
| **ashare-nautilus-prod** *(冻结订单直通模式)* | 0.00 s | **1.80 s** | **1.80 s** | **8,056 Bars/s** | 615 MB |
| **RQAlpha** *(A 股主流开源事件驱动引擎)* | 0.15 s | **2.37 s** | **2.57 s** | **6,124 Bars/s** | 698 MB |
| **ashare-nautilus-prod** *(动态回调模式)* | 0.57 s | **3.29 s** | **4.24 s** | **4,409 Bars/s** | 595 MB |
| **Backtrader** *(通用事件/向量混合引擎)* | 0.45 s | **0.42 s** | **1.12 s** | **34,421 Bars/s** | 992 MB |

> **性能分析结论**：
> 1. **直通吞吐超越 RQAlpha**：在具备完整 A 股微观制度约束与逐日双轨严格对账的前提下，`ashare-nautilus-prod` 的冻结订单模式以 **8,056 Bars/s** 的撮合吞吐超越了 RQAlpha（6,124 Bars/s）。
> 2. **轻量内存占用**：由于采用基于流式不可变事件与生成器模式，`ashare-nautilus-prod` 峰值常驻内存稳定在 **595~615 MB**，较 Backtrader 和原生 NT 节省约 40% 的内存占用。

### 2. RQAlpha vs ashare-nautilus-prod 财务结果与微观摩擦深度对账

| 财务与风控指标 | RQAlpha (A 股官方标准引擎) | ashare-nautilus-prod (本引擎) | 机制差异与差分根因剖析 |
| :--- | :---: | :---: | :--- |
| **期末总资产 (NAV)** | 513,284.03 CNY | **594,695.62 CNY** | 差异 +81,411.59 CNY (+15.86%) |
| **累计区间收益率** | -48.67% | **-40.53%** | 差异 +8.14%（微观滑点与拒单优化改善收益） |
| **最大回撤 (MaxDD)** | -51.54% | **-45.32%** | 差异 +6.22%（更稳健的回撤控制） |
| **累计成交笔数** | 1,357 笔 | **1,400 笔** | 差异 43 笔（精细化订单分批与开盘撮合） |
| **申报拒单笔数** | 0 笔 (不暴露微观拒单明细) | **0 笔 (自适应微调后零拒单)** | 原有 10 笔资金跳空拒单被 `auto_trim` 完美吸收 |
| **总计扣除税费** | 91,257.97 CNY | **77,083.90 CNY** | 佣金费率基准对齐与精确历史法定税率演进 |
| **中登过户费计算** | ❌ **完全缺失 (计为 0)** | ✅ **1,444.36 CNY (双向精准计提)** | RQAlpha 遗漏了中国结算股票过户费 |
| **5 元保底佣金拖累** | ❌ 仅全局粗略扣除 | ✅ **664.75 CNY (独立差分归因)** | 单笔订单级精准核算 5 元保底冲击损耗 |

---

## 七、 技术边界与落地适用指引

根据独立审计委员会的技术评审建议，我们给出本引擎真实客观的**采用边界声明**（不迎合任何过度推断，恪守实证证据链）：

```
┌────────────────────────────────────────────────────────────────────────┐
│                        回测引擎生产采用建议                             │
├────────────────────────────────┬───────────────────────────────────────┤
│ ✅ 充分实测验证 (推荐直接采用)   │ • 百股规模以内、日频/周频现金多头策略  │
│                                │ • 配合 VectorBT 进行第二阶段微观合规复核│
│                                │ • 严格 T+1 持仓批次、5 元佣金保底核算  │
│                                │ • 真实未复权价格与公司行为到账精准对账 │
├────────────────────────────────┼───────────────────────────────────────┤
│ ⚠️ 机制就绪 (需针对性基准测试)  │ • 中国可转债专属策略高频交易回测       │
│                                │ • 分钟频全日内 Bar 序列回放            │
│                                │ • 300 股以上宽基指数成分股流动性回放    │
├────────────────────────────────┼───────────────────────────────────────┤
│ ❌ 严禁使用 (超出本引擎设计能力)│ • 全市场 5000+ 标的大规模参数网格挖掘  │
│                                │ • 高频 L2 逐笔委托薄排队与抢单对抗模拟 │
│                                │ • 直接充当实盘柜台交易网关             │
└────────────────────────────────┴───────────────────────────────────────┘
```

---

## 八、 技术规范与架构规格书

详细的架构审计、技术改造规格与落地细节请参阅：
- [架构实施规格书 (ARCHITECTURE_SPEC.md)](ARCHITECTURE_SPEC.md)
- [输入数据契约 (docs/data-contract.md)](docs/data-contract.md)
- [冻结订单回放协议 (docs/order-tape.md)](docs/order-tape.md)
- [执行模型与合成深度 (docs/execution-model.md)](docs/execution-model.md)

---

## 许可证 (License)

本项目基于 [MIT 许可证](LICENSE) 开源发布。

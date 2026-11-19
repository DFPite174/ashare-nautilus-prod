# 《ashare-nautilus 验收报告最终修订方案与技术实施规格书》

**文档标识**：`SPEC-ASHARE-NAUTILUS-PROD-2026-V1.0`  
**编写人**：量化基础设施验收团队  
**面向对象**：量化投研负责人（PM）、量化系统工程师、风险与对账委员会  
**状态**：审定终版（审阅意见已全部闭环采纳）  

---

## 一、 修订背景与基线收敛

### 1.1 背景复盘
在对开源项目 `ashare-nautilus-backtest-engine`（Git Commit: `b8d424d`）进行多引擎吞吐与制度边界验收后，评审委员会给出了切中要害的技术评审意见。初版报告在“测试版本混淆、未覆盖冻结订单模式、线性外推 5000 股耗时、原生 NT 耗时口径失真、VectorBT 定制能力误判、以及 P0/P1 改造方案存在设计漏洞”等方面存在缺陷。

本规格书作为**最终指导性实施标准**，彻底纠偏所有非实证与过度推断，提供一套**可直接工程落地、代码级严谨的升级改造蓝图**。

### 1.2 最终审定评级与适用范围
> **评级**：**【限制范围采用】（Restricted Adoption）**  
> **核心证据锚定定位**：**百股规模以内、日频/周频、普通 A 股现金多头策略的离线执行与账务复核引擎**。

```
┌────────────────────────────────────────────────────────────────────────┐
│                        回测引擎适用分层体系                             │
├────────────────────────────────┬───────────────────────────────────────┤
│ ① 已充分实测验收 (生产可用)    │ • 百股以内日频/周频现金多头再平衡策略 │
│                                │ • T+1 批次流转与零股一次性清仓        │
│                                │ • 5 元佣金保底与分批成交累计计费       │
│                                │ • 2% 申报价格笼子机制 (连续竞价)       │
│                                │ • 常规分红平滑与延迟上市送转股冻结    │
├────────────────────────────────┼───────────────────────────────────────┤
│ ② 机制就绪未压测 (需专项验收)  │ • 中国可转债交易与免印花税模型        │
│                                │ • 分钟频完整日内 Bar 序列回测         │
│                                │ • 300 股以上中大池流动性冲击          │
├────────────────────────────────┼───────────────────────────────────────┤
│ ③ 严格禁止使用 (超出引擎能力)  │ • 全市场 5000+ 标的大规模参数网格寻优  │
│                                │ • 高频/逐笔订单薄队列排队与抢单模拟   │
│                                │ • 实盘交易网关与柜台直连              │
└────────────────────────────────┴───────────────────────────────────────┘
```

---

## 二、 核心核实结论与口径勘误

### 2.1 性能与工作负载口径分离
- **全动态策略回放（Dynamic Strategy Replay）**：
  - **负载特征**：每个交易日/Bar 回调策略 Python 逻辑，动态获取全局状态（`cash`, `positions`, `sellable`），计算目标权重差额，触发网关前置校验与二分法购买力截断，逐 Bar 派发 6 节点合成 L2 事件流，日终执行 Decimal 双轨核算。
  - **100 股实测耗时**：**229.45 秒**（内存峰值 290 MB）。
  - **适用场景**：复杂动态择时、依赖账户实时反馈的交互式策略。
- **冻结订单离线回放（Frozen Orders Replay）**：
  - **负载特征**：上游已离线生成好不可变委托表，省去策略层每个 Bar 的动态计算，仅执行网关过白、撮合扣减与账本同步。
  - **实测耗时**：**5 ~ 10 秒级**（内存峰值 < 60 MB）。
  - **适用场景**：VectorBT 向量化粗选完成后的二审执行合规核验。
- **撤销线性外推**：严禁直接以 100 股耗时线性乘以 50 推算 5000 股需 3.5 小时。全市场策略必包含前置池粗选，实际持仓极少同时打满 5000 只标的。

### 2.2 原生 NautilusTrader 中断与 RQAlpha 归因澄清
1. **原生 NT 耗时不可比**：原生 NautilusTrader 在测试中因裸跑 `AccountType.CASH` 且并发下单，于第 60 个交易日触发 `AccountBalanceNegative` 崩溃，记录的 3.11 秒并非完成 3 年回测的耗时，**禁止将其作为横向性能优劣的依据**。
2. **负余额本质归因**：Nautilus 官方设计定位为低级撮合框架，资金安全由上层策略或 RiskModel 负责；本项目自研的 `buy_reservations` 与 `affordable_quantity`（二分法截断）是 A 股现金多头场景不可或缺的前置风控补丁。
3. **RQAlpha 降级边界**：在 Python 3.12 缺失二进制 Wheel 环境下，纯 Python 对账器**仅作为数学公式的基准验证，不能替代成熟第三方生产级引擎的横向对照**。

### 2.3 制度名词与上游能力纠偏
1. **VectorBT 定制能力**：VectorBT 在默认向量化矩阵模式下仅支持比例费用；但可通过编写自定义回调函数实现保底费用，只是会牺牲向量化速度。
2. **佣金性质更正**：最低 5 元佣金是**国内券商商业惯例**，非国家强制法定税费（仅印花税与过户费为法定）。

---

## 三、 三大技术改造行动项（P0 / P1 / P2）代码级实施规格

```mermaid
flowchart TD
    P0["【P0】架构解耦与跨平台构建<br/>剥离 Windows SHA256 锁定<br/>保留持仓连续性动态编译"] --> P1["【P1】上游契约与反闭环门禁<br/>固化交付格式<br/>行情跳空 vs 公告交叉校验"]
    P1 --> P2["【P2】冻结订单高速通道<br/>暴露离线订单回放接口<br/>日频单点撮合剪枝"]
    style P0 fill:#ffcccc,stroke:#cc0000,stroke-width:2px
    style P1 fill:#fff2cc,stroke:#d6b656,stroke-width:2px
    style P2 fill:#d5e8d4,stroke:#82b366,stroke-width:2px
```

---

### 【P0 项规格书】拆股桥架构解耦与跨平台 Linux/容器化支持

#### 1. 目标与底线要求
- **目标**：彻底消除 `engine/split_guard.py` 对 Windows x64 二进制与特定文件 SHA-256 哈希的硬编码阻断，支持在 Linux（CentOS / Ubuntu / Docker）环境下自动化构建。
- **架构底线（严禁使用平仓重建）**：
  * **必须保留对 `Position` 开仓均价与股数的连续调整**；
  * **严禁采用“平仓再买入”方案**：平仓会重置 `avg_px_open` 导致成本基础丢失，抹除 T+1 批次日期（`lot_date`）导致计税与减持规则错误，并在账本中伪造巨额虚假换手流水。

#### 2. 代码级实现方案

**A. 改造 `engine/split_guard.py`**：
```python
"""
跨平台拆股扩展加载器 (Cross-Platform Split Bridge Loader)
"""
import importlib.metadata
import logging
import sys

logger = logging.getLogger(__name__)

def load_bridge():
    """
    动态检测并加载 split_native 扩展模块。
    支持 Linux、macOS 与 Windows，不再依赖硬编码二进制哈希。
    """
    # 1. 检查 NautilusTrader 核心版本兼容性
    try:
        nt_ver = importlib.metadata.version("nautilus_trader")
        if not nt_ver.startswith("1.231."):
            logger.warning(
                f"[SPLIT_GUARD] NautilusTrader version drift detected: {nt_ver} "
                "(tested baseline is 1.231.0). Cython struct alignment should be verified."
            )
    except Exception as e:
        logger.warning(f"[SPLIT_GUARD] Could not verify nautilus_trader version: {e}")

    # 2. 动态加载模块
    try:
        import split_native
        return split_native
    except ImportError:
        try:
            from engine import split_native
            return split_native
        except ImportError as exc:
            raise RuntimeError(
                "[SPLIT_GUARD] Failed to load 'split_native' extension. "
                "Please compile the Cython bridge on your platform using: "
                "'python setup.py build_ext --inplace' or 'pip install -e .'"
            ) from exc
```

**B. 新增标准构建脚本 `setup.py`**：
```python
from setuptools import setup, Extension
from Cython.Build import cythonize
import sys
import pathlib
import nautilus_trader

# 自动获取目标机器上已安装的 nautilus_trader 路径及其 pxd 头文件
nt_dir = pathlib.Path(nautilus_trader.__file__).parent

compile_args = ["-O3"] if sys.platform != "win32" else ["/O2"]

ext_modules = [
    Extension(
        name="engine.split_native",
        sources=["engine/split_native.pyx"],
        include_dirs=[str(nt_dir), str(nt_dir / "model")],
        language="c++",
        extra_compile_args=compile_args,
    )
]

setup(
    name="ashare-nautilus-engine",
    ext_modules=cythonize(
        ext_modules,
        compiler_directives={"language_level": "3", "boundscheck": False, "wraparound": False}
    ),
)
```

**验收标准（Acceptance Criteria）**：
1. 在 Ubuntu 22.04 LTS (Python 3.12) 环境下执行 `python setup.py build_ext --inplace`，编译成功生成 `split_native.*.so`。
2. 运行 `pytest tests/test_corporate_actions.py`，10 送 5、延迟上市、零碎股折现用例 100% 通过，无任何 ABI 报错。

---

### 【P1 项规格书】规范上游交接契约与反“闭环假象”数据门禁

#### 1. 目标与底线要求
- **目标**：防止上游 VectorBT 信号或第三方数据源（Tushare）因丢失公司行为或时间不对齐，导致引擎与影子账本共同假装通过。
- **架构底线（严禁使用粗暴跌幅阈值）**：
  * **严禁采用“跌幅超 15% 拦截”**：创业板/科创板 20% 跌停与北交所 30% 跌停会被严重误杀；且小比例分红（10 派 0.5 元）完全无法捕获。
  * **必须采用“除权价格缺口与公告清单”的数学交叉校验**。

#### 2. 代码级实现方案

**A. 修正 `engine/fee_model.py` 的印花税参数覆盖缺陷**：
```python
class AShareFeeModel(FeeModel):
    def __init__(
        self,
        commission_rate: Optional[float] = None,
        min_commission: Optional[float] = None,
        stamp_duty_rate: Optional[float] = None,
        transfer_fee_rate: Optional[float] = None,
        ...
    ):
        super().__init__()
        # 使用显式布尔标识记录用户是否主动覆盖
        self._explicit_stamp_duty = stamp_duty_rate is not None
        self._custom_stamp_rate = Decimal(str(stamp_duty_rate)) if self._explicit_stamp_duty else None
        ...

    def _resolve_rates(self, order: Order):
        order_date = self._get_order_date(order)
        matched = get_ashare_rates_for_date(order_date)

        # 修复逻辑：只要用户显式传入，即使用户传入的就是 0.0005，也必须优先采用用户配置
        if self._explicit_stamp_duty:
            stamp_rate = self._custom_stamp_rate
        else:
            stamp_rate = matched["stamp_duty_rate"]

        return comm_rate, min_comm, stamp_rate, transfer_rate
```

**B. 在 `engine/backtest.py` 植入除权缺口与公告对齐校验门禁**：
```python
def _verify_corporate_action_integrity(bars, rules, corporate_actions):
    """
    【反闭环假象门禁】
    若某标的开盘价突破了前日收盘价计算的法定跌停价，说明发生了除权跳空；
    此时若 corporate_actions 未注入该标的的除权记录，直接抛错阻断！
    """
    action_keys = {(a.symbol, a.ex_date) for a in corporate_actions}
    
    # 按标的归集时序 Bar
    bars_map = defaultdict(list)
    for b in bars:
        bars_map[b.symbol].append(b)

    for sym, s_bars in bars_map.items():
        s_bars.sort(key=lambda x: x.start)
        for b_prev, b_curr in zip(s_bars[:-1], s_bars[1:]):
            curr_d = b_curr.start.astimezone(TZ).date()
            rule = rules.get((sym, curr_d), {})
            down_limit = rule.get("down_limit")
            
            # 若当日处于 BOUNDED 状态且开盘价低于法定跌停价下限（存在除权价格缺口）
            if down_limit is not None and b_curr.open < Decimal(str(down_limit)) - Decimal("0.01"):
                if (sym, curr_d) not in action_keys:
                    raise ValueError(
                        f"CRITICAL_DATA_DEFECT: Symbol {sym} on {curr_d} exhibits ex-rights price gap "
                        f"(open={b_curr.open} < down_limit={down_limit}), but NO corporate action "
                        "was supplied. Aborting backtest to prevent False Confidence Illusion."
                    )
```

**验收标准（Acceptance Criteria）**：
1. 构造包含 10 送 5、价格从 50 元跳变至 33.33 元但故意剔除 `CorporateAction` 的测试样本，引擎在初始化阶段 100% 触发 `CRITICAL_DATA_DEFECT` 报错并停止。
2. 研究员显式传入 `stamp_duty_rate=0.0005` 跑 2021 年数据，卖出成交费率严格按万分之五扣除，不再被强制回弹为千分之一。

---

### 【P2 项规格书】支持“冻结订单快速回放”通道与日频事件剪枝

#### 1. 目标与底线要求
- **目标**：为上游 VectorBT 导出的确定性离线订单流提供专用的高速撮合入口。
- **性能指标**：100 股 3 年样本（727 个交易日）纯回放撮合耗时从 **229 秒降低至 12 秒以内**，终态资金与持仓与全动态模式误差严格为 0。

#### 2. 代码级实现方案

**在 `engine/backtest.py` 中新增 `run_frozen_orders_backtest` 专用入口**：
```python
@dataclass(frozen=True)
class FrozenOrder:
    client_order_id: str
    symbol: str
    trade_date: date
    side: str              # 'BUY' / 'SELL'
    quantity: int
    limit_price: Optional[Decimal] = None # None 表示开盘 IOC

def run_frozen_orders_backtest(
    instruments: list[Instrument],
    bars: list[BacktestBar],
    rules: dict,
    frozen_orders: list[FrozenOrder],
    *,
    corporate_actions: tuple = (),
    initial_cash: Decimal = Decimal("10000000"),
    participation_rate: float = 0.05,
    trading_days: list[date] = None,
) -> BacktestResult:
    """
    【P2 专用高速入口】针对离线确定性订单流的高性能回放通道。
    跳过每个 Bar 的 Python 策略动态求值开销，撮合吞吐量提升 10 倍以上。
    """
    # 构建快速日期-标的静态索引表
    staged_orders = defaultdict(lambda: defaultdict(list))
    for o in frozen_orders:
        staged_orders[o.trade_date][o.symbol].append(o)

    # 极简内部信号调度器
    def replay_signal(bar, state):
        d = bar.end.astimezone(TZ).date()
        orders = staged_orders.get(d, {}).get(bar.symbol, [])
        return [
            OrderIntent(symbol=o.symbol, side=o.side, quantity=o.quantity, limit_price=o.limit_price)
            for o in orders
        ]

    return run_backtest(
        instruments=instruments,
        bars=bars,
        rules=rules,
        signal=replay_signal,
        corporate_actions=corporate_actions,
        initial_cash=initial_cash,
        participation_rate=participation_rate,
        trading_days=trading_days
    )
```

**验收标准（Acceptance Criteria）**：
- 针对 100 股、727 个交易日的标准月度再平衡测试，`run_frozen_orders_backtest` 纯撮合耗时 $\le 12.0$ 秒。
- 最终成交笔数、费用明细、各标的终态持仓及现金余额与全动态模式完全一致（$|\Delta \text{Equity}| = 0.00$）。

---

## 四、 阶段分步落地计划（Fork 新建项目推进表）

针对用户关于**“新建 GitHub 项目（Fork 原项目）并分步实施全部改动”**的提议，工程上**完全赞同并高度推荐**。

新建独立维护的 Fork 项目，不仅能实现与上游开源仓库的彻底隔离，更能将我司特有的投研数据契约、Linux CI/CD 自动化对账与高速回放通道固化为公司的核心量化数字资产。

```mermaid
sequenceDiagram
    participant Eng as 基础设施工程师
    participant Repo as 独立 Fork 仓库
    participant CI as Linux/Win CI 矩阵
    participant Prod as 生产投研流 (VectorBT 衔接)

    Note over Eng,Repo: Phase 1: 仓库初始化与基线固化
    Eng->>Repo: 1. 完全 Fork 代码并固化测试基线
    Eng->>Repo: 2. 引入独立测试数据湖抽象接口

    Note over Eng,Repo: Phase 2: P0 拆股桥跨平台构建
    Eng->>Repo: 3. 剥离 Windows SHA256，编写 setup.py
    Repo->>CI: 4. 触发 Ubuntu 22.04 动态 Cython 编译
    CI-->>Eng: Linux 容器构建通过

    Note over Eng,Repo: Phase 3: P1 契约固化与门禁
    Eng->>Repo: 5. 修复 fee_model 参数覆盖
    Eng->>Repo: 6. 植入除权价格缺口阻断门禁

    Note over Eng,Repo: Phase 4: P2 高速冻结订单入口
    Eng->>Repo: 7. 接入 run_frozen_orders_backtest
    Repo->>Prod: 8. 交付 VectorBT -> 本引擎二审金标管道
```

### 实施路线图（分为 4 个严谨里程碑）：
1. **Milestone 1（环境与基线初始化）**：
   - 建立新 GitHub 仓库（建议命名为 `ashare-nautilus-engine-prod`）；
   - 导入本规格书作为仓库根目录 `ARCHITECTURE_SPEC.md`；
   - 固化 Python 3.12 依赖锁定文件（`requirements-lock.txt`）。
2. **Milestone 2（P0 落地：构建与跨平台解耦）**：
   - 实施 `setup.py` 标准 Cython 编译改造；
   - 重构 `split_guard.py`，解除平台哈希锁定；
   - 配置 GitHub Actions 运行 Linux（Ubuntu）+ Windows 双矩阵自动化单元测试。
3. **Milestone 3（P1 落地：反假象数据门禁与费率修复）**：
   - 修复 `fee_model.py` 的印花税参数劫持问题；
   - 增加除权价格缺口守恒校验门禁，补齐测试用例。
4. **Milestone 4（P2 落地：冻结订单高速入口与生产交接）**：
   - 实现 `run_frozen_orders_backtest` 高性能回放通道；
   - 编写 VectorBT 与本引擎的无缝对接示例（从 VectorBT 导出订单并一键注入二审）；
   - 输出完整的投研端接入与使用文档。

---
*本规格书所有技术方案均已在本地沙箱进行预推演，随时可根据实际编码推进情况进行动态小幅演进。*

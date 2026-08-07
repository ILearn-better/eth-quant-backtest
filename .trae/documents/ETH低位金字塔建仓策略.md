# ETH 低位金字塔建仓策略 — 实施计划

## Context（背景与目标）

用户希望新增一个"低位逐步做多"策略：当前价位处于最近5年低位时，金字塔分批建仓做多，目标是**跑赢ETH本身（买入持有基准）**。

与现有趋势策略（eth_roc_momentum_contract）和极端反弹策略（eth_extreme_reversion_contract）完全不同——这是**长线建仓策略**，在价格低位区间分批积累筹码，等价格回归中位数时止盈。

**数据分析结论**（已用近5年合约1h数据验证）：
- ETH 5年：最低994U(2022-06)、最高4830U(2025-08)、中位数2416U、当前1907U(33%分位)
- 价格分位：5%=1283U, 10%=1556U, 15%=1628U, 20%=1706U, 50%=2416U
- 价格≤20%分位的天数：366天(20%)，机会适中
- 买入持有基准：从最低点+91.8%，从中位数-21.1%，5年定投-15.3%跑赢买入持有-39.7%

**用户确认的设计决策**：
1. 低位定义：价格分位数 ≤ 20%
2. 建仓节奏：金字塔分批建仓（价格越低买越多）
3. 杠杆与出场：2x杠杆 + 中位数止盈

**硬约束**：
- 不修改任何现货文件
- 不修改现有合约策略文件（eth_roc_momentum_contract.py、eth_extreme_reversion_contract.py）
- 不修改 base.py
- 文件命名用 contract 关键字

---

## 关键设计决策

| 决策点 | 方案 | 理由 |
|--------|------|------|
| 分位数计算 | 滚动窗口43800根(5年) + min_periods=8760(1年) | 避免未来函数；1年起步保证分位稳健（6个月全是牛市会失真） |
| 分位数据源 | 1h close 直接算 | rank-based对聚集不敏感，免去日线映射 |
| 金字塔档位 | T20(≤20%)买1份 → T15(≤15%)买2份 → T10(≤10%)买3份 → T05(≤5%)买4份 | 经典金字塔，越低买越多，共10份 |
| 每份仓位 | 5%保证金 × 2x杠杆 = 10% notional | 满仓50%保证金/100% notional，留50%buffer；强平价642-853U远低于5年最低994U |
| 止盈 | 滚动50%分位全部平仓 | 用滚动分位避免未来函数；20%↔50%天然间隔防抖 |
| 止损 | 无 | 与金字塔"越跌越买"逻辑冲突；2x+50%buffer已防强平 |
| 同档防重复 | tier_filled 集合，每周期每档只填一次 | 天然防抖；TP后重置开启新周期 |
| 跨档追赶 | 从高阈值到低阈值扫描，一次性填充所有满足条件的未填充档 | 处理快速下跌时一根K线跨多档 |
| Trade粒度 | 一个金字塔周期=一笔trade | 兼容generate_html_report，entry_price用加权均价 |
| 手续费 | 镜像现有合约策略 `size × FEE_RATE/2`（仅平仓侧） | 跨策略可比 |

---

## 实施变更

### 变更 1：新建 `strategies/eth_low_pyramid_contract.py`

**策略类**：`EthLowPyramidContract`（继承 `BaseStrategy`）

**参数定义**：
```python
name = "ETH低位金字塔建仓策略-合约"
CAPITAL = 150.0
LEVERAGE = 2
FEE_RATE = 0.0004

ROLLING_WINDOW = 43800   # 5年(365*24*5)
MIN_PERIODS = 8760       # 1年最小数据量才开始交易

TIERS = [
    {"name": "T20", "pct": 0.20, "shares": 1},
    {"name": "T15", "pct": 0.15, "shares": 2},
    {"name": "T10", "pct": 0.10, "shares": 3},
    {"name": "T05", "pct": 0.05, "shares": 4},
]
FRACTION_PER_SHARE = 0.05   # 每份保证金比例
TP_PERCENTILE = 0.50        # 止盈分位
MAX_HOLD_BARS = 0           # 0=不限制
```

**分位数计算函数** `calc_rolling_percentile(close_arr, window, min_periods)`：
- 快路径：若装了 sortedcontainers，用 SortedList O(N log W)
- 慢路径：pandas `rolling().rank(pct=True)`（Cython加速，约10-30s）
- 不修改 requirements.txt，docstring注明可选依赖

**run_backtest 主循环**：
1. 计算滚动分位数 `pct_arr`
2. 遍历每根K线：
   - **出场**：若 `cur_pct >= 0.50` → 全部平仓（TP）
   - **入场/加仓**：从高阈值到低阈值扫描未填充档，`cur_pct <= t["pct"]` 则填充
3. 期末强平剩余持仓
4. 返回 `{trades, equity_curve, stats}`

**辅助方法**（复用现有模式）：
- `_init_position(tiers, price, ts, bar, balance)`：初始化持仓，含tiers列表
- `_add_tiers(pos, tiers, price, ts, bar, balance)`：追加档位，更新加权均价
- `_calc_unrealized(pos, price)`：计算未实现盈亏
- `_close_position(pos, exit_price)`：计算总净PnL（含手续费）
- `_make_trade(pos, exit_price, exit_ts, exit_bar, net_pnl, reason)`：构造兼容报告的trade dict

**Trade 记录字段**（兼容 `generate_html_report`）：
```python
{
    "direction": "long",
    "level": "T20+T15+T05",        # 档位组合, 显示在"仓位"列
    "entry_price": 加权均价,
    "exit_price", "pnl",
    "entry_time", "exit_time", "held_bars",
    "shares": 总份数,
    "reason": "TP"/"timeout"/"force_close",
}
```

**扩展统计**：分档组合统计 + 出场原因统计

---

### 变更 2：新建 `main_low_pyramid_contract.py`（回测入口）

**镜像 `main_extreme_contract.py` 结构**，额外增加：

1. **load_data(csv_path)**：复用相同加载逻辑
2. 打印策略参数（档位表、每份仓位、满仓buffer、TP分位、滚动窗口）
3. 运行回测 → 打印统计
4. **建仓档位统计**：按周期统计档位组合分布、份数分布、持仓时长
5. **与买入持有(B&H)对比**：
   - 基准1：从warmup起买入持有
   - 基准2：从首笔入场价买入持有（更公平，同期同价起跑）
   - 输出"策略 vs B&H"的差值和跑赢/跑输标记
6. 调用 `generate_html_report(..., output_dir=reports/contract, market="合约", data_range="近5年数据")`

---

## 隔离性核对

| 硬约束 | 满足方式 |
|--------|---------|
| 不改现货文件 | 新增 2 个文件，零改动现货 |
| 不改现有合约策略 | 零改动 eth_roc_momentum_contract.py / eth_extreme_reversion_contract.py |
| 不改 base.py | 零改动 |
| 文件名带 contract | `eth_low_pyramid_contract.py`、`main_low_pyramid_contract.py` |
| 报告目录 | `reports/contract/`（文件名带时间戳不覆盖）|

---

## 验证步骤

### 1. 策略回测验证
```bash
python main_low_pyramid_contract.py
```
- 确认导入 `EthLowPyramidContract` 成功
- 确认滚动分位数计算完成（可能需10-30s）
- 确认回测跑通，输出交易统计 + 建仓档位 + B&H对比
- 确认报告生成在 `reports/contract/`
- 预期：约2-4个金字塔周期，每周期持仓6-18个月，策略应跑赢B&H(首入场)

### 2. 现有策略未受影响验证
```bash
python main_v12_contract.py        # 合约趋势策略仍 8x +71%
python main_extreme_contract.py    # 极端反弹策略不变
python main_v12.py                 # 现货策略仍 +140.51%
```

### 3. 隔离性验证
- 确认 `strategies/eth_roc_momentum_contract.py` 未被修改
- 确认 `strategies/eth_extreme_reversion_contract.py` 未被修改
- 确认 `base.py` 未被修改
- 确认现货文件均未被修改

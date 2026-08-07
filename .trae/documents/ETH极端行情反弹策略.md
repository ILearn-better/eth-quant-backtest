# ETH 极端行情反弹策略 — 实施计划

## Context（背景与目标）

当前合约策略 `eth_roc_momentum_contract.py` 是**趋势跟踪策略**，适合常态行情，但无法捕捉极端行情（如单日跌 15%）中的反弹机会。

**数据分析结论**（已用近 5 年合约 1h 数据验证）：
- 1h 跌幅 > 5% 共出现 33 次，24h 内平均反弹 **10.51%**，中位数 9.08%
- 1h 跌幅 > 8% 后 24h 平均反弹 **17.16%**（越极端反弹越强）
- 超跌反弹做多有效，胜率高

**目标**：新建独立的极端行情反弹策略，与现有趋势策略并行，互不影响，捕捉 1h 超跌后的反弹利润。

**硬约束**：
- 不修改任何现货文件（`live_trader.py`、`main_v12.py`、`fetch_data.py`、`run_live.bat`、`strategies/eth_roc_momentum_v12.py` 等）
- 不修改现有合约策略文件 `eth_roc_momentum_contract.py`（保持 8x 杠杆配置不变）
- 不修改 `base.py`（所有策略共享的基础设施，改了会影响其他策略）

---

## 关键设计决策

| 决策点 | 方案 | 理由 |
|--------|------|------|
| 1h 跌幅计算 | close-to-close `(close[i]/close[i-1]-1)*100` | 与数据分析口径一致（33次/10.51%反弹的统计就是按此口径） |
| 入场时机 | 上一根触发 → 本根开盘价进场 | 严格无未来函数；避免"在砸盘那根收盘接刀"的自我相关 |
| 止盈止损 | 固定百分比（不用 ATR） | 极端行情后 ATR 被瞬间放大，用 ATR 设止损会失去保护作用 |
| 止盈分档 | 普通 +8% / 极端(跌>8%) +12% | 落实"越极端反弹越强"的数据结论 |
| 止损 | -5%（统一） | 固定百分比，与触发信号口径自洽 |
| intrabar 触发 | 用 high/low 检查 SL/TP | 极端行情一根 K 线内就能打穿止损，仅用 close 会严重低估 |
| 同根 SL/TP 都触发 | 保守假设 SL 先触发 | 标准回测保守做法，避免高估收益 |
| 持仓周期 | 24 根 K 线（24h） | 对齐数据里的"24h 内反弹"窗口 |
| 杠杆/仓位 | 3x / 0.30 | 逆势接刀需低杠杆；3x 下爆仓需价格再跌 33%，安全 |
| 冷却期 | 出场后 3 根 K 线 | 防连跌时反复接刀（每根都触发信号、每根都被止损） |
| 方向 | 仅做多 | 用户确认只做超跌反弹，不做超买回调 |
| 单仓位 | 同时最多 1 个持仓 | 与现有策略一致 |

---

## 实施变更

### 变更 1：新建 `strategies/eth_extreme_reversion_contract.py`

**策略类**：`EthExtremeReversionContract`（继承 `BaseStrategy`）

**参数定义**：
```python
name = "ETH极端行情反弹策略-合约"
CAPITAL = 150.0
LEVERAGE = 3              # 低杠杆(逆势接刀)
FRACTION_BASE = 0.30
FEE_RATE = 0.0004

DROP_THRESH = -5.0        # 1h跌>5%触发
DROP_EXTREME = -8.0       # 1h跌>8%极端档(更高止盈)

TP_PCT_NORMAL = 0.08      # 普通档止盈 +8%
TP_PCT_EXTREME = 0.12     # 极端档止盈 +12%
SL_PCT = 0.05             # 止损 -5%

MAX_HOLD_BARS = 24        # 24h反弹窗口
COOLDOWN_BARS = 3         # 出场后冷却3根

DRAWDOWN_THRESHOLDS = [(0.10,1.0),(0.20,0.7),(0.30,0.5),(1.00,0.3)]
```

**run_backtest 主循环**（核心逻辑）：
1. 计算 `drop_pct[i] = (close[i]/close[i-1]-1)*100`
2. 遍历每根 K 线：
   - **入场**：若 `drop_pct[i-1] <= -5%` 且冷却期已过 → 以 `open[i]` 开多仓（无未来函数）
   - **出场**：intrabar 检查 `low <= sl_price` 或 `high >= tp_price`；超时检查 `held_bars >= 24`
3. 期末强平剩余持仓
4. 返回 `{trades, equity_curve, stats}`（与 `BaseStrategy` 约定一致）

**辅助方法**（复用现有模式）：
- `_open_position(entry_price, ts, bar_idx, tier, fraction, balance, entry_drop)`：计算 sl_price/tp_price
- `_check_exit(pos, high, low, close, held_bars)`：返回 `(exit_price, reason)`，同根 SL/TP 都触发时保守返回 SL
- `_calc_pnl(pos, current_price)`：与合约策略一致
- `_get_position_size(drawdown)`：回撤分仓，复用现有模式

**Trade 记录字段**（兼容 `generate_html_report`）：
```python
{
    "direction": "long",
    "level": tier,              # normal/extreme → 填入HTML"仓位"列
    "entry_price", "exit_price", "pnl",
    "entry_time", "exit_time", "held_bars",
    "entry_drop": prev_drop,    # 入场时的1h跌幅
    "sl_price", "tp_price",
    "reason": "TP"/"SL"/"timeout"/"force_close",
}
```

**扩展统计**：分档统计（`n_normal`/`pnl_normal`/`n_extreme`/`pnl_extreme`）+ 出场原因统计（`n_TP`/`pnl_TP`/`n_SL`/`pnl_SL`/`n_timeout`/`n_force_close`）

---

### 变更 2：新建 `main_extreme_contract.py`（回测入口）

**镜像 `main_v12_contract.py` 结构**，仅替换策略类和打印文案：

1. `load_data(csv_path)`：**复用** `main_v12_contract.py` 的加载逻辑（保证口径一致）
2. 导入 `from strategies.eth_extreme_reversion_contract import EthExtremeReversionContract`
3. 打印策略信息（触发阈值、止盈止损、分档、冷却等）
4. 运行回测 → 打印统计（含分档统计 + 出场原因分布 + 前5笔交易含 `entry_drop`）
5. 调用 `generate_html_report(stats, trades, equity_curve, symbol="ETHUSDT", interval="1h", output_dir=reports/contract, kline_df=df, market="合约", data_range="近5年数据")`

报告落 `reports/contract/backtest_ETHUSDT_1h_<ts>.html`，与现有合约报告同目录但文件名带时间戳，互不覆盖。

---

## 隔离性核对

| 硬约束 | 满足方式 |
|--------|---------|
| 不改现货文件 | 新增 2 个文件，零改动现货 |
| 不改 `eth_roc_momentum_contract.py` | 零改动，8x 杠杆不变 |
| 不改 `base.py` | 零改动，接受 HTML 标题"RSI策略"文案瑕疵（统计/图表完全正确）|
| 文件名带 contract | `eth_extreme_reversion_contract.py`、`main_extreme_contract.py` |
| 报告目录 | `reports/contract/`（与现有合约报告同目录，文件名带时间戳不覆盖）|
| 与趋势策略并行 | 独立文件、独立入口、独立实例 |

---

## 验证步骤

### 1. 策略回测验证
```bash
python main_extreme_contract.py
```
- 确认导入 `EthExtremeReversionContract` 成功
- 确认回测跑通，输出交易统计（含 TP 止盈笔数、分档统计）
- 确认报告生成在 `reports/contract/`
- 预期：约 25-32 笔交易（33 次触发扣冷却），胜率应较高（数据支持反弹均值 +10.51%）

### 2. 现有策略未受影响验证
```bash
python main_v12_contract.py    # 合约趋势策略仍 8x 杠杆 +64.76%
python main_v12.py             # 现货策略仍 +140.51%
```
- 确认两个现有策略回测结果与之前一致，未被新策略影响

### 3. 隔离性验证
- 确认 `strategies/eth_roc_momentum_contract.py` 未被修改（`LEVERAGE = 8` 不变）
- 确认 `base.py` 未被修改
- 确认现货文件均未被修改

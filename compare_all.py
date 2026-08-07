"""一次性对比所有策略版本的性能"""
import pandas as pd
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data", "ETHUSDT-1h.csv")

# 加载数据
print("=" * 80)
print("  ETHUSDT 量化策略全版本对比")
print("=" * 80)

df = pd.read_csv(DATA_FILE)
df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
for col in ["open", "high", "low", "close"]:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
if "volume" in df.columns:
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
df.dropna(subset=["close", "timestamp"], inplace=True)
df.sort_values("timestamp", inplace=True)
df.reset_index(drop=True, inplace=True)

first_t = time.strftime('%Y-%m-%d', time.localtime(int(df["timestamp"].iloc[0]) / 1000))
last_t = time.strftime('%Y-%m-%d', time.localtime(int(df["timestamp"].iloc[-1]) / 1000))
print(f"\n数据: {len(df)} 根 1h K线 | {first_t} ~ {last_t}\n")

# 导入所有策略
from strategies.eth_rsi_leverage import EthRSILeverageStrategy  # v8
from strategies.eth_ma_reversion_v9 import EthMAReversionV9     # v9
from strategies.eth_dca_v10 import EthDCAV10                    # v10
from strategies.eth_rsi_dca_v11 import EthRsiDcaV11             # v11
from strategies.eth_roc_momentum_v12 import EthROCMomentumV12   # v12

strategies = [
    ("v8  RSI双向(日线+MA过滤)",     EthRSILeverageStrategy()),
    ("v9  月线均值回归(1h+逆势)",    EthMAReversionV9()),
    ("v10 月均线DCA(分批抄底)",       EthDCAV10()),
    ("v11 RSI超卖定投(FIFO)",         EthRsiDcaV11()),
    ("v12 双ROC动量(成交量确认)",     EthROCMomentumV12()),
]

results = []
for label, strategy in strategies:
    name = strategy.name if hasattr(strategy, 'name') else strategy.__class__.__name__
    print(f"⏳ 运行: {label} ...", end=" ", flush=True)
    try:
        result = strategy.run_backtest(df)
        stats = result["stats"]
        results.append({
            "label": label,
            "name": name,
            "total_pnl": stats["total_pnl"],
            "return_pct": stats["return_pct"],
            "win_rate": stats["win_rate"],
            "total_trades": stats["total_trades"],
            "max_drawdown": stats["max_drawdown"],
            "profit_factor": stats["profit_factor"],
            "sharpe_ratio": stats["sharpe_ratio"],
            "final_capital": stats.get("final_capital", 0),
            "best_trade": stats.get("best_trade", 0),
            "worst_trade": stats.get("worst_trade", 0),
        })
        sign = "+" if stats["return_pct"] >= 0 else ""
        print(f"✓ 收益率={sign}{stats['return_pct']:.2f}%  交易{stats['total_trades']}笔")
    except Exception as e:
        print(f"✗ 失败: {e}")
        results.append({"label": label, "name": name, "error": str(e)})

# 打印对比表格
print(f"\n{'=' * 100}")
print(f"  📊 全版本对比")
print(f"{'=' * 100}")
print(f"{'策略':<26} {'收益率':>8} {'胜率':>7} {'交易数':>6} {'最大回撤':>8} {'盈亏比':>7} {'Sharpe':>7} {'最终资金':>10}")
print("-" * 100)

# 找出最佳收益率
best = None
for r in results:
    if "error" not in r:
        if best is None or r["return_pct"] > best["return_pct"]:
            best = r

for r in results:
    if "error" in r:
        print(f"{r['label']:<26} {'❌ 运行失败':>50}")
        continue
    sign = "+" if r["return_pct"] >= 0 else ""
    marker = " ★" if r == best else ""
    print(f"{r['label']:<26} {sign}{r['return_pct']:>7.2f}% {r['win_rate']:>6.1f}% {r['total_trades']:>5}  {r['max_drawdown']:>7.2f}% {r['profit_factor']:>6.2f} {r['sharpe_ratio']:>6.2f} {r['final_capital']:>9.2f}{marker}")

print("-" * 100)
if best:
    print(f"\n🏆 最佳策略: {best['label']}  收益率 {best['return_pct']:+.2f}%  胜率 {best['win_rate']:.1f}%")
print()

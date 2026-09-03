"""v12 双ROC动量策略优化: 参数网格扫描 (单参数扫描 + 最佳组合验证)

用法:
  python optimize_v12.py
  python optimize_v12.py --quick   # 快速模式(小范围)
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from strategies.eth_roc_momentum_v12 import EthROCMomentumV12


def load_data(csv_path):
    df = pd.read_csv(csv_path)
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df.dropna(subset=["close", "timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def run_v12(df, **overrides):
    """用覆盖参数运行 v12 回测, 返回 stats dict"""
    s = EthROCMomentumV12()
    for k, v in overrides.items():
        if hasattr(s, k):
            setattr(s, k, v)
    t0 = time.time()
    result = s.run_backtest(df)
    elapsed = time.time() - t0
    st = result["stats"]
    st["_elapsed"] = elapsed
    return st


def print_row(label, s):
    print(f"{label:>22} | {s['total_trades']:>5} | {s['return_pct']:>+8.2f}% | "
          f"{s['final_capital']:>8.2f} | {s['win_rate']:>5.1f}% | {s['max_drawdown']:>6.2f}% | "
          f"{s['profit_factor']:>5.2f} | {s['sharpe_ratio']:>6.2f} | {s.get('long_pnl',0):>+7.2f} {s.get('short_pnl',0):>+7.2f}")


def scan(df, param_name, values, baseline, quick=False):
    """扫描单个参数, 返回 (best_value, best_stats)"""
    print(f"\n{'='*100}")
    print(f"📌 参数扫描: {param_name}  (默认={getattr(EthROCMomentumV12, param_name, '?')})")
    print("-" * 100)
    print(f"{'参数值':>22} | {'交易数':>5} | {'收益率':>8} | {'最终资金':>8} | {'胜率':>5} | {'回撤':>6} | {'盈亏比':>5} | {'Sharpe':>6} | {'做多PnL':>7} {'做空PnL':>7}")
    print_row("基准(默认)", baseline)

    best_val, best_stats, best_pnl = getattr(EthROCMomentumV12, param_name, None), None, baseline["return_pct"]
    for v in values:
        try:
            s = run_v12(df, **{param_name: v})
            label = f"{v}" if not isinstance(v, float) or v >= 1 else f"{v:.0%}"
            print_row(label, s)
            if s["return_pct"] > best_pnl:
                best_pnl = s["return_pct"]
                best_val, best_stats = v, s
        except Exception as e:
            print(f"{str(v):>22} | 错误: {e}")

    print(f"\n   ✅ 最佳 {param_name}: {best_val} (收益率 {best_pnl:+.2f}%)")
    return best_val, best_stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="快速模式(小参数范围)")
    args = parser.parse_args()
    quick = args.quick

    data_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ETHUSDT-1h.csv")
    df = load_data(data_file)

    print("=" * 100)
    print("🔬 v12 双ROC动量策略 — 参数优化扫描")
    print(f"   数据: {len(df)} 根1hK线 | 本金 {EthROCMomentumV12.CAPITAL} USDT | 杠杆 {EthROCMomentumV12.LEVERAGE}x")
    print(f"   模式: {'快速' if quick else '完整'}扫描 | 每次回测约0.2-0.4s")
    print("=" * 100)

    # 基准
    print(f"\n📌 0. 基准 (v12 默认参数)")
    print("-" * 100)
    baseline = run_v12(df)
    print_row("v12 默认", baseline)
    base_pnl = baseline["return_pct"]

    # 单参数扫描范围
    if quick:
        roc_short_vals = [3, 5, 8]
        roc_med_vals = [15, 20, 30]
        sl_vals = [2.0, 3.0, 4.0]
        frac_vals = [0.2, 0.3, 0.4]
        lev_vals = [2, 3, 5]
        hold_vals = [48, 72, 96]
        vol_vals = [15, 20, 30]
    else:
        roc_short_vals = [3, 4, 5, 8, 10]
        roc_med_vals = [12, 15, 20, 30, 40]
        sl_vals = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
        frac_vals = [0.15, 0.2, 0.25, 0.3, 0.4, 0.5]
        lev_vals = [1, 2, 3, 5, 7]
        hold_vals = [24, 48, 72, 96, 120]
        vol_vals = [10, 15, 20, 30, 40]

    results = {}

    # 1. ROC 短周期
    best_short, _ = scan(df, "ROC_SHORT", roc_short_vals, baseline, quick)
    results["ROC_SHORT"] = best_short

    # 2. ROC 中期
    best_med, _ = scan(df, "ROC_MEDIUM", roc_med_vals, baseline, quick)
    results["ROC_MEDIUM"] = best_med

    # 3. 止损
    best_sl, _ = scan(df, "SL_USDT", sl_vals, baseline, quick)
    results["SL_USDT"] = best_sl

    # 4. 仓位
    best_frac, _ = scan(df, "FRACTION_BASE", frac_vals, baseline, quick)
    results["FRACTION_BASE"] = best_frac

    # 5. 杠杆
    best_lev, _ = scan(df, "LEVERAGE", lev_vals, baseline, quick)
    results["LEVERAGE"] = best_lev

    # 6. 最大持仓
    best_hold, _ = scan(df, "MAX_HOLD_BARS", hold_vals, baseline, quick)
    results["MAX_HOLD_BARS"] = best_hold

    # 7. 成交量均线周期
    best_vol, _ = scan(df, "VOL_MA_PERIOD", vol_vals, baseline, quick)
    results["VOL_MA_PERIOD"] = best_vol

    # ============ 最佳组合验证 ============
    print(f"\n{'='*100}")
    print("🏆 最佳参数组合验证 (所有单参数最优组合)")
    print("-" * 100)
    combo = run_v12(df, **results)
    print_row("最优组合", combo)
    print_row("v12 默认基准", baseline)

    # ============ 结论 ============
    print(f"\n{'='*100}")
    print("📋 优化结论")
    print("-" * 100)
    for k, v in results.items():
        default = getattr(EthROCMomentumV12, k, None)
        marker = "⚠️ 已变" if v != default else "= 默认"
        print(f"  {k:<16}: {v}  (默认 {default}) {marker}")

    print(f"\n  基准收益率:   {base_pnl:+.2f}%")
    print(f"  组合收益率:   {combo['return_pct']:+.2f}%")
    diff = combo["return_pct"] - base_pnl
    if diff > 0:
        print(f"  优化提升:     +{diff:.2f}% 🎉")
    else:
        print(f"  组合未跑赢基准: {diff:+.2f}% (单参数最优≠全局最优, 属正常现象)")

    print(f"""
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  📌 建议
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ① 若组合收益率明显提升, 可将最优参数写入
     strategies/eth_roc_momentum_v12.py 的类属性
  ② 若要更精细, 可在最优参数附近做小步长二次扫描
  ③ 注意: 参数优化有过拟合风险, 建议用 2026-03 之后
     数据做样本外验证后再用于模拟盘
  """)


if __name__ == "__main__":
    main()

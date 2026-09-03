"""ETH 低位金字塔建仓策略 — 合约(USDⓈ-M Futures)回测入口

与 main_v12_contract.py / main_extreme_contract.py 独立, 互不影响:
  - 策略: strategies/eth_low_pyramid_contract.py (低位金字塔分批做多)
  - 数据源: data/futures/ETHUSDT-1h.csv (合约历史数据)
  - 报告  : reports/contract/ (与趋势/反弹策略同目录, 文件名带时间戳不覆盖)

启动:
  python main_low_pyramid_contract.py
"""
import pandas as pd
import numpy as np
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.eth_low_pyramid_contract import EthLowPyramidContract
from base import generate_html_report

# 合约市场标识, 用于报告标注
MARKET = "合约"
DATA_RANGE = "近5年数据"


def load_data(csv_path):
    """加载K线CSV数据 (与 main_v12_contract.py 复用相同逻辑, 保证口径一致)"""
    print(f"📂 加载{MARKET}数据: {csv_path}")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df.dropna(subset=["close", "timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    first_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[0]) / 1000))
    last_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[-1]) / 1000))
    print(f"   数据量: {len(df)} 根 | 范围: {first_t} ~ {last_t}")
    if "volume" in df.columns:
        print(f"   含成交量数据 ✅")
    else:
        print(f"   ⚠️ 无成交量列, 将使用默认值")
    return df


def compare_with_buy_hold(df, trades, stats, warmup):
    """与买入持有(B&H)基准对比"""
    closes = df["close"].values
    capital = 150.0
    fee = 0.0004

    # 基准1: 从策略可交易起点(warmup)买入持有到末尾
    bh_entry1 = closes[warmup]
    bh_exit = closes[-1]
    bh1_ret = (bh_exit - bh_entry1) / bh_entry1 * 100 - fee * 2 * 100

    # 基准2: 从策略首次入场价买入持有到末尾 (更公平, 同期同价起跑)
    if trades:
        first_entry_price = trades[0]["entry_price"]
        bh2_ret = (bh_exit - first_entry_price) / first_entry_price * 100 - fee * 2 * 100
    else:
        first_entry_price = bh_entry1
        bh2_ret = bh1_ret

    print(f"\n  --- 📊 与买入持有(B&H)对比 ---")
    print(f"  策略收益率:        {stats['return_pct']:+.2f}%  (最终 {stats['final_capital']:.2f}U)")
    print(f"  B&H 从warmup起:    {bh1_ret:+.2f}%  (开@{bh_entry1:.0f} → 平@{bh_exit:.0f})")
    print(f"  B&H 从首笔入场起:  {bh2_ret:+.2f}%  (开@{first_entry_price:.0f} → 平@{bh_exit:.0f})")
    diff = stats['return_pct'] - bh2_ret
    flag = "✅ 跑赢" if diff > 0 else "❌ 跑输"
    print(f"  策略 vs B&H(首入场): {diff:+.2f}%  {flag}")


def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_file = os.path.join(base_dir, "data", "futures", "ETHUSDT-1h.csv")
    if not os.path.exists(data_file):
        print(f"❌ {MARKET}数据文件不存在: {data_file}")
        print(f"   请先运行: python fetch_data_contract.py")
        sys.exit(1)

    df = load_data(data_file)

    strategy = EthLowPyramidContract()
    print(f"\n{'='*65}")
    print(f"🎯 策略: {strategy.name}  [{MARKET}数据]")
    print(f"   市场: {MARKET} (USDⓈ-M Futures) | 数据范围: {DATA_RANGE}")
    print(f"   本金: {strategy.CAPITAL} USDT | 杠杆: {strategy.LEVERAGE}x | 无止损")
    print(f"   档位: T20(≤20%)×1 → T15(≤15%)×2 → T10(≤10%)×3 → T05(≤5%)×4  (共10份)")
    print(f"   每份: {strategy.FRACTION_PER_SHARE*100:.0f}%保证金 × {strategy.LEVERAGE}x = "
          f"{strategy.FRACTION_PER_SHARE*strategy.LEVERAGE*100:.0f}% notional")
    print(f"   满仓: {strategy.FRACTION_PER_SHARE*10*100:.0f}%保证金 / "
          f"{strategy.FRACTION_PER_SHARE*10*strategy.LEVERAGE*100:.0f}% notional (留50%buffer)")
    print(f"   TP: 滚动{strategy.TP_PERCENTILE*100:.0f}%分位 | 滚动窗口: {strategy.ROLLING_WINDOW}根 "
          f"min_periods={strategy.MIN_PERIODS}")
    print(f"   📥 入场: 滚动分位≤档位阈值 → 金字塔分批加仓")
    print(f"   🚪 出场: 分位≥50%全平 / 期末强平")
    print(f"{'='*65}\n")

    print("⏳ 开始回测 (滚动分位数计算可能需10-30s)...")
    t0 = time.time()
    result = strategy.run_backtest(df)
    elapsed = time.time() - t0

    trades = result["trades"]
    equity_curve = result["equity_curve"]
    stats = result["stats"]

    print(f"\n{'='*65}")
    print(f"📊 回测结果 [{MARKET}数据]  (耗时 {elapsed:.1f}s)")
    print(f"{'='*65}")
    print(f"  总交易次数:   {stats['total_trades']}")
    print(f"  胜率:         {stats['win_rate']:.1f}%")
    print(f"  总盈亏:       {stats['total_pnl']:+.2f} USDT")
    print(f"  收益率:       {stats['return_pct']:+.2f}%")
    print(f"  最大回撤:     {stats['max_drawdown']:.2f}%")
    print(f"  盈亏比:       {stats['profit_factor']:.2f}")
    print(f"  Sharpe:       {stats['sharpe_ratio']:.2f}")
    print(f"  平均盈利:     {stats['avg_win']:+.4f} USDT")
    print(f"  平均亏损:     {stats['avg_loss']:+.4f} USDT")
    print(f"  最佳单笔:     {stats['best_trade']:+.2f} USDT")
    print(f"  最差单笔:     {stats['worst_trade']:+.2f} USDT")
    print(f"  本金→最终:   {stats['initial_capital']:.0f} → {stats['final_capital']:.2f} USDT")

    # 建仓档位统计
    print(f"\n  --- 📥 金字塔档位分布 (按周期) ---")
    combo_counter = Counter(t["level"] for t in trades)
    for combo, cnt in combo_counter.most_common():
        pnls = [t["pnl"] for t in trades if t["level"] == combo]
        print(f"  {combo:20s}: {cnt}周期, 总PnL={sum(pnls):+.2f}, 均PnL={np.mean(pnls):+.2f}")

    shares_dist = Counter(t["shares"] for t in trades)
    print(f"  份数分布: {dict(sorted(shares_dist.items()))}")

    if trades:
        holds = [t["held_bars"] for t in trades]
        print(f"  持仓K线数: 均{np.mean(holds):.0f} ({np.mean(holds)/24:.0f}天) / "
              f"中位{np.median(holds):.0f} / 最长{max(holds)} ({max(holds)/24:.0f}天)")

    # 出场原因分布
    reason_labels = {"TP": "止盈", "timeout": "超时", "force_close": "期末强平"}
    print(f"\n  --- 出场原因分布 ---")
    for reason, label in reason_labels.items():
        n = stats.get(f"n_{reason}", 0)
        p = stats.get(f"pnl_{reason}", 0)
        if n > 0:
            print(f"  {label}: {n}笔, PnL={p:+.2f}")

    # 前5笔交易
    if trades:
        print(f"\n  前5笔交易:")
        for t in trades[:5]:
            entry_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['entry_time'] / 1000))
            exit_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['exit_time'] / 1000))
            reason_label = reason_labels.get(t.get("reason", ""), t.get("reason", ""))
            print(f"    多 开@{t['entry_price']:.2f}(均价) 平@{t['exit_price']:.2f} "
                  f"盈亏={t['pnl']:+.4f}U | 档位={t.get('level','?')} 份数={t.get('shares','?')} | "
                  f"持仓{t.get('held_bars','?')}根({t.get('held_bars',0)/24:.0f}天) | {reason_label}")

    # 与买入持有对比
    compare_with_buy_hold(df, trades, stats, strategy.MIN_PERIODS)
    print(f"{'='*65}")

    report_path = generate_html_report(stats, trades, equity_curve,
                                        symbol="ETHUSDT", interval="1h",
                                        output_dir=os.path.join(base_dir, "reports", "contract"),
                                        kline_df=df, market=MARKET, data_range=DATA_RANGE)
    print(f"\n✅ {MARKET}回测完成! 报告: {report_path}")
    return report_path


if __name__ == "__main__":
    main()

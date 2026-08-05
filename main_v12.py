"""ETH 双ROC动量策略 v12 回测入口"""
import pandas as pd
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.eth_roc_momentum_v12 import EthROCMomentumV12
from base import generate_html_report


def load_data(csv_path):
    """加载K线CSV数据"""
    print(f"📂 加载数据: {csv_path}")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # volume 列 (如果有)
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df.dropna(subset=["close", "timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    import time
    first_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[0]) / 1000))
    last_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[-1]) / 1000))
    print(f"   数据量: {len(df)} 根 | 范围: {first_t} ~ {last_t}")

    # 检查 volume 列
    if "volume" in df.columns:
        print(f"   含成交量数据 ✅")
    else:
        print(f"   ⚠️ 无成交量列, 将使用默认值")
    return df


def main():
    data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ETHUSDT-1h.csv")

    if not os.path.exists(data_file):
        print(f"❌ 数据文件不存在: {data_file}")
        sys.exit(1)

    df = load_data(data_file)

    strategy = EthROCMomentumV12()
    print(f"\n{'='*65}")
    print(f"🎯 策略: {strategy.name}")
    print(f"   本金: {strategy.CAPITAL} USDT | 杠杆: {strategy.LEVERAGE}x")
    print(f"   ROC 周期: 短期={strategy.ROC_SHORT} / 中期={strategy.ROC_MEDIUM}")
    print(f"   硬止损: {strategy.SL_USDT}U | 最大持仓: {strategy.MAX_HOLD_BARS}根K线")
    print(f"   📥 做多条件:")
    print(f"      ROC({strategy.ROC_SHORT}) > 0 且 ROC({strategy.ROC_MEDIUM}) > 0 且 ROC({strategy.ROC_SHORT}) > ROC({strategy.ROC_MEDIUM})")
    print(f"      成交量 > VolMA({strategy.VOL_MA_PERIOD})")
    print(f"   📤 做空条件:")
    print(f"      ROC({strategy.ROC_SHORT}) < 0 且 ROC({strategy.ROC_MEDIUM}) < 0 且 ROC({strategy.ROC_SHORT}) < ROC({strategy.ROC_MEDIUM})")
    print(f"      成交量 > VolMA({strategy.VOL_MA_PERIOD})")
    print(f"   🚪 出场:")
    print(f"      动量衰竭(ROC(5)反向穿零) / 硬止损{strategy.SL_USDT}U / 超时{strategy.MAX_HOLD_BARS}根")
    print(f"{'='*65}\n")

    print("⏳ 开始回测...")
    result = strategy.run_backtest(df)

    trades = result["trades"]
    equity_curve = result["equity_curve"]
    stats = result["stats"]

    print(f"\n{'='*65}")
    print(f"📊 回测结果")
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

    # 分方向
    print(f"\n  --- 做多: {stats.get('long_count', 0)}笔, PnL={stats.get('long_pnl', 0):+.2f}")
    print(f"  --- 做空: {stats.get('short_count', 0)}笔, PnL={stats.get('short_pnl', 0):+.2f}")

    # 按出场原因
    reason_labels = {
        "momentum_death": "动量衰竭",
        "SL": "止损",
        "timeout": "超时",
        "force_close": "期末强平",
    }
    print(f"\n  --- 出场原因分布 ---")
    for reason, label in reason_labels.items():
        n = stats.get(f"n_{reason}", 0)
        p = stats.get(f"pnl_{reason}", 0)
        if n > 0:
            print(f"  {label}: {n}笔, PnL={p:+.2f}")

    # 前几笔交易详情
    if trades:
        print(f"\n  前5笔交易:")
        for t in trades[:5]:
            import time
            entry_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['entry_time'] / 1000))
            exit_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['exit_time'] / 1000))
            dir_label = {"long": "多", "short": "空"}.get(t["direction"], t["direction"])
            reason_label = reason_labels.get(t.get("reason", ""), t.get("reason", ""))
            roc_info = f"ROC5={t.get('entry_roc5','?')}/{t.get('exit_roc5','?')}"
            held = t.get('held_bars', '?')
            print(f"    {dir_label} 开@{t['entry_price']:.2f} 平@{t['exit_price']:.2f} "
                  f"盈亏={t['pnl']:+.4f}U | {roc_info} | 持仓{held}根 | {reason_label}")

    print(f"{'='*65}")

    # 生成 HTML 报告
    report_path = generate_html_report(
        stats, trades, equity_curve,
        symbol="ETHUSDT", interval="1h", output_dir="reports",
        kline_df=df
    )
    print(f"\n✅ 回测完成! 报告: {report_path}")
    return report_path


if __name__ == "__main__":
    main()

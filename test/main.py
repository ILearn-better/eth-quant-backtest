"""ETH 分批抄底策略回测 - 主入口 (v10: 月均线DCA)"""
import pandas as pd
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.eth_dca_v10 import EthDCAV10
from base import generate_html_report


def load_data(csv_path):
    """加载K线CSV数据"""
    print(f"📂 加载数据: {csv_path}")
    df = pd.read_csv(csv_path)
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")

    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["close", "timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    import time
    first_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[0])/1000))
    last_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[-1])/1000))
    print(f"   数据量: {len(df)} 根 | 范围: {first_t} ~ {last_t}")

    return df


def main():
    data_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ETHUSDT-1h.csv")

    if not os.path.exists(data_file):
        print(f"❌ 数据文件不存在: {data_file}")
        sys.exit(1)

    # 1. 加载数据 (1h线)
    df = load_data(data_file)

    # 2. 初始化 v10 策略
    strategy = EthDCAV10()
    print(f"\n{'='*60}")
    print(f"🎯 策略: {strategy.name}")
    print(f"   本金: {strategy.CAPITAL} USDT | 杠杆: {strategy.LEVERAGE}x")
    print(f"   月均线: MA{strategy.MA_PERIOD} (150天×24h)")
    print(f"   手续费: {strategy.FEE_RATE*100:.2f}%")
    print(f"\n   📥 抄底档位 (月均线下方):")
    for idx, (drop_pct, frac) in enumerate(strategy.DCA_LEVELS):
        level = idx + 1
        print(f"      L{level}: 跌{abs(drop_pct)*100:.0f}% → 买入 {frac*100:.0f}%本金 × {strategy.LEVERAGE}x杠杆")
    print(f"\n   📤 卖出条件:")
    print(f"      价格 >= 过去{strategy.SELL_HIGH_LOOKBACK//24}天最高价 × {strategy.SELL_HIGH_RECOVERY*100:.0f}% → 全仓卖出")
    print(f"      (即从近期高点回落不超过{abs(strategy.SELL_HIGH_RECOVERY-1)*100:.0f}%时止盈)")
    print(f"   特点: 只做多 | 不止损(长期持有) | 越跌越买 | 动态回撤减仓")
    print(f"{'='*60}\n")

    # 3. 运行回测
    print("⏳ 开始回测...")
    result = strategy.run_backtest(df)

    trades = result["trades"]
    equity_curve = result["equity_curve"]
    stats = result["stats"]

    # 4. 输出统计
    print(f"\n{'='*60}")
    print(f"📊 回测结果")
    print(f"{'='*60}")
    print(f"  总交易次数: {stats['total_trades']}")
    print(f"  胜率:       {stats['win_rate']:.1f}%")
    print(f"  总盈亏:     {stats['total_pnl']:+.2f} USDT")
    print(f"  收益率:     {stats['return_pct']:+.2f}%")
    print(f"  最大回撤:   {stats['max_drawdown']:.2f}%")
    print(f"  盈亏比:     {stats['profit_factor']:.2f}")
    print(f"  Sharpe:     {stats['sharpe_ratio']:.2f}")
    print(f"  平均盈利:   {stats['avg_win']:+.4f} USDT")
    print(f"  平均亏损:   {stats['avg_loss']:+.4f} USDT")
    print(f"  最佳单笔:   {stats['best_trade']:+.2f} USDT")
    print(f"  最差单笔:   {stats['worst_trade']:+.2f} USDT")
    print(f"  本金→最终: {stats['initial_capital']:.0f} → {stats['final_capital']:.2f} USDT")

    # 按档位拆分统计
    long_trades = [t for t in trades if t["direction"] == "long"]
    if long_trades:
        lpnl = sum(t["pnl"] for t in long_trades)
        lwins = len([t for t in long_trades if t["pnl"] > 0])
        print(f"\n  --- 做多总计: {len(long_trades)}笔, PnL={lpnl:+.2f}, 胜率={lwins/len(long_trades)*100:.1f}%")

    for lvl in ["L1", "L2", "L3"]:
        lvl_trades = [t for t in trades if t.get("level","") == lvl]
        if lvl_trades:
            p = sum(t["pnl"] for t in lvl_trades)
            w = len([t for t in lvl_trades if t["pnl"] > 0])
            avg_entry = sum(t["entry_price"] for t in lvl_trades) / len(lvl_trades)
            avg_exit = sum(t["exit_price"] for t in lvl_trades) / len(lvl_trades)
            reasons = {}
            for t in lvl_trades:
                r = t.get("reason", "?")
                reasons[r] = reasons.get(r, 0) + 1
            reason_str = ", ".join(f"{k}:{v}" for k,v in reasons.items())
            print(f"  --- {lvl}档位: {len(lvl_trades)}笔, PnL={p:+.2f}, 胜率={w/len(lvl_trades)*100:.1f}%, "
                  f"均入{avg_entry:.0f}/均出{avg_exit:.0f}, 平仓原因=[{reason_str}]")

    # 5. 生成 HTML 报告
    report_path = generate_html_report(
        stats, trades, equity_curve,
        symbol="ETHUSDT", interval="1h", output_dir="reports",
        kline_df=df
    )

    print(f"\n✅ 回测完成!")
    return report_path


if __name__ == "__main__":
    main()

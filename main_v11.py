"""ETH 日线RSI超卖定投策略 v11 回测入口"""
import pandas as pd
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.eth_rsi_dca_v11 import EthRsiDcaV11
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
    data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ETHUSDT-1h.csv")

    if not os.path.exists(data_file):
        print(f"❌ 数据文件不存在: {data_file}")
        sys.exit(1)

    df = load_data(data_file)

    strategy = EthRsiDcaV11()
    print(f"\n{'='*65}")
    print(f"🎯 策略: {strategy.name}")
    print(f"   本金: {strategy.CAPITAL} USDT | 杠杆: {strategy.LEVERAGE}x (合约)")
    print(f"   RSI 周期: {strategy.RSI_PERIOD} | 买<超卖{strategy.RSI_OVERSOLD} / 卖>超买{strategy.RSI_OVERBOUGHT}")
    print(f"   分批笔数: 最多 {strategy.MAX_ENTRIES} 笔 | 每笔: {strategy.CAPITAL/strategy.MAX_ENTRIES:.1f} USDT")
    print(f"   买入信号: 日线RSI < {strategy.RSI_OVERSOLD} (每日最多触发1次)")
    print(f"   卖出信号: 日线RSI > {strategy.RSI_OVERBOUGHT} (FIFO, 每日最多卖出1笔)")
    print(f"   结算方式: 期末剩余持仓全部清仓")
    print(f"   特点: {strategy.LEVERAGE}x杠杆 | 合约定投(FIFO) | 超买卖入/超买卖出 | 长期持有")
    print(f"{'='*65}\n")

    print("⏳ 开始回测...")
    result = strategy.run_backtest(df)

    trades = result["trades"]
    equity_curve = result["equity_curve"]
    stats = result["stats"]

    print(f"\n{'='*65}")
    print(f"📊 回测结果")
    print(f"{'='*65}")
    print(f"  触发买入次数: {stats.get('n_entries', 0)} 笔 / {strategy.MAX_ENTRIES} 笔 (上限)")
    print(f"  每笔买入:     {stats.get('per_entry_usdt', 0)} USDT")
    print(f"  超买卖出次数: {stats.get('n_overbought_sells', 0)} 笔")
    print(f"  期末清仓笔数: {stats.get('n_period_end_sells', 0)} 笔")
    print(f"  最终现金:     {stats.get('cash_remaining', 0):.2f} USDT")
    print(f"  总交易次数:   {stats['total_trades']}")
    print(f"  胜率:         {stats['win_rate']:.1f}%")
    print(f"  总盈亏:       {stats['total_pnl']:+.2f} USDT")
    print(f"  收益率:       {stats['return_pct']:+.2f}%")
    print(f"  最大回撤:     {stats['max_drawdown']:.2f}%")
    print(f"  盈亏比:       {stats['profit_factor']:.2f}")
    print(f"  Sharpe:       {stats['sharpe_ratio']:.2f}")
    print(f"  本金→最终:   {stats['initial_capital']:.0f} → {stats['final_capital']:.2f} USDT")
    print(f"{'='*65}")

    if trades:
        print(f"\n  前5笔买入记录:")
        for t in trades[:5]:
            import time
            entry_t = time.strftime('%Y-%m-%d', time.localtime(t['entry_time']/1000))
            print(f"    {t['level']}: 买入@{t['entry_price']:.2f}  RSI={t.get('entry_rsi','?')}  "
                  f"平仓@{t['exit_price']:.2f}  盈亏={t['pnl']:+.4f}U")

    # 生成 HTML 报告
    report_path = generate_html_report(
        stats, trades, equity_curve,
        symbol="ETHUSDT", interval="1h(日线RSI)", output_dir="reports",
        kline_df=df
    )
    print(f"\n✅ 回测完成! 报告: {report_path}")
    return report_path


if __name__ == "__main__":
    main()

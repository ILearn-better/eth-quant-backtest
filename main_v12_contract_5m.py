"""ETH 双ROC动量策略 — 合约 5m 回测入口 (独立于 main_v12_contract.py)

数据源: data/futures/ETHUSDT-5m-full.csv (5年 5m 合约数据)
报告  : reports/contract/5m/ (独立目录, 不污染 1h 报告)
策略  : EthROCMomentumContractResonance5m (五维共振逻辑, ROC 周期 5m 放大为 40/100/250)
      对照原参数(8/20/50)报告在 reports/contract/5m/

注意: 其余时间尺度参数(MA50/VolMA20/MAX_HOLD=72根等)仍为 5m 原值。

启动:
  python main_v12_contract_5m.py            # 全量 5 年
  python main_v12_contract_5m.py 30000      # 只跑前 N 根(快速验证链路/测速)
"""
import pandas as pd
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 输出重定向到文件时 stdout 默认 GBK, emoji/中文会报错, 强制 UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from strategies.eth_roc_momentum_contract_resonance_5m import EthROCMomentumContractResonance5m as EthROCMomentumContract
from base import generate_html_report

# 合约市场标识, 用于报告标注 (与现货报告区分)
MARKET = "合约"
DATA_RANGE = "近5年数据"
INTERVAL = "5m"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "futures", "ETHUSDT-5m-full.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "reports", "contract", "5m", "roc40-100-250")


def load_data(csv_path, limit=None):
    """加载K线CSV数据 (可选只取前 limit 根, 用于快速验证链路)"""
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
    if limit:
        df = df.iloc[:limit].reset_index(drop=True)

    first_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[0]) / 1000))
    last_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(int(df["timestamp"].iloc[-1]) / 1000))
    print(f"   数据量: {len(df)} 根 | 范围: {first_t} ~ {last_t}")
    if "volume" in df.columns:
        print(f"   含成交量数据 ✅")
    else:
        print(f"   ⚠️ 无成交量列, 将使用默认值")
    return df


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    if not os.path.exists(DATA_FILE):
        print(f"❌ {MARKET}数据文件不存在: {DATA_FILE}")
        sys.exit(1)

    df = load_data(DATA_FILE, limit)

    strategy = EthROCMomentumContract()
    tp_desc = f"{strategy.TP_ATR_MULT}×ATR" if strategy.TP_ATR_MULT < 100 else "关闭(靠动量出场)"
    md_desc = f"±{strategy.MOMENTUM_DEATH_THRESH}" if strategy.MOMENTUM_DEATH_THRESH > 0 else "穿零即出"
    print(f"\n{'='*65}")
    print(f"🎯 策略: {strategy.name}  [{MARKET}·{INTERVAL}数据]")
    print(f"   市场: {MARKET} (USDⓈ-M Futures) | 数据范围: {DATA_RANGE} | 周期: {INTERVAL}")
    print(f"   本金: {strategy.CAPITAL} USDT | 杠杆: {strategy.LEVERAGE}x | 仓位: {strategy.FRACTION_BASE*100:.0f}% (有效{strategy.LEVERAGE*strategy.FRACTION_BASE:.1f}x)")
    print(f"   ROC 周期: 短={strategy.ROC_SHORT} / 中={strategy.ROC_MEDIUM} / 长={strategy.ROC_LONG} | VolMA={strategy.VOL_MA_PERIOD} | 趋势MA={strategy.TREND_MA_PERIOD}")
    print(f"   ATR 止损: {strategy.SL_ATR_MULT}×ATR | 止盈: {tp_desc}")
    print(f"   动量衰竭: {md_desc} | 最大持仓: {strategy.MAX_HOLD_BARS}根K线")
    print(f"   ⚠️ 5m 适配: ROC 周期放大为 40/100/250 (对应 1h 的 8/20/50), 其余时间参数保持原值")
    print(f"{'='*65}\n")

    print("⏳ 开始回测...")
    t0 = time.time()
    result = strategy.run_backtest(df)
    print(f"   ⏱ 回测耗时: {time.time()-t0:.1f} 秒")

    trades = result["trades"]
    equity_curve = result["equity_curve"]
    stats = result["stats"]

    print(f"\n{'='*65}")
    print(f"📊 回测结果 [{MARKET}·{INTERVAL}数据]")
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

    print(f"\n  --- 做多: {stats.get('long_count', 0)}笔, PnL={stats.get('long_pnl', 0):+.2f}")
    print(f"  --- 做空: {stats.get('short_count', 0)}笔, PnL={stats.get('short_pnl', 0):+.2f}")

    reason_labels = {"momentum_death": "动量衰竭", "SL": "止损", "TP": "止盈", "timeout": "超时", "force_close": "期末强平"}
    print(f"\n  --- 出场原因分布 ---")
    for reason, label in reason_labels.items():
        n = stats.get(f"n_{reason}", 0)
        p = stats.get(f"pnl_{reason}", 0)
        if n > 0:
            print(f"  {label}: {n}笔, PnL={p:+.2f}")

    if trades:
        print(f"\n  前5笔交易:")
        for t in trades[:5]:
            entry_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['entry_time'] / 1000))
            exit_t = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['exit_time'] / 1000))
            dir_label = {"long": "多", "short": "空"}.get(t["direction"], t["direction"])
            reason_label = reason_labels.get(t.get("reason", ""), t.get("reason", ""))
            held = t.get('held_bars', '?')
            print(f"    {dir_label} 开@{t['entry_price']:.2f} 平@{t['exit_price']:.2f} "
                  f"盈亏={t['pnl']:+.4f}U | 持仓{held}根 | {reason_label}")

    print(f"{'='*65}")

    report_path = generate_html_report(stats, trades, equity_curve,
                                        symbol="ETHUSDT", interval=INTERVAL,
                                        output_dir=OUTPUT_DIR,
                                        kline_df=df, market=MARKET, data_range=DATA_RANGE)
    print(f"\n✅ {MARKET}·{INTERVAL}回测完成! 报告: {report_path}")
    return report_path


if __name__ == "__main__":
    main()

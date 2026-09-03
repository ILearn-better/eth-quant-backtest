"""v4 策略优化分析: 参数扫描 + 诊断"""
import numpy as np
import pandas as pd
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies.eth_rsi_leverage import calc_rsi, calc_ma


def run_backtest_with_params(df,
                              rsi_thresh=40,
                              ma_period=200,
                              tp=4.0, sl=2.0,
                              leverage=5,
                              fraction_base=0.2,
                              fee_rate=0.0004,
                              rsi_period=14):
    """用给定参数跑回测，返回 (stats_dict, trades, final_balance)"""
    closes = df["close"].values.astype(float)
    timestamps = df["timestamp"].values.astype(np.int64)
    n_bars = len(closes)
    CAPITAL = 150.0

    rsi = calc_rsi(closes, rsi_period)
    ma = calc_ma(closes, ma_period)

    balance = CAPITAL
    peak = CAPITAL
    positions = []
    trades = []
    warmup = max(rsi_period, ma_period) + 1

    # 动态仓位阈值 (和 v4 一致)
    DD_THRESHOLDS = [(0.10, 1.0), (0.20, 0.7), (0.30, 0.5), (1.00, 0.3)]

    def get_size(dd):
        for t, m in DD_THRESHOLDS:
            if dd <= t:
                return m
        return 0.3

    for i in range(warmup, n_bars):
        ts = int(timestamps[i])
        price = closes[i]
        cur_rsi = rsi[i]
        cur_ma = ma[i]

        # TP/SL 检查
        still_open = []
        for pos in positions:
            if pos["direction"] == "long":
                pdiff = price - pos["entry_price"]
            else:
                pdiff = pos["entry_price"] - price
            pnl = (pdiff / pos["entry_price"]) * pos["size_usdt"]

            should_close = False
            if pnl >= tp:
                should_close = True
                reason = "TP"
            elif pnl <= -sl:
                should_close = True
                reason = "SL"

            if should_close:
                cf = pos["size_usdt"] * fee_rate / 2
                np_ = pnl - cf
                trades.append({
                    "pnl": round(np_, 4),
                    "reason": reason,
                    "pnl_raw": round(pnl, 4),
                    "fee": round(cf, 4),
                })
                balance += np_
            else:
                still_open.append(pos)
        positions = still_open

        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0

        # 开仓
        longs = [p for p in positions if p["direction"] == "long"]
        if (len(longs) == 0 and cur_rsi < rsi_thresh
                and not np.isnan(cur_ma) and price > cur_ma):
            mult = get_size(dd)
            frac = fraction_base * mult
            notional = balance * frac * leverage
            positions.append({
                "direction": "long",
                "entry_price": price,
                "size_usdt": notional,
            })

    # 强平
    for pos in positions:
        if pos["direction"] == "long":
            pdiff = closes[-1] - pos["entry_price"]
        else:
            pdiff = pos["entry_price"] - closes[-1]
        pnl = (pdiff / pos["entry_price"]) * pos["size_usdt"]
        cf = pos["size_usdt"] * fee_rate / 2
        trades.append({"pnl": round(pnl - cf, 4), "reason": "force", "pnl_raw": round(pnl, 4)})
        balance += pnl - cf

    # 统计
    n = len(trades)
    if n == 0:
        return {"trades": 0, "final": CAPITAL, "return_pct": 0, "max_dd": 0,
                "win_rate": 0, "pf": 0, "sharpe": 0}, [], CAPITAL

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)

    # 最大回撤 (简单版: 基于余额曲线)
    equity = [CAPITAL]
    running = CAPITAL
    for p in pnls:
        running += p
        equity.append(running)

    peak_eq = equity[0]
    max_dd = 0
    for e in equity:
        if e > peak_eq:
            peak_eq = e
        dd = (peak_eq - e) / peak_eq if peak_eq > 0 else 0
        if dd > max_dd:
            max_dd = dd

    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 1e-10
    pf = avg_win / avg_loss if avg_loss > 0 else 0

    # Sharpe (简化: 年化假设 24*365 根bar → 每根1h)
    if len(pnls) > 1:
        rets = np.array(pnls) / CAPITAL
        sharpe = np.mean(rets) / (np.std(rets) + 1e-10) * np.sqrt(8760 / (n_bars / n) * n)
    else:
        sharpe = 0

    stats = {
        "trades": n,
        "final": round(balance, 2),
        "return_pct": round((balance - CAPITAL) / CAPITAL * 100, 2),
        "max_dd": round(max_dd * 100, 2),
        "win_rate": round(len(wins) / n * 100, 1),
        "pf": round(pf, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2) if losses else 0,
        "total_pnl": round(total_pnl, 2),
        "tp_count": len([t for t in trades if t["reason"] == "TP"]),
        "sl_count": len([t for t in trades if t["reason"] == "SL"]),
        "force_count": len([t for t in trades if t["reason"] == "force"]),
        "sharpe": round(sharpe, 2),
    }
    return stats, trades, balance


def main():
    data_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ETHUSDT-1h.csv")
    df = pd.read_csv(data_file)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df.dropna(subset=["close", "timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    print("=" * 80)
    print("🔬 ETH RSI 策略优化诊断 — 参数网格扫描")
    print(f"   数据: {len(df)} 根K线 | 本金 150 USDT\n")

    # ================================================================
    # 1. 基准线 (当前 v4 参数)
    # ================================================================
    print("=" * 80)
    print("📌 0. 基准线 (v4 当前参数)")
    print("-" * 80)
    base_stats, _, _ = run_backtest_with_params(df)
    _print_row("v4 基准", base_stats)
    base_pnl = base_stats["final"]

    # ================================================================
    # 2. RSI 阈值扫描
    # ================================================================
    print(f"\n{'=' * 80}")
    print("📌 1. RSI 阈值扫描 (开多阈值)")
    print("-" * 80)
    print(f"{'RSI':>6} | {'交易数':>6} | {'最终余额':>8} | {'收益率':>7} | {'胜率':>6} | {'最大回撤':>7} | {'盈亏比':>6} | {'TP':>4} {'SL':>4} {'强平':>4}")
    best_rsi = None
    best_rsi_pnl = 0
    for rsi_t in [30, 35, 38, 40, 42, 45, 50]:
        s, _, _ = run_backtest_with_params(df, rsi_thresh=rsi_t)
        _print_row(str(rsi_t), s)
        if s["final"] > best_rsi_pnl:
            best_rsi_pnl = s["final"]
            best_rsi = rsi_t

    print(f"\n   ✅ 最佳 RSI 阈值: {best_rsi} (最终 {best_rsi_pnl:.2f} USDT)")

    # ================================================================
    # 3. MA 周期扫描
    # ================================================================
    print(f"\n{'=' * 80}")
    print("📌 2. MA 趋势过滤周期扫描")
    print("-" * 80)
    print(f"{'MA周期':>6} | {'交易数':>6} | {'最终余额':>8} | {'收益率':>7} | {'胜率':>6} | {'最大回撤':>7} | {'盈亏比':>6} | {'TP':>4} {'SL':>4} {'强平':>4}")
    best_ma = None
    best_ma_pnl = 0
    for ma_p in [50, 100, 120, 150, 200, 250, 300]:
        s, _, _ = run_backtest_with_params(df, ma_period=ma_p)
        _print_row(str(ma_p), s)
        if s["final"] > best_ma_pnl:
            best_ma_pnl = s["final"]
            best_ma = ma_p

    print(f"\n   ✅ 最佳 MA 周期: {best_ma} (最终 {best_ma_pnl:.2f} USDT)")

    # ================================================================
    # 4. TP/SL 扫描 (固定比例)
    # ================================================================
    print(f"\n{'=' * 80}")
    print("📌 3. 止盈止损组合扫描")
    print("-" * 80)
    print(f"{'TP':>4} {'SL':>4} | {'交易数':>6} | {'最终余额':>8} | {'收益率':>7} | {'胜率':>6} | {'最大回撤':>7} | {'盈亏比':>6} | {'Sharpe':>6}")
    tp_sl_pairs = [
        (2, 1), (3, 1.5), (4, 2), (5, 2.5), (6, 3),
        (4, 1), (5, 2), (6, 2), (6, 3), (8, 3),
        (8, 4), (10, 5), (5, 3), (5, 4),
    ]
    best_tp_sl = None
    best_tp_sl_pnl = 0
    for tp_val, sl_val in tp_sl_pairs:
        s, _, _ = run_backtest_with_params(df, tp=tp_val, sl=sl_val)
        _print_row_tp_sl(tp_val, sl_val, s)
        if s["final"] > best_tp_sl_pnl:
            best_tp_sl_pnl = s["final"]
            best_tp_sl = (tp_val, sl_val)

    print(f"\n   ✅ 最佳 TP/SL: +{best_tp_sl[0]}U / -{best_tp_sl[1]}U (最终 {best_tp_sl_pnl:.2f} USDT)")

    # ================================================================
    # 5. 杠杆扫描
    # ================================================================
    print(f"\n{'=' * 80}")
    print("📌 4. 杠杆倍数扫描")
    print("-" * 80)
    print(f"{'杠杆':>4} | {'交易数':>6} | {'最终余额':>8} | {'收益率':>7} | {'胜率':>6} | {'最大回撤':>7} | {'盈亏比':>6}")
    best_lev = None
    best_lev_pnl = 0
    for lev in [1, 2, 3, 4, 5, 7, 10]:
        s, _, _ = run_backtest_with_params(df, leverage=lev)
        _print_row(f"{lev}x", s)
        if s["final"] > best_lev_pnl:
            best_lev_pnl = s["final"]
            best_lev = lev

    print(f"\n   ✅ 最佳杠杆: {best_lev}x (最终 {best_lev_pnl:.2f} USDT)")

    # ================================================================
    # 6. 仓位大小扫描
    # ================================================================
    print(f"\n{'=' * 80}")
    print("📌 5. 基础仓位比例扫描")
    print("-" * 80)
    print(f"{'仓位':>6} | {'交易数':>6} | {'最终余额':>8} | {'收益率':>7} | {'胜率':>6} | {'最大回撤':>7} | {'盈亏比':>6}")
    best_frac = None
    best_frac_pnl = 0
    for frac in [0.05, 0.1, 0.15, 0.2, 0.25, 0.3]:
        s, _, _ = run_backtest_with_params(df, fraction_base=frac)
        _print_row(f"{frac:.0%}", s)
        if s["final"] > best_frac_pnl:
            best_frac_pnl = s["final"]
            best_frac = frac

    print(f"\n   ✅ 最佳仓位: {best_frac:.0%} (最终 {best_frac_pnl:.2f} USDT)")

    # ================================================================
    # 7. 组合最佳参数
    # ================================================================
    print(f"\n{'=' * 80}")
    print("📌 6. 🏆 最佳参数组合验证")
    print("-" * 80)

    combo_stats, combo_trades, combo_bal = run_backtest_with_params(
        df,
        rsi_thresh=best_rsi,
        ma_period=best_ma,
        tp=best_tp_sl[0],
        sl=best_tp_sl[1],
        leverage=best_lev,
        fraction_base=best_frac,
    )
    _print_row("最佳组合", combo_stats)

    # ================================================================
    # 8. 诊断分析
    # ================================================================
    print(f"\n{'=' * 80}")
    print("🔍 诊断分析与优化建议")
    print("-" * 80)

    # 分析基准交易的亏损结构
    _, base_trades, _ = run_backtest_with_params(df)
    if base_trades:
        tp_trades = [t for t in base_trades if t["reason"] == "TP"]
        sl_trades = [t for t in base_trades if t["reason"] == "SL"]
        force_trades = [t for t in base_trades if t["reason"] == "force"]

        print(f"\n  【v4基准 交易拆解】")
        print(f"  止盈(TP):  {len(tp_trades)}笔, 总PnL: +{sum(t['pnl'] for t in tp_trades):.2f}U")
        print(f"  止损(SL):  {len(sl_trades)}笔, 总PnL: {sum(t['pnl'] for t in sl_trades):.2f}U")
        print(f"  强平:      {len(force_trades)}笔, 总PnL: {sum(t['pnl'] for t in force_trades):.2f}U")

        if sl_trades:
            avg_sl = np.mean([abs(t["pnl"]) for t in sl_trades])
            print(f"\n  【关键发现】")
            print(f"  • 平均单笔止损亏损: {avg_sl:.2f}U")
            print(f"  • TP/SL 笔数比: {len(tp_trades)}:{len(sl_trades)} ≈ 1:{len(sl_trades)/max(len(tp_trades),1):.1f}")
            print(f"  • 盈亏需要至少 {avg_sl / (np.mean([t['pnl'] for t in tp_trades]) if tp_trades else 1):.1f}:1 的笔数比才能打平")

    print(f"""
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  📋 优化路线图 (按优先级排序)
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  🔴 高优先级 (预期改善大)

  ① 【加入波动率自适应止盈止损】
     当前问题: 固定±U 的 TP/SL 在高波动时太容易被扫，
              低波动时又持仓太久浪费机会
     方案: 用 ATR(14) 或 近N根K线的波幅来动态设定 TP/SL
     例: SL = 1.5 × ATR(14) 对应的USDT金额,
         TP = 3.0 × ATR(14) 对应的金额 (保持 2:1 风险回报比)
     预期效果: 减少噪音交易被扫止损的概率

  ② 【加入场时间过滤器】
     当前问题: 不分时段，任何时刻 RSI<40 都会开仓。
              但很多假突破发生在低流动性时段
     方案: 只在以下条件开仓:
       - 北京时间 20:00~06:00 (美股+欧股重叠段，波动最活跃)
       - 或者排除周末/节假日效应
     预期效果: 减少 15~25% 低质量信号

  ③ 【加确认信号 (多因子)】
     当前问题: 仅靠 RSI 一个指标，单一信号假信号率高
     方案: RSI<40 + 至少一个额外确认:
       a) 成交量放大 (vol > MA_vol(20) × 1.2)
       b) K线出现下影线 (low 远低于 close, 买盘承接)
       c) MACD 底背离或金叉确认
     预期效果: 胜率从 35% 提升到 45%+

  🟡 中优先级 (稳定性的改善)

  ④ 【移动止损 (Trailing Stop)】
     当前: 固定止损 -2U，不跟踪利润
     方案: 当浮盈超过 +2U 时，止损上移到保本价；
          浮盈超过 +4U 时，止损锁定 +1U 利润
     效果: 减少盈利变亏损的情况

  ⑤ 【冷却期 (Cooldown)】
     当前: 连续止损后会立刻重新开仓（同位置反复挨打）
     方案: 止损后等待 N 根 K线 (如 12~24h) 再允许开新仓，
          或要求 RSI 先回到 50 以上再回落才开
     效果: 避免在同一个下跌段反复接飞刀

  ⑥ 【EMA 替代 SMA 作为趋势过滤】
     当前: 用 SMA(200) 判断趋势
     方案: 改用 EMA(200) 或 EMA(100)+EMA(200) 双均线，
          或加入 EMA(50) 斜率判断（向上才做多）
     效果: 更快识别趋势转折

  🟢 低优先级 (锦上添花)

  ⑦ 【多标的分散】
     同时跑 BTC/ETH/SOL 等，降低单一资产风险

  ⑧ 【 Kelly 公式优化仓位】
     用历史胜率和盈亏比计算最优 f 值替代固定 20% 仓位
  """)


def _print_row(label, s):
    print(f"{label:>6} | {s['trades']:>6} | {s['final']:>8.2f} | {s['return_pct']:>+7.2f}% | "
          f"{s['win_rate']:>5.1f}% | {s['max_dd']:>6.2f}% | {s['pf']:>5.2f} | "
          f"{s['tp_count']:>4} {s['sl_count']:>4} {s['force_count']:>4}")


def _print_row_tp_sl(tp, sl, s):
    print(f"{tp:>4} {sl:>4} | {s['trades']:>6} | {s['final']:>8.2f} | {s['return_pct']:>+7.2f}% | "
          f"{s['win_rate']:>5.1f}% | {s['max_dd']:>6.2f}% | {s['pf']:>5.2f} | {s['sharpe']:>6.2f}")


if __name__ == "__main__":
    main()

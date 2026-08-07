"""ETH 日线RSI超卖定投策略 v11 (1h数据 + 日线RSI + 分50笔买入 + RSI超买卖出 + 期末清仓)"""
import numpy as np
import pandas as pd
from base import BaseStrategy, compute_stats, generate_html_report


def calc_rsi(close_series, period=14):
    """计算 RSI 指标 (Wilder's smoothing)"""
    n = len(close_series)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi

    deltas = np.diff(close_series)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


class EthRsiDcaV11(BaseStrategy):
    """
    ETH 日线RSI超卖定投策略 v11

    核心思路: 每当日线RSI进入超卖区(<30), 就买入一小笔仓位(FIFO队列)。
             当日线RSI进入超买区(>70), 卖出最早买入的一笔(FIFO)。
             期末无论剩余多少笔全部清仓。

    数据周期: 1h K线 → 每24根聚合为日K → 计算RSI(14)
    本金: 150 USDT | **10x 杠杆** (合约) | 超卖定投

    === 买入规则 ===
      触发: 日线RSI < 30 (超卖)
      每次买入: 150 / 100 = 1.5 USDT × 10x = 15 USDT 名义价值 / 笔
      同一日K最多买一次, 最多累积 100 笔

    === 卖出规则 ===
      触发: 日线RSI > 70 (超买) 且有持仓
      每次卖出: 最早买入的一笔 (FIFO)
      同一日K最多卖一次
      期末: 全部剩余持仓强制平仓

    === 杠杆说明 ===
      size_usdt = buy_amount × LEVERAGE (名义仓位)
      盈亏 = (exit_price - entry_price) / entry_price × size_usdt
      即: 价格涨跌% × 名义仓位 = 实际盈亏(已含10x杠杆放大)
      保证金仅 1U/笔, 名义仓位 10U/笔

    === 注意 ===
      买入与卖出在同一日K收盘时判断, 优先执行卖出再买入
      (防止同一天超买又超卖的极端情况)
    """

    name = "ETH日线RSI超卖定投策略-v11"
    CAPITAL = 150.0
    LEVERAGE = 10             # 10x 杠杆 (合约)
    FEE_RATE = 0.0004         # 合约手续费 (Binance: 0.04%)
    MAX_ENTRIES = 100
    RSI_PERIOD = 14
    RSI_OVERSOLD = 30    # 买入阈值
    RSI_OVERBOUGHT = 70  # 卖出阈值

    BARS_PER_DAY = 24

    def run_backtest(self, df):
        closes_1h = df["close"].values.astype(float)
        timestamps_1h = df["timestamp"].values.astype(np.int64)
        n_1h = len(closes_1h)

        # ---- 1. 聚合日K ----
        day_closes, day_ts, day_bar_idx = [], [], []
        for d in range(n_1h // self.BARS_PER_DAY):
            end_i = (d + 1) * self.BARS_PER_DAY - 1
            day_closes.append(closes_1h[end_i])
            day_ts.append(timestamps_1h[end_i])
            day_bar_idx.append(end_i)

        day_closes = np.array(day_closes)
        day_ts = np.array(day_ts, dtype=np.int64)
        day_bar_idx = np.array(day_bar_idx)

        # ---- 2. 计算日线RSI ----
        rsi_daily = calc_rsi(day_closes, self.RSI_PERIOD)

        # ---- 3. 回测 ----
        per_entry_amount = self.CAPITAL / self.MAX_ENTRIES  # 3 USDT/笔 (保证金)
        cash = self.CAPITAL
        positions = []   # FIFO队列, 最早在前
        n_entries = 0
        trades = []

        equity_curve = [(int(timestamps_1h[0]), float(self.CAPITAL))]

        last_buy_day = -999   # 防同日重复买
        last_sell_day = -999  # 防同日重复卖

        bar_to_day = {int(b_i): d_i for d_i, b_i in enumerate(day_bar_idx)}

        for i in range(self.BARS_PER_DAY, n_1h):
            ts = int(timestamps_1h[i])
            price = closes_1h[i]

            if i in bar_to_day:
                d_i = bar_to_day[i]
                cur_rsi = rsi_daily[d_i]

                if not np.isnan(cur_rsi):

                    # --- 先检查卖出 (RSI > 70 超买, FIFO卖最早一笔) ---
                    if (cur_rsi > self.RSI_OVERBOUGHT
                            and positions
                            and d_i != last_sell_day):

                        pos = positions.pop(0)  # FIFO: 取最早一笔
                        sell_notional = pos["qty"] * price   # 卖出名义价值
                        sell_fee = sell_notional * self.FEE_RATE / 2  # 平仓手续费
                        net_sell = sell_notional - sell_fee
                        pnl = net_sell - pos["size_usdt"]    # 卖收 - 开仓名义 = 盈亏(含杠杆)

                        trades.append({
                            "direction": "long",
                            "level": pos["level"],
                            "entry_price": round(pos["entry_price"], 2),
                            "exit_price": round(price, 2),
                            "entry_rsi": pos["entry_rsi"],
                            "exit_rsi": round(cur_rsi, 2),
                            "qty": round(pos["qty"], 6),
                            "size_usdt": round(pos["size_usdt"], 2),
                            "pnl": round(pnl, 4),
                            "entry_time": pos["entry_ts"],
                            "exit_time": ts,
                            "reason": "rsi_overbought",
                        })
                        cash += pos["margin"] + pnl           # 返还保证金 + 盈亏
                        last_sell_day = d_i

                    # --- 再检查买入 (RSI < 30 超卖) ---
                    if (cur_rsi < self.RSI_OVERSOLD
                            and n_entries < self.MAX_ENTRIES
                            and d_i != last_buy_day):

                        margin = per_entry_amount           # 保证金 3 USDT
                        notional = margin * self.LEVERAGE   # 名义价值 9 USDT (3x)
                        fee = notional * self.FEE_RATE / 2 # 开仓手续费
                        qty = notional / price              # 持仓量 (ETH)

                        if cash >= margin:
                            cash -= margin                  # 扣除保证金(非名义)
                            n_entries += 1
                            last_buy_day = d_i

                            positions.append({
                                "entry_price": price,
                                "qty": qty,
                                "size_usdt": notional,       # 名义仓位
                                "margin": margin,
                                "entry_ts": ts,
                                "entry_bar": i,
                                "entry_rsi": round(cur_rsi, 2),
                                "level": f"#{n_entries}",
                            })

            # 权益曲线
            holdings_value = sum(p["qty"] * price for p in positions)
            equity_curve.append((ts, round(cash + holdings_value, 4)))

        # ---- 4. 期末强制清仓所有剩余持仓 ----
        final_price = closes_1h[-1]
        final_ts = int(timestamps_1h[-1])

        for pos in positions:
            sell_notional = pos["qty"] * final_price
            sell_fee = sell_notional * self.FEE_RATE / 2
            net_sell = sell_notional - sell_fee
            pnl = net_sell - pos["size_usdt"]

            trades.append({
                "direction": "long",
                "level": pos["level"],
                "entry_price": round(pos["entry_price"], 2),
                "exit_price": round(final_price, 2),
                "entry_rsi": pos["entry_rsi"],
                "exit_rsi": None,
                "qty": round(pos["qty"], 6),
                "size_usdt": round(pos["size_usdt"], 2),
                "pnl": round(pnl, 4),
                "entry_time": pos["entry_ts"],
                "exit_time": final_ts,
                "reason": "period_end",
            })
            cash += pos["margin"] + pnl           # 返还保证金 + 盈亏

        equity_curve.append((final_ts, round(cash, 4)))
        stats = compute_stats(trades, self.CAPITAL)

        # 定投专属统计
        overbought_sells = [t for t in trades if t.get("reason") == "rsi_overbought"]
        period_end_sells = [t for t in trades if t.get("reason") == "period_end"]
        stats["n_entries"] = n_entries
        stats["n_overbought_sells"] = len(overbought_sells)
        stats["n_period_end_sells"] = len(period_end_sells)
        stats["per_entry_usdt"] = round(per_entry_amount, 2)
        stats["cash_remaining"] = round(cash, 2)

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "stats": stats,
        }

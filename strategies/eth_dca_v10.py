"""ETH 月均线分批抄底策略 v10 (1h + MA720 + 越跌越买 + 高点回撤止盈)"""
import numpy as np
from base import BaseStrategy, compute_stats, generate_html_report


def calc_ma(close_series, period):
    """计算简单移动平均线"""
    ma = np.full(len(close_series), np.nan)
    if len(close_series) < period:
        return ma
    cumsum = np.cumsum(close_series)
    ma[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return ma


class EthDCAV10(BaseStrategy):
    """
    ETH 月均线分批抄底策略 v10

    核心思路: 在月均线(MA720)下方越跌越买(Dollar Cost Averaging),
             持有到价格回到两年高点均值的-20%位置全仓卖出。

    数据周期: **1小时线 (1h)**
    本金: 150 USDT, 杠杆: 3x
    月均线: MA720 (150天 × 24h = 真正的月均线)

    === 买入规则(月均线下的分批抄底) ===
      Level 1: price <= MA720 × (1 - 0.20) → 买入 1/5 仓位 (首次抄底)
      Level 2: price <= MA720 × (1 - 0.40) → 累计加到 2/5 仓位 (深度抄底)
      可选 Level 3: price <= MA720 × (1 - 0.60) → 累计到 3/5 仓位 (极端抄底)

    === 卖出规则 ===
      止盈目标: 价格 >= (两年滚动最高价均值) × (1 - 0.20) → 全仓卖出
      注意: "两年最高点均值" 是一个滚动窗口内的最高价平均值,
            这里简化为过去两年(约17520根1hK线)的rolling max的均值。
            实际上用更短的回看窗口(如90天=2160根)来近似"长期高点"。

    === 特点 ===
    - 只做多, 不做空
    - 不设止损 (长期持有信念)
    - 分批建仓, 越跌仓位越大
    - 用高杠杆放大收益 (但也要注意风险)
    """

    name = "ETH月均线分批抄底策略-v10"
    CAPITAL = 150.0
    LEVERAGE = 3
    FEE_RATE = 0.0004           # 手续费率

    # 均线参数
    MA_PERIOD = 720              # 月均线: 150天 × 24h

    # 抄底档位: (距MA的跌幅%, 买入比例 of capital)
    DCA_LEVELS = [
        (-0.20, 0.20),   # L1: 跌20%以下 → 买 1/5 仓位
        (-0.40, 0.40),   # L2: 跌40%以下 → 累计 2/5 仓位
        (-0.60, 0.60),   # L3: 跌60%以下 → 累计 3/5 仓位 (极端情况)
    ]

    # 卖出条件: 价格回到高点均值上方
    SELL_HIGH_LOOKBACK = 2160   # 高点回看窗口: 90天×24h ≈ 3个月
    SELL_HIGH_RECOVERY = 0.80   # 卖出阈值: 高点均值 × 80% (即从高点回落不超过20%)

    def run_backtest(self, df):
        closes = df["close"].values.astype(float)
        timestamps = df["timestamp"].values.astype(np.int64)
        n_bars = len(closes)

        # 计算月均线 MA720
        ma720 = calc_ma(closes, self.MA_PERIOD)

        # 计算"两年最高点均值"(用滚动窗口的最高价作为参考)
        # 实际用 SELL_HIGH_LOOKBACK 周期的 rolling high
        high_lookback = self.SELL_HIGH_LOOKBACK
        rolling_high = np.full(n_bars, np.nan)
        for i in range(n_bars):
            start_idx = max(0, i - high_lookback + 1)
            if i - start_idx + 1 >= 1:
                rolling_high[i] = np.max(closes[start_idx:i + 1])

        balance = self.CAPITAL
        peak_balance = self.CAPITAL

        # 持仓状态: 支持多笔分批建仓
        positions = []          # list of position dicts
        total_invested = 0.0    # 总投入金额(size_usdt总和)
        trades = []
        equity_curve = [(int(timestamps[0]), balance)]

        # 已触达的最大档位 (防止重复加仓)
        max_level_reached = 0   # 0=无仓, 1=L1已买, 2=L2已买, 3=L3已买

        # 预热期: 需要MA720 + rolling_high 都有值
        warmup = max(self.MA_PERIOD, high_lookback) + 1

        for i in range(warmup, n_bars):
            ts = int(timestamps[i])
            price = closes[i]
            cur_ma = ma720[i]
            cur_high = rolling_high[i]

            if np.isnan(cur_ma) or np.isnan(cur_high):
                continue

            # ---- 1. 卖出检查 ----
            if positions:
                sell_threshold = cur_high * self.SELL_HIGH_RECOVERY

                if price >= sell_threshold:
                    # 全仓卖出所有持仓
                    total_pnl = 0.0
                    total_fee = 0.0
                    total_entry_cost = 0.0
                    entry_time_earliest = positions[0]["entry_time"]
                    entry_price_avg = 0.0

                    for pos in positions:
                        pnl = self._calc_pnl(pos, price)
                        fee = pos["size_usdt"] * self.FEE_RATE / 2
                        total_pnl += pnl
                        total_fee += fee
                        total_entry_cost += pos["size_usdt"]
                        entry_price_avg += pos["entry_price"] * pos["size_usdt"]

                    net_pnl = total_pnl - total_fee
                    avg_entry = entry_price_avg / total_entry_cost if total_entry_cost > 0 else price

                    trade_record = {
                        "direction": "long",
                        "level": f"L{max_level_reached}",
                        "entry_price": round(avg_entry, 2),
                        "exit_price": round(price, 2),
                        "pnl": round(net_pnl, 4),
                        "entry_time": entry_time_earliest,
                        "exit_time": ts,
                        "reason": "sell_high_recovery",
                    }
                    trades.append(trade_record)
                    balance += net_pnl
                    positions = []
                    total_invested = 0.0
                    max_level_reached = 0

            # 更新资金峰值和当前回撤
            if balance > peak_balance:
                peak_balance = balance
            current_drawdown = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0

            # ---- 2. 分批买入检查 (只做多) ----
            for level_idx, (drop_pct, fraction) in enumerate(self.DCA_LEVELS):
                level_num = level_idx + 1  # 1-based

                # 如果这个档位还没触发过
                if level_num > max_level_reached:
                    threshold = cur_ma * (1 + drop_pct)  # drop_pct is negative e.g. -0.20

                    if price <= threshold:
                        # 触发这一档买入
                        actual_fraction = fraction * self._get_position_size(current_drawdown)
                        notional = balance * actual_fraction * self.LEVERAGE
                        open_fee = notional * self.FEE_RATE / 2

                        position = {
                            "direction": "long",
                            "entry_price": price,
                            "size_usdt": notional,
                            "fraction": actual_fraction,
                            "level": f"L{level_num}",
                            "fee_paid": open_fee,
                            "entry_time": ts,
                        }
                        positions.append(position)
                        total_invested += notional
                        max_level_reached = level_num

            # 记录权益曲线 (含未实现盈亏)
            unrealized = sum(self._calc_pnl(p, price) for p in positions)
            equity_curve.append((ts, round(balance + unrealized, 4)))

        # ---- 3. 期末强平 ----
        final_price = closes[-1]
        final_ts = int(timestamps[-1])

        if positions:
            total_pnl = 0.0
            total_fee = 0.0
            total_entry_cost = 0.0
            entry_price_avg = 0.0
            entry_time_earliest = positions[0]["entry_time"]

            for pos in positions:
                pnl = self._calc_pnl(pos, final_price)
                fee = pos["size_usdt"] * self.FEE_RATE / 2
                total_pnl += pnl
                total_fee += fee
                total_entry_cost += pos["size_usdt"]
                entry_price_avg += pos["entry_price"] * pos["size_usdt"]

            net_pnl = total_pnl - total_fee
            avg_entry = entry_price_avg / total_entry_cost if total_entry_cost > 0 else final_price

            trades.append({
                "direction": "long",
                "level": f"L{max_level_reached}",
                "entry_price": round(avg_entry, 2),
                "exit_price": round(final_price, 2),
                "pnl": round(net_pnl, 4),
                "entry_time": entry_time_earliest,
                "exit_time": final_ts,
                "reason": "force_close",
            })
            balance += net_pnl

        equity_curve.append((final_ts, round(balance, 4)))
        stats = compute_stats(trades, self.CAPITAL)

        return {
            "trades": trades,
            "equity_curve": equity_curve,
            "stats": stats,
        }

    def _get_position_size(self, drawdown):
        """根据回撤幅度返回仓位系数"""
        thresholds = [
            (0.10, 1.0),
            (0.20, 0.8),
            (0.30, 0.6),
            (1.00, 0.4),
        ]
        for threshold, multiplier in thresholds:
            if drawdown <= threshold:
                return multiplier
        return 0.4

    def _calc_pnl(self, pos, current_price):
        """计算多仓盈亏 (不含手续费)"""
        price_diff = current_price - pos["entry_price"]
        pct_change = price_diff / pos["entry_price"]
        return pct_change * pos["size_usdt"]
